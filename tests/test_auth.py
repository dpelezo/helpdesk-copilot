"""API-key auth on the app's routes. No database and no Temporal: auth fails before either."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_needs_no_key():
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
def test_review_routes_refuse_a_missing_or_wrong_key(headers):
    response = client.get("/api/v1/reviews/release-x", headers=headers)
    assert response.status_code in (401, 403)


def test_no_keys_configured_is_a_server_error(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    response = client.get("/api/v1/reviews/release-x", headers={"Authorization": "Bearer test-key-1"})
    assert response.status_code == 500


def test_the_poc_routes_are_gone():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not {p for p in paths if "kb" in p or "tickets/resolve" in p}
