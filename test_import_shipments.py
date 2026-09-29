"""Shipment import endpoint regressions without a production database."""

import io
from contextlib import contextmanager

import openpyxl
import pymupdf
import pytest
from fastapi.testclient import TestClient

import portal
from main import app, IMPORT_SHIPMENTS_SQL


client = TestClient(app)
AUTH = {"Authorization": "Bearer warehouse-session"}


def spreadsheet(rows, headers=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers or ["Tracking Number", "Buyer Name", "Items Description", "CIF Verified", "FOB Verified"])
    for row in rows:
        ws.append(row)
    result = io.BytesIO()
    wb.save(result)
    wb.close()
    return result.getvalue()


def payment_pdf(text):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    result = doc.tobytes()
    doc.close()
    return result


def upload(rows=None, pdf=None, arrival_date="2026-09-28", auth=AUTH, xlsx=None):
    files = {"xlsx_file": ("shipment.xlsx", xlsx if xlsx is not None else spreadsheet(rows or []),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    if pdf is not None:
        files["pdf_file"] = ("order.pdf", pdf, "application/pdf")
    return client.post("/portal/import-shipments", files=files, data={"arrival_date": arrival_date}, headers=auth)


@pytest.fixture
def fake_db(monkeypatch):
    entries = {}
    queries = []

    class Cursor:
        inserted = False

        def execute(self, sql, record):
            queries.append(sql)
            tracking = record["tracking_number"]
            self.inserted = tracking not in entries
            entries.setdefault(tracking, {}).update(record)

        def fetchone(self):
            return (self.inserted,)

    class Conn:
        def cursor(self):
            return Cursor()

    @contextmanager
    def conn():
        yield Conn()

    monkeypatch.setattr(portal, "_require_db", lambda: None)
    monkeypatch.setattr(portal, "get_conn", conn)
    return entries, queries


def test_import_with_pdf_matches_digits_and_calculates_fees(fake_db):
    entries, queries = fake_db
    response = upload([
        ("MLBS0000694XX", " Natalie ", "[Shoes][T-Shirt]", 100.257, 80),
        ("MLBS37241XX", "Keshonda", "[Laptop]", 50, 25),
        ("MLBS999XX", "Other", "[Paper]", 0, 0),
    ], payment_pdf("2026 0694NATALIEALLIE\n00RB100\nIM4\n28.72\n"
                   "2026 37241KESHONDALEY\n00RB200\nIM4\n21.09"))
    assert response.status_code == 200, response.text
    assert response.json() == {"imported": 3, "updated": 0, "message": "3 shipments imported, 0 updated"}
    natalie = entries["MLBS0000694XX"]
    assert natalie["buyer_name"] == "Natalie"
    assert natalie["description"] == "Shoes, T-Shirt"
    assert str(natalie["date_of_arrival"]) == "2026-09-28"
    assert natalie["cif_value"] == 100.26
    assert {key: natalie[key] for key in ("customs_duties", "clearance_fee", "aaspa_security_fee", "total_due")} == {
        "customs_duties": 28.72, "clearance_fee": 1.44, "aaspa_security_fee": 10.0, "total_due": 40.16}
    assert entries["MLBS37241XX"]["customs_duties"] == 21.09
    assert entries["MLBS999XX"]["total_due"] == 10.0
    assert len(queries) == 3


def test_reimport_updates_all_values_without_touching_payment_and_counts_once(fake_db):
    entries, queries = fake_db
    entries["MLBS1"] = {"date_paid": "2026-09-26", "payment_method": "cash", "paid": True,
                        "customs_duties": 40.0, "total_due": 52.0}
    response = upload([("MLBS1", "Changed", "[Lamp]", 0, 10),
                       ("MLBS1", "Final", "[Lamp][Book]", 0, 10),
                       ("MLBS2", "New", "Plain", 0, 10)], auth={"Authorization": "Bearer accounting-session"})
    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert response.json()["updated"] == 1
    assert entries["MLBS1"]["buyer_name"] == "Final"
    assert entries["MLBS1"]["description"] == "Lamp, Book"
    assert entries["MLBS1"]["customs_duties"] == 0
    assert entries["MLBS1"]["total_due"] == 10
    assert entries["MLBS1"]["date_paid"] == "2026-09-26"
    assert entries["MLBS1"]["payment_method"] == "cash"
    assert entries["MLBS1"]["paid"] is True
    assert len(queries) == 2
    update_clause = IMPORT_SHIPMENTS_SQL.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    for payment_col in ("date_paid", "payment_method", "paid"):
        assert payment_col not in update_clause


def test_import_requires_login_and_valid_files(fake_db):
    assert upload([("MLBS1", "Buyer", "Item", 10, 8)], auth={}).status_code == 401
    assert upload([("MLBS1", "Buyer", "Item", 10, 8)], arrival_date="09/28/2026").status_code == 400
    assert upload([("MLBS1", "Buyer", "Item", 10, 8)], arrival_date="2026-02-30").status_code == 400
    assert upload(xlsx=b"not excel").status_code == 400
    assert upload(rows=[]).status_code == 400
    assert upload([("MLBS1", "Buyer", "Item", 10, 8)], pdf=b"not pdf").status_code == 400
    assert not fake_db[1]
