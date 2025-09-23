from __future__ import annotations

import importlib
import os
import pytest
from httpx import ASGITransport, AsyncClient

@pytest.mark.asyncio
async def test_memory_metadata_in_responses(monkeypatch):
    # ensure app loaded
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {"model": "lm:qwen/qwen3-14b", "input": "hello", "max_output_tokens": 64}
        r = await ac.post("/responses", json=payload)
    assert r.status_code in (200, 424, 502)
    if r.status_code == 200:
        d = r.json()
        mem = d.get('metadata', {}).get('memory')
        assert mem is not None
        assert 'l1_tokens' in mem and 'caps' in mem and 'free_pct' in mem

@pytest.mark.asyncio
async def test_tools_tokens_reduce_l1_cap(monkeypatch):
    from packages.orchestration.memory_manager import compute_level_caps
    caps1 = compute_level_caps(1000, tools_tokens=0)
    caps2 = compute_level_caps(1000, tools_tokens=500)
    assert caps2['l1'] <= caps1['l1']
