# apps/api/main.py (only relevant diffs applied earlier)

from __future__ import annotations

import asyncio
import json
import os
import time
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from uuid import uuid4
from urllib.parse import urlparse
import uuid

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
from packages.providers.lmstudio_model_info import fetch_model_info
from packages.providers import lmstudio_tokens
from packages.orchestration.redactor import redact_fragment, safe_profile_output
from packages.orchestration.context_manager import build_context
from packages.orchestration.context_builder import assemble_context
from packages.orchestration.summarizer import try_autosummarize
from packages.orchestration.budget import compute_budgets
from packages.storage.repo import (
    append_message,
    create_thread,
    fetch_context as repo_fetch_context,
    save_response,
    session_scope,
    get_profile as repo_get_profile,
    save_profile as repo_save_profile,
    get_thread_messages_for_l1,
)
from packages.storage.models import Message, Thread
from packages.utils.tokens import approx_tokens, profile_text_view, approx_tokens_messages
from packages.orchestration.memory_manager import update_memory
from packages.orchestration.tool_runtime import ToolRuntime
from packages.orchestration.stream_handlers import ToolCallAssembler
from packages.orchestration.retry_policy import make_retry_suffix, should_retry_length

# Prometheus metrics (simple counters)
try:
    from prometheus_client import Counter
    LR_CTX_OVERFLOW = Counter('lr_context_overflow_prevented_total', 'Context squeezes occurred')
    LR_CTX_TOKENS = Counter('lr_context_tokens_total', 'Context tokens total by part', ['part'])
    LR_CTX_SQUEEZES = Counter('lr_context_squeezes_total', 'Squeeze actions total', ['type'])
    LR_SUMMARY_CREATED = Counter('lr_summary_created_total', 'Summaries created by compaction', ['level'])
    LR_COMPACTION_STEPS = Counter('lr_compaction_steps_total', 'Compaction steps', ['type'])
except Exception:  # fallback dummies
    class _Dummy:
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): pass
    LR_CTX_OVERFLOW = _Dummy(); LR_CTX_TOKENS = _Dummy(); LR_CTX_SQUEEZES = _Dummy()
    LR_SUMMARY_CREATED = _Dummy(); LR_COMPACTION_STEPS = _Dummy()

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


class ProfileIn(BaseModel):
    display_name: Optional[str] = None
    preferred_language: Optional[str] = None
    tone: Optional[str] = None
    timezone: Optional[str] = None
    region_coarse: Optional[str] = None
    work_hours: Optional[str] = None
    ui_format_prefs: Optional[Any] = None
    goals_mood: Optional[str] = None
    decisions_tasks: Optional[str] = None
    brevity: Optional[str] = None
    format_defaults: Optional[Any] = None
    interests_topics: Optional[Any] = None
    workflow_tools: Optional[Any] = None
    os: Optional[str] = None
    runtime: Optional[str] = None
    hardware_hint: Optional[str] = None
    source: Optional[str] = None
    confidence: Optional[int] = None


class ProfileOut(ProfileIn):
    updated_at: Optional[str] = None
    core_tokens: int
    core_cap: int


@app.get("/profile")
async def get_profile() -> JSONResponse:
    row = repo_get_profile()
    # Build dict and normalize JSON-like fields
    data: Dict[str, Any] = {
        "display_name": row.display_name,
        "preferred_language": row.preferred_language,
        "tone": row.tone,
        "timezone": row.timezone,
        "region_coarse": row.region_coarse,
        "work_hours": row.work_hours,
        "ui_format_prefs": _maybe_json(row.ui_format_prefs),
        "goals_mood": row.goals_mood,
        "decisions_tasks": row.decisions_tasks,
        "brevity": row.brevity,
        "format_defaults": _maybe_json(row.format_defaults),
        "interests_topics": _maybe_json(row.interests_topics),
        "workflow_tools": _maybe_json(row.workflow_tools),
        "os": row.os,
        "runtime": row.runtime,
        "hardware_hint": row.hardware_hint,
        "source": row.source,
        "confidence": row.confidence,
        "updated_at": row.updated_at.isoformat() if getattr(row, 'updated_at', None) else None,
    }
    # Compute tokens
    text = profile_text_view(data)
    core_tokens = approx_tokens(text)
    core_cap = int(math.ceil(core_tokens * 1.10))
    data = safe_profile_output(data) | {"core_tokens": core_tokens, "core_cap": core_cap}
    return JSONResponse(content=data)


def _maybe_json(txt: Optional[str]) -> Any:
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return txt


def _json_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


@app.put("/profile")
async def put_profile(payload: ProfileIn) -> JSONResponse:
    # normalize values
    data = payload.model_dump()
    for k in ("ui_format_prefs", "format_defaults", "interests_topics", "workflow_tools"):
        data[k] = _json_or_none(data.get(k))
    # save
    row = repo_save_profile(data)
    # respond with normalized + tokens
    out = {
        **payload.model_dump(),
        "updated_at": row.updated_at.isoformat() if getattr(row, 'updated_at', None) else None,
    }
    text = profile_text_view(out)
    core_tokens = approx_tokens(text)
    core_cap = int(math.ceil(core_tokens * 1.10))
    out = safe_profile_output(out) | {"core_tokens": core_tokens, "core_cap": core_cap}
    return JSONResponse(content=out)


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
    # profile slice and tokens
    prof_row = repo_get_profile()
    prof_dict = {
        "display_name": prof_row.display_name,
        "preferred_language": prof_row.preferred_language,
        "tone": prof_row.tone,
        "timezone": prof_row.timezone,
        "region_coarse": prof_row.region_coarse,
        "work_hours": prof_row.work_hours,
        "ui_format_prefs": _maybe_json(prof_row.ui_format_prefs),
        "brevity": prof_row.brevity,
        "os": prof_row.os,
        "runtime": prof_row.runtime,
        "hardware_hint": prof_row.hardware_hint,
    }
    prof_text = profile_text_view(prof_dict)
    core_tokens = approx_tokens(prof_text)
    core_cap = int(math.ceil(core_tokens * 1.10))

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
        "context": {
            "CTX_CORE_SYS_PAD_TOK": settings.ctx_core_sys_pad_tok,
            "CONTEXT_MIN_CORE_SKELETON_TOK": settings.context_min_core_skeleton_tok,
        },
        "profile": safe_profile_output(prof_dict) | {"core_tokens": core_tokens, "core_cap": core_cap},
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
    settings_local = settings
    mid = model.split(":", 1)[1] if model.startswith("lm:") else model
    info = await fetch_model_info(mid)
    ttl = settings_local.ctx_model_info_ttl_sec
    if info.get("source") == "default":
        return JSONResponse(status_code=200, content={
            "model": mid,
            "context_length": settings_local.ctx_default_context_length,
            "loaded_context_length": None,
            "max_context_length": settings_local.ctx_default_context_length,
            "source": "default",
            "ttl_sec": ttl,
            "state": info.get("state"),
            "error": info.get("error"),
        })
    ctx_len = info.get("loaded_context_length") or info.get("max_context_length")
    src = "lmstudio.loaded_context_length" if info.get("loaded_context_length") else "lmstudio.max_context_length"
    return JSONResponse(content={
        "model": mid,
        "context_length": ctx_len,
        "loaded_context_length": info.get("loaded_context_length"),
        "max_context_length": info.get("max_context_length"),
        "source": src,
        "ttl_sec": ttl,
        "state": info.get("state"),
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


class TokenizeReq(BaseModel):
    model: str
    messages: Optional[list[dict]] = None
    text: Optional[str] = None


@app.post("/providers/lmstudio/tokenize")
async def tokenize(req: TokenizeReq):
    if settings.TOKEN_COUNT_MODE != "proxy":
        return {"error": "TOKEN_COUNT_MODE is not 'proxy'"}
    if req.messages:
        n = lmstudio_tokens.count_tokens_chat(req.model, req.messages, settings.TOKEN_CACHE_TTL_SEC)
        return {"mode": "chat", "prompt_tokens": n}
    if req.text is not None:
        n = lmstudio_tokens.count_tokens_text(req.model, req.text, settings.TOKEN_CACHE_TTL_SEC)
        return {"mode": "text", "prompt_tokens": n}
    return {"error": "provide messages or text"}


async def send_sse_meta(queue, payload: Dict[str, Any]):
    await queue.put(await _sse_format("meta", payload))


@app.post("/responses")
async def create_response(request: Request, req: ResponsesRequest, stream: bool = False):
    model = req.model
    provider_model = model.split(":", 1)[1] if isinstance(model, str) and model.startswith("lm:") else model

    if not settings.lmstudio_base_url:
        raise HTTPException(status_code=503, detail="LM Studio base URL is not configured")

    provider = get_lmstudio_provider()
    provider_info = {"name": "lmstudio", "base_url": str(settings.lmstudio_base_url)}

    # Define price per 1k tokens once for both modes
    price_1k = price_for("lmstudio", provider_model, settings.price_overrides) or settings.price_per_1k_default

    thread_id = _ensure_thread(req)

    # Save user message early for pairing and UI echo
    saved_user = append_message(thread_id, "user", req.input)
    current_user_id = saved_user.id if saved_user else None

    # streaming early echo will be handled in stream branch

    assembled = await assemble_context(
        thread_id=thread_id,
        model_id=model,
        max_output_tokens=req.max_output_tokens,
        tool_results_text=None,
        tool_results_tokens=0,
        last_user_lang=None,
        current_user_text=req.input,
    )
    system_text = assembled["system_text"]
    messages_for_provider = ([{"role": "system", "content": system_text}] + assembled["messages"] + [{"role": "user", "content": req.input}])

    # Preflight tokens with silence protection
    try:
        prompt_tok = lmstudio_tokens.count_tokens_chat(provider_model, messages_for_provider, settings.TOKEN_CACHE_TTL_SEC)
        token_mode = "proxy"
    except Exception:
        prompt_tok = approx_tokens_messages(messages_for_provider)
        token_mode = "approx"
    C_eff = int(assembled["context_budget"].get("C_eff") or 0)
    R_sys = int(assembled["context_budget"].get("R_sys") or 0)
    Safety = int(assembled["context_budget"].get("Safety") or 0)
    free_out = max(0, C_eff - int(prompt_tok) - R_sys - Safety)
    requested_mt = req.max_output_tokens or int(assembled["context_budget"].get("effective_max_output_tokens") or 0) or getattr(settings, 'CTX_ROUT_DEFAULT', 512)
    eff_out = int(min(requested_mt, free_out))
    assembled["context_budget"]["effective_max_output_tokens"] = eff_out

    # metadata base
    metadata = {"context_budget": assembled.get("context_budget"), "context_assembly": assembled.get("stats", {})}
    metadata["context_assembly"]["l1_pairs_count"] = assembled.get("stats", {}).get("l1_pairs_count", 0)
    metadata["context_assembly"]["token_count_mode"] = token_mode
    metadata["context_assembly"]["prompt_tokens_precise"] = int(prompt_tok)
    metadata["context_assembly"]["free_out_cap"] = int(free_out)

    provider_request = {
        "url": f"{provider_info['base_url'].rstrip('/')}/v1/chat/completions",
        "payload": {
            "model": provider_model,
            "messages": messages_for_provider,
            "temperature": req.temperature,
            "max_tokens": eff_out,
            **({"stream": True} if stream else {}),
        },
    }

    # Non-stream: return final metadata with preflight info
    if not stream:
        log_lm.info("lmstudio.generate start: model_id=%s", provider_model)
        try:
            text, usage_dict = await provider.generate(
                system=None,
                user="",
                model=provider_model,
                temperature=req.temperature,
                max_tokens=eff_out,
                messages=messages_for_provider,
            )
            tool_calls = []
            assembler = ToolCallAssembler()
            for call in assembler.feed(text):
                if call and isinstance(call, dict) and "name" in call and "arguments" in call:
                    result = tool_runtime.try_execute(call["name"], call["arguments"])
                    tool_calls.append({"call": call, "result": result})
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
        out_item = ResponsesOutputItem(content=[{"type": "output_text", "text": text}])
        meta_common = {"thread_id": thread_id, "context_budget": metadata["context_budget"], "context_assembly": metadata["context_assembly"]}
        resp = ResponsesResponse(
            id=f"resp_{uuid4().hex}",
            created=int(time.time()),
            model=provider_model,
            status="completed",
            output=[out_item],
            usage=ResponsesUsage(**usage_vals),
            provider=provider_info,
            metadata=meta_common | {"provider_request": provider_request, "tool_calls": tool_calls} | (req.metadata or {}),
        )
        append_message(thread_id, "assistant", text, tokens=usage_vals)
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
            tool_results_tokens = 0
            mem = await update_memory(thread_id, assembled.get("context_budget", {}), tool_results_tokens, int(time.time()))
            resp.metadata = (resp.metadata or {}) | {"memory": mem}
        except Exception:
            pass
        try:
            await try_autosummarize(thread_id, ctx.get("messages", []) + [{"role": "user", "content": req.input}, {"role": "assistant", "content": text}])
            summary_reason = "scheduled"
        except Exception:
            summary_reason = "error"
        with session_scope() as s:
            t = s.get(Thread, thread_id)
            extra = {"summary_scheduled": bool(summary_reason == "scheduled"), "summary_reason": summary_reason}
            if t and t.summary:
                extra |= {"summary": t.summary, "summary_updated_at": t.summary_updated_at.isoformat() if t.summary_updated_at else None}
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
        assembler = ToolCallAssembler()

        async def heartbeat_loop():
            try:
                while not done_event.is_set():
                    await asyncio.sleep(10)
                    if cancel_flag["cancelled"]:
                        break
                    if time.monotonic() - last_delta_ts > 8:
                        await queue.put(await _sse_format("ping", {"ts": datetime.now(timezone.utc).isoformat()}))
            except asyncio.CancelledError:
                pass

        async def produce_loop():
            nonlocal last_delta_ts
            # First send user echo
            await queue.put(await _sse_format("message", {"role":"user","content": redact_fragment(req.input), "message_id": current_user_id, "thread_id": thread_id}))
            # Then meta
            meta_payload = {"id": resp_id, "created": created, "model": provider_model, "provider": provider_info, "status": "in_progress", "metadata": {"thread_id": thread_id, "context_budget": metadata["context_budget"], "context_assembly": metadata["context_assembly"], "provider_request": provider_request}}
            await send_sse_meta(queue, meta_payload)
            try:
                log_lm.info("lmstudio.agenerate_stream start: model_id=%s", provider_model)
                async for frag in provider.agenerate_stream(
                    system=None,
                    user="",
                    model=provider_model,
                    temperature=req.temperature,
                    max_tokens=eff_out,
                    messages=messages_for_provider,
                ):
                    if cancel_flag["cancelled"]:
                        break
                    last_delta_ts = time.monotonic()
                    text = frag
                    collected.append(text)
                    for call in assembler.feed(text):
                        if call and isinstance(call, dict) and "name" in call and "arguments" in call:
                            tool_runtime.try_execute(call["name"], call["arguments"])
                    await queue.put(await _sse_format("delta", {"index": 0, "type": "output_text.delta", "text": text}))
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, httpx.RequestError):
                    msg = f"Failed to reach LM Studio: {exc}"
                elif isinstance(exc, httpx.HTTPStatusError):
                    try:
                        status = exc.response.status_code if exc.response is not None else 0
                        detail_text = exc.response.text if exc.response is not None else str(exc)
                        msg = f"LM Studio error {status}: {detail_text}"
                    except Exception:
                        msg = str(exc)
                else:
                    msg = str(exc)
                await queue.put(await _sse_format("error", {"message": msg, "provider_request": provider_request}))
                done_event.set()
                return

            final_text = "".join(collected)
            usage_vals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            append_message(thread_id, "assistant", final_text, tokens=usage_vals)
            cost = Decimal(((usage_vals.get("total_tokens", 0)) / 1000) * float(price_1k)).quantize(Decimal("0.000001"))
            response_json = json.dumps({"id": resp_id, "object": "response", "created": created, "model": provider_model, "status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": final_text}]}], "usage": usage_vals, "provider": provider_info, "metadata": {"thread_id": thread_id}}, ensure_ascii=False)
            save_response(resp_id=resp_id, thread_id=thread_id, request_json=json.dumps(req.model_dump(), ensure_ascii=False), response_json=response_json, status="cancelled" if cancel_flag["cancelled"] else "completed", model=provider_model, provider_name=provider_info["name"], provider_base_url=provider_info["base_url"], usage=usage_vals, cost=cost)
            try:
                tool_results_tokens = 0
                mem = await update_memory(thread_id, assembled.get("context_budget", {}), tool_results_tokens, int(time.time()))
                await queue.put(await _sse_format("meta.update", {"memory": mem}))
            except Exception:
                pass
            with session_scope() as s:
                t = s.get(Thread, thread_id)
                meta = {"summary_scheduled": False, "summary_reason": "stream"}
                await queue.put(await _sse_format("meta.update", meta))
                if t and t.summary:
                    await queue.put(await _sse_format("summary", {"summary": t.summary, "summary_updated_at": t.summary_updated_at.isoformat() if t.summary_updated_at else None}))
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
            cancel_flag["cancelled"] = True
        finally:
            prod_task.cancel(); hb_task.cancel(); ACTIVE_STREAMS.pop(resp_id, None)

    headers = {"Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    return StreamingResponse(event_iter(), headers=headers)


@app.post("/responses/{resp_id}/cancel")
async def cancel_response(resp_id: str) -> JSONResponse:
    entry = ACTIVE_STREAMS.get(resp_id)
    if not entry:
        raise HTTPException(status_code=404, detail="response id not found or already completed")
    entry["flag"]["cancelled"] = True
    return JSONResponse(content={"status": "cancelling"})
