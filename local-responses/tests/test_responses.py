# tests/test_responses.py
from __future__ import annotations

import os
import importlib

import pytest
import respx
from httpx import Response, AsyncClient, ASGITransport


@pytest.mark.asyncio
@respx.mock
async def test_responses_lmstudio_success(async_client_factory=None) -> None:
    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
    os.environ["LMSTUDIO_BASE_URL"] = base_url

    # Reset cached settings and reload app to pick up env
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    # Mock LM Studio endpoint
    route = respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "Привет!"}}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        })
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "model": "lm:qwen2.5-instruct",
            "input": "Скажи привет одному предложению",
            "system": "Ты лаконичный ассистент.",
            "temperature": 0.3,
            "max_output_tokens": 128,
        }
        resp = await ac.post("/responses", json=payload)

    assert route.called
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["model"] == "lm:qwen2.5-instruct"
    assert data["provider"]["name"] == "lmstudio"
    assert "output" in data and data["output"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_responses_unsupported_model_prefix() -> None:
    # Ensure app is loaded (no need for LM env here)
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "model": "gpt-4o-mini",
            "input": "hi",
        }
        resp = await ac.post("/responses", json=payload)
    assert resp.status_code == 400
