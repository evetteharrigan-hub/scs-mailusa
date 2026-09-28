"""Browser test for payment-order upload in declaration customer invoices."""

import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pymupdf
import pytest


ROOT = Path(__file__).parent


def browser(session, *args):
    result = subprocess.run(
        ['agent-browser', '--session', session, *args],
        capture_output=True, text=True, timeout=45, check=True,
    )
    return result.stdout


def pdf_file(path, text):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    document.save(path)
    document.close()


@pytest.fixture
def ui(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        ['python', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', str(port), '--log-level', 'error'],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    session = 'payment-' + uuid.uuid4().hex[:10]
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    break
            time.sleep(.1)
        else:
            pytest.fail('UI server did not start')
        browser(session, 'open', f'http://127.0.0.1:{port}/')
        browser(session, 'eval', "sessionStorage.setItem('scs_auth', 'authenticated')")
        browser(session, 'reload')
        yield session, tmp_path
    finally:
        browser(session, 'close')
        server.terminate()
        server.wait(timeout=10)
        server.stderr.close()


def values(session):
    return json.loads(browser(session, 'eval',
                              "Array.from(document.querySelectorAll('input[type=number]')).map(i => i.value)"))


def test_upload_autofills_only_matching_tracking_and_allows_override(ui):
    session, tmp_path = ui
    browser(session, 'find', 'role', 'button', 'click', '--name', 'DECLARATIONS')
    browser(session, 'find', 'placeholder', 'e.g., 176-12345678', 'fill', 'AWB-TEST')
    browser(session, 'find', 'placeholder', 'e.g., MAN-2024-001', 'fill', 'MAN-TEST')
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Next')
    xlsx = tmp_path / 'shipments.xlsx'
    xlsx.write_bytes(b'preview mocked')
    browser(session, 'upload', 'input[accept=".xlsx,.xls"]', str(xlsx))
    browser(session, 'network', 'route', '**/preview', '--body', json.dumps({'rows': [
        {'tracking_number': 'MLBS0000694XX', 'buyer_name': 'Natalie'},
        {'tracking_number': 'MLBS37241XX', 'buyer_name': 'Keshonda'},
        {'tracking_number': 'MLBS999XX', 'buyer_name': 'Other'},
    ]}))
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Preview')
    browser(session, 'wait', '--text', 'MLBS37241XX')
    browser(session, 'network', 'route', '**/generate-declarations', '--body', '{"mock":"zip"}')
    browser(session, 'find', 'role', 'button', 'click', '--name', 'Download Declarations ZIP')
    browser(session, 'wait', '--text', 'Upload ASYCUDA Payment Order PDF to auto-fill duties')
    assert values(session) == ['', '', '']

    order = tmp_path / 'payment.pdf'
    pdf_file(order, '2026 0694NATALIEALLIE\n00RB100\nIM4\n28.72\n'
                    '2026 37241KESHONDALEY\n00RB200\nIM4\n21.09')
    browser(session, 'upload', '#payment-order-pdf', str(order))
    browser(session, 'wait', '--text', '✓ 2 duties auto-filled from payment order')
    assert values(session) == ['28.72', '21.09', '']

    browser(session, 'fill', 'input[type=number]', '17.89')
    assert values(session) == ['17.89', '21.09', '']

    no_match = tmp_path / 'unmatched.pdf'
    pdf_file(no_match, '2026 4444NOMATCH\n00RB300\nIM4\n5.50')
    browser(session, 'upload', '#payment-order-pdf', str(no_match))
    browser(session, 'wait', '--text', 'No payment order references matched customer tracking numbers.')
    assert values(session) == ['17.89', '21.09', '']
    assert 'duties auto-filled from payment order' not in browser(session, 'snapshot')
