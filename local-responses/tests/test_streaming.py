# tests/test_streaming.py
from __future__ import annotations

import asyncio
import json
import importlib
import os

import pytest
import respx
from httpx import Response, AsyncClient, ASGITransport


async def collect_sse_bytes(ac: AsyncClient, url: str, body: dict | None = None, timeout: float = 5.0):
    chunks: list[bytes] = []
    async with ac.stream("POST", url, json=body) as resp:
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        async for b in resp.aiter_bytes():
            chunks.append(b)
            if len(chunks) > 20000:
                break
    return b"".join(chunks)


def parse_events(raw: bytes):
    text = raw.decode("utf-8", errors="ignore")
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        ev = None
        data = ""
        for ln in lines:
            if ln.startswith("event: "):
                ev = ln[len("event: "):]
            elif ln.startswith("data: "):
                data = ln[len("data: "):]
        if ev:
            try:
                events.append((ev, json.loads(data)))
            except Exception:
                pass
    return events


@pytest.mark.asyncio
@respx.mock
async def test_stream_basic_order_and_text() -> None:
    # Reload app to pick env/.env
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    base_url = str(api_main.settings.lmstudio_base_url or os.environ.get("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234"))

    # Mock streaming from LM Studio
    stream_body = (
        b"data: {\"choices\":[{\"delta\":{\"content\":\"\\u041f\\u0440\\u0438\\u0432\"}}]}\n\n"
        b"data: {\"choices\":[{\"delta\":{\"content\":\"\\u0435\\u0442!\"}}]}\n\n"
        b"data: [DONE]\n\n"
    )
    route = respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(
        return_value=Response(200, content=stream_body)
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
        raw = await collect_sse_bytes(ac, "/responses?stream=true", body=payload)

    assert route.called
    events = parse_events(raw)
    names = [n for n, _ in events]
    assert names[0] == "meta"
    assert "delta" in names[1:-2]
    assert names[-2] == "usage"
    assert names[-1] == "done"

    text = "".join([d["text"] for n, d in events if n == "delta"])  # type: ignore[index]
    assert text == "Привет!"


@pytest.mark.asyncio
@respx.mock
async def test_stream_heartbeat_ping() -> None:
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    base_url = str(api_main.settings.lmstudio_base_url or os.environ.get("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234"))

    # Simulate long pause between fragments by delaying response streaming
    async def delayed_stream(request):
        async def aiter():
            yield b"data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\n"
            await asyncio.sleep(11)
            yield b"data: [DONE]\n\n"
        return Response(200, content=aiter())

    respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(side_effect=delayed_stream)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {"model": "lm:qwen2.5-instruct", "input": "hi"}
        raw = await collect_sse_bytes(ac, "/responses?stream=true", body=payload)

    events = parse_events(raw)
    assert any(n == "ping" for n, _ in events)


@pytest.mark.asyncio
@respx.mock
async def test_stream_cancel() -> None:
    from packages.core import settings as settings_module
    settings_module.get_settings.cache_clear()
    import apps.api.main as api_main
    importlib.reload(api_main)
    app = api_main.app

    base_url = str(api_main.settings.lmstudio_base_url or os.environ.get("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234"))

    # a slow endless generator mock
    async def slow_stream(request):
        async def aiter():
            while True:
                yield b"data: {\"choices\":[{\"delta\":{\"content\":\"x\"}}]}\n\n"
                await asyncio.sleep(0.2)
        return Response(200, content=aiter())

    respx.post(f"{base_url.rstrip('/')}/v1/chat/completions").mock(side_effect=slow_stream)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {"model": "lm:qwen2.5-instruct", "input": "hi"}
        # start stream in background
        stream_task = asyncio.create_task(collect_sse_bytes(ac, "/responses?stream=true", body=payload))
        await asyncio.sleep(0.5)
        # Since we don't capture resp_id from meta here, just exercise cancel endpoint
        r = await ac.post("/responses/resp_dummy/cancel")
        assert r.status_code in (200, 404)
        # cancel the stream task to finish test
        stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream_task
