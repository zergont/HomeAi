# apps/api/main.py
from __future__ import annotations

import asyncio
import json
import os
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from uuid import uuid4
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from packages.core.settings import get_settings
from packages.core.logging import configure_logging, request_logging_middleware
from packages.core.pricing import price_for
from packages.providers.lmstudio import get_lmstudio_provider
from packages.orchestration.redactor import redact_fragment
from packages.orchestration.context_manager import build_context
from packages.orchestration.summarizer import try_autosummarize
from packages.storage.repo import (
    append_message,
    create_thread,
    fetch_context as repo_fetch_context,
    save_response,
    session_scope,
)
from packages.storage.models import Message, Thread

settings = get_settings()
configure_logging(level=settings.log_level)

app = FastAPI(title=settings.app_name, version="0.0.1")
log_lm = logging.getLogger("app.lmstudio")

# CORS
# Allow specific origin for development, avoid wildcard with credentials
allow_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/response logging middleware
app.middleware("http")(request_logging_middleware)

# Optionally serve built web UI if exists
web_dist = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
if web_dist.exists():
    app.mount("/ui", StaticFiles(directory=str(web_dist), html=True), name="ui")

# In-memory active streams registry
ACTIVE_STREAMS: dict[str, dict[str, Any]] = {}


class ResponsesRequest(BaseModel):
    model: str
    input: str
    system: Optional[str] = None
    temperature: float = 0.7
    max_output_tokens: int = 512
    metadata: Optional[Dict[str, Any]] = None

    thread_id: Optional[str] = None
    create_thread: bool = False


class ResponsesOutputItem(BaseModel):
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[Dict[str, Any]]


class ResponsesUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ResponsesResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created: int
    model: str
    status: Literal["completed", "failed"]
    output: list[ResponsesOutputItem]
    usage: ResponsesUsage
    provider: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = None


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/config")
async def config() -> JSONResponse:
    db_dialect = settings.db_dialect
    safe_config = {
        "app_name": settings.app_name,
        "env": settings.app_env,
        "db_dialect": db_dialect,
        "log_level": settings.log_level,
        "providers": {
            "lmstudio": {"base_url": str(settings.lmstudio_base_url)}
            if settings.lmstudio_base_url
            else {}
        },
        "summary": {
            "trigger_tokens": settings.summary_trigger_tokens,
            "max_age_sec": settings.ctx_summary_max_age_sec,
            "max_chars": settings.summary_max_chars,
            "debounce_sec": settings.summary_debounce_sec,
            "default_model": settings.default_summary_model,
        },
    }
    return JSONResponse(content=safe_config)


@app.get("/providers/lmstudio/health")
async def lmstudio_health() -> JSONResponse:
    if not settings.lmstudio_base_url:
        return JSONResponse(status_code=200, content={"status": "error", "detail": "not configured"})
    url = str(settings.lmstudio_base_url).rstrip("/") + "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return JSONResponse(content={"status": "ok"})
    except httpx.HTTPError as e:
        return JSONResponse(status_code=200, content={"status": "error", "detail": str(e)})


@app.get("/providers/lmstudio/models")
async def lmstudio_models() -> JSONResponse:
    if not settings.lmstudio_base_url:
        return JSONResponse(status_code=200, content={"data": [], "error": "not configured"})
    url = str(settings.lmstudio_base_url).rstrip("/") + "/v1/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            return JSONResponse(content=data)
    except httpx.HTTPError as e:
        # Return empty list instead of raising to avoid crashing UI flows
        return JSONResponse(status_code=200, content={"data": [], "error": f"upstream error: {e}"})


@app.get("/providers/lmstudio/models/v0")
async def lmstudio_models_v0() -> JSONResponse:
    if not settings.lmstudio_base_url:
        return JSONResponse(status_code=200, content={"data": [], "error": "not configured"})
    url = str(settings.lmstudio_base_url).rstrip("/") + "/api/v0/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            return JSONResponse(content=data)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=200, content={"data": [], "error": f"upstream error: {e}"})


@app.get("/providers/lmstudio/context-length")
async def lmstudio_context_length(model: str) -> JSONResponse:
    if not settings.lmstudio_base_url:
        return JSONResponse(status_code=200, content={
            "model": model,
            "context_length": None,
            "source": None,
            "state": None,
            "max_context_length": None,
            "error": "not configured",
        })
    # strip provider prefix if present
    model_id = model.split(":", 1)[1] if model.startswith("lm:") else model

    # Try proprietary LM Studio endpoint first
    try:
        url = f"{str(settings.lmstudio_base_url).rstrip('/')}/api/v0/models/{model_id}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            return JSONResponse(content={
                "model": model_id,
                "context_length": data.get("loaded_context_length") if isinstance(data.get("loaded_context_length"), int) else None,
                "source": "api/v0",
                "state": data.get("state"),
                "max_context_length": data.get("max_context_length"),
            })
    except httpx.HTTPError:
        pass  # Fallback

    # Fallback: Try Python SDK
    parsed = urlparse(str(settings.lmstudio_base_url))
    hostport = parsed.netloc or parsed.path

    async def sdk_call() -> Optional[int]:
        try:
            import importlib

            def _run() -> Optional[int]:
                try:
                    mod = importlib.import_module("lmstudio")
                    LMStudioCls = getattr(mod, "LMStudio", None)
                    if LMStudioCls is None:
                        mod2 = importlib.import_module("lmstudio.client")
                        LMStudioCls = getattr(mod2, "LMStudio", None)
                    if LMStudioCls is None:
                        return None
                    client = LMStudioCls(host=hostport)
                    mdl = client.get_model(model_id)
                    if hasattr(mdl, "get_context_length"):
                        return int(mdl.get_context_length())
                    if hasattr(mdl, "getContextLength"):
                        return int(mdl.getContextLength())
                    return None
                except Exception:
                    return None

            return await asyncio.to_thread(_run)
        except Exception:
            return None

    ctx_len = await sdk_call()
    if isinstance(ctx_len, int):
        return JSONResponse(content={
            "model": model_id,
            "context_length": ctx_len,
            "source": "sdk",
            "state": None,
            "max_context_length": None,
        })

    # Fallback: try OpenAI-compatible /v1/models for any hints
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(str(settings.lmstudio_base_url).rstrip("/") + "/v1/models")
            r.raise_for_status()
            data = r.json()
            items = (data or {}).get("data") or []
            found = next((m for m in items if (m or {}).get("id") == model_id), None)
            if found:
                params = found.get("params") or {}
                for key in ("context_length", "max_context_length", "max_context_tokens"):
                    if isinstance(found.get(key), int):
                        return JSONResponse(content={
                            "model": model_id,
                            "context_length": int(found[key]),
                            "source": "rest",
                            "state": None,
                            "max_context_length": None,
                        })
                    if isinstance(params.get(key), int):
                        return JSONResponse(content={
                            "model": model_id,
                            "context_length": int(params[key]),
                            "source": "rest",
                            "state": None,
                            "max_context_length": None,
                        })
    except httpx.HTTPError:
        pass

    # Never raise; return null so UI can show 'unknown'
    return JSONResponse(status_code=200, content={
        "model": model_id,
        "context_length": None,
        "source": None,
        "state": None,
        "max_context_length": None,
        "error": "unavailable",
    })


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> JSONResponse:
    with session_scope() as s:
        t = s.get(Thread, thread_id)
        if not t:
            raise HTTPException(status_code=404, detail="thread not found")
        data = {
            "id": t.id,
            "summary": t.summary,
            "summary_updated_at": t.summary_updated_at.isoformat() if t.summary_updated_at else None,
            "summary_lang": t.summary_lang,
            "summary_quality": t.summary_quality,
            "summary_source_hash": t.summary_source_hash,
            "is_summarizing": bool(t.is_summarizing),
            "last_summary_run_at": t.last_summary_run_at,
        }
    return JSONResponse(content=data)


@app.post("/threads/{thread_id}/summary/rebuild")
async def rebuild_summary(thread_id: str) -> JSONResponse:
    # schedule manual rebuild if not already running and debounce allows
    with session_scope() as s:
        t = s.get(Thread, thread_id)
        if not t:
            raise HTTPException(status_code=404, detail="thread not found")
        now_ts = int(time.time())
        if t.is_summarizing:
            return JSONResponse(status_code=202, content={"scheduled": False, "reason": "already_running"})
        if t.last_summary_run_at and (now_ts - t.last_summary_run_at) < settings.summary_debounce_sec:
            return JSONResponse(status_code=202, content={"scheduled": False, "reason": "debounced"})

    async def _bg():
        # Build a basic messages list from DB (user/assistant/tool) without including current summary
        with session_scope() as s2:
            msgs = [
                {"role": m.role, "content": m.content}
                for m in s2.query(Message).filter(Message.thread_id == thread_id).order_by(Message.created_at.asc())
                if m.role in ("user", "assistant", "tool")
            ]
        await try_autosummarize(thread_id, msgs)

    asyncio.create_task(_bg())
    return JSONResponse(status_code=202, content={"scheduled": True, "reason": "manual"})


@app.get("/threads/{thread_id}/messages")
async def get_thread_messages(thread_id: str) -> JSONResponse:
    with session_scope() as s:
        t = s.get(Thread, thread_id)
        q = (
            s.query(Message)
            .filter(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
        items = [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in q
        ]
    # Build context view (summary/system + last messages within budget)
    ctx = repo_fetch_context(thread_id, settings.ctx_max_input_tokens)
    return JSONResponse(content={
        "thread_id": thread_id,
        "summary": (t.summary if t else None),
        "summary_updated_at": (t.summary_updated_at.isoformat() if t and t.summary_updated_at else None),
        "messages": items,
        "context": ctx,
    })


async def _sse_format(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _ensure_thread(req: ResponsesRequest) -> str:
    thread_id = req.thread_id
    if not thread_id and req.create_thread:
        th = create_thread(title=None)
        thread_id = th.id
    if not thread_id:
        # Create thread implicitly for both modes to persist history
        th = create_thread(title=None)
        thread_id = th.id
    return thread_id


@app.post("/responses")
async def create_response(request: Request, req: ResponsesRequest, stream: bool = False):
    model = req.model
    # Accept both formats: "lm:<id>" and plain "<id>"
    provider_model = model.split(":", 1)[1] if isinstance(model, str) and model.startswith("lm:") else model

    if not settings.lmstudio_base_url:
        raise HTTPException(status_code=503, detail="LM Studio base URL is not configured")

    provider = get_lmstudio_provider()

    # thread and context
    thread_id = _ensure_thread(req)
    ctx = build_context(thread_id)
    system_text = req.system or ctx.get("system") or "You are a helpful assistant."

    # Build full chat messages for provider memory
    messages_for_provider = [
        {"role": "system", "content": system_text},
        *ctx.get("messages", []),
        {"role": "user", "content": req.input},
    ]

    # store user message immediately
    append_message(thread_id, "user", redact_fragment(req.input))

    provider_info = {"name": "lmstudio", "base_url": str(settings.lmstudio_base_url)}
    price_1k = price_for("lmstudio", provider_model, settings.price_overrides) or settings.price_per_1k_default

    # Log resolved model id and base URL
    log_lm.info("lmstudio.request: model_id=%s base_url=%s stream=%s", provider_model, provider_info["base_url"], bool(stream))

    if not stream:
        log_lm.info("lmstudio.generate start: model_id=%s", provider_model)
        try:
            text, usage_dict = await provider.generate(
                system=system_text,
                user=req.input,
                model=provider_model,
                temperature=req.temperature,
                max_tokens=req.max_output_tokens,
                messages=messages_for_provider,
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 500
            # Provide clearer message for common 404 when model/endpoint is unavailable
            if status == 404:
                msg = (
                    "Upstream returned 404. Ensure LM Studio is running, the base URL is correct, "
                    "and the selected model is loaded and supports /v1/chat/completions."
                )
                raise HTTPException(status_code=424, detail=msg)
            # Pass through other client/server errors with context
            detail_text = None
            try:
                detail_text = e.response.text
            except Exception:
                detail_text = str(e)
            raise HTTPException(status_code=502, detail=f"LM Studio error {status}: {detail_text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to reach LM Studio: {e}")
        usage_vals = usage_dict or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        cost = Decimal(((usage_vals.get("total_tokens", 0)) / 1000) * float(price_1k)).quantize(Decimal("0.000001"))
        out_item = ResponsesOutputItem(content=[{"type": "output_text", "text": redact_fragment(text)}])
        resp = ResponsesResponse(
            id=f"resp_{uuid4().hex}",
            created=int(time.time()),
            model=provider_model,
            status="completed",
            output=[out_item],
            usage=ResponsesUsage(**usage_vals),
            provider=provider_info,
            metadata={"thread_id": thread_id} | (req.metadata or {}),
        )
        append_message(thread_id, "assistant", redact_fragment(text), tokens=usage_vals)
        save_response(
            resp_id=resp.id,
            thread_id=thread_id,
            request_json=json.dumps(req.model_dump(), ensure_ascii=False),
            response_json=json.dumps(resp.model_dump(), ensure_ascii=False),
            status=resp.status,
            model=provider_model,
            provider_name=provider_info["name"],
            provider_base_url=provider_info["base_url"],
            usage=usage_vals,
            cost=cost,
        )
        try:
            await try_autosummarize(thread_id, ctx.get("messages", []) + [{"role": "user", "content": req.input}, {"role": "assistant", "content": text}])
            summary_reason = "scheduled"  # computed inside; here just mark scheduled
        except Exception:
            summary_reason = "error"
        # Fetch summary to return immediately in metadata
        with session_scope() as s:
            t = s.get(Thread, thread_id)
            extra = {
                "summary_scheduled": bool(summary_reason == "scheduled"),
                "summary_reason": summary_reason,
            }
            if t and t.summary:
                extra |= {
                    "summary": t.summary,
                    "summary_updated_at": t.summary_updated_at.isoformat() if t.summary_updated_at else None,
                }
            resp.metadata = (resp.metadata or {}) | extra
        return JSONResponse(content=resp.model_dump())

    # stream == True
    resp_id = f"resp_{uuid4().hex}"
    created = int(time.time())
    cancel_flag = {"cancelled": False}
    ACTIVE_STREAMS[resp_id] = {"flag": cancel_flag}

    async def event_iter():
        last_delta_ts = time.monotonic()
        done_event = asyncio.Event()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        collected: list[str] = []

        async def heartbeat_loop():
            try:
                while not done_event.is_set():
                    await asyncio.sleep(10)
                    if cancel_flag["cancelled"]:
                        break
                    if time.monotonic() - last_delta_ts > 8:
                        await queue.put(
                            await _sse_format("ping", {"ts": datetime.now(timezone.utc).isoformat()})
                        )
            except asyncio.CancelledError:
                pass

        async def produce_loop():
            nonlocal last_delta_ts
            await queue.put(
                await _sse_format(
                    "meta",
                    {"id": resp_id, "created": created, "model": provider_model, "provider": provider_info, "status": "in_progress", "metadata": {"thread_id": thread_id}},
                )
            )
            try:
                log_lm.info("lmstudio.agenerate_stream start: model_id=%s", provider_model)
                async for frag in provider.agenerate_stream(
                    system=system_text,
                    user=req.input,
                    model=provider_model,
                    temperature=req.temperature,
                    max_tokens=req.max_output_tokens,
                    messages=messages_for_provider,
                ):
                    if cancel_flag["cancelled"]:
                        break
                    last_delta_ts = time.monotonic()
                    text = redact_fragment(frag)
                    collected.append(text)
                    await queue.put(
                        await _sse_format("delta", {"index": 0, "type": "output_text.delta", "text": text})
                    )
            except Exception as exc:  # noqa: BLE001
                await queue.put(await _sse_format("error", {"message": str(exc)}))
                done_event.set()
                return

            final_text = "".join(collected)
            usage_vals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

            # Persist assistant message and response and autosummarize
            append_message(thread_id, "assistant", redact_fragment(final_text), tokens=usage_vals)
            cost = Decimal(((usage_vals.get("total_tokens", 0)) / 1000) * float(price_1k)).quantize(Decimal("0.000001"))
            response_json = json.dumps(
                {
                    "id": resp_id,
                    "object": "response",
                    "created": created,
                    "model": provider_model,
                    "status": "completed",
                    "output": [
                        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": final_text}]}
                    ],
                    "usage": usage_vals,
                    "provider": provider_info,
                    "metadata": {"thread_id": thread_id},
                },
                ensure_ascii=False,
            )
            save_response(
                resp_id=resp_id,
                thread_id=thread_id,
                request_json=json.dumps(req.model_dump(), ensure_ascii=False),
                response_json=response_json,
                status="cancelled" if cancel_flag["cancelled"] else "completed",
                model=provider_model,
                provider_name=provider_info["name"],
                provider_base_url=provider_info["base_url"],
                usage=usage_vals,
                cost=cost,
            )
            try:
                await try_autosummarize(thread_id, ctx.get("messages", []) + [{"role": "user", "content": req.input}, {"role": "assistant", "content": final_text}])
                scheduled = True
                reason = "scheduled"
            except Exception:
                scheduled = False
                reason = "error"

            # Send summary event immediately if exists
            with session_scope() as s:
                t = s.get(Thread, thread_id)
                meta = {"summary_scheduled": scheduled, "summary_reason": reason}
                await queue.put(await _sse_format("meta.update", meta))
                if t and t.summary:
                    await queue.put(
                        await _sse_format(
                            "summary",
                            {
                                "summary": t.summary,
                                "summary_updated_at": t.summary_updated_at.isoformat() if t.summary_updated_at else None,
                            },
                        )
                    )

            # Now send usage and done
            await queue.put(await _sse_format("usage", usage_vals))
            await queue.put(await _sse_format("done", {"status": "cancelled" if cancel_flag["cancelled"] else "completed"}))
            done_event.set()

        hb_task = asyncio.create_task(heartbeat_loop())
        prod_task = asyncio.create_task(produce_loop())
        try:
            while True:
                if done_event.is_set() and queue.empty():
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield chunk
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            # client disconnected or server cancelled stream; ensure cleanup
            cancel_flag["cancelled"] = True
        finally:
            prod_task.cancel()
            hb_task.cancel()
            ACTIVE_STREAMS.pop(resp_id, None)

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    return StreamingResponse(event_iter(), headers=headers)


@app.post("/responses/{resp_id}/cancel")
async def cancel_response(resp_id: str) -> JSONResponse:
    entry = ACTIVE_STREAMS.get(resp_id)
    if not entry:
        raise HTTPException(status_code=404, detail="response id not found or already completed")
    entry["flag"]["cancelled"] = True
    return JSONResponse(content={"status": "cancelling"})
