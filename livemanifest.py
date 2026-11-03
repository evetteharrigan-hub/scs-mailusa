"""Live Manifest Creator: crew add packages as they are received on the vessel,
the agent's office watches the list update live. Separate tables from the portal."""
import base64
import hmac
import io
import os
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import portal as portal_module
from portal import get_conn

import psycopg2.extras

router = APIRouter(prefix="/live")
AST = timezone(timedelta(hours=-4))

LIVE_USERS = {
    "crew": ("CREW_PASSWORD", "crew"),
    "agent": ("AGENT_PASSWORD", "agent"),
}

_ready = False


def _init():
    global _ready
    if _ready:
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS manifests (
            id SERIAL PRIMARY KEY,
            manifest_number TEXT UNIQUE NOT NULL,
            vessel TEXT,
            arrival_date DATE,
            status TEXT NOT NULL DEFAULT 'open',
            next_bl INTEGER NOT NULL DEFAULT 1,
            created_by TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        for col in ("voyage_number", "master_awb", "date_of_departure"):
            cur.execute(f"ALTER TABLE manifests ADD COLUMN IF NOT EXISTS {col} TEXT")
        cur.execute("ALTER TABLE manifests ADD COLUMN IF NOT EXISTS ballast BOOLEAN NOT NULL DEFAULT FALSE")
        for col in ("port_from_code", "port_from_name", "port_to_code", "port_to_name"):
            cur.execute(f"ALTER TABLE manifests ADD COLUMN IF NOT EXISTS {col} TEXT")
        cur.execute("""CREATE TABLE IF NOT EXISTS live_ports (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("""INSERT INTO live_ports (code, name) VALUES ('MFSXM','GALIS BAY'), ('AIAXA','ROAD BAY')
                       ON CONFLICT (code) DO NOTHING""")
        cur.execute("""CREATE TABLE IF NOT EXISTS manifest_items (
            id SERIAL PRIMARY KEY,
            manifest_id INTEGER NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
            bl_number INTEGER NOT NULL,
            client_id TEXT,
            consignee TEXT,
            description TEXT,
            pieces INTEGER DEFAULT 1,
            weight NUMERIC(10,2),
            added_by TEXT,
            edited_by TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (manifest_id, bl_number))""")
        for col in ("shipper", "shipper_address", "consignee_address", "consignee_phone", "shipper_phone",
                    "notify_name", "consignee_email", "freight_terms"):
            cur.execute(f"ALTER TABLE manifest_items ADD COLUMN IF NOT EXISTS {col} TEXT")
        cur.execute("ALTER TABLE manifest_items ADD COLUMN IF NOT EXISTS cubic_feet NUMERIC(10,2)")
        cur.execute("ALTER TABLE manifest_items ADD COLUMN IF NOT EXISTS freight_usd NUMERIC(10,2)")
        cur.execute("ALTER TABLE live_parties ADD COLUMN IF NOT EXISTS email TEXT")
        cur.execute("""CREATE TABLE IF NOT EXISTS manifest_photos (
            id SERIAL PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES manifest_items(id) ON DELETE CASCADE,
            manifest_id INTEGER NOT NULL,
            thumb BYTEA,
            data BYTEA NOT NULL,
            added_by TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mphotos_item ON manifest_photos (item_id)")
        cur.execute("""CREATE TABLE IF NOT EXISTS live_parties (
            id SERIAL PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (kind, name))""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mitems_client ON manifest_items (manifest_id, client_id) WHERE client_id IS NOT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mitems_manifest ON manifest_items (manifest_id, bl_number)")
    _ready = True


def current_live_user(authorization: Optional[str] = Header(default=None)) -> dict:
    token = (authorization or "").split(" ", 1)[-1].strip()
    if token not in ("crew-live", "agent-live"):
        raise HTTPException(status_code=401, detail="Please log in.", headers={"WWW-Authenticate": "Bearer"})
    return {"role": token.split("-")[0]}


def any_live(user: dict = Depends(current_live_user)) -> dict:
    return user


def agent_only(user: dict = Depends(current_live_user)) -> dict:
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="Agent access required.")
    return user


@router.post("/login")
async def live_login(username: str = Form(...), password: str = Form(...)):
    entry = LIVE_USERS.get(username.strip().lower())
    if entry:
        env_name, role = entry
        expected = os.environ.get(env_name, "")
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            return {"success": True, "role": role, "token": f"{role}-live"}
    return JSONResponse(status_code=401, content={"success": False, "message": "Invalid username or password"})


def _m(r):
    return {"id": r["id"], "manifest_number": r["manifest_number"], "vessel": r["vessel"],
            "arrival_date": r["arrival_date"].isoformat() if r["arrival_date"] else None,
            "status": r["status"], "count": r.get("count", 0), "weight": float(r.get("weight") or 0),
            "voyage_number": r.get("voyage_number") or "", "master_awb": r.get("master_awb") or "",
            "date_of_departure": r.get("date_of_departure") or "", "ballast": bool(r.get("ballast")),
            "port_from_code": r.get("port_from_code") or DEFAULT_FROM[0], "port_from_name": r.get("port_from_name") or DEFAULT_FROM[1],
            "port_to_code": r.get("port_to_code") or DEFAULT_TO[0], "port_to_name": r.get("port_to_name") or DEFAULT_TO[1]}


DEFAULT_FROM = ("MFSXM", "GALIS BAY")
DEFAULT_TO = ("AIAXA", "ROAD BAY")


def _port(code, name, default):
    """Clean one port. Blank means the automatic port (Galis Bay -> Road Bay); a half-filled port is refused."""
    code = re.sub(r"\s+", "", code or "").upper(); name = re.sub(r"\s+", " ", name or "").strip().upper()
    if not code and not name:
        return default
    if not name or not code:
        raise HTTPException(status_code=400, detail="Enter both the port name and its port code.")
    if not re.fullmatch(r"[A-Z0-9]{2,8}", code):
        raise HTTPException(status_code=400, detail="Port code can only use letters and numbers (for example AIAXA).")
    return (code, name[:60])


def _remember_ports(cur, *ports):
    for code, name in ports:
        cur.execute("""INSERT INTO live_ports (code, name) VALUES (%s,%s)
                       ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()""", (code, name))


@router.delete("/ports/{code}")
async def delete_port(code: str, user: dict = Depends(agent_only)):
    """Remove a saved port from the suggestions. Galis Bay and Road Bay are the automatic ports and stay."""
    _init()
    code = code.strip().upper()
    if code in (DEFAULT_FROM[0], DEFAULT_TO[0]):
        raise HTTPException(status_code=400, detail="Galis Bay and Road Bay are the automatic ports and cannot be removed.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM live_ports WHERE code = %s", (code,))
        return {"ok": True, "removed": cur.rowcount}


@router.get("/ports")
async def list_ports(user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT code, name FROM live_ports ORDER BY updated_at DESC, name")
        return {"ports": [dict(r) for r in cur.fetchall()]}


@router.get("/manifests")
async def list_manifests(user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT m.*, COUNT(i.id) AS count, COALESCE(SUM(i.weight),0) AS weight
                       FROM manifests m LEFT JOIN manifest_items i ON i.manifest_id = m.id
                       GROUP BY m.id ORDER BY (m.status='open') DESC, m.created_at DESC LIMIT 60""")
        return [_m(r) for r in cur.fetchall()]


@router.get("/last-voyage")
async def last_voyage(user: dict = Depends(any_live)):
    """The most recently started voyage, so the crew know the last number and date before starting a new one."""
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT m.*, COUNT(i.id) AS count, COALESCE(SUM(i.weight),0) AS weight
                       FROM manifests m LEFT JOIN manifest_items i ON i.manifest_id = m.id
                       GROUP BY m.id ORDER BY m.created_at DESC, m.id DESC LIMIT 1""")
        r = cur.fetchone()
    return {"last": _m(r) if r else None}


class BallastBody(BaseModel):
    voyage_number: str
    date_of_departure: str
    vessel: str = ""
    port_from_code: str = ""
    port_from_name: str = ""
    port_to_code: str = ""
    port_to_name: str = ""


@router.post("/ballast")
async def process_ballast(body: BallastBody, user: dict = Depends(any_live)):
    """Record a voyage as IN BALLAST (no cargo). It is stored closed, with no BL entries."""
    _init()
    voy = re.sub(r"\s+", "", body.voyage_number or "").upper()
    if not voy:
        raise HTTPException(status_code=400, detail="Voyage number is required.")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,30}", voy):
        raise HTTPException(status_code=400, detail="Voyage number can only use letters, numbers and . / - characters.")
    dep = portal_module.parse_date(body.date_of_departure)
    if not dep:
        raise HTTPException(status_code=400, detail="Enter the date.")
    key = f"{voy} {dep.isoformat()}"
    pf = _port(body.port_from_code, body.port_from_name, DEFAULT_FROM); pt = _port(body.port_to_code, body.port_to_name, DEFAULT_TO)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _remember_ports(cur, pf, pt)
        cur.execute("""INSERT INTO manifests (manifest_number, vessel, arrival_date, voyage_number, date_of_departure, created_by, status, ballast,
                       port_from_code, port_from_name, port_to_code, port_to_name)
                       VALUES (%s,%s,%s,%s,%s,%s,'closed',TRUE,%s,%s,%s,%s) ON CONFLICT (manifest_number) DO NOTHING""",
                    (key, body.vessel.strip(), dep, voy, dep.isoformat(), user["role"], pf[0], pf[1], pt[0], pt[1]))
        cur.execute("SELECT * FROM manifests WHERE manifest_number = %s FOR UPDATE", (key,))
        m = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS c FROM manifest_items WHERE manifest_id = %s", (m["id"],))
        n = cur.fetchone()["c"]
        if n:
            raise HTTPException(status_code=409, detail=f"Voyage {voy} already has {n} cargo entr{'y' if n == 1 else 'ies'}, so it cannot be recorded as in ballast.")
        cur.execute("UPDATE manifests SET ballast = TRUE, status = 'closed' WHERE id = %s RETURNING *", (m["id"],))
        return {"manifest": _m(cur.fetchone())}


class NewManifest(BaseModel):
    voyage_number: str = ""
    manifest_number: str = ""      # older callers
    vessel: str = ""
    arrival_date: str = ""
    date_of_departure: str = ""
    port_from_code: str = ""
    port_from_name: str = ""
    port_to_code: str = ""
    port_to_name: str = ""


@router.post("/manifests")
async def create_manifest(body: NewManifest, user: dict = Depends(any_live)):
    _init()
    voy = re.sub(r"\s+", "", (body.voyage_number or body.manifest_number or "")).upper()
    if not voy:
        raise HTTPException(status_code=400, detail="Voyage number is required.")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,30}", voy):
        raise HTTPException(status_code=400, detail="Voyage number can only use letters, numbers and . / - characters.")
    dep = portal_module.parse_date(body.date_of_departure or body.arrival_date)
    if not dep:
        raise HTTPException(status_code=400, detail="Enter the date.")
    arrival = portal_module.parse_date(body.arrival_date) or dep
    key = f"{voy} {dep.isoformat()}"      # one voyage = voyage number + date
    pf = _port(body.port_from_code, body.port_from_name, DEFAULT_FROM); pt = _port(body.port_to_code, body.port_to_name, DEFAULT_TO)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _remember_ports(cur, pf, pt)
        cur.execute("SELECT * FROM manifests WHERE manifest_number = %s", (key,))
        r = cur.fetchone()
        if r:  # joining an existing voyage is how a second crew member gets on the same list
            return {"manifest": _m(r), "existing": True}
        cur.execute("""INSERT INTO manifests (manifest_number, vessel, arrival_date, voyage_number, date_of_departure, created_by,
                       port_from_code, port_from_name, port_to_code, port_to_name)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (key, body.vessel.strip(), arrival, voy, dep.isoformat(), user["role"], pf[0], pf[1], pt[0], pt[1]))
        return {"manifest": _m(cur.fetchone()), "existing": False}


def bl_label(voyage: str, n: int) -> str:
    """BL reference = voyage number + '-' + 2-digit sequence (-01, -02 ... -10 ... -100)."""
    return f"{(voyage or '').strip()}-{int(n):02d}"


def _item(r, voyage: str = ""):
    return {"id": r["id"], "bl_number": r["bl_number"], "bl": bl_label(voyage or r.get("voyage_number", ""), r["bl_number"]),
            "consignee": r["consignee"] or "",
            "description": r["description"] or "", "pieces": r["pieces"] or 1,
            "weight": float(r["weight"]) if r["weight"] is not None else None,
            "shipper": r.get("shipper") or "", "shipper_address": r.get("shipper_address") or "",
            "consignee_address": r.get("consignee_address") or "", "consignee_phone": r.get("consignee_phone") or "",
            "shipper_phone": r.get("shipper_phone") or "", "notify_name": r.get("notify_name") or "",
            "consignee_email": r.get("consignee_email") or "", "freight_terms": r.get("freight_terms") or "",
            "cubic_feet": float(r["cubic_feet"]) if r.get("cubic_feet") is not None else None,
            "freight_usd": float(r["freight_usd"]) if r.get("freight_usd") is not None else None,
            "photos": r.get("photos", 0),
            "added_by": r["added_by"] or "", "edited_by": r["edited_by"] or "",
            "created_at": r["created_at"].astimezone(AST).strftime("%I:%M %p").lstrip("0"),
            "version": r["updated_at"].timestamp() if r.get("updated_at") else 0}


@router.get("/manifests/{mid}/items")
async def get_items(mid: int, since: float = Query(0), user: dict = Depends(any_live)):
    """since = newest updated_at the screen already has; only new or corrected rows are sent (cheap live polling)."""
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM manifests WHERE id = %s", (mid,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(status_code=404, detail="Manifest not found.")
        cur.execute("""SELECT i.*, (SELECT COUNT(*) FROM manifest_photos p WHERE p.item_id = i.id) AS photos
                       FROM manifest_items i WHERE i.manifest_id = %s AND i.updated_at > to_timestamp(%s) ORDER BY i.bl_number""", (mid, since))
        items = [_item(r, m["voyage_number"]) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(weight),0) AS w, COALESCE(SUM(pieces),0) AS p FROM manifest_items WHERE manifest_id = %s", (mid,))
        t = cur.fetchone()
        cur.execute("SELECT bl_number FROM manifest_items WHERE manifest_id = %s", (mid,))
        ids = [r["bl_number"] for r in cur.fetchall()]
    return {"status": m["status"], "manifest": _m({**m, "count": t["c"], "weight": t["w"]}), "items": items,
            "all_bl": ids, "total": {"count": t["c"], "pieces": int(t["p"]), "weight": float(t["w"])}}


class NewItem(BaseModel):
    consignee: str = ""
    description: str = ""
    pieces: int = 1
    weight: Optional[float] = None
    added_by: str = ""
    client_id: str = ""
    shipper: str = ""
    shipper_address: str = ""
    shipper_phone: str = ""
    consignee_address: str = ""
    consignee_phone: str = ""
    consignee_email: str = ""
    notify_name: str = ""
    cubic_feet: Optional[float] = None
    freight_usd: Optional[float] = None
    freight_terms: str = ""
    photos: List[str] = []
    thumbs: List[str] = []


def _check(body):
    if len(body.photos) > MAX_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Up to {MAX_PHOTOS} photos per entry.")
    if not (body.consignee or "").strip():
        raise HTTPException(status_code=400, detail="Consignee name is required.")
    if body.pieces < 1 or body.pieces > 999:
        raise HTTPException(status_code=400, detail="Pieces must be 1 or more.")
    if body.weight is not None and (body.weight != body.weight or body.weight < 0 or body.weight > 99999):
        raise HTTPException(status_code=400, detail="Weight looks wrong.")
    for label, v in (("Cubic feet", body.cubic_feet), ("Freight", body.freight_usd)):
        if v is not None and (v != v or v < 0 or v > 999999):
            raise HTTPException(status_code=400, detail=f"{label} looks wrong.")
    if body.freight_terms not in ("", "COLLECT", "PREPAID"):
        raise HTTPException(status_code=400, detail="Freight must be COLLECT or PREPAID.")
    em = (body.consignee_email or "").strip()
    if em and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", em):
        raise HTTPException(status_code=400, detail="That email address does not look right.")


MAX_PHOTOS = 8


def _decode_image(s: str, limit: int = 2_500_000) -> bytes:
    raw = base64.b64decode(s.split(",", 1)[-1], validate=False)
    if len(raw) > limit:
        raise HTTPException(status_code=400, detail="A photo is too large.")
    if not (raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="Photos must be JPEG or PNG images.")
    return raw


def _save_photos(cur, mid: int, iid: int, photos: List[str], thumbs: List[str], who: str):
    if not photos:
        return
    cur.execute("SELECT COUNT(*) AS c FROM manifest_photos WHERE item_id = %s", (iid,))
    if cur.fetchone()["c"] + len(photos) > MAX_PHOTOS:
        raise HTTPException(status_code=400, detail=f"Up to {MAX_PHOTOS} photos per entry.")
    for n, p in enumerate(photos):
        data = _decode_image(p)
        thumb = _decode_image(thumbs[n], 400_000) if n < len(thumbs) and thumbs[n] else data
        cur.execute("INSERT INTO manifest_photos (item_id, manifest_id, thumb, data, added_by) VALUES (%s,%s,%s,%s,%s)",
                    (iid, mid, thumb, data, who))
    cur.execute("UPDATE manifest_items SET updated_at = NOW() WHERE id = %s", (iid,))


def _remember(cur, kind: str, name: str, address: str, phone: str = "", email: str = ""):
    """Keep the shipper / consignee in the saved list. A later entry for the same name fills itself in."""
    name = (name or "").strip()[:120]
    if not name:
        return
    cur.execute("""INSERT INTO live_parties (kind, name, address, phone, email) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (kind, name) DO UPDATE SET
                     address = CASE WHEN EXCLUDED.address <> '' THEN EXCLUDED.address ELSE live_parties.address END,
                     phone = CASE WHEN EXCLUDED.phone <> '' THEN EXCLUDED.phone ELSE live_parties.phone END,
                     email = CASE WHEN COALESCE(EXCLUDED.email, '') <> '' THEN EXCLUDED.email ELSE live_parties.email END,
                     updated_at = NOW()""", (kind, name, (address or "").strip(), (phone or "").strip(), (email or "").strip().lower()))


@router.get("/parties")
async def find_parties(kind: str = Query("consignee"), q: str = Query(""), user: dict = Depends(any_live)):
    _init()
    if kind not in ("shipper", "consignee"):
        raise HTTPException(status_code=400, detail="kind must be shipper or consignee")
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""SELECT id, name, address, phone, email FROM live_parties WHERE kind = %s AND LOWER(name) LIKE %s
                       ORDER BY updated_at DESC LIMIT 8""", (kind, f"%{q.strip().lower()}%"))
        return cur.fetchall()


@router.delete("/parties/{pid}")
async def delete_party(pid: int, user: dict = Depends(agent_only)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM live_parties WHERE id = %s", (pid,))
    return {"success": True}


@router.post("/manifests/{mid}/items")
async def add_item(mid: int, body: NewItem, user: dict = Depends(any_live)):
    """Adds one package and hands out the next BL number for this voyage. The voyage row is locked for the
    moment of the insert, so two crew adding at once always get different numbers. A retry with the same
    client_id (e.g. after a dropped signal) returns the same BL instead of creating a second entry."""
    _init()
    _check(body)
    cid = (body.client_id or "").strip()[:64] or None
    who = (body.added_by or user["role"]).strip()[:40]
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT status, next_bl, voyage_number FROM manifests WHERE id = %s FOR UPDATE", (mid,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(status_code=404, detail="Manifest not found.")
        if cid:
            cur.execute("SELECT * FROM manifest_items WHERE manifest_id = %s AND client_id = %s", (mid, cid))
            ex = cur.fetchone()
            if ex:
                return {"item": _item(ex, m["voyage_number"]), "duplicate_retry": True}
        if m["status"] != "open" and user["role"] != "agent":
            raise HTTPException(status_code=409, detail="This manifest is closed.")
        bl = m["next_bl"]
        cur.execute("UPDATE manifests SET next_bl = next_bl + 1 WHERE id = %s", (mid,))
        cur.execute("""INSERT INTO manifest_items (manifest_id, bl_number, client_id, consignee, description, pieces, weight,
                       added_by, shipper, shipper_address, consignee_address, consignee_phone, shipper_phone, notify_name,
                       consignee_email, cubic_feet, freight_usd, freight_terms)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (mid, bl, cid, body.consignee.strip(), body.description.strip(), body.pieces, body.weight, who,
                     body.shipper.strip(), body.shipper_address.strip(), body.consignee_address.strip(), body.consignee_phone.strip(),
                     body.shipper_phone.strip(), body.notify_name.strip(), body.consignee_email.strip().lower(),
                     body.cubic_feet, body.freight_usd, body.freight_terms))
        r = cur.fetchone()
        cur.execute("UPDATE manifests SET ballast = FALSE WHERE id = %s AND ballast", (mid,))
        _save_photos(cur, mid, r["id"], body.photos, body.thumbs, who)
        _remember(cur, "consignee", body.consignee, body.consignee_address, body.consignee_phone, body.consignee_email)
        _remember(cur, "shipper", body.shipper, body.shipper_address, body.shipper_phone)
        r["photos"] = len(body.photos)
        return {"item": _item(r, m["voyage_number"])}


@router.put("/manifests/{mid}/items/{iid}")
async def edit_item(mid: int, iid: int, body: NewItem, user: dict = Depends(agent_only)):
    _init()
    _check(body)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""UPDATE manifest_items SET consignee=%s, description=%s, pieces=%s, weight=%s,
                       shipper=%s, shipper_address=%s, consignee_address=%s, consignee_phone=%s,
                       shipper_phone=%s, notify_name=%s, consignee_email=%s, cubic_feet=%s, freight_usd=%s, freight_terms=%s,
                       edited_by=%s, updated_at=NOW() WHERE id=%s AND manifest_id=%s RETURNING *""",
                    (body.consignee.strip(), body.description.strip(), body.pieces, body.weight,
                     body.shipper.strip(), body.shipper_address.strip(), body.consignee_address.strip(), body.consignee_phone.strip(),
                     body.shipper_phone.strip(), body.notify_name.strip(), body.consignee_email.strip().lower(),
                     body.cubic_feet, body.freight_usd, body.freight_terms,
                     (body.added_by or "agent").strip()[:40], iid, mid))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Entry not found.")
        _remember(cur, "consignee", body.consignee, body.consignee_address, body.consignee_phone, body.consignee_email)
        _remember(cur, "shipper", body.shipper, body.shipper_address, body.shipper_phone)
        cur.execute("SELECT COUNT(*) AS c FROM manifest_photos WHERE item_id = %s", (iid,))
        r["photos"] = cur.fetchone()["c"]
        cur.execute("SELECT voyage_number FROM manifests WHERE id = %s", (mid,))
        return {"item": _item(r, cur.fetchone()["voyage_number"])}


@router.delete("/manifests/{mid}/items/{iid}")
async def delete_item(mid: int, iid: int, user: dict = Depends(agent_only)):
    """Removes an entry. Its BL number is never reused, so numbers already printed stay unique."""
    _init()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM manifest_items WHERE id = %s AND manifest_id = %s", (iid, mid))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Entry not found.")
    return {"success": True}


class StatusBody(BaseModel):
    status: str


@router.post("/manifests/{mid}/status")
async def set_status(mid: int, body: StatusBody, user: dict = Depends(agent_only)):
    _init()
    if body.status not in ("open", "closed"):
        raise HTTPException(status_code=400, detail="Bad status.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE manifests SET status = %s, ballast = CASE WHEN %s = 'open' THEN FALSE ELSE ballast END WHERE id = %s",
                    (body.status, body.status, mid))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Manifest not found.")
    return {"success": True}


class EditManifest(BaseModel):
    vessel: str = ""
    arrival_date: str = ""
    voyage_number: str = ""
    master_awb: str = ""
    date_of_departure: str = ""
    port_from_code: str = ""
    port_from_name: str = ""
    port_to_code: str = ""
    port_to_name: str = ""


@router.put("/manifests/{mid}")
async def edit_manifest(mid: int, body: EditManifest, user: dict = Depends(agent_only)):
    _init()
    arrival = portal_module.parse_date(body.arrival_date)
    pf = _port(body.port_from_code, body.port_from_name, DEFAULT_FROM); pt = _port(body.port_to_code, body.port_to_name, DEFAULT_TO)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        _remember_ports(cur, pf, pt)
        # the voyage number is NOT editable here: BL numbers are issued from it, so changing it would rename them all
        cur.execute("""UPDATE manifests SET vessel=%s, arrival_date=COALESCE(%s, arrival_date),
                       master_awb=%s, date_of_departure=%s,
                       port_from_code=%s, port_from_name=%s, port_to_code=%s, port_to_name=%s WHERE id=%s RETURNING *""",
                    (body.vessel.strip(), arrival, body.master_awb.strip(), body.date_of_departure.strip(), pf[0], pf[1], pt[0], pt[1], mid))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Manifest not found.")
        return {"manifest": _m(r)}


class MorePhotos(BaseModel):
    photos: List[str]
    thumbs: List[str] = []
    added_by: str = ""


@router.post("/manifests/{mid}/items/{iid}/photos")
async def add_photos(mid: int, iid: int, body: MorePhotos, user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id FROM manifest_items WHERE id = %s AND manifest_id = %s", (iid, mid))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Entry not found.")
        _save_photos(cur, mid, iid, body.photos, body.thumbs, (body.added_by or user["role"])[:40])
        cur.execute("SELECT COUNT(*) AS c FROM manifest_photos WHERE item_id = %s", (iid,))
        return {"photos": cur.fetchone()["c"]}


@router.get("/manifests/{mid}/items/{iid}/photos")
async def list_photos(mid: int, iid: int, user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, added_by, created_at FROM manifest_photos WHERE item_id = %s AND manifest_id = %s ORDER BY id", (iid, mid))
        return [{"id": r["id"], "added_by": r["added_by"] or "", "at": r["created_at"].astimezone(AST).strftime("%d %b %I:%M %p")} for r in cur.fetchall()]


@router.get("/photos/{pid}")
async def get_photo(pid: int, size: str = Query("full"), user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT thumb, data FROM manifest_photos WHERE id = %s", (pid,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Photo not found.")
    raw = bytes(r[0] if size == "thumb" and r[0] else r[1])
    mt = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
    return StreamingResponse(io.BytesIO(raw), media_type=mt, headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/photos/{pid}")
async def delete_photo(pid: int, user: dict = Depends(agent_only)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM manifest_photos WHERE id = %s RETURNING item_id", (pid,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Photo not found.")
        cur.execute("UPDATE manifest_items SET updated_at = NOW() WHERE id = %s", (r[0],))
    return {"success": True}


@router.delete("/manifests/{mid}")
async def delete_manifest(mid: int, user: dict = Depends(agent_only)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM manifests WHERE id = %s", (mid,))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="Manifest not found.")
    return {"success": True}


@router.get("/manifests/{mid}/pdf")
async def manifest_pdf(mid: int, user: dict = Depends(any_live)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM manifests WHERE id = %s", (mid,))
        m = cur.fetchone()
        if not m:
            raise HTTPException(status_code=404, detail="Manifest not found.")
        cur.execute("SELECT * FROM manifest_items WHERE manifest_id = %s ORDER BY bl_number", (mid,))
        items = cur.fetchall()
    pdf = _build_pdf(m, items)
    clean = re.sub(r"[^A-Z0-9]", "_", (m["voyage_number"] or m["manifest_number"]).upper()).strip("_")
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="SCS_Live_Manifest_{clean}.pdf"'})


def _build_pdf(m, items) -> bytes:
    from reportlab.lib.colors import HexColor, grey, white
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    maroon, cream, border = HexColor("#8B0000"), HexColor("#FAF8F5"), HexColor("#CCCCCC")
    ss = getSampleStyleSheet()
    S = lambda n, **k: ParagraphStyle(n, parent=ss["Normal"], **k)
    co = S("co", fontSize=18, fontName="Helvetica-Bold", textColor=maroon, alignment=TA_CENTER)
    ad = S("ad", fontSize=9, textColor=grey, alignment=TA_CENTER, spaceAfter=2)
    ti = S("ti", fontSize=16, fontName="Helvetica-Bold", textColor=maroon, alignment=TA_CENTER, spaceAfter=6, spaceBefore=4)
    inf = S("inf", fontSize=10, leading=13)
    cell, cellr, cellc = S("c", fontSize=8, leading=10), S("cr", fontSize=8, leading=10, alignment=TA_RIGHT), S("cc", fontSize=8, leading=10, alignment=TA_CENTER)
    hd = S("h", fontSize=8, fontName="Helvetica-Bold", textColor=white, leading=10)
    hdc = S("hc", fontSize=8, fontName="Helvetica-Bold", textColor=white, leading=10, alignment=TA_CENTER)
    hdr = S("hr", fontSize=8, fontName="Helvetica-Bold", textColor=white, leading=10, alignment=TA_RIGHT)
    bold = lambda t, st: Paragraph(f"<b>{t}</b>", st)
    pw, _ = landscape(A4)
    esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    total_pcs = sum((i["pieces"] or 1) for i in items)
    total_wt = sum(float(i["weight"] or 0) for i in items)
    arrival = m["arrival_date"].strftime("%B %d, %Y").replace(" 0", " ") if m["arrival_date"] else ""
    el = [Paragraph("SAFE CARGO SERVICES", co), Paragraph("Sandy Ground, Anguilla  |  Tel: (264) 498-0194", ad),
          Paragraph("CARGO MANIFEST", ti)]
    left = [Paragraph(f"<b>Voyage No.:</b> {esc(m['voyage_number'] or m['manifest_number'])}", inf), Paragraph(f"<b>Vessel:</b> {esc(m['vessel'] or '-')}", inf),
            Paragraph(f"<b>Date of Arrival:</b> {arrival}", inf)]
    mp = _m(m)
    left += [Paragraph(f"<b>Port of Departure:</b> {esc(mp['port_from_name'])} ({esc(mp['port_from_code'])})", inf),
             Paragraph(f"<b>Port of Arrival:</b> {esc(mp['port_to_name'])} ({esc(mp['port_to_code'])})", inf)]
    right = [Paragraph(f"<b>Total Shipments:</b> {len(items)}", inf), Paragraph(f"<b>Total Pieces:</b> {total_pcs}", inf),
             Paragraph(f"<b>Total Weight:</b> {total_wt:,.2f} kg", inf)]
    it = Table([[left, right]], colWidths=[130*mm, 117*mm])
    it.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el += [it, Spacer(1, 4*mm)]
    if m.get("ballast") and not items:
        big = S("big", fontSize=30, fontName="Helvetica-Bold", textColor=maroon, alignment=TA_CENTER, spaceAfter=10)
        sub = S("sub", fontSize=13, alignment=TA_CENTER)
        el += [Spacer(1, 22*mm), Paragraph("VESSEL IN BALLAST", big), Paragraph("No cargo aboard for this voyage.", sub)]
    else:
        data = [[Paragraph("BL No.", hdc), Paragraph("Consignee Name", hd), Paragraph("Description", hd),
                 Paragraph("Pcs", hdc), Paragraph("Weight (kg)", hdr)]]
        for i in items:
            w = f"{float(i['weight']):,.2f}" if i["weight"] is not None else ""
            data.append([Paragraph(esc(bl_label(m["voyage_number"], i["bl_number"])), cellc), Paragraph(esc(i["consignee"]), cell),
                         Paragraph(esc(i["description"]), cell), Paragraph(str(i["pieces"] or 1), cellc), Paragraph(w, cellr)])
        data.append([Paragraph("", cell), Paragraph("", cell), bold("TOTAL", cell), bold(str(total_pcs), cellc), bold(f"{total_wt:,.2f}", cellr)])
        t = Table(data, colWidths=[32*mm, 76*mm, 94*mm, 18*mm, 27*mm], repeatRows=1)
        st = [("BACKGROUND", (0, 0), (-1, 0), maroon), ("GRID", (0, 0), (-1, -1), 0.4, border), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("BACKGROUND", (0, -1), (-1, -1), HexColor("#F0E6D2"))]
        for r in range(1, len(data) - 1):
            if r % 2 == 0:
                st.append(("BACKGROUND", (0, r), (-1, r), cream))
        t.setStyle(TableStyle(st))
        el.append(t)
    buf = io.BytesIO()
    def foot(c, d):
        c.saveState(); c.setFont("Helvetica", 8); c.setFillColor(grey)
        c.drawCentredString(pw / 2, 18*mm, "Prepared by: Safe Cargo Services - Customs Declarant DC2114")
        c.drawRightString(pw - 20*mm, 18*mm, f"Page {d.page}"); c.restoreState()
    SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=20*mm, rightMargin=20*mm, topMargin=15*mm, bottomMargin=25*mm).build(el, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()


def _xml_row(i, voyage: str = ""):
    """Map a live entry onto the row shape the existing waybill generator expects. Anything the crew did not
    enter stays blank rather than being invented."""
    return {"tracking_number": bl_label(voyage, i["bl_number"]), "buyer_name": (i["consignee"] or "").strip(),
            "items_description": i["description"] or "", "weight": str(float(i["weight"])) if i["weight"] is not None else "0",
            "items": str(i["pieces"] or 1), "shipper": (i.get("shipper") or "").strip(),
            "shipper_address1": " ".join(x for x in ((i.get("shipper_address") or "").strip(), (i.get("shipper_phone") or "").strip()) if x),
            "buyer_address1": " ".join(x for x in ((i.get("consignee_address") or "").strip(), (i.get("consignee_phone") or "").strip(), (i.get("consignee_email") or "").strip()) if x),
            "notify_name": (i.get("notify_name") or "").strip(),
            "notify_address": " ".join(x for x in ((i.get("consignee_phone") or "").strip(), (i.get("consignee_email") or "").strip()) if x),
            "volume_m3": f"{float(i['cubic_feet']) * 0.0283168:.3f}" if i.get("cubic_feet") else "",
            "freight_usd": f"{float(i['freight_usd']):.2f}" if i.get("freight_usd") is not None else "",
            "freight_terms": (i.get("freight_terms") or "").strip(),
            "shipper_country": "", "fob_verified": "0", "cif_verified": "0"}


def _xml_for(m, i) -> bytes:
    import main as main_module
    mm = _m(m)
    if not m["master_awb"]:
        raise HTTPException(status_code=400, detail="Add the Master AWB to this voyage first (Edit voyage).")
    info = {"master_awb": m["master_awb"], "voyage_number": m["voyage_number"] or "",
            "date_of_departure": m["date_of_departure"] or "", "manifest_reference": m["manifest_number"],
            "port_of_loading_code": mm["port_from_code"], "port_of_loading_name": mm["port_from_name"],
            "port_of_unloading_code": mm["port_to_code"], "port_of_unloading_name": mm["port_to_name"], "use_pieces": True}
    return main_module.generate_waybill_xml(_xml_row(i, m["voyage_number"] or ""), info, i["bl_number"])


@router.get("/manifests/{mid}/items/{iid}/xml")
async def item_xml(mid: int, iid: int, user: dict = Depends(agent_only)):
    _init()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM manifests WHERE id = %s", (mid,))
        m = cur.fetchone()
        cur.execute("SELECT * FROM manifest_items WHERE id = %s AND manifest_id = %s", (iid, mid))
        i = cur.fetchone()
    if not m or not i:
        raise HTTPException(status_code=404, detail="Entry not found.")
    xml = _xml_for(m, i)
    name = re.sub(r"[^A-Z0-9]+", "_", (i["consignee"] or "").upper()).strip("_") or "CONSIGNEE"
    return StreamingResponse(io.BytesIO(xml), media_type="application/xml",
                             headers={"Content-Disposition": f'attachment; filename="{name}_{re.sub(r"[^A-Za-z0-9_-]", "_", bl_label(m["voyage_number"], i["bl_number"]))}_waybill.xml"'})
