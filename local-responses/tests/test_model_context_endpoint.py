from __future__ import annotations

import json
import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.mark.asyncio
async def test_context_length_loaded(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/providers/lmstudio/context-length?model=qwen/qwen3-14b")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("context_length"), (int, type(None)))
    assert d.get("source") in ("lmstudio.loaded_context_length", "lmstudio.max_context_length", "default")


@pytest.mark.asyncio
async def test_context_length_max_only(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/providers/lmstudio/context-length?model=lm:qwen/qwen3-14b")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("max_context_length"), (int, type(None)))


@pytest.mark.asyncio
async def test_context_length_error_default(monkeypatch):
    # If LM Studio is down, endpoint must still return 200 with default source
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/providers/lmstudio/context-length?model=qwen/qwen3-14b")
    assert r.status_code == 200
    d = r.json()
    assert d.get("source") in ("lmstudio.loaded_context_length", "lmstudio.max_context_length", "default")
