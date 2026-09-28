"""Tests for the PDF Waybill Splitter endpoint."""
import io
import zipfile
import pytest
from fastapi.testclient import TestClient

# Need pymupdf to create test PDFs
import pymupdf as fitz

from main import app, _extract_tracking_from_page, _extract_consignee_from_page, _clean_name_for_file

client = TestClient(app)


# ─── Unit tests for helper functions ─────────────────────────────────────────


class TestExtractTracking:
    def test_rr_pattern(self):
        text = "Some text RR123456789US more text"
        assert _extract_tracking_from_page(text) == "RR123456789US"

    def test_mlbs_pattern(self):
        text = "Tracking: MLBS123456XX shipped"
        assert _extract_tracking_from_page(text) == "MLBS123456XX"

    def test_cc_pattern(self):
        text = "Reference CC1234567 item"
        assert _extract_tracking_from_page(text) == "CC1234567"

    def test_postal_pattern(self):
        text = "AWB EE123456789US"
        assert _extract_tracking_from_page(text) == "EE123456789US"

    def test_keyword_tracking(self):
        text = "Tracking Number: ABC12345678"
        result = _extract_tracking_from_page(text)
        assert result == "ABC12345678"

    def test_waybill_no_keyword(self):
        text = "Waybill No: WB99887766"
        result = _extract_tracking_from_page(text)
        assert result == "WB99887766"

    def test_empty_text(self):
        assert _extract_tracking_from_page("") == ""
        assert _extract_tracking_from_page(None) == ""

    def test_no_tracking(self):
        text = "Just some random text with no tracking info"
        assert _extract_tracking_from_page(text) == ""


class TestExtractConsignee:
    def test_consignee_keyword(self):
        text = "Consignee: John Smith\nAddress: 123 Main St"
        assert "John Smith" in _extract_consignee_from_page(text)

    def test_deliver_to_keyword(self):
        text = "Deliver To: Jane Doe\nCity: Valley"
        assert "Jane Doe" in _extract_consignee_from_page(text)

    def test_recipient_keyword(self):
        text = "Recipient: Bob Johnson\nPhone: 555"
        assert "Bob Johnson" in _extract_consignee_from_page(text)

    def test_ship_to_keyword(self):
        text = "Ship To: Maria Garcia\n123 St"
        assert "Maria Garcia" in _extract_consignee_from_page(text)

    def test_empty_text(self):
        assert _extract_consignee_from_page("") == ""
        assert _extract_consignee_from_page(None) == ""

    def test_no_consignee(self):
        text = "Some random text without consignee"
        assert _extract_consignee_from_page(text) == ""


class TestCleanName:
    def test_basic(self):
        assert _clean_name_for_file("John Smith") == "JOHN_SMITH"

    def test_special_chars(self):
        assert _clean_name_for_file("O'Brien-Jones") == "OBRIENJONES"

    def test_empty(self):
        assert _clean_name_for_file("") == ""
        assert _clean_name_for_file(None) == ""

    def test_multiple_spaces(self):
        assert _clean_name_for_file("  John   Smith  ") == "JOHN_SMITH"


# ─── Helper to create test PDFs ─────────────────────────────────────────────


def create_test_pdf(pages_text: list) -> bytes:
    """Create a multi-page PDF with given text on each page."""
    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=12)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ─── Integration tests for the endpoint ──────────────────────────────────────


class TestSplitWaybillsEndpoint:
    def test_split_basic(self):
        """Split a 3-page PDF, each page becomes a separate file."""
        pdf = create_test_pdf([
            "Tracking: MLBS123456XX\nConsignee: John Smith\nPackage details",
            "AWB: RR987654321US\nConsignee: Jane Doe\nMore info",
            "Page with no tracking info\nJust some text",
        ])

        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("test_waybills.pdf", pdf, "application/pdf")},
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "split_waybills.zip" in response.headers.get("content-disposition", "")
        assert response.headers.get("x-page-count") == "3"

        # Verify ZIP contents
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        assert len(names) == 3

        # Check that named files exist (with tracking numbers)
        names_str = " ".join(names)
        assert "MLBS123456XX" in names_str
        assert "RR987654321US" in names_str
        # Third page should be page_3_waybill.pdf
        assert any("page_3" in n for n in names)

        # Each file should be a valid PDF
        for name in names:
            data = zf.read(name)
            assert data[:4] == b'%PDF'

        zf.close()

    def test_split_single_page(self):
        """Single page PDF should produce a ZIP with one file."""
        pdf = create_test_pdf(["Tracking: CC1234567\nConsignee: Alice Brown"])

        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("single.pdf", pdf, "application/pdf")},
        )

        assert response.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        assert len(names) == 1
        assert "CC1234567" in names[0]
        zf.close()

    def test_split_empty_file(self):
        """Empty file should return 400 error."""
        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("empty.pdf", b"", "application/pdf")},
        )
        assert response.status_code == 400

    def test_split_invalid_file(self):
        """Non-PDF file should return 400 error."""
        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("bad.pdf", b"not a pdf", "application/pdf")},
        )
        assert response.status_code == 400

    def test_consignee_in_filename(self):
        """Consignee name should appear cleaned in filename."""
        pdf = create_test_pdf([
            "Tracking: MLBS999999XX\nConsignee: Bob O'Brien-Smith\nDetails here",
        ])

        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("test.pdf", pdf, "application/pdf")},
        )

        assert response.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = zf.namelist()
        assert len(names) == 1
        # Should contain tracking and cleaned consignee
        assert "MLBS999999XX" in names[0]
        assert "_waybill.pdf" in names[0]
        zf.close()

    def test_no_tracking_fallback(self):
        """Pages without tracking should use page_N naming."""
        pdf = create_test_pdf([
            "Just some random text",
            "More random text here",
        ])

        response = client.post(
            "/split-waybills-pdf",
            files={"waybill_pdf": ("test.pdf", pdf, "application/pdf")},
        )

        assert response.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(response.content))
        names = sorted(zf.namelist())
        assert len(names) == 2
        assert "page_1" in names[0]
        assert "page_2" in names[1]
        zf.close()
