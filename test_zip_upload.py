"""Tests for ZIP-based invoice upload in SCS-MAILUSA."""
import io
import os
import zipfile
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

# Ensure test can import main
import sys
sys.path.insert(0, os.path.dirname(__file__))

from main import app

client = TestClient(app)


def _make_fake_xlsx():
    """Create a minimal valid xlsx file for testing."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Tracking Number", "Buyer Name", "Weight", "CIF Verified", "FOB Verified",
               "Items", "Items Description", "Items HS Codes", "Items Quantities",
               "Shipper", "Shipper Country", "DDP/DDU"])
    ws.append(["MLBS001TEST", "John Doe", 2.5, 50.0, 40.0,
               "1", "[Widget]", "[8471300000]", "[1]",
               "Test Shipper", "US", "FOB"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def _make_fake_pdf(tracking="MLBS001TEST"):
    """Create a minimal PDF-like bytes (won't parse real items but tests the flow)."""
    # Just enough bytes to be recognized as "uploaded" 
    return b"%PDF-1.4 fake pdf content for " + tracking.encode()


def _make_invoice_zip(pdf_map: dict) -> bytes:
    """Create a ZIP file containing PDFs. pdf_map: {filename: bytes}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in pdf_map.items():
            zf.writestr(name, data)
    buf.seek(0)
    return buf.getvalue()


class TestEndpointSignatures:
    """Verify all endpoints accept invoice_zip instead of pdf_files."""

    def test_generate_declarations_accepts_zip(self):
        xlsx_bytes = _make_fake_xlsx()
        zip_bytes = _make_invoice_zip({"MLBS001TEST_invoice.pdf": _make_fake_pdf()})

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
            "voyage_number": "V001",
            "date_of_departure": "2026-01-15",
            "carrier_name": "TEST CARRIER",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("invoice_zip", ("invoices.zip", io.BytesIO(zip_bytes), "application/zip")),
        ])
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

    def test_generate_declarations_no_zip(self):
        """Declarations should work without invoice ZIP."""
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ])
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

    def test_generate_combined_accepts_zip(self):
        xlsx_bytes = _make_fake_xlsx()
        zip_bytes = _make_invoice_zip({"MLBS001TEST_invoice.pdf": _make_fake_pdf()})

        resp = client.post("/generate", data={
            "master_awb": "176-99999999",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("invoice_zip", ("invoices.zip", io.BytesIO(zip_bytes), "application/zip")),
        ])
        assert resp.status_code == 200

    def test_preview_accepts_zip(self):
        xlsx_bytes = _make_fake_xlsx()
        zip_bytes = _make_invoice_zip({"MLBS001TEST_invoice.pdf": _make_fake_pdf()})

        resp = client.post("/preview", data={
            "master_awb": "176-99999999",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("invoice_zip", ("invoices.zip", io.BytesIO(zip_bytes), "application/zip")),
        ])
        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert data["total"] >= 1

    def test_waybills_no_pdf_param(self):
        """Waybills endpoint should work (never accepted PDFs)."""
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-waybills", data={
            "master_awb": "176-99999999",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ])
        assert resp.status_code == 200


class TestZipExtraction:
    """Verify ZIP contents are properly extracted and processed."""

    def test_zip_with_nested_pdfs(self):
        """PDFs in subdirectories inside ZIP should be extracted."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("invoices/MLBS001TEST_invoice.pdf", _make_fake_pdf("MLBS001TEST"))
            zf.writestr("other/README.txt", "ignore me")
        buf.seek(0)
        zip_bytes = buf.getvalue()
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("invoice_zip", ("invoices.zip", io.BytesIO(zip_bytes), "application/zip")),
        ])
        assert resp.status_code == 200

    def test_zip_skips_macosx(self):
        """__MACOSX entries should be skipped."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr("MLBS001TEST.pdf", _make_fake_pdf())
            zf.writestr("__MACOSX/._MLBS001TEST.pdf", b"mac resource fork")
        buf.seek(0)
        zip_bytes = buf.getvalue()
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("invoice_zip", ("invoices.zip", io.BytesIO(zip_bytes), "application/zip")),
        ])
        assert resp.status_code == 200

    def test_old_pdf_files_param_rejected(self):
        """Sending the old 'pdf_files' field name should NOT be processed as invoice data."""
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("pdf_files", ("MLBS001TEST.pdf", io.BytesIO(_make_fake_pdf()), "application/pdf")),
        ])
        # Should still succeed (old param is just ignored), but no invoices matched
        assert resp.status_code == 200


class TestOutputZipContents:
    """Verify the output ZIP structure."""

    def test_declarations_zip_has_xml(self):
        xlsx_bytes = _make_fake_xlsx()

        resp = client.post("/generate-declarations", data={
            "master_awb": "176-99999999",
            "manifest_reference": "MAN-TEST-001",
        }, files=[
            ("xlsx_file", ("test.xlsx", io.BytesIO(xlsx_bytes), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ])
        assert resp.status_code == 200

        out_zip = zipfile.ZipFile(io.BytesIO(resp.content))
        names = out_zip.namelist()
        assert len(names) >= 1
        assert any(name.endswith('_declaration.xml') for name in names)
