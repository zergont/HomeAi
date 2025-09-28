# apps/api/main.py (excerpt with modifications for HF-33 integration)
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
from packages.orchestration.context_builder import assemble_context
from packages.orchestration.summarizer import try_autosummarize
from packages.storage.repo import (
    append_message,
    create_thread,
    fetch_context as repo_fetch_context,
    save_response,
    session_scope,
    get_profile as repo_get_profile,
    save_profile as repo_save_profile,
    get_thread_messages_for_l1,
    get_l2_for_thread,
    get_l3_for_thread,
)
from packages.storage.models import Message, Thread
from packages.utils.tokens import approx_tokens, profile_text_view, approx_tokens_messages
from packages.orchestration.memory_manager import update_memory
from packages.orchestration.stream_handlers import ToolCallAssembler
from packages.orchestration.after_reply import normalize_after_reply

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

@app.get("/threads/{thread_id}/memory")
async def get_thread_memory(thread_id: str) -> JSONResponse:
    # Ensure thread exists
    with session_scope() as s:
        t = s.get(Thread, thread_id)
        if not t:
            raise HTTPException(status_code=404, detail="thread not found")
    # Helper to normalize created_at (can be datetime or int epoch or None)
    def _norm_created(v):
        if v is None:
            return None
        try:
            # datetime instance
            if hasattr(v, 'isoformat'):
                return v.isoformat()
            # int/float epoch
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v, timezone.utc).isoformat()
            return str(v)
        except Exception:
            return None
    # Build L1 pairs (oldest -> newest)
    msgs = get_thread_messages_for_l1(thread_id, exclude_message_id=None, max_items=2000)
    l1_pairs = []
    last_u = None
    for m in msgs:  # ASC
        if m.role == 'user':
            last_u = m
        elif m.role == 'assistant' and last_u is not None:
            l1_pairs.append({
                'u_id': last_u.id,
                'u_text': last_u.content,
                'a_id': m.id,
                'a_text': m.content,
            })
            last_u = None
    l2_records = get_l2_for_thread(thread_id, limit=500)
    l3_records = get_l3_for_thread(thread_id, limit=200)
    data = {
        'thread_id': thread_id,
        'l1_pairs': l1_pairs,
        'l2': [{
            'id': r.id,
            'u': r.start_message_id,
            'a': r.end_message_id,
            'text': r.text,
            'tokens': r.tokens,
            'created_at': _norm_created(getattr(r, 'created_at', None)),
        } for r in l2_records],
        'l3': [{
            'id': r.id,
            'start_l2_id': r.start_l2_id,
            'end_l2_id': r.end_l2_id,
            'text': r.text,
            'tokens': r.tokens,
            'created_at': _norm_created(getattr(r, 'created_at', None)),
        } for r in l3_records],
    }
    return JSONResponse(content=data)

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

# === RESTORED Pydantic models for /responses (were removed inadvertently causing HTTP 422) ===
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

@app.post("/providers/lmstudio/tokenize")
async def tokenize(req: TokenizeReq):
    if settings.TOKEN_COUNT_MODE != "proxy":
        return {"error": "TOKEN_COUNT_MODE is not 'proxy'"}
    if req.messages:
        try:
            n, mode = lmstudio_tokens.count_tokens_chat(req.model, req.messages)
        except Exception:
            n = approx_tokens_messages(req.messages)
            mode = "approx"
        return {"mode": mode, "prompt_tokens": n}
    if req.text is not None:
        n = lmstudio_tokens.count_tokens_text(req.model, req.text)
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
        current_user_id=current_user_id,
    )
    system_text = assembled["system_text"]
    messages_for_provider = assembled.get("provider_messages") or ([{"role": "system", "content": system_text}] + assembled["messages"] + [{"role": "user", "content": req.input}])

    # Preflight tokens with compactor-aware free_out_cap
    try:
        prompt_tok_tuple = lmstudio_tokens.count_tokens_chat(provider_model, messages_for_provider)
        if isinstance(prompt_tok_tuple, tuple):
            prompt_tok, token_mode = prompt_tok_tuple
        else:
            prompt_tok, token_mode = int(prompt_tok_tuple), "proxy-http"
    except Exception:
        prompt_tok = approx_tokens_messages(messages_for_provider)
        token_mode = "approx"

    context_budget = assembled["context_budget"]
    stats = assembled.get("stats", {})
    C_eff = int(context_budget.get("C_eff") or 0)
    R_sys = int(context_budget.get("R_sys") or 0)
    Safety = int(context_budget.get("Safety") or 0)
    free_out_cap = int(stats.get("free_out_cap") if stats.get("free_out_cap") is not None else max(0, C_eff - int(prompt_tok) - R_sys - Safety))
    requested = req.max_output_tokens or settings.R_OUT_MIN
    effective = max(settings.R_OUT_FLOOR, min(requested, free_out_cap))
    context_budget["effective_max_output_tokens"] = effective

    # metadata base
    metadata = {"context_budget": context_budget, "context_assembly": stats}
    metadata["context_assembly"]["token_count_mode"] = token_mode
    metadata["context_assembly"]["prompt_tokens_precise"] = int(prompt_tok)

    provider_request = {
        "url": f"{provider_info['base_url'].rstrip('/')}/v1/chat/completions",
        "payload": {
            "model": provider_model,
            "messages": messages_for_provider,
            "temperature": req.temperature,
            "max_tokens": effective,
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
                max_tokens=effective,
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
            if status == 404:
                msg = (
                    "Upstream returned 404. Ensure LM Studio is running, the base URL is correct, "
                    "and the selected model is loaded and supports /v1/chat/completions."
                )
                raise HTTPException(status_code=424, detail=msg)
            detail_text = None
            try:
                detail_text = e.response.text
            except Exception:
                detail_text = str(e)
            raise HTTPException(status_code=502, detail=f"LM Studio error {status}: {detail_text}")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to reach LM Studio: {e}")
        usage_vals = usage_dict or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        # Persist assistant message first
        assistant_msg = append_message(thread_id, "assistant", text, tokens=usage_vals)
        # HF-31B post-reply normalization (eager cascade) — recompute L1/L2/L3 & compaction
        try:
            caps = (metadata.get("context_assembly", {}).get("caps")
                    or assembled.get("stats", {}).get("caps")
                    or {"l1": 0, "l2": 0, "l3": 0})
            # Extract numeric caps if keys are named differently
            norm_caps = {
                "l1": caps.get("l1") or caps.get("L1") or 0,
                "l2": caps.get("l2") or caps.get("L2") or 0,
                "l3": caps.get("l3") or caps.get("L3") or 0,
            }
            system_text_for_norm = assembled.get("system_text") or system_text
            norm_result = await normalize_after_reply(
                model_id=provider_model,
                thread_id=thread_id,
                system_msg={"role": "system", "content": system_text_for_norm} if system_text_for_norm else None,
                lang="ru",
                caps=norm_caps,
                meta=metadata,
            )
            asm = metadata.setdefault("context_assembly", {})
            asm["compaction_steps"] = (asm.get("compaction_steps") or []) + norm_result.get("compaction_steps", [])
            sc = asm.setdefault("summary_counters", {"l1_to_l2": 0, "l2_to_l3": 0})
            sc["l1_to_l2"] += norm_result.get("summary_counters", {}).get("l1_to_l2", 0)
            sc["l2_to_l3"] += norm_result.get("summary_counters", {}).get("l2_to_l3", 0)
            # refresh includes after normalization
            from packages.storage import repo as repo_mod
            st_inst = get_settings()
            l2_records_norm = repo_mod.get_l2_for_thread(thread_id, limit=getattr(st_inst, 'L2_FETCH_LIMIT', 500))
            l3_records_norm = repo_mod.get_l3_for_thread(thread_id, limit=getattr(st_inst, 'L3_FETCH_LIMIT', 200))
            inc = asm.setdefault("includes", {})
            inc["l2_pairs"] = [{"id": r.id, "u": r.start_message_id, "a": r.end_message_id} for r in l2_records_norm]
            inc["l3_ids"] = [r.id for r in l3_records_norm]
        except Exception:
            pass
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
            metadata=meta_common | {"provider_request": provider_request, "tool_calls": []} | (req.metadata or {}),
        )
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
            # Replace undefined 'ctx' reference with direct constructed messages list
            await try_autosummarize(thread_id, [{"role": "user", "content": req.input}, {"role": "assistant", "content": text}])
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
            # Initial meta
            meta_payload = {"id": resp_id, "created": created, "model": provider_model, "provider": provider_info, "status": "in_progress", "metadata": {"thread_id": thread_id, "context_budget": metadata["context_budget"], "context_assembly": metadata["context_assembly"], "provider_request": provider_request}}
            await send_sse_meta(queue, meta_payload)
            # Batched meta.update with summary counters (optional duplicate for live UI refresh)
            await queue.put(await _sse_format("meta.update", {"summary_counters": metadata["context_assembly"].get("summary_counters", {}), "includes": metadata["context_assembly"].get("includes", {})}))
            try:
                log_lm.info("lmstudio.agenerate_stream start: model_id=%s", provider_model)
                async for frag in provider.agenerate_stream(
                    system=None,
                    user="",
                    model=provider_model,
                    temperature=req.temperature,
                    max_tokens=effective,
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
            # Post-reply normalization HF-31B before final meta events
            try:
                caps = metadata.get("context_assembly", {}).get("caps") or assembled.get("stats", {}).get("caps") or {"l1":0,"l2":0,"l3":0}
                norm_caps = {"l1": caps.get("l1") or 0, "l2": caps.get("l2") or 0, "l3": caps.get("l3") or 0}
                system_text_for_norm = assembled.get("system_text") or system_text
                norm_result = await normalize_after_reply(
                    model_id=provider_model,
                    thread_id=thread_id,
                    system_msg={"role": "system", "content": system_text_for_norm} if system_text_for_norm else None,
                    lang="ru",
                    caps=norm_caps,
                    meta=metadata,
                )
                asm = metadata.setdefault("context_assembly", {})
                asm["compaction_steps"] = (asm.get("compaction_steps") or []) + norm_result.get("compaction_steps", [])
                sc = asm.setdefault("summary_counters", {"l1_to_l2": 0, "l2_to_l3": 0})
                sc["l1_to_l2"] += norm_result.get("summary_counters", {}).get("l1_to_l2", 0)
                sc["l2_to_l3"] += norm_result.get("summary_counters", {}).get("l2_to_l3", 0)
                from packages.storage import repo as repo_mod
                st_inst = get_settings()
                l2_records_norm = repo_mod.get_l2_for_thread(thread_id, limit=getattr(st_inst, 'L2_FETCH_LIMIT', 500))
                l3_records_norm = repo_mod.get_l3_for_thread(thread_id, limit=getattr(st_inst, 'L3_FETCH_LIMIT', 200))
                inc = asm.setdefault("includes", {})
                inc["l2_pairs"] = [{"id": r.id, "u": r.start_message_id, "a": r.end_message_id} for r in l2_records_norm]
                inc["l3_ids"] = [r.id for r in l3_records_norm]
                await queue.put(await _sse_format("meta.update", {"summary_counters": sc, "includes": inc, "compaction_steps": asm.get("compaction_steps", [])}))
            except Exception:
                pass
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
                meta2 = {"summary_scheduled": False, "summary_reason": "stream"}
                await queue.put(await _sse_format("meta.update", meta2))
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
