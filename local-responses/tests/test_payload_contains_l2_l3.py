from __future__ import annotations
import os, importlib
import pytest, respx
from httpx import Response, AsyncClient, ASGITransport

@pytest.mark.asyncio
@respx.mock
async def test_payload_contains_l2_l3_blocks():
    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234")
    os.environ["LMSTUDIO_BASE_URL"] = base_url

    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()

    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    # Mock LM Studio
    respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35},
        })
    )

    from packages.storage.repo import create_thread, append_message, insert_l2_summary, insert_l3_summary
    import time
    th = create_thread(title=None)
    # Add some baseline history (2 pairs)
    append_message(th.id, "user", "hello")
    append_message(th.id, "assistant", "world")
    append_message(th.id, "user", "how are you?")
    append_message(th.id, "assistant", "fine")

    now = int(time.time())
    # Manually insert L2 and L3 summaries
    l2 = insert_l2_summary(th.id, start_msg_id="uX", end_msg_id="aX", text="pair summary", now=now)
    insert_l3_summary(th.id, l2_ids=[l2.id], text="block summary", now=now)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/responses", json={"model":"lm:qwen/qwen3-14b","input":"ask","max_output_tokens":32,"thread_id":th.id})
    assert r.status_code == 200
    meta = r.json().get("metadata", {})
    asm = meta.get("context_assembly", {})
    inc = asm.get("includes", {})
    # Expect L3 and L2 presence in includes
    assert inc.get("l3_ids"), "Expected l3_ids in includes"
    assert inc.get("l2_pairs"), "Expected l2_pairs in includes"
