# packages/orchestration/summarizer.py
from __future__ import annotations

import hashlib
import html
from datetime import datetime, UTC
from typing import Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.providers.lmstudio import get_lmstudio_provider
from packages.storage.repo import (
    save_thread_summary,
    session_scope,
    set_thread_summarizing,
)
from packages.storage.models import Message, Thread
from packages.utils.tokens import approx_tokens
from packages.orchestration.redactor import redact_fragment
from packages.orchestration.context_manager import build_summary_source


def _detect_lang(messages: List[Dict[str, str]]) -> Optional[str]:
    for m in reversed(messages):
        if m.get("role") == "user":
            txt = m.get("content", "").strip()
            if not txt:
                continue
            # naive: latin vs cyrillic vs other
            if any("а" <= ch.lower() <= "я" for ch in txt):
                return "ru"
            return "en"
    return None


def _calc_source_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _trim_to_max_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    # cut to last space to avoid mid-word
    i = cut.rfind(" ")
    if i <= 0:
        i = limit
    return cut[:i].rstrip() + "…"


async def try_autosummarize(thread_id: str, messages: List[Dict[str, str]]) -> None:
    settings = get_settings()

    # Load thread state
    with session_scope() as s:
        th = s.get(Thread, thread_id)
        if not th:
            return
    now_ts = int(datetime.now(UTC).timestamp())

    # Build sanitized source (exclude current summary)
    source_messages: List[Dict[str, str]] = [
        m for m in messages if m.get("role") in ("user", "assistant", "tool")
    ]
    # sanitize content
    for m in source_messages:
        m["content"] = html.escape(redact_fragment(m.get("content", "")))
    source_text = build_summary_source(thread_id, messages=source_messages)["text"]

    # Triggers
    over_tokens = approx_tokens(source_text) >= settings.summary_trigger_tokens
    age_expired = (
        (th.summary_updated_at is None) or
        ((datetime.now(UTC) - th.summary_updated_at.replace(tzinfo=UTC)).total_seconds() > settings.ctx_summary_max_age_sec)
    )
    source_hash = _calc_source_hash("|".join(
        f"{i}:{len(m.get('content',''))}:{m.get('role','')}" for i, m in enumerate(source_messages[-200:])
    ))
    source_changed = (th.summary_source_hash != source_hash)

    reason = None
    if over_tokens:
        reason = "tokens"
    elif age_expired:
        reason = "age"
    elif source_changed:
        reason = "source_hash"
    else:
        reason = None

    # Debounce and lock
    if reason is None:
        return
    if th.is_summarizing:
        return
    if th.last_summary_run_at and (now_ts - th.last_summary_run_at) < settings.summary_debounce_sec:
        return

    # Acquire lock
    set_thread_summarizing(thread_id, True)

    try:
        provider = get_lmstudio_provider()
        lang = _detect_lang(source_messages) or th.summary_lang or "en"
        system = settings.summary_system_prompt
        user = source_text
        text, _usage = await provider.generate(
            system=system,
            user=user,
            model=settings.default_summary_model,
            temperature=0.2,
            max_tokens=300,
        )
        cleaned = redact_fragment(text)
        cleaned = _trim_to_max_chars(cleaned, settings.summary_max_chars)
        quality = "ok" if cleaned.strip() else "draft"
        if not cleaned.strip():
            # fallback draft from last messages
            snippets = []
            for m in reversed(source_messages[-6:]):
                t = m.get("content", "")
                if t:
                    snippets.append(t.strip())
                if len(snippets) >= 3:
                    break
            cleaned = "\n\n".join(snippets)
            quality = "draft"
        save_thread_summary(
            thread_id=thread_id,
            summary=cleaned,
            lang=lang,
            quality=quality,
            source_hash=source_hash,
        )
    except Exception:
        # fallback to draft
        try:
            snippets = []
            for m in reversed(source_messages[-6:]):
                t = m.get("content", "")
                if t:
                    snippets.append(t.strip())
                if len(snippets) >= 3:
                    break
            cleaned = "\n\n".join(snippets)
            save_thread_summary(
                thread_id=thread_id,
                summary=cleaned,
                lang=_detect_lang(source_messages) or "en",
                quality="draft",
                source_hash=source_hash,
            )
        except Exception:
            pass
    finally:
        set_thread_summarizing(thread_id, False)
