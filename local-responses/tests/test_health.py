# tests/test_health.py
from __future__ import annotations

from httpx import AsyncClient, ASGITransport
from apps.api.main import app


async def test_health() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "time" in data
