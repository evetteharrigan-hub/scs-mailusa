"""Browser regressions for warehouse/accounting spreadsheet import modal."""

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).parent


def browser(session, *args):
    result = subprocess.run(["agent-browser", "--session", session, *args],
                            capture_output=True, text=True, timeout=45, check=True)
    return result.stdout


@pytest.fixture
def ui(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(["python", "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "error"], cwd=ROOT,
                              env={**os.environ, "WAREHOUSE_PASSWORD": "test-warehouse", "ACCOUNTING_PASSWORD": "test-accounting"},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = "import-" + uuid.uuid4().hex[:12]
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(.1)
        else:
            pytest.fail("UI server did not start")
        browser(session, "open", f"http://127.0.0.1:{port}/portal")
        yield session, tmp_path
    finally:
        browser(session, "close")
        server.terminate()
        server.wait(timeout=10)


def sample_sheet(path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Tracking Number", "Buyer Name", "Items Description", "CIF Verified", "FOB Verified"])
    sheet.append(["MLBS12345", "Buyer", "[Tools]", 20, 15])
    workbook.save(path)
    workbook.close()


@pytest.mark.parametrize("role,password,search_url", [
    ("warehouse", "test-warehouse", "**/portal/shipments/search*"),
    ("accounting", "test-accounting", "**/portal/accounting/search*"),
])
def test_import_modal_for_both_roles_success_and_error(ui, role, password, search_url):
    session, tmp_path = ui
    browser(session, "network", "route", search_url, "--body", "[]")
    browser(session, "fill", "#username", role)
    browser(session, "fill", "#password", password)
    browser(session, "click", "#login-btn")
    browser(session, "wait", "--text", "Import Shipments")
    browser(session, "click", "#open-import-btn")
    snapshot = browser(session, "snapshot", "-i")
    assert "Import Shipments from Spreadsheet" in snapshot
    assert "ASYCUDA Payment Order PDF (optional, auto-fills duties)" in snapshot
    assert json.loads(browser(session, "eval", "document.querySelector('label[for=import-date]').textContent")) == "Date of Arrival"
    xlsx = tmp_path / "shipments.xlsx"
    sample_sheet(xlsx)
    assert json.loads(browser(session, "eval", "document.querySelector('#import-date').value = '2026-09-28'")) == "2026-09-28"
    browser(session, "upload", "#import-xlsx", str(xlsx))
    browser(session, "network", "route", "**/portal/import-shipments", "--body",
            json.dumps({"imported": 1, "updated": 2, "message": "ok"}))
    browser(session, "click", "#import-btn")
    browser(session, "wait", "--text", "✓ 1 shipments imported, 2 updated")
    assert json.loads(browser(session, "eval", "document.querySelector('#import-xlsx').value")) == ""
    bad_xlsx = tmp_path / "bad.xlsx"
    bad_xlsx.write_bytes(b"not an xlsx")
    browser(session, "upload", "#import-xlsx", str(bad_xlsx))
    browser(session, "network", "unroute", "**/portal/import-shipments")
    browser(session, "click", "#import-btn")
    browser(session, "wait", "--text", "Could not read .xlsx spreadsheet.")
    assert json.loads(browser(session, "eval", "document.querySelector('#import-msg').textContent")) == "Could not read .xlsx spreadsheet."
