# packages/orchestration/context_manager.py
from __future__ import annotations

from typing import Any, Dict, List

from packages.core.settings import get_settings
from packages.storage.repo import fetch_context as repo_fetch_context


def build_context(thread_id: str) -> Dict[str, Any]:
    settings = get_settings()
    budget = settings.ctx_max_input_tokens
    ctx = repo_fetch_context(thread_id, budget)
    return ctx


def build_summary_source(thread_id: str, *, messages: List[Dict[str, str]]) -> Dict[str, Any]:
    # Expects sanitized messages (user|assistant|tool). Do not include current threads.summary here.
    settings = get_settings()
    # Limit approximate source length to ~12k chars
    max_chars = int(settings.summary_max_chars * 12 // 3)  # ~3x cap vs target length
    buf: list[str] = []
    total = 0
    for m in messages[-200:]:
        part = f"{m['role']}: {m['content']}\n\n"
        if total + len(part) > max_chars:
            break
        buf.append(part)
        total += len(part)
    source_text = "".join(buf)
    return {"text": source_text}
