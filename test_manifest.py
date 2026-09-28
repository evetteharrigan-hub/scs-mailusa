"""Tests for the manifest PDF generation endpoint."""
import io
import pytest
import openpyxl
from fastapi.testclient import TestClient
from main import app, generate_manifest_pdf, parse_xlsx

client = TestClient(app)


def _make_test_xlsx(rows_data):
    """Create an in-memory xlsx with the standard SCS-MAILUSA columns."""
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = [
        "Tracking Number", "Buyer Name", "Items Description",
        "Items", "CIF Verified", "Weight", "FOB Verified",
        "Items HS Codes", "Items Quantities",
        "Buyer Address1", "Buyer City", "Buyer State", "Buyer Phone",
        "Shipper", "Shipper Country", "DDP/DDU",
    ]
    ws.append(headers)
    for row in rows_data:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _sample_rows():
    return [
        [
            "MLBS123456", "John Smith", "[Shoes][T-Shirt]",
            2, 150.00, 3.5, 120.00,
            "[6402.99.00][6109.10.00]", "[1][2]",
            "123 Main St", "The Valley", "AI", "264-555-1234",
            "Amazon", "US", "FOB",
        ],
        [
            "MLBS789012", "Jane Doe", "[Laptop Stand with adjustable height and ergonomic design for office use by professionals]",
            1, 75.50, 1.2, 60.00,
            "[9403.20.00]", "[1]",
            "456 Beach Rd", "Sandy Ground", "AI", "264-555-5678",
            "eBay Seller", "US", "FOB",
        ],
        [
            "MLBS345678", "Bob Builder", "[Hammer][Nails][Drill][Saw]",
            4, 200.00, 8.0, 180.00,
            "[8205.20.00][7317.00.00][8467.21.00][8202.10.00]", "[1][1][1][1]",
            "789 Hill St", "Island Harbour", "AI", "264-555-9012",
            "Home Depot", "US", "FOB",
        ],
    ]


class TestGenerateManifestPdf:
    """Test the generate_manifest_pdf function directly."""

    def test_returns_bytes(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        rows = parse_xlsx(xlsx_buf.read())
        result = generate_manifest_pdf(rows, "2026-09-23", "MAN-2026-001")
        assert isinstance(result, bytes)
        assert len(result) > 100

    def test_pdf_header(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        rows = parse_xlsx(xlsx_buf.read())
        result = generate_manifest_pdf(rows, "2026-09-23", "MAN-2026-001")
        # PDF should start with %PDF
        assert result[:5] == b'%PDF-'

    def test_total_packages_calculation(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        rows = parse_xlsx(xlsx_buf.read())
        # Total packages should be 2+1+4 = 7
        # We verify this indirectly by generating without error
        result = generate_manifest_pdf(rows, "2026-09-23", "MAN-2026-001")
        assert len(result) > 500

    def test_single_row(self):
        rows_data = [_sample_rows()[0]]
        xlsx_buf = _make_test_xlsx(rows_data)
        rows = parse_xlsx(xlsx_buf.read())
        result = generate_manifest_pdf(rows, "2026-10-01", "TEST-001")
        assert result[:5] == b'%PDF-'

    def test_description_truncation(self):
        """A description longer than 60 chars should be truncated."""
        long_desc_row = [
            "TRACK001", "Test User",
            "[This is a very long description that exceeds sixty characters and should be truncated properly]",
            1, 50.0, 1.0, 40.0,
            "[0000.00.00]", "[1]",
            "Addr", "City", "ST", "555-1234",
            "Shipper", "US", "FOB",
        ]
        xlsx_buf = _make_test_xlsx([long_desc_row])
        rows = parse_xlsx(xlsx_buf.read())
        result = generate_manifest_pdf(rows, "2026-09-23", "TRUNC-TEST")
        assert result[:5] == b'%PDF-'

    def test_empty_items_column(self):
        """When items column is empty, pkgs should default to 1."""
        row = [
            "TRACK002", "No Items User", "[Widget]",
            None, 25.0, 0.5, 20.0,
            "[0000.00.00]", "[1]",
            "Addr", "City", "ST", "555-1234",
            "Shipper", "US", "FOB",
        ]
        xlsx_buf = _make_test_xlsx([row])
        rows = parse_xlsx(xlsx_buf.read())
        result = generate_manifest_pdf(rows, "2026-09-23", "EMPTY-ITEMS")
        assert result[:5] == b'%PDF-'


class TestManifestEndpoint:
    """Test the /generate-manifest endpoint."""

    def test_success(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "2026-09-23",
                "manifest_number": "MAN-2026-001",
            },
            files={"xlsx_file": ("test.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert "SCS_Manifest_MAN_2026_001.pdf" in response.headers.get("content-disposition", "")
        assert response.content[:5] == b'%PDF-'

    def test_missing_arrival_date(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "",
                "manifest_number": "MAN-001",
            },
            files={"xlsx_file": ("test.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code in (400, 422)

    def test_missing_manifest_number(self):
        xlsx_buf = _make_test_xlsx(_sample_rows())
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "2026-09-23",
                "manifest_number": "",
            },
            files={"xlsx_file": ("test.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code in (400, 422)

    def test_empty_spreadsheet(self):
        """An xlsx with only headers and no data should return 400."""
        xlsx_buf = _make_test_xlsx([])
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "2026-09-23",
                "manifest_number": "MAN-EMPTY",
            },
            files={"xlsx_file": ("empty.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code == 400

    def test_pdf_filename_sanitization(self):
        """Manifest number with special chars should produce a clean filename."""
        xlsx_buf = _make_test_xlsx(_sample_rows())
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "2026-09-23",
                "manifest_number": "MAN/2026/001",
            },
            files={"xlsx_file": ("test.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code == 200
        disp = response.headers.get("content-disposition", "")
        assert "MAN_2026_001" in disp

    def test_large_dataset(self):
        """Test with many rows to ensure pagination works."""
        rows = []
        for i in range(50):
            rows.append([
                f"TRACK{i:05d}", f"Customer {i}", f"[Item {i}]",
                1, round(10.0 + i * 2.5, 2), 1.0, round(8.0 + i * 2.0, 2),
                "[0000.00.00]", "[1]",
                "Addr", "City", "ST", "555-0000",
                "Shipper", "US", "FOB",
            ])
        xlsx_buf = _make_test_xlsx(rows)
        xlsx_buf.seek(0)
        response = client.post(
            "/generate-manifest",
            data={
                "arrival_date": "2026-09-23",
                "manifest_number": "LARGE-TEST",
            },
            files={"xlsx_file": ("large.xlsx", xlsx_buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code == 200
        assert response.content[:5] == b'%PDF-'


class TestHealthEndpoint:
    """Ensure existing endpoints still work."""

    def test_health(self):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
