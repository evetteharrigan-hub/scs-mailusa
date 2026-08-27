import os
import pytest
from fastapi.testclient import TestClient

# Set up test credentials before importing app
os.environ["SCS_MAILUSA_USERNAME"] = "testuser"
os.environ["SCS_MAILUSA_PASSWORD"] = "testpass"

from main import app

client = TestClient(app)


def test_login_success():
    """Valid credentials return 200 with success=true and token."""
    res = client.post("/login", data={"username": "testuser", "password": "testpass"})
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["token"] == "scs-mailusa-session"


def test_login_wrong_password():
    """Wrong password returns 401 with success=false."""
    res = client.post("/login", data={"username": "testuser", "password": "wrongpass"})
    assert res.status_code == 401
    body = res.json()
    assert body["success"] is False
    assert "message" in body


def test_login_wrong_username():
    """Wrong username returns 401."""
    res = client.post("/login", data={"username": "baduser", "password": "testpass"})
    assert res.status_code == 401
    body = res.json()
    assert body["success"] is False


def test_login_empty_credentials():
    """Empty credentials result in rejection (401 or 422 depending on validation)."""
    res = client.post("/login", data={"username": "", "password": ""})
    assert res.status_code in (401, 422)  # rejected either way — cannot authenticate


def test_login_wrong_both():
    """Both username and password wrong returns 401."""
    res = client.post("/login", data={"username": "nobody", "password": "nothing"})
    assert res.status_code == 401
    body = res.json()
    assert body["success"] is False
    assert body["message"] == "Invalid credentials"


def test_existing_generate_waybills_still_accessible():
    """The /generate-waybills endpoint still exists (no auth guard added)."""
    # Just verify a POST without files gives a 422 (validation error), not 401/404
    res = client.post("/generate-waybills")
    assert res.status_code == 422  # FastAPI validation error for missing required fields


def test_existing_health_endpoint():
    """Health endpoint still works."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
