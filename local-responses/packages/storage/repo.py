# packages/storage/repo.py
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, UTC
from decimal import Decimal
from typing import Any, Dict, Optional, List, Tuple
import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.core.settings import get_settings
from packages.storage.models import Base, Message, Response, Thread, Profile, MemoryState, L2Summary, L3MicroSummary, ToolRun
from packages.utils.tokens import approx_tokens
from packages.orchestration.redactor import redact_fragment


settings = get_settings()
engine = create_engine(settings.db_url, echo=False, future=True)
Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Session:
    with Session(engine, future=True, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def create_thread(title: Optional[str] = None) -> Thread:
    tid = uuid.uuid4().hex
    th = Thread(id=tid, title=title)
    with session_scope() as s:
        s.add(th)
    return th


def get_thread(thread_id: str) -> Optional[Thread]:
    with session_scope() as s:
        return s.get(Thread, thread_id)


def set_thread_summarizing(thread_id: str, flag: bool) -> None:
    with session_scope() as s:
        th = s.get(Thread, thread_id)
        if th:
            th.is_summarizing = flag
            th.last_summary_run_at = int(datetime.now(UTC).timestamp()) if flag else th.last_summary_run_at
            s.add(th)


def save_thread_summary(
    *, thread_id: str, summary: str, lang: Optional[str], quality: str, source_hash: Optional[str],
) -> None:
    with session_scope() as s:
        th = s.get(Thread, thread_id)
        if not th:
            return
        th.summary = summary
        th.summary_updated_at = datetime.now(UTC)
        th.summary_lang = lang
        th.summary_quality = quality
        th.summary_source_hash = source_hash
        th.is_summarizing = False
        s.add(th)


def append_message(
    thread_id: str,
    role: str,
    content: str,
    tokens: Optional[Dict[str, int]] = None,
    finish_reason: Optional[str] = None,
    attempt: Optional[int] = None,
) -> Message:
    # Не сохранять первую попытку assistant-ответа при finish_reason:'length' (ретрай)
    if role == "assistant" and finish_reason == "length" and (attempt is None or attempt == 1):
        return None
    mid = uuid.uuid4().hex
    msg = Message(
        id=mid,
        thread_id=thread_id,
        role=role,
        content=content,
    )
    if tokens:
        msg.input_tokens = tokens.get("input_tokens")
        msg.output_tokens = tokens.get("output_tokens")
        total = tokens.get("total_tokens")
        if total is None:
            total = (msg.input_tokens or 0) + (msg.output_tokens or 0)
        msg.total_tokens = total
    else:
        msg.total_tokens = approx_tokens(content)
    with session_scope() as s:
        s.add(msg)
    return msg


def save_response(
    *,
    resp_id: str,
    thread_id: str,
    request_json: str,
    response_json: str,
    status: str,
    model: str,
    provider_name: str,
    provider_base_url: Optional[str],
    usage: Dict[str, int],
    cost: Decimal,
) -> Response:
    record = Response(
        id=resp_id,
        thread_id=thread_id,
        request_json=request_json,
        response_json=response_json,
        status=status,
        model=model,
        provider_name=provider_name,
        provider_base_url=provider_base_url,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        cost=cost,
    )
    with session_scope() as s:
        s.add(record)
    return record


def update_thread_summary(thread_id: str, summary_text: str) -> None:
    # Legacy helper: kept for compatibility
    save_thread_summary(
        thread_id=thread_id,
        summary=summary_text,
        lang=None,
        quality="ok",
        source_hash=None,
    )


def fetch_context(thread_id: str, budget_tokens: int) -> Dict[str, Any]:
    # System summary + latest messages (user/assistant) up to budget
    with session_scope() as s:
        th = s.get(Thread, thread_id)
        system = th.summary if th and th.summary else "You are a helpful assistant."
        q = (
            s.query(Message)
            .filter(Message.thread_id == thread_id)
            .order_by(Message.created_at.asc())
        )
        items = [m for m in q if m.role in ("user", "assistant", "tool")]
        total = approx_tokens(system)
        kept: list[Dict[str, str]] = []
        for m in reversed(items):
            # sanitize content for model context (strip <think>)
            sanitized = redact_fragment(m.content or "")
            t = approx_tokens(sanitized)
            if total + t > budget_tokens and kept:
                break
            total += t
            kept.insert(0, {"role": m.role, "content": sanitized})
        return {"system": system, "messages": kept}

# Profile CRUD

def get_profile() -> Profile:
    with session_scope() as s:
        row = s.get(Profile, 1)
        if row is None:
            row = Profile(id=1)
            s.add(row)
            s.flush()
            try:
                s.refresh(row)
            except Exception:
                pass
        return row


def save_profile(data: Dict[str, Any]) -> Profile:
    with session_scope() as s:
        row = s.get(Profile, 1)
        if row is None:
            row = Profile(id=1)
            s.add(row)
        # Assign allowed fields only
        fields = {
            'display_name','preferred_language','tone','timezone','region_coarse','work_hours','ui_format_prefs',
            'goals_mood','decisions_tasks','brevity','format_defaults','interests_topics','workflow_tools',
            'os','runtime','hardware_hint','source','confidence'
        }
        for k, v in data.items():
            if k in fields:
                setattr(row, k, v)
        s.add(row)
        # Ensure server-generated timestamps are loaded before returning detached instance
        s.flush()
        try:
            s.refresh(row)
        except Exception:
            pass
        return row

# Memory CRUD utilities

def get_or_create_memory_state(thread_id: str) -> MemoryState:
    with session_scope() as s:
        st = s.get(MemoryState, thread_id)
        if st is None:
            st = MemoryState(thread_id=thread_id, l1_tokens=0, l2_tokens=0, l3_tokens=0, updated_at=int(datetime.now(UTC).timestamp()))
            s.add(st)
        return st


def get_messages_since(thread_id: str, last_id: Optional[str]) -> list:
    with session_scope() as s:
        items = list(s.query(Message).filter(Message.thread_id == thread_id)
                           .order_by(Message.created_at.asc()))
        out: list = []
        if last_id is None:
            seq = items
        else:
            found = False
            seq = []
            for m in items:
                if not found:
                    if m.id == last_id:
                        found = True
                    continue
                seq.append(m)
            if not found:
                seq = items  # fallback: не нашли last_id → берём все
        for m in seq:
            if m.role in ("user", "assistant"):
                m.content = redact_fragment(m.content or "")
                out.append(m)
        return out


def insert_l2(thread_id: str, start_msg_id: str, end_msg_id: str, text: str, tokens: int) -> L2Summary:
    rec = L2Summary(thread_id=thread_id, start_message_id=start_msg_id, end_message_id=end_msg_id, text=text, tokens=tokens, created_at=int(datetime.now(UTC).timestamp()))
    with session_scope() as s:
        s.add(rec)
        s.flush()
        return rec


def insert_l3(thread_id: str, start_l2_id: int, end_l2_id: int, text: str, tokens: int) -> L3MicroSummary:
    rec = L3MicroSummary(thread_id=thread_id, start_l2_id=start_l2_id, end_l2_id=end_l2_id, text=text, tokens=tokens, created_at=int(datetime.now(UTC).timestamp()))
    with session_scope() as s:
        s.add(rec)
        s.flush()
        return rec


def trim_l3_if_over(thread_id: str, max_tokens: int) -> int:
    with session_scope() as s:
        q = s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.asc())
        items = list(q)
        total = sum(x.tokens or 0 for x in items)
        removed = 0
        while total > max_tokens and items:
            x = items.pop(0)
            total -= x.tokens or 0
            s.delete(x)
            removed += 1
        return removed


def update_memory_counters(thread_id: str, l1_tokens: int, l2_tokens: int, l3_tokens: int) -> None:
    with session_scope() as s:
        st = s.get(MemoryState, thread_id)
        if st is None:
            st = MemoryState(thread_id=thread_id)
        st.l1_tokens = l1_tokens
        st.l2_tokens = l2_tokens
        st.l3_tokens = l3_tokens
        st.updated_at = int(datetime.now(UTC).timestamp())
        s.add(st)

# Expose L2/L3 getters for context_builder if needed

def get_latest_l2(thread_id: str, limit: int = 20) -> List[L2Summary]:
    with session_scope() as s:
        return list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()).limit(limit))


def get_latest_l3(thread_id: str, limit: int = 20) -> List[L3MicroSummary]:
    with session_scope() as s:
        return list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()).limit(limit))

def get_tool_run(thread_id: str, tool_name: str, args_hash: str):
    with session_scope() as s:
        return s.query(ToolRun).filter_by(thread_id=thread_id, tool_name=tool_name, args_hash=args_hash).first()

def insert_tool_run(thread_id: str, attempt_id: str, tool_name: str, args_json: str, args_hash: str, result_text: str, status: str, created_at: int):
    with session_scope() as s:
        run = ToolRun(
            thread_id=thread_id,
            attempt_id=attempt_id,
            tool_name=tool_name,
            args_json=args_json,
            args_hash=args_hash,
            result_text=result_text,
            status=status,
            created_at=created_at,
        )
        s.add(run)
        s.commit()
        return run

# New: On-demand summarization helpers used by compaction

def ensure_l2_for_pairs(thread_id: str, pairs: List[Tuple[str, str]], lang: str, now: int) -> int:
    """pairs: list of (user_msg_id, assistant_msg_id). Create L2 if absent for each pair.
    Returns number of L2 created."""
    created = 0
    from packages.orchestration import summarizer  # local import to avoid cycles
    from packages.orchestration.redactor import sanitize_for_memory

    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    with session_scope() as s:
        for (uid, aid) in pairs:
            exists = s.query(L2Summary).filter(
                L2Summary.thread_id == thread_id,
                L2Summary.start_message_id == uid,
                L2Summary.end_message_id == aid,
            ).first()
            if exists:
                continue
            um = s.get(Message, uid); am = s.get(Message, aid)
            if not um or not am:
                continue
            u_txt = sanitize_for_memory(um.content or ""); a_txt = sanitize_for_memory(am.content or "")
            try:
                if loop and loop.is_running():
                    # nested loop: run in thread or simple fallback to naive
                    l2_text = f"- {(u_txt.strip().splitlines() or [''])[0][:200]} → {(a_txt.strip().splitlines() or [''])[0][:200]}"
                else:
                    loop = loop or asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    l2_text = loop.run_until_complete(summarizer.summarize_pair_to_l2(u_txt, a_txt, lang or "ru"))
            except Exception:
                u_short = (u_txt.strip().splitlines() or [""])[0][:200]
                a_short = (a_txt.strip().splitlines() or [""])[0][:200]
                l2_text = f"- {u_short} → {a_short}"
            rec = L2Summary(thread_id=thread_id, start_message_id=uid, end_message_id=aid, text=l2_text, tokens=approx_tokens(l2_text), created_at=now)
            s.add(rec)
            created += 1
    return created


def promote_l2_to_l3(thread_id: str, l2_ids: List[int], lang: str, now: int) -> int:
    """Create one L3 from given L2 ids (oldest-first) and delete those L2. Returns number of L3 created (0/1)."""
    if not l2_ids:
        return 0
    from packages.orchestration import summarizer  # local import

    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    with session_scope() as s:
        items = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id, L2Summary.id.in_(l2_ids)).order_by(L2Summary.id.asc()))
        if not items:
            return 0
        l2_texts = [x.text or "" for x in items]
        try:
            if loop and loop.is_running():
                l3_text = "\n".join([f"• {(t.splitlines() or [''])[0][:200]}" for t in l2_texts[:2]])
            else:
                loop = loop or asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                l3_text = loop.run_until_complete(summarizer.summarize_l2_block_to_l3(l2_texts, lang or "ru"))
        except Exception:
            bullets = [f"• {(t.splitlines() or [''])[0][:200]}" for t in l2_texts]
            l3_text = "\n".join(bullets[:2])
        start_id = items[0].id; end_id = items[-1].id
        l3 = L3MicroSummary(thread_id=thread_id, start_l2_id=start_id, end_l2_id=end_id, text=l3_text, tokens=approx_tokens(l3_text), created_at=now)
        s.add(l3)
        for x in items:
            s.delete(x)
        return 1
