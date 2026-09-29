"""Browser coverage for the manual customer invoice flow."""
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parent


def browser(session, *args):
    result = subprocess.run(['agent-browser', '--session', session, *args],
                            capture_output=True, text=True, timeout=50, check=True)
    return result.stdout


@pytest.fixture
def ui():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(['python', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1',
                               '--port', str(port), '--log-level', 'error'], cwd=ROOT,
                              env={**os.environ, 'SCS_MAILUSA_USERNAME': 'test', 'SCS_MAILUSA_PASSWORD': 'test'},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = 'manual-' + uuid.uuid4().hex[:12]
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            time.sleep(.1)
        else:
            pytest.fail('UI server did not start')
        yield session, port
    finally:
        try:
            browser(session, 'close')
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_manual_invoice_search_calculate_save_and_open(ui):
    session, port = ui
    matches = [
        {'tracking_number': 'MLBS101', 'buyer_name': 'Jane Doe', 'buyer_address': 'The Valley',
         'buyer_phone': '2645551111', 'buyer_email': 'jane@example.com', 'description': 'Books',
         'cif_value': 20, 'date_of_arrival': '2026-09-29', 'shipper_name': 'Bookseller'},
        {'tracking_number': 'MLBS102', 'buyer_name': 'Jane Doe', 'buyer_address': 'Sandy Ground',
         'buyer_phone': '2645552222', 'buyer_email': 'jane@example.com', 'description': 'Shoes',
         'cif_value': 40, 'date_of_arrival': '2026-09-28', 'shipper_name': 'Shoemaker'},
    ]
    browser(session, 'network', 'route', '**/search-customer*', '--body', json.dumps(matches))
    browser(session, 'network', 'route', '**/save-invoice', '--body', json.dumps({'id': 71, 'success': True}))
    browser(session, 'network', 'route', '**/api/history', '--body', json.dumps({'id': 'inv-71', 'type': 'Customer Invoice'}))
    browser(session, 'open', f'http://127.0.0.1:{port}/')
    browser(session, 'fill', 'input[placeholder="Enter username"]', 'test')
    browser(session, 'fill', 'input[placeholder="Enter password"]', 'test')
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Sign In')
    browser(session, 'wait', '--text', 'CUSTOMER INVOICE')
    browser(session, 'find', 'role', 'button', 'click', '--name', 'CUSTOMER INVOICE')
    titles = json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("details > summary")).map(el => el.textContent)'))
    assert titles.index('Create Invoice Manually') < titles.index('Generate Individual Invoice PDF')
    browser(session, 'click', 'details > summary')
    browser(session, 'fill', '#manual-customer-search', 'Jane')
    browser(session, 'wait', '--text', 'Jane Doe — MLBS101')
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Jane Doe — MLBS101')
    assert json.loads(browser(session, 'eval', 'document.querySelector("#manual-customer-name").value')) == 'Jane Doe'
    assert json.loads(browser(session, 'eval', 'document.querySelector("#manual-arrival").value')) == '2026-09-29'
    assert json.loads(browser(session, 'eval', 'document.querySelector("#manual-description").value')) == 'Books'
    browser(session, 'select', '#manual-tracking', 'MLBS102')
    assert json.loads(browser(session, 'eval', 'document.querySelector("#manual-description").value')) == 'Shoes'
    assert json.loads(browser(session, 'eval', 'document.querySelector("#manual-arrival").value')) == '2026-09-28'
    browser(session, 'fill', '#manual-duties', '100')
    assert json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("input[readonly]")).find(el => el.getAttribute("aria-label") === "Import Clearance Fee (EC$)").value')) == '5.00'
    assert json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("input[readonly]")).find(el => el.getAttribute("aria-label") === "Total EC$").value')) == '115.00'
    assert json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("input[readonly]")).find(el => el.getAttribute("aria-label") === "Total US$").value')) == '42.78'
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Process Invoice')
    browser(session, 'wait', '--text', '✓ Invoice saved')
    assert json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("a")).find(el => el.getAttribute("href") === "/invoice-pdf/71").textContent')) == 'Open PDF'
    assert json.loads(browser(session, 'eval', 'Array.from(document.querySelectorAll("a")).find(el => el.getAttribute("href") === "/invoice-pdf/71?print=true").textContent')) == 'Print'
    requests = json.loads(browser(session, 'network', 'requests', '--json'))
    assert '/generate-customer-invoice' in str(requests), requests
    assert '/save-invoice' in str(requests), requests
    assert '/api/history' in str(requests), requests
