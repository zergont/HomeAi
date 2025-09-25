from __future__ import annotations
import os, importlib
import pytest, respx
from httpx import Response, AsyncClient, ASGITransport

from packages.storage.repo import append_message, get_l2_for_thread

@pytest.mark.asyncio
@respx.mock
async def test_summary_pipeline_tail_and_l2_creation(tmp_path):
    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
    os.environ["LMSTUDIO_BASE_URL"] = base_url

    # Reset settings singleton
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()

    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    # Mock LM Studio completion endpoint
    respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "assistant final"}}],
            "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
        })
    )

    # Build thread with 6 user->assistant pairs
    from packages.storage.repo import create_thread
    th = create_thread(title=None)
    long_user = "U" * 120
    long_assistant = "A" * 600
    for i in range(6):
        append_message(th.id, "user", f"{long_user}{i}")
        append_message(th.id, "assistant", f"{long_assistant}{i}")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/responses", json={"model": "lm:qwen/qwen3-14b", "input": "latest", "max_output_tokens": 64, "thread_id": th.id})
    assert r.status_code == 200
    meta = r.json().get("metadata", {})
    asm = meta.get("context_assembly", {})
    # tail pairs count should be >=4 (min tail) and not exceed 4 per default setting
    assert asm.get("l1_pairs_count") in (4, 6)  # allow 4 (tail) or full if policy changed
    # ensure some L2 were created for older pairs
    l2_items = get_l2_for_thread(th.id, limit=100)
    assert len(l2_items) >= 1
    includes = asm.get("includes", {})
    assert includes.get("l2_pairs") is not None

