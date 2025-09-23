from __future__ import annotations

import importlib
import os
import pytest
import respx
from httpx import Response, AsyncClient, ASGITransport

@pytest.mark.asyncio
@respx.mock
async def test_responses_includes_context_assembly():
    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
    os.environ["LMSTUDIO_BASE_URL"] = base_url

    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    # Mock LM Studio
    route = respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        })
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {"model": "lm:qwen/qwen3-14b", "input": "hi", "max_output_tokens": 128}
        r = await ac.post("/responses", json=payload)

    assert route.called
    assert r.status_code == 200
    d = r.json()
    stats = d.get('metadata', {}).get('context_assembly')
    assert stats is not None
    assert 'tokens' in stats and 'caps' in stats and 'budget' in stats
