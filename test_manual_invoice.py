"""Manual invoice API regressions without a production database."""
import base64
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient

import portal
from main import app, IMPORT_SHIPMENTS_SQL


@pytest.fixture
def invoice_db(monkeypatch):
    invoices = {}
    shipments = [
        {"tracking_number": "MLBS1", "buyer_name": "Jane Doe", "buyer_address": "Sandy Ground",
         "buyer_phone": "123", "buyer_email": "jane@example.com", "description": "Books",
         "cif_value": 25.0, "date_of_arrival": date(2026, 9, 29), "shipper_name": "Shop"},
        {"tracking_number": "MLBS2", "buyer_name": "Jane Doe", "buyer_address": "Sandy Ground",
         "buyer_phone": "123", "buyer_email": "jane@example.com", "description": "Shoes",
         "cif_value": 30.0, "date_of_arrival": None, "shipper_name": "Other"},
        {"tracking_number": "XYZ", "buyer_name": "James", "buyer_address": "Valley",
         "buyer_phone": "", "buyer_email": "", "description": "", "cif_value": 0,
         "date_of_arrival": None, "shipper_name": ""},
    ]
    statements = []

    class Cursor:
        result = None

        def execute(self, sql, params=None):
            statements.append((sql, params))
            if "information_schema.columns" in sql:
                self.result = [{'column_name': name} for name in ('buyer_address', 'buyer_phone', 'buyer_email', 'shipper_name')]
            elif "FROM shipments" in sql:
                assert "buyer_name ILIKE %s" in sql and "LIMIT 15" in sql
                needle = params[0].strip('%').lower()
                self.result = [row for row in shipments if needle in row['buyer_name'].lower()][:20]
            elif "CREATE TABLE IF NOT EXISTS invoices" in sql:
                self.result = []
            elif "INSERT INTO invoices" in sql:
                idx = len(invoices) + 1
                invoices[idx] = (*params[:-1], params[-1].adapted)
                self.result = [(idx,)]
            elif "FROM invoices" in sql:
                self.result = [(invoices[params[0]][-1],)] if params[0] in invoices else []
            else:
                raise AssertionError(sql)

        def fetchall(self):
            return self.result

        def fetchone(self):
            return self.result[0] if self.result else None

    class Conn:
        def cursor(self, **kwargs):
            return Cursor()

    @contextmanager
    def get_conn():
        yield Conn()

    monkeypatch.setattr(portal, "get_conn", get_conn)
    monkeypatch.setattr(portal, "_require_db", lambda: None)
    yield TestClient(app), invoices, statements


def test_customer_search_prefills_and_limits(invoice_db):
    client, _, statements = invoice_db
    assert client.get('/search-customer?q=').json() == []
    assert len(statements) == 0
    response = client.get('/search-customer', params={'q': 'Jane'})
    assert response.status_code == 200
    assert response.json() == [
        {"tracking_number": "MLBS1", "buyer_name": "Jane Doe", "buyer_address": "Sandy Ground",
         "buyer_phone": "123", "buyer_email": "jane@example.com", "description": "Books",
         "cif_value": 25.0, "date_of_arrival": "2026-09-29", "shipper_name": "Shop"},
        {"tracking_number": "MLBS2", "buyer_name": "Jane Doe", "buyer_address": "Sandy Ground",
         "buyer_phone": "123", "buyer_email": "jane@example.com", "description": "Shoes",
         "cif_value": 30.0, "date_of_arrival": None, "shipper_name": "Other"},
    ]
    assert statements[1][1] == ('%Jane%',)


def test_customer_search_when_db_unavailable(monkeypatch):
    @contextmanager
    def down():
        raise RuntimeError('down')
        yield

    monkeypatch.setattr(portal, 'get_conn', down)
    assert TestClient(app).get('/search-customer?q=Jane').json() == []


def test_save_and_open_invoice(invoice_db):
    client, invoices, _ = invoice_db
    pdf = b'%PDF-1.4\nmanual invoice\n%%EOF'
    response = client.post('/save-invoice', json={
        'tracking_number': 'MLBS1', 'customer_name': 'Jane Doe', 'customs_duties': 100,
        'arrival_date': '2026-09-29', 'total_ec': 115, 'total_usd': 42.78,
        'pdf_base64': base64.b64encode(pdf).decode(),
    })
    assert response.status_code == 200
    assert response.json() == {'id': 1, 'success': True}
    assert invoices[1][0:8] == ('MLBS1', 'Jane Doe', 100.0, 5.0, 10.0, 115.0, 42.78, '2026-09-29')
    opened = client.get('/invoice-pdf/1?print=true')
    assert opened.status_code == 200
    assert opened.content == pdf
    assert opened.headers['content-type'] == 'application/pdf'
    assert opened.headers['content-disposition'].startswith('inline;')
    assert client.get('/invoice-pdf/2').status_code == 404


@pytest.mark.parametrize('amount,encoded', [(-1, base64.b64encode(b'%PDF-1.4').decode()),
                                            (10, 'not base64'), (10, base64.b64encode(b'not a pdf').decode())])
def test_reject_invalid_invoice(invoice_db, amount, encoded):
    client, invoices, _ = invoice_db
    response = client.post('/save-invoice', json={
        'tracking_number': 'MLBS1', 'customer_name': 'Jane Doe', 'customs_duties': amount,
        'arrival_date': '', 'total_ec': 20, 'total_usd': 7.44, 'pdf_base64': encoded,
    })
    assert response.status_code == 422
    assert not invoices


def test_search_source_and_history_schema_support_prefill():
    for column in ('buyer_address', 'buyer_phone', 'buyer_email', 'shipper_name'):
        assert column in portal.SCHEMA_SQL
        assert column in portal.UPSERT_SQL
        assert column in IMPORT_SHIPMENTS_SQL
    assert 'invoice_form_data JSONB' in portal.SCHEMA_SQL
    assert 'pdf_data BYTEA' in portal.SCHEMA_SQL


def test_customer_invoice_form_saved_to_history(monkeypatch):
    queries = []

    class Cursor:
        def execute(self, sql, params):
            queries.append((sql, params))

        def fetchone(self):
            return {'id': 'inv-42', 'type': 'Customer Invoice',
                    'invoiceFormData': {'tracking_number': 'MLBS1', 'customs_duties': '100'}}

    class Conn:
        def cursor(self, **kwargs):
            return Cursor()

    @contextmanager
    def get_conn():
        yield Conn()

    monkeypatch.setattr(portal, 'get_conn', get_conn)
    monkeypatch.setattr(portal, '_require_db', lambda: None)
    response = TestClient(app).post('/api/history', json={
        'id': 'inv-42', 'type': 'Customer Invoice', 'row_count': 1,
        'invoiceFormData': {'tracking_number': 'MLBS1', 'customs_duties': '100'},
    })
    assert response.status_code == 200
    assert response.json()['invoiceFormData']['tracking_number'] == 'MLBS1'
    assert 'invoice_form_data' in queries[0][0]
    assert queries[0][1][-1].adapted['tracking_number'] == 'MLBS1'


def test_save_unavailable_returns_false(monkeypatch):
    def unavailable():
        raise RuntimeError('database down')

    @contextmanager
    def down():
        unavailable()
        yield

    monkeypatch.setattr(portal, 'get_conn', down)
    response = TestClient(app).post('/save-invoice', json={
        'tracking_number': 'MLBS1', 'customer_name': 'Jane Doe', 'customs_duties': 10,
        'arrival_date': '2026-09-29', 'total_ec': 20.5, 'total_usd': 7.63,
        'pdf_base64': base64.b64encode(b'%PDF-1.4\n%%EOF').decode(),
    })
    assert response.json() == {'success': False}
