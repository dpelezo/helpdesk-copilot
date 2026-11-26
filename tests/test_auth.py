import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_health_no_auth_needed():
    """Health endpoint should be public."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_protected_route_no_key():
    """Request with no API key should return 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/kb-drafts")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_protected_route_wrong_key():
    """Request with wrong API key should return 401."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb-drafts",
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_valid_key():
    """Request with valid API key should return 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb-drafts",
            headers={"Authorization": "Bearer test-key-1"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_second_valid_key_works():
    """Multiple API keys should all work."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb-drafts",
            headers={"Authorization": "Bearer test-key-2"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_kb_list_protected():
    """KB list endpoint should require API key."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/kb")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_kb_list_with_valid_key():
    """KB list endpoint should return 200 with valid key."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/kb",
            headers={"Authorization": "Bearer test-key-1"},
        )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_post_ticket_no_key():
    """POST ticket without key should return 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/tickets/resolve",
            json={
                "ticket_id": "TKT-TEST",
                "subject": "test",
                "description": "test",
                "conversation": [],
            },
        )
    assert response.status_code == 403
