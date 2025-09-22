# tests/test_autosummary.py
from __future__ import annotations

import importlib
import os

import pytest
import respx
from httpx import Response

from packages.orchestration.summarizer import try_autosummarize
from packages.storage.repo import create_thread, append_message


@pytest.mark.asyncio
@respx.mock
async def test_autosummary_trigger_and_save() -> None:
    os.environ["LMSTUDIO_BASE_URL"] = "http://127.0.0.1:1234"
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)

    # mock provider response
    respx.post("http://127.0.0.1:1234/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "summary text"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })
    )

    th = create_thread(None)
    # Make enough content to exceed trigger tokens
    for _ in range(40):
        append_message(th.id, "user", "x" * 50)

    await try_autosummarize(th.id, [{"role": "user", "content": "x" * 50} for _ in range(40)])

    from packages.storage.repo import session_scope
    from packages.storage.models import Thread

    with session_scope() as s:
        t = s.get(Thread, th.id)
        assert t.summary and "summary text" in t.summary
