"""Tests for the customer invoice PDF generation endpoint."""
import io
import sys
import pytest

sys.path.insert(0, '.')

from fastapi.testclient import TestClient
from main import app, generate_customer_invoice_pdf


@pytest.fixture
def client():
    return TestClient(app)


class TestGenerateCustomerInvoicePDF:
    """Tests for the generate_customer_invoice_pdf function."""

    def test_returns_bytes(self):
        result = generate_customer_invoice_pdf(
            tracking_number="TEST123",
            customer_name="John Doe",
            address="123 Main St",
            telephone="1234567890",
            email="john@example.com",
            shipper_name="Amazon",
            shipper_invoice_number="INV-001",
            description="Books",
            total_order_value=100.00,
            customs_duties=50.00,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_pdf_header(self):
        result = generate_customer_invoice_pdf(
            tracking_number="TEST123",
            customer_name="John Doe",
            address="123 Main St",
            telephone="1234567890",
            email="john@example.com",
            shipper_name="Amazon",
            shipper_invoice_number="INV-001",
            description="Books",
            total_order_value=100.00,
            customs_duties=50.00,
        )
        # PDF files start with %PDF
        assert result[:4] == b'%PDF'

    def test_calculations(self):
        """Verify the calculation logic: fee=5%, total_ec, total_usd."""
        customs_duties = 116.10
        fee = customs_duties * 0.05  # 5.805
        total_ec = customs_duties + 10.00 + fee  # 131.905
        total_usd = total_ec / 2.6882  # ~49.07

        assert abs(fee - 5.805) < 0.001
        assert abs(total_ec - 131.905) < 0.001
        assert abs(total_usd - 49.07) < 0.1

    def test_zero_duties(self):
        """Test with zero customs duties."""
        result = generate_customer_invoice_pdf(
            tracking_number="ZERO001",
            customer_name="Jane Doe",
            address="456 Elm St",
            telephone="9876543210",
            email="jane@example.com",
            shipper_name="Walmart",
            shipper_invoice_number="INV-002",
            description="Shoes",
            total_order_value=25.00,
            customs_duties=0.0,
        )
        assert result[:4] == b'%PDF'
        assert len(result) > 100


class TestEndpoint:
    """Tests for the /generate-customer-invoice endpoint."""

    def test_success(self, client):
        response = client.post("/generate-customer-invoice", data={
            "tracking_number": "MLBS000000218XX",
            "customer_name": "BETHEL SHAKEIO",
            "address": "5240 NW 163RD ST, MIAMI LAKES, FL",
            "telephone": "2424370591",
            "email": "shakeioob@gmail.com",
            "shipper_name": "SHEIN",
            "shipper_invoice_number": "C26080100171761",
            "description": "MEN S T SHIRT, EARRINGS, HAT",
            "total_order_value": "43.19",
            "customs_duties": "116.10",
        })
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert 'SCS_Invoice_MLBS000000218XX.pdf' in response.headers["content-disposition"]
        assert response.content[:4] == b'%PDF'

    def test_content_disposition_filename(self, client):
        response = client.post("/generate-customer-invoice", data={
            "tracking_number": "ABC123",
            "customer_name": "Test User",
            "address": "",
            "telephone": "",
            "email": "",
            "shipper_name": "",
            "shipper_invoice_number": "",
            "description": "",
            "total_order_value": "0",
            "customs_duties": "0",
        })
        assert response.status_code == 200
        assert 'SCS_Invoice_ABC123.pdf' in response.headers["content-disposition"]

    def test_missing_required_field(self, client):
        """Missing tracking_number should return 422."""
        response = client.post("/generate-customer-invoice", data={
            "customer_name": "Test User",
        })
        assert response.status_code == 422

    def test_large_values(self, client):
        response = client.post("/generate-customer-invoice", data={
            "tracking_number": "LARGE001",
            "customer_name": "BIG SPENDER",
            "address": "1 Rich Ave, Luxury City",
            "telephone": "5551234567",
            "email": "big@spender.com",
            "shipper_name": "Luxury Brand Inc.",
            "shipper_invoice_number": "LUX-99999",
            "description": "Diamond necklace, Gold watch, Platinum ring",
            "total_order_value": "99999.99",
            "customs_duties": "50000.00",
        })
        assert response.status_code == 200
        assert response.content[:4] == b'%PDF'


