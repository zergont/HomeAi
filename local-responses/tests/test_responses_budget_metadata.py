from __future__ import annotations

import json
import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.mark.asyncio
async def test_responses_budget_capped_max_tokens(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {"model": "lm:qwen/qwen3-14b", "input": "Привет", "max_output_tokens": 4000}
        r = await ac.post("/responses", json=payload)
    assert r.status_code in (200, 424, 502)
    if r.status_code == 200:
        d = r.json()
        cb = d.get('metadata', {}).get('context_budget')
        assert cb is not None
        assert 'R_out' in cb
        assert 'effective_max_output_tokens' in cb
        assert 'core_sys_pad' in cb
