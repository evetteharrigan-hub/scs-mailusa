"""
SCS-MAILUSA Portal: warehouse + accounting module.

- PostgreSQL via psycopg2, connection string from env DATABASE_URL.
- One EC$10.00 AASPA/Security fee per shipment (single field: aaspa_security_fee).
- total_due = customs_duties + clearance_fee (5% of duties) + aaspa_security_fee
"""
import io
import os
import contextlib
import re
import hmac
from datetime import datetime, date, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None

router = APIRouter(prefix="/portal")
history_router = APIRouter(prefix="/api/history")

AST = timezone(timedelta(hours=-4))  # Anguilla, no DST
AASPA_SECURITY_FEE = 10.00
CLEARANCE_RATE = 0.05

PAYMENT_METHODS = {
    "cash": "Cash",
    "bank_transfer": "Bank Transfer",
    "card": "Credit/Debit Card",
    "cheque": "Cheque",
}

_db_ready = False

# Portal logins work like the main app: username + password compared directly
# against environment variables (WAREHOUSE_PASSWORD / ACCOUNTING_PASSWORD).
PORTAL_USERS = {
    "warehouse": ("WAREHOUSE_PASSWORD", "warehouse"),
    "accounting": ("ACCOUNTING_PASSWORD", "accounting"),
}


def today_ast() -> date:
    return datetime.now(AST).date()


# ─── Database ──────────────────────────────────────────────────────────────────

def _dsn() -> str:
    return os.environ.get("DATABASE_URL", "")


@contextlib.contextmanager
def get_conn():
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shipments (
    id SERIAL PRIMARY KEY,
    tracking_number TEXT UNIQUE NOT NULL,
    buyer_name TEXT,
    buyer_address TEXT,
    buyer_phone TEXT,
    buyer_email TEXT,
    shipper_name TEXT,
    description TEXT,
    date_of_arrival DATE,
    cif_value REAL DEFAULT 0,
    customs_duties REAL DEFAULT 0,
    clearance_fee REAL DEFAULT 0,
    aaspa_security_fee REAL DEFAULT 10.00,
    total_due REAL DEFAULT 0,
    date_paid DATE,
    payment_method TEXT,
    paid BOOLEAN DEFAULT FALSE,
    manifest_reference TEXT,
    voyage_number TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    session_date TEXT,
    type TEXT,
    master_awb TEXT,
    voyage_number TEXT,
    date_of_departure TEXT,
    carrier_name TEXT,
    manifest_reference TEXT,
    spreadsheet_name TEXT,
    row_count INTEGER DEFAULT 0,
    "rows" JSONB,
    duties_map JSONB,
    invoice_form_data JSONB
);

CREATE TABLE IF NOT EXISTS invoices (
    id SERIAL PRIMARY KEY,
    tracking_number TEXT,
    customer_name TEXT,
    customs_duties REAL,
    clearance_fee REAL,
    aaspa_security_fee REAL DEFAULT 10.00,
    total_ec REAL,
    total_usd REAL,
    arrival_date TEXT,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pdf_data BYTEA
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shipments_buyer ON shipments (LOWER(buyer_name));
CREATE INDEX IF NOT EXISTS idx_shipments_date_paid ON shipments (date_paid);
"""


def init_db() -> bool:
    """Create tables and default users. Safe to call repeatedly. Never raises."""
    global _db_ready
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Execute each statement separately (psycopg2 doesn't support multi-statement execute)
            for stmt in [s.strip() for s in SCHEMA_SQL.split(';') if s.strip()]:
                cur.execute(stmt)
            for column in ("buyer_address", "buyer_phone", "buyer_email", "shipper_name"):
                cur.execute(f"ALTER TABLE shipments ADD COLUMN IF NOT EXISTS {column} TEXT")
            cur.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS invoice_form_data JSONB")
            cur.execute("""DELETE FROM invoices a USING invoices b
                           WHERE a.tracking_number = b.tracking_number AND a.id < b.id""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_tracking ON invoices (tracking_number)")
            # Migrate older schema that had two separate $10 fees -> one field
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'shipments'
            """)
            cols = {r[0] for r in cur.fetchall()}
            if "aaspa_security_fee" not in cols:
                cur.execute("ALTER TABLE shipments ADD COLUMN aaspa_security_fee REAL DEFAULT 10.00")
            for old in ("aaspa_fee", "security_fee"):
                if old in cols:
                    cur.execute(f"ALTER TABLE shipments DROP COLUMN {old}")
            if cols & {"aaspa_fee", "security_fee"}:
                cur.execute("""
                    UPDATE shipments SET aaspa_security_fee = 10.00,
                        total_due = ROUND((customs_duties + clearance_fee + 10.00)::numeric, 2)
                """)
            # Portal users (username + role only; passwords live in environment variables)
            for username, (env_name, role) in PORTAL_USERS.items():
                cur.execute(
                    "INSERT INTO users (username, password, role) VALUES (%s, %s, %s) "
                    "ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password, role = EXCLUDED.role",
                    (username, f"env:{env_name}", role),
                )
        _db_ready = True
        print("[portal] Database initialised (shipments, users, sessions)")
    except Exception as e:
        _db_ready = False
        print(f"[portal] WARNING: database init failed: {e}")
    return _db_ready


def _require_db():
    if not _db_ready and not init_db():
        raise HTTPException(status_code=503, detail="Database unavailable. Check DATABASE_URL.")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
                "%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _describe(raw) -> str:
    s = "" if raw is None else str(raw).strip()
    parts = [m.strip() for m in re.findall(r"\[([^\]]*)\]", s) if m.strip()]
    return ", ".join(parts) if parts else s


def calc_fees(duties: float) -> dict:
    duties = round(float(duties or 0), 2)
    clearance = round(duties * CLEARANCE_RATE, 2)
    total = round(duties + clearance + AASPA_SECURITY_FEE, 2)
    return {"customs_duties": duties, "clearance_fee": clearance,
            "aaspa_security_fee": AASPA_SECURITY_FEE, "total_due": total}


def _row_to_json(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, float):
            out[k] = round(v, 2)
        else:
            out[k] = v
    if "payment_method" in out:
        out["payment_method_label"] = PAYMENT_METHODS.get(out.get("payment_method") or "", out.get("payment_method") or "")
    if out.get("date_of_arrival") and "paid" in out and not out.get("paid"):
        try:
            out["days_outstanding"] = (today_ast() - date.fromisoformat(out["date_of_arrival"])).days
        except ValueError:
            pass
    return out


UPSERT_SQL = """
INSERT INTO shipments (tracking_number, buyer_name, description, date_of_arrival, cif_value,
    customs_duties, clearance_fee, aaspa_security_fee, total_due, manifest_reference, voyage_number,
    buyer_address, buyer_phone, buyer_email, shipper_name)
VALUES (%(tracking_number)s, %(buyer_name)s, %(description)s, %(date_of_arrival)s, %(cif_value)s,
    %(customs_duties)s, %(clearance_fee)s, %(aaspa_security_fee)s, %(total_due)s,
    %(manifest_reference)s, %(voyage_number)s, %(buyer_address)s, %(buyer_phone)s,
    %(buyer_email)s, %(shipper_name)s)
ON CONFLICT (tracking_number) DO UPDATE SET
    buyer_name = COALESCE(NULLIF(EXCLUDED.buyer_name, ''), shipments.buyer_name),
    description = COALESCE(NULLIF(EXCLUDED.description, ''), shipments.description),
    buyer_address = COALESCE(NULLIF(EXCLUDED.buyer_address, ''), shipments.buyer_address),
    buyer_phone = COALESCE(NULLIF(EXCLUDED.buyer_phone, ''), shipments.buyer_phone),
    buyer_email = COALESCE(NULLIF(EXCLUDED.buyer_email, ''), shipments.buyer_email),
    shipper_name = COALESCE(NULLIF(EXCLUDED.shipper_name, ''), shipments.shipper_name),
    date_of_arrival = COALESCE(EXCLUDED.date_of_arrival, shipments.date_of_arrival),
    cif_value = CASE WHEN EXCLUDED.cif_value > 0 THEN EXCLUDED.cif_value ELSE shipments.cif_value END,
    customs_duties = CASE WHEN EXCLUDED.customs_duties > 0 THEN EXCLUDED.customs_duties ELSE shipments.customs_duties END,
    clearance_fee = CASE WHEN EXCLUDED.customs_duties > 0 THEN EXCLUDED.clearance_fee ELSE shipments.clearance_fee END,
    aaspa_security_fee = EXCLUDED.aaspa_security_fee,
    total_due = CASE WHEN EXCLUDED.customs_duties > 0 THEN EXCLUDED.total_due
                     ELSE ROUND((COALESCE(shipments.customs_duties,0) + COALESCE(shipments.clearance_fee,0) + EXCLUDED.aaspa_security_fee)::numeric, 2) END,
    manifest_reference = COALESCE(NULLIF(EXCLUDED.manifest_reference, ''), shipments.manifest_reference),
    voyage_number = COALESCE(NULLIF(EXCLUDED.voyage_number, ''), shipments.voyage_number)
"""


def upsert_shipments(rows: list, arrival_date: str = "", duties_map: Optional[dict] = None,
                     manifest_reference: str = "", voyage_number: str = "") -> int:
    """Insert/update shipments from spreadsheet rows. Never raises (generation must not fail
    because the DB is down). Existing duties / payment info are never wiped by blank values."""
    duties_map = duties_map or {}
    arrival = parse_date(arrival_date)
    records = []
    for row in rows or []:
        tracking = str(row.get("tracking_number") or "").strip()
        if not tracking:
            continue
        rec = {
            "tracking_number": tracking,
            "buyer_name": str(row.get("buyer_name") or "").strip(),
            "buyer_address": ", ".join(str(row.get(k) or "").strip() for k in
                                       ("buyer_address1", "buyer_city", "buyer_state") if row.get(k)),
            "buyer_phone": str(row.get("buyer_phone") or "").strip(),
            "buyer_email": str(row.get("buyer_email") or "").strip(),
            "shipper_name": str(row.get("shipper") or "").strip(),
            "description": _describe(row.get("items_description")),
            "date_of_arrival": arrival,
            "cif_value": round(_num(row.get("cif_verified")), 2),
            "manifest_reference": (manifest_reference or "").strip(),
            "voyage_number": (voyage_number or "").strip(),
        }
        rec.update(calc_fees(_num(duties_map.get(tracking, 0))))
        records.append(rec)
    if not records:
        return 0
    try:
        _require_db()
        with get_conn() as conn:
            cur = conn.cursor()
            psycopg2.extras.execute_batch(cur, UPSERT_SQL, records)
        print(f"[portal] Upserted {len(records)} shipment(s)")
        return len(records)
    except Exception as e:
        print(f"[portal] WARNING: shipment upsert failed: {e}")
        return 0


# ─── Auth ──────────────────────────────────────────────────────────────────────

def _unauthorised(msg: str = "Not logged in. Please log in to the portal."):
    return HTTPException(status_code=401, detail=msg)


def current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Simple session check (same style as the main app): the frontend sends the
    '{role}-session' token it received at login as `Authorization: Bearer <token>`."""
    token = (authorization or "").split(" ", 1)[-1].strip()
    if token not in ("warehouse-session", "accounting-session"):
        raise _unauthorised()
    return {"role": token.split("-")[0]}


def any_user(user: dict = Depends(current_user)) -> dict:
    return user


def accounting_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "accounting":
        raise HTTPException(status_code=403, detail="Accounting access required.")
    return user


@router.post("/login")
async def portal_login(username: str = Form(...), password: str = Form(...)):
    entry = PORTAL_USERS.get(username.strip().lower())
    if entry:
        env_name, role = entry
        expected = os.environ.get(env_name, "")
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            return {"success": True, "role": role, "token": f"{role}-session"}
    return JSONResponse(status_code=401, content={"success": False, "message": "Invalid username or password"})


# ─── Main app session history ───────────────────────────────────────────────────

HISTORY_COLS = """id, session_date AS "date", type, master_awb, voyage_number,
    date_of_departure, carrier_name, manifest_reference, spreadsheet_name,
    row_count, "rows", duties_map AS "dutiesMap", invoice_form_data AS "invoiceFormData"""


@history_router.post("")
async def save_history(entry: dict = Body(...)):
    if not isinstance(entry.get("id"), str) or not entry["id"].strip():
        raise HTTPException(status_code=422, detail="A nonempty session id is required.")
    if not isinstance(entry.get("rows", []), list) or not isinstance(entry.get("dutiesMap", {}), dict):
        raise HTTPException(status_code=422, detail="rows must be a list and dutiesMap must be an object.")
    row_count = entry.get("row_count", 0)
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        raise HTTPException(status_code=422, detail="row_count must be an integer.")
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            INSERT INTO sessions (id, session_date, type, master_awb, voyage_number,
                date_of_departure, carrier_name, manifest_reference, spreadsheet_name,
                row_count, "rows", duties_map, invoice_form_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                session_date = EXCLUDED.session_date, type = EXCLUDED.type,
                master_awb = EXCLUDED.master_awb, voyage_number = EXCLUDED.voyage_number,
                date_of_departure = EXCLUDED.date_of_departure,
                carrier_name = EXCLUDED.carrier_name,
                manifest_reference = EXCLUDED.manifest_reference,
                spreadsheet_name = EXCLUDED.spreadsheet_name,
                row_count = EXCLUDED.row_count, "rows" = EXCLUDED."rows",
                duties_map = EXCLUDED.duties_map,
                invoice_form_data = EXCLUDED.invoice_form_data
            RETURNING {HISTORY_COLS}
        """, (
            entry["id"], entry.get("date"), entry.get("type"), entry.get("master_awb"),
            entry.get("voyage_number"), entry.get("date_of_departure"),
            entry.get("carrier_name"), entry.get("manifest_reference"),
            entry.get("spreadsheet_name"), row_count,
            psycopg2.extras.Json(entry.get("rows", [])),
            psycopg2.extras.Json(entry.get("dutiesMap", {})),
            psycopg2.extras.Json(entry["invoiceFormData"]) if entry.get("invoiceFormData") else None,
        ))
        return dict(cur.fetchone())


@history_router.get("")
async def list_history():
    _require_db()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, session_date AS date, type, master_awb, voyage_number,
                           date_of_departure, carrier_name, manifest_reference, spreadsheet_name,
                           row_count, rows, duties_map, invoice_form_data
                    FROM sessions ORDER BY created_at DESC LIMIT 50
                """)
                cols = [d[0] for d in cur.description]
                rows_data = cur.fetchall()
                result = []
                for row in rows_data:
                    d = dict(zip(cols, row))
                    d["dutiesMap"] = d.pop("duties_map", None)
                    d["invoiceFormData"] = d.pop("invoice_form_data", None)
                    result.append(d)
                return result
    except Exception as e:
        print(f"[history] list error: {e}")
        return []


@history_router.delete("/{id}")
async def delete_history(id: str):
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE id = %s", (id,))
    return {"success": True}


@history_router.delete("")
async def clear_history():
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions")
    return {"success": True}


# ─── Warehouse ─────────────────────────────────────────────────────────────────

SHIPMENT_COLS = """tracking_number, buyer_name, description, date_of_arrival, cif_value, customs_duties,
    (COALESCE(customs_duties, 0) > 0) AS processed,
    clearance_fee, aaspa_security_fee, total_due, date_paid, payment_method, paid,
    manifest_reference, voyage_number, created_at"""


def _search(q: str, limit: int = 200, name_only: bool = False) -> list:
    _require_db()
    q = (q or "").strip()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if q:
            like = f"%{q.lower()}%"
            where = "LOWER(buyer_name) LIKE %s" if name_only else "(LOWER(buyer_name) LIKE %s OR LOWER(tracking_number) LIKE %s)"
            params = (like,) if name_only else (like, like)
            cur.execute(f"SELECT {SHIPMENT_COLS} FROM shipments WHERE {where} "
                        f"ORDER BY date_of_arrival DESC NULLS LAST, buyer_name LIMIT {int(limit)}", params)
        else:
            cur.execute(f"SELECT {SHIPMENT_COLS} FROM shipments "
                        f"ORDER BY date_of_arrival DESC NULLS LAST, created_at DESC LIMIT {int(limit)}")
        return [_row_to_json(r) for r in cur.fetchall()]


@router.get("/shipments/search")
async def warehouse_search(q: str = "", user: dict = Depends(any_user)):
    return _search(q)


@router.get("/shipments/{tracking}")
async def warehouse_get(tracking: str, user: dict = Depends(any_user)):
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT {SHIPMENT_COLS} FROM shipments WHERE tracking_number = %s", (tracking.strip(),))
        r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return _row_to_json(r)


# ─── Accounting ────────────────────────────────────────────────────────────────

@router.get("/accounting/search")
async def accounting_search(q: str = "", user: dict = Depends(accounting_user)):
    rows = _search(q)
    keys = ("tracking_number", "buyer_name", "date_of_arrival", "customs_duties", "clearance_fee",
            "aaspa_security_fee", "total_due", "paid", "date_paid", "payment_method",
            "payment_method_label", "description", "days_outstanding", "processed")
    return [{k: r.get(k) for k in keys if k in r} for r in rows]


class ProcessRequest(BaseModel):
    tracking_number: str
    customs_duties: float


def _apply_duties(cur, tracking: str, duties: float):
    fees = calc_fees(duties)
    cur.execute(f"""UPDATE shipments SET customs_duties = %(customs_duties)s, clearance_fee = %(clearance_fee)s,
                    aaspa_security_fee = %(aaspa_security_fee)s, total_due = %(total_due)s
                    WHERE tracking_number = %(t)s RETURNING {SHIPMENT_COLS}""", {**fees, "t": tracking})
    return cur.fetchone()


@router.post("/accounting/process")
async def accounting_process(body: ProcessRequest, user: dict = Depends(accounting_user)):
    _require_db()
    if body.customs_duties != body.customs_duties or body.customs_duties <= 0:
        raise HTTPException(status_code=400, detail="Enter customs duties greater than 0.")
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        r = _apply_duties(cur, body.tracking_number.strip(), body.customs_duties)
    if not r:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return {"success": True, "shipment": _row_to_json(r)}


@router.post("/accounting/process-payment-order")
async def accounting_process_payment_order(pdf_files: List[UploadFile] = File(...), user: dict = Depends(accounting_user)):
    """Read duties from an ASYCUDA payment order PDF and process every matching unprocessed shipment."""
    import main as main_module
    _require_db()
    matches, seen = [], set()
    for f in pdf_files:
        for m in main_module.parse_payment_order_pdf(await f.read()):
            key = (m["declarant_ref"], m["amount_ec"])
            if key not in seen:  # same line repeated across uploaded files counts once
                seen.add(key)
                matches.append(m)
    if not matches:
        raise HTTPException(status_code=400, detail="No payment order lines found in the PDF(s).")
    processed, unmatched = [], []
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT tracking_number, buyer_name FROM shipments WHERE COALESCE(customs_duties, 0) <= 0")
        pending = cur.fetchall()
        used = set()
        for m in matches:
            hits = [s for s in pending if m["digits"] in s["tracking_number"] and s["tracking_number"] not in used]
            if not hits:
                unmatched.append(m["declarant_ref"])
                continue
            s = hits[0]
            used.add(s["tracking_number"])
            _apply_duties(cur, s["tracking_number"], m["amount_ec"])
            processed.append({"tracking_number": s["tracking_number"], "buyer_name": s["buyer_name"],
                              "customs_duties": m["amount_ec"]})
    return {"success": True, "processed": processed, "unmatched": unmatched}


@router.delete("/accounting/shipment/{tracking}")
async def accounting_delete_shipment(tracking: str, user: dict = Depends(accounting_user)):
    """Remove an unpaid shipment (e.g. a mistaken import). Paid records cannot be deleted."""
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM shipments WHERE tracking_number = %s AND paid = FALSE", (tracking.strip(),))
        deleted = cur.rowcount
        cur.execute("DELETE FROM invoices WHERE tracking_number = %s", (tracking.strip(),))
    if not deleted:
        raise HTTPException(status_code=404, detail="No unpaid shipment with that tracking number.")
    return {"success": True}


class MarkPaidRequest(BaseModel):
    tracking_number: str
    date_paid: Optional[str] = None
    payment_method: str


def _normalise_method(m: str) -> str:
    s = (m or "").strip().lower()
    for code, label in PAYMENT_METHODS.items():
        if s in (code, label.lower()):
            return code
    if "bank" in s or "transfer" in s:
        return "bank_transfer"
    if "card" in s or "credit" in s or "debit" in s:
        return "card"
    if "cheque" in s or "check" in s:
        return "cheque"
    if "cash" in s:
        return "cash"
    raise HTTPException(status_code=400, detail="Invalid payment method. Use cash, bank_transfer, card or cheque.")


@router.post("/accounting/mark-paid")
async def accounting_mark_paid(body: MarkPaidRequest, user: dict = Depends(accounting_user)):
    _require_db()
    method = _normalise_method(body.payment_method)
    paid_on = parse_date(body.date_paid) if body.date_paid else today_ast()
    if paid_on is None:
        raise HTTPException(status_code=400, detail="Invalid date_paid. Use YYYY-MM-DD.")
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""UPDATE shipments SET paid = TRUE, date_paid = %s, payment_method = %s
                        WHERE tracking_number = %s RETURNING {SHIPMENT_COLS}""",
                    (paid_on, method, body.tracking_number.strip()))
        r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return {"success": True, "shipment": _row_to_json(r)}


def _daily_rows(d: date) -> list:
    _require_db()
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT {SHIPMENT_COLS} FROM shipments WHERE paid = TRUE AND date_paid = %s "
                    f"ORDER BY payment_method, buyer_name", (d,))
        return [_row_to_json(r) for r in cur.fetchall()]


def _summarise(rows: list) -> dict:
    by_method = {}
    for r in rows:
        m = r.get("payment_method") or "unspecified"
        s = by_method.setdefault(m, {"method": m, "label": PAYMENT_METHODS.get(m, m.title()), "count": 0,
                                     "customs_duties": 0.0, "clearance_fee": 0.0,
                                     "aaspa_security_fee": 0.0, "total": 0.0})
        s["count"] += 1
        s["customs_duties"] += r.get("customs_duties") or 0
        s["clearance_fee"] += r.get("clearance_fee") or 0
        s["aaspa_security_fee"] += r.get("aaspa_security_fee") or 0
        s["total"] += r.get("total_due") or 0
    for s in by_method.values():
        for k in ("customs_duties", "clearance_fee", "aaspa_security_fee", "total"):
            s[k] = round(s[k], 2)
    grand = {k: round(sum((r.get(src) or 0) for r in rows), 2) for k, src in
             (("customs_duties", "customs_duties"), ("clearance_fee", "clearance_fee"),
              ("aaspa_security_fee", "aaspa_security_fee"), ("total", "total_due"))}
    grand["count"] = len(rows)
    order = list(PAYMENT_METHODS.keys())
    subtotals = sorted(by_method.values(), key=lambda s: order.index(s["method"]) if s["method"] in order else 99)
    return {"subtotals": subtotals, "grand_total": grand}


def _parse_report_date(s: Optional[str]) -> date:
    if not s:
        return today_ast()
    d = parse_date(s)
    if d is None:
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD.")
    return d


@router.get("/accounting/daily-report")
async def accounting_daily_report(date: Optional[str] = None, user: dict = Depends(accounting_user)):
    d = _parse_report_date(date)
    rows = _daily_rows(d)
    return {"date": d.isoformat(), "payments": rows, **_summarise(rows)}


def _monthly_rows(year: int, month: int) -> list:
    from calendar import monthrange
    last_day = monthrange(year, month)[1]
    start = date(year, month, 1)
    end = date(year, month, last_day)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tracking_number, buyer_name, customs_duties, clearance_fee,
                       aaspa_security_fee, total_due, date_paid, payment_method
                FROM shipments
                WHERE paid = TRUE AND date_paid BETWEEN %s AND %s
                ORDER BY buyer_name ASC
            """, (start, end))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

def _unclaimed_rows(days: int) -> list:
    _require_db()
    cutoff = today_ast() - timedelta(days=days)
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""SELECT {SHIPMENT_COLS} FROM shipments
                        WHERE date_paid IS NULL AND COALESCE(paid, FALSE) = FALSE
                          AND date_of_arrival IS NOT NULL AND date_of_arrival <= %s
                        ORDER BY date_of_arrival ASC, buyer_name""", (cutoff,))
        rows = [_row_to_json(r) for r in cur.fetchall()]
    rows.sort(key=lambda r: r.get("days_outstanding", 0), reverse=True)
    return rows


@router.get("/accounting/monthly-report")
async def accounting_monthly_report(year: int = Query(...), month: int = Query(...), user: dict = Depends(accounting_user)):
    """Return all paid shipments for a given month."""
    rows = _monthly_rows(year, month)
    total = sum(r["total_due"] for r in rows)
    subtotals = {}
    for r in rows:
        pm = r.get("payment_method") or "unknown"
        subtotals[pm] = subtotals.get(pm, 0) + r["total_due"]
    return {"year": year, "month": month, "rows": rows, "total": total, "subtotals": subtotals, "count": len(rows)}

@router.get("/accounting/monthly-report/pdf")
async def accounting_monthly_report_pdf(year: int = Query(...), month: int = Query(...), user: dict = Depends(accounting_user)):
    rows = _monthly_rows(year, month)
    return _pdf_response(build_monthly_report_pdf(year, month, rows), f"SCS_Monthly_Report_{year}_{month:02d}.pdf")

@router.get("/accounting/unclaimed")
async def accounting_unclaimed(days: int = Query(14, ge=0), user: dict = Depends(accounting_user)):
    rows = _unclaimed_rows(days)
    return {"as_of": today_ast().isoformat(), "days": days, "count": len(rows),
            "total_due": round(sum(r.get("total_due") or 0 for r in rows), 2), "packages": rows}


# ─── PDF reports ───────────────────────────────────────────────────────────────

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

MAROON = HexColor("#7A1F2B")
GOLD = HexColor("#C9A227")
LIGHT = HexColor("#F7F1E3")
GRID = HexColor("#D9CFC0")

_H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=18, textColor=MAROON, alignment=TA_CENTER, leading=22)
_H2 = ParagraphStyle("h2", fontName="Helvetica", fontSize=10, textColor=HexColor("#444444"), alignment=TA_CENTER, leading=13)
_H3 = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=13, textColor=GOLD, alignment=TA_CENTER, leading=17, spaceBefore=4)
_META = ParagraphStyle("meta", fontName="Helvetica", fontSize=10, alignment=TA_LEFT, leading=13)
_CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5)
_SEC = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=11, textColor=MAROON, leading=14, spaceBefore=10, spaceAfter=4)


def _money(v) -> str:
    return f"EC${(v or 0):,.2f}"


def _fmt_date(s) -> str:
    d = parse_date(s)
    return d.strftime("%d %b %Y") if d else ""


def _footer(canvas, doc):
    canvas.saveState()
    w, _ = doc.pagesize
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(0.8)
    canvas.line(doc.leftMargin, 14 * mm, w - doc.rightMargin, 14 * mm)
    canvas.setFont("Helvetica-Oblique", 8.5)
    canvas.setFillColor(HexColor("#555555"))
    canvas.drawString(doc.leftMargin, 9 * mm, "Prepared by Accounting \u2014 Safe Cargo Services")
    canvas.drawRightString(w - doc.rightMargin, 9 * mm,
                           f"Generated {datetime.now(AST).strftime('%d %b %Y %I:%M %p')} AST  |  Page {doc.page}")
    canvas.restoreState()


def _table(data, col_widths, money_cols=(), total_rows=0):
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), MAROON),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for c in money_cols:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    body_end = len(data) - 1 - total_rows
    for i in range(1, body_end + 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
    if total_rows:
        style += [("BACKGROUND", (0, -total_rows), (-1, -1), HexColor("#EFE3C2")),
                  ("FONTNAME", (0, -total_rows), (-1, -1), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def build_daily_report_pdf(d: date, rows: list) -> bytes:
    summary = _summarise(rows)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=20 * mm,
                            title=f"Daily Cash Report {d.isoformat()}", author="Safe Cargo Services")
    el = [Paragraph("SAFE CARGO SERVICES", _H1), Paragraph("Sandy Ground, Anguilla", _H2),
          Paragraph("DAILY CASH REPORT", _H3), Spacer(1, 6),
          Paragraph(f"<b>Date:</b> {d.strftime('%A, %d %B %Y')}", _META), Spacer(1, 6)]

    header = ["Tracking #", "Customer Name", "Customs Duties", "5% Fee", "AASPA/Security Fee", "Total", "Payment Method"]
    data = [header]
    for r in rows:
        data.append([r.get("tracking_number", ""), Paragraph(r.get("buyer_name") or "", _CELL),
                     _money(r.get("customs_duties")), _money(r.get("clearance_fee")),
                     _money(r.get("aaspa_security_fee")), _money(r.get("total_due")),
                     r.get("payment_method_label") or ""])
    g = summary["grand_total"]
    data.append(["GRAND TOTAL", f"{g['count']} payment(s)", _money(g["customs_duties"]), _money(g["clearance_fee"]),
                 _money(g["aaspa_security_fee"]), _money(g["total"]), ""])
    if not rows:
        data.insert(1, ["", "No payments recorded for this date.", "", "", "", "", ""])
    widths = [42 * mm, 62 * mm, 30 * mm, 26 * mm, 36 * mm, 30 * mm, 42 * mm]
    el.append(_table(data, widths, money_cols=(2, 3, 4, 5), total_rows=1))

    el.append(Paragraph("Subtotals by Payment Method", _SEC))
    sdata = [["Payment Method", "Payments", "Customs Duties", "5% Fee", "AASPA/Security Fee", "Total"]]
    for s in summary["subtotals"]:
        sdata.append([s["label"], str(s["count"]), _money(s["customs_duties"]), _money(s["clearance_fee"]),
                      _money(s["aaspa_security_fee"]), _money(s["total"])])
    sdata.append(["GRAND TOTAL", str(g["count"]), _money(g["customs_duties"]), _money(g["clearance_fee"]),
                  _money(g["aaspa_security_fee"]), _money(g["total"])])
    el.append(_table(sdata, [50 * mm, 24 * mm, 34 * mm, 30 * mm, 38 * mm, 34 * mm],
                     money_cols=(1, 2, 3, 4, 5), total_rows=1))
    doc.build(el, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def build_monthly_report_pdf(year: int, month: int, rows: list) -> bytes:
    import calendar as cal_mod
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=15*mm, rightMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    MAROON = colors.HexColor("#8B0000")
    GOLD = colors.HexColor("#D4AC0D")
    CREAM = colors.HexColor("#FAF8F5")

    month_name = cal_mod.month_name[month]
    elements = []
    elements.append(Paragraph("SAFE CARGO SERVICES", styles["Title"]))
    elements.append(Paragraph("Sandy Ground, Anguilla  |  Tel: (264) 498-0194", styles["Normal"]))
    elements.append(Spacer(1, 4*mm))
    elements.append(Paragraph(f"MONTHLY CASH REPORT — {month_name.upper()} {year}", styles["Heading2"]))
    elements.append(Spacer(1, 4*mm))

    headers = ["#", "Tracking #", "Customer", "Duties (EC$)", "5% Fee (EC$)", "AASPA/Sec (EC$)", "Total (EC$)", "Payment Method", "Date Paid"]
    data = [headers]
    total = 0
    subtotals = {}
    for i, r in enumerate(rows, 1):
        pm = (r.get("payment_method") or "—").replace("_", " ").title()
        data.append([
            str(i),
            r.get("tracking_number",""),
            r.get("buyer_name",""),
            f"{r.get('customs_duties',0):.2f}",
            f"{r.get('clearance_fee',0):.2f}",
            f"{r.get('aaspa_security_fee',10):.2f}",
            f"{r.get('total_due',0):.2f}",
            pm,
            str(r.get("date_paid",""))
        ])
        total += r.get("total_due", 0)
        subtotals[pm] = subtotals.get(pm, 0) + r.get("total_due", 0)

    # Totals row
    data.append(["", "", "TOTAL", "", "", "", f"{total:.2f}", "", ""])

    col_widths = [15, 90, 90, 65, 60, 70, 65, 85, 65]
    t = Table(data, colWidths=[w*mm for w in col_widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), MAROON),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 7),
        ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, CREAM]),
        ("BACKGROUND", (0,-1), (-1,-1), GOLD),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
        ("GRID", (0,0), (-1,-1), 0.3, colors.grey),
        ("ALIGN", (3,0), (6,-1), "RIGHT"),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 4*mm))

    # Subtotals by payment method
    sub_lines = "  |  ".join(f"{k}: EC${v:.2f}" for k, v in subtotals.items())
    elements.append(Paragraph(f"<b>Subtotals by Payment Method:</b> {sub_lines}", styles["Normal"]))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph(f"<b>Grand Total: EC${total:.2f}  (US${total/2.6882:.2f})</b>", styles["Normal"]))
    elements.append(Spacer(1, 6*mm))
    elements.append(Paragraph("Prepared by Accounting — Safe Cargo Services", styles["Normal"]))

    doc.build(elements)
    buf.seek(0)
    return buf.read()

def build_unclaimed_pdf(days: int, rows: list) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=12 * mm, bottomMargin=20 * mm,
                            title="Unclaimed Packages Report", author="Safe Cargo Services")
    el = [Paragraph("SAFE CARGO SERVICES", _H1), Paragraph("Sandy Ground, Anguilla", _H2),
          Paragraph("UNCLAIMED PACKAGES REPORT", _H3), Spacer(1, 6),
          Paragraph(f"<b>As of:</b> {today_ast().strftime('%A, %d %B %Y')}", _META),
          Paragraph(f"Packages unpaid for more than {days} days", _META), Spacer(1, 6)]
    data = [["Tracking #", "Customer", "Description", "Date of Arrival", "Days Outstanding", "Total Due"]]
    for r in rows:
        data.append([r.get("tracking_number", ""), Paragraph(r.get("buyer_name") or "", _CELL),
                     Paragraph((r.get("description") or "")[:300], _CELL), _fmt_date(r.get("date_of_arrival")),
                     str(r.get("days_outstanding", "")), _money(r.get("total_due"))])
    if not rows:
        data.append(["", "No unclaimed packages.", "", "", "", ""])
    total = round(sum(r.get("total_due") or 0 for r in rows), 2)
    data.append(["TOTAL", f"{len(rows)} package(s)", "", "", "", _money(total)])
    widths = [42 * mm, 52 * mm, 84 * mm, 30 * mm, 28 * mm, 32 * mm]
    el.append(_table(data, widths, money_cols=(4, 5), total_rows=1))
    doc.build(el, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _pdf_response(pdf: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/accounting/daily-report/pdf")
async def accounting_daily_report_pdf(date: Optional[str] = None, user: dict = Depends(accounting_user)):
    d = _parse_report_date(date)
    return _pdf_response(build_daily_report_pdf(d, _daily_rows(d)), f"SCS_Daily_Cash_Report_{d.isoformat()}.pdf")


@router.get("/accounting/unclaimed/pdf")
async def accounting_unclaimed_pdf(days: int = Query(14, ge=0), user: dict = Depends(accounting_user)):
    rows = _unclaimed_rows(days)
    return _pdf_response(build_unclaimed_pdf(days, rows), f"SCS_Unclaimed_Packages_{today_ast().isoformat()}.pdf")
