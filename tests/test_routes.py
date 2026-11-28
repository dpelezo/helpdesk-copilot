import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

pytestmark = pytest.mark.usefixtures("database")

VALID_KEY = "Bearer test-key-1"
HEADERS = {"Authorization": VALID_KEY}


@pytest.mark.asyncio
async def test_get_drafts_returns_list():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/kb-drafts", headers=HEADERS)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_get_kb_returns_list():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/kb", headers=HEADERS)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_get_single_draft_not_found():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb-drafts/00000000-0000-0000-0000-000000000000",
            headers=HEADERS,
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_single_kb_not_found():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb/00000000-0000-0000-0000-000000000000",
            headers=HEADERS,
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_review_invalid_action():
    """Review endpoint should reject invalid actions."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/kb-drafts/00000000-0000-0000-0000-000000000000/review",
            headers=HEADERS,
            json={"action": "invalid_action"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_review_regenerate_requires_feedback():
    """Regenerate action must include feedback."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/kb-drafts/00000000-0000-0000-0000-000000000000/review",
            headers=HEADERS,
            json={"action": "regenerate"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_ticket_invalid_body():
    """POST ticket with missing fields should return 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/tickets/resolve",
            headers=HEADERS,
            json={"ticket_id": "TKT-TEST"},  # missing required fields
        )
    assert response.status_code == 422
