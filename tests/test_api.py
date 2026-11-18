"""Check HTTP contracts without model calls."""

from decimal import Decimal
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)


@pytest.mark.parametrize("mime,content", [("text/plain", b"text"), ("application/pdf", b"bad"), ("application/pdf", b"%PDF-" + b"x" * (10 * 1024 * 1024))])
def test_invalid_upload(mime, content):
    """Reject wrong MIME type, invalid signature, and oversized uploads."""
    assert client.post("/documents", files={"file": ("invoice.pdf", content, mime)}).status_code == 400


def test_upload_preserves_money(monkeypatch):
    """Serialize exact Decimal amounts as strings."""
    monkeypatch.setattr(main.run, "process_upload", MagicMock(return_value={"total": Decimal("12.34"), "status": "approved"}))
    response = client.post("/documents", files={"file": ("invoice.pdf", b"%PDF-test", "application/pdf")})
    assert response.status_code == 200
    assert response.json()["total"] == "12.34"


def test_missing_documents(monkeypatch):
    """Return 404 for both lookup and approval of an unknown UUID."""
    monkeypatch.setattr(main.db, "get_document", lambda identifier: None)
    monkeypatch.setattr(main.db, "approve_document", lambda identifier: None)
    identifier = uuid4()
    assert client.get(f"/documents/{identifier}").status_code == 404
    assert client.post(f"/documents/{identifier}/approve").status_code == 404


def test_pagination_and_filter(monkeypatch):
    """Forward valid filters and reject invalid pagination."""
    listing = MagicMock(return_value=[])
    monkeypatch.setattr(main.db, "list_documents", listing)
    assert client.get("/documents?status=needs_review&limit=5&offset=2").json() == []
    listing.assert_called_once_with("needs_review", 5, 2)
    for query in ("limit=101", "offset=-1", "status=unknown"):
        assert client.get("/documents?" + query).status_code == 422


def test_approval_and_health(monkeypatch):
    """Expose approval results and report unhealthy dependencies."""
    monkeypatch.setattr(main.db, "approve_document", lambda identifier: {"status": "approved"})
    assert client.post(f"/documents/{uuid4()}/approve").json()["status"] == "approved"
    monkeypatch.setattr(main.db, "health", lambda: {"database": False, "model_api_key_configured": True})
    assert client.get("/health").status_code == 503
