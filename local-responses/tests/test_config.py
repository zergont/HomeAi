# tests/test_config.py
from __future__ import annotations

from httpx import AsyncClient, ASGITransport
from apps.api.main import app


async def test_config_safe_fields() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert set(["app_name", "env", "db_dialect", "log_level", "providers"]).issubset(data.keys())
    assert "db_url" not in data
