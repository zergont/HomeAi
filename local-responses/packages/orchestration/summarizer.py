# packages/orchestration/summarizer.py
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, UTC
from typing import Dict, List, Optional, Tuple, Sequence

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

# ---------------- L2 (unchanged style) ----------------
async def summarize_pair_to_l2(user_text: str, assistant_text: str, lang: str) -> str:
    s = get_settings()
    provider = get_lmstudio_provider()
    system = (
        "Суммируй диалоговую пару кратко, в 1–3 строках, тезисно, без воды." if lang == "ru" else
        "Summarize the user→assistant pair in 1–3 short lines, concise, no fluff."
    )
    user = f"User:\n{user_text}\n\nAssistant:\n{assistant_text}\n\n" + ("Ключевые числа/итоги, будь краток." if lang == "ru" else "Key numbers/outcome, be brief.")
    text, _ = await provider.generate(
        system=system,
        user=user,
        model=s.default_summary_model,
        temperature=0.2,
        max_tokens=s.SUMMARY_GEN_MAX_TOKENS,
    )
    return redact_fragment(text)

async def summarize_pairs_group_to_l2(
    pairs: List[Tuple[str, str]],
    texts: List[Tuple[str, str]],
    lang: str = "ru",
    max_tokens: Optional[int] = None,
    style: str = "bullets",
) -> str:
    """Group several user→assistant exchanges into one L2 block.
    style: 'bullets' | 'sentences'
    """
    s = get_settings()
    provider = get_lmstudio_provider()
    if style == "sentences":
        sys = (
            "Сводка нескольких диалогов. Дай 2–4 коротких предложения: факты/решения/итог." if lang.startswith("ru") else
            "Summarize several exchanges. Provide 2–4 short sentences with facts / decisions / outcome."
        )
    else:
        sys = (
            "Сводка нескольких диалогов (user→assistant). Дай 3–6 пунктов: ключевые факты, решения, числа, итог. Без рассуждений." if lang.startswith("ru") else
            "You will summarize several user→assistant exchanges. Provide 3–6 bullet points: key facts, actions, numbers, outcome. No reasoning or fluff."
        )
    lines: List[str] = []
    for i, (ut, at) in enumerate(texts, 1):
        lines.append(f"[{i}] user: {ut}\nassistant: {at}")
    user_prompt = ("Суммаризируй блок:\n" if lang.startswith("ru") else "Summarize the block:\n") + "\n\n".join(lines)
    text, _ = await provider.generate(
        system=sys,
        user=user_prompt,
        model=s.default_summary_model,
        temperature=0.2,
        max_tokens=max_tokens or s.SUMMARY_GEN_MAX_TOKENS,
    )
    return redact_fragment(text)

# ---------------- HF-34 L3 summarization ----------------
_NONEMPTY_RE = re.compile(r"[А-Яа-яA-Za-z0-9]")

def _is_meaningful(text: str) -> bool:
    if not text:
        return False
    s = get_settings()
    t = text.strip().strip("-•*•").strip()
    return len(t) >= s.L3_MIN_NONEMPTY_CHARS and bool(_NONEMPTY_RE.search(t))

def _debullet(text: str) -> str:
    lines = [ln.strip(" -*•\t") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return " ".join(lines)

async def _call_llm_summary(*, system: str, user: str, lang: str, max_tokens: int) -> str:
    s = get_settings()
    provider = get_lmstudio_provider()
    txt, _ = await provider.generate(
        system=system,
        user=user,
        model=s.default_summary_model,
        temperature=0.15,
        max_tokens=max_tokens,
    )
    return redact_fragment(txt)

async def summarize_l2_block_to_l3(l2_texts: List[str], lang: str = "ru", max_tokens: int | None = None) -> str:
    """Condense 4–5 L2 items into one L3 micro summary (HF-34).
    Style default: short sentences (no bullets). Guarantees non-empty meaningful output via retries + fallback.
    """
    s = get_settings()
    if not l2_texts:
        return ""
    cleaned = [_debullet(t) for t in l2_texts if t and t.strip()]
    if not cleaned:
        return ""

    if s.L3_STYLE == "sentences":
        sys = "Сожми тезисы в 1–2 очень коротких предложения: общий итог и ключевой вывод. Без списков и вводных." if lang.startswith("ru") else "Compress theses into 1–2 very short sentences: overall result + key takeaway. No lists, no preface."
        user = "Тезисы:\n" + "\n".join(f"- {t}" for t in cleaned) if lang.startswith("ru") else "Theses:\n" + "\n".join(f"- {t}" for t in cleaned)
        style_hint = "Ответь двумя короткими предложениями." if lang.startswith("ru") else "Reply with two short sentences."
    else:
        sys = "Дай 1–2 пункта по сути. Без преамбулы." if lang.startswith("ru") else "Provide 1–2 bullet points capturing the essence. No preface."
        user = "Тезисы:\n" + "\n".join(f"- {t}" for t in cleaned) if lang.startswith("ru") else "Theses:\n" + "\n".join(f"- {t}" for t in cleaned)
        style_hint = "Без пустых маркеров, каждый пункт с содержанием." if lang.startswith("ru") else "No empty bullets; each bullet must have content."

    max_tok = max_tokens or s.L3_GROUP_MAX_TOKENS

    async def _call(note: str) -> str:
        return await _call_llm_summary(system=sys, user=user + "\n\n" + note, lang=lang, max_tokens=max_tok)

    out = await _call(style_hint)
    if _is_meaningful(out):
        return _debullet(out)

    for _ in range(s.L3_RETRY_ATTEMPTS):
        retry_note = (
            "Выведи ОДНУ строку: краткий итог без списков. Не добавляй маркеры или заголовки." if lang.startswith("ru") else
            "Return ONE line: concise outcome, no lists, no bullets, no headings."
        )
        out = await _call(retry_note)
        if _is_meaningful(out):
            return _debullet(out)

    # Fallback heuristic
    joined = " ".join(cleaned)[:400]
    parts = re.split(r"[.!?]\s", joined)
    fallback = ". ".join(parts[:2]).strip()
    if _is_meaningful(fallback):
        return fallback
    return cleaned[0][:200]

# Legacy wrappers kept for compatibility
async def summarize_pairs_group_to_l2_alias(pairs_ids: Sequence[tuple[str,str]],
                                            pairs_texts: Sequence[tuple[str,str]],
                                            lang: str = "ru",
                                            max_tokens: int | None = None) -> str:
    return await summarize_pairs_group_to_l2(list(pairs_ids), list(pairs_texts), lang=lang, max_tokens=max_tokens)

async def summarize_l2_block_to_l3_text(l2_texts: Sequence[str], lang: str, max_tokens: int | None = None) -> str:
    return await summarize_l2_block_to_l3(list(l2_texts), lang=lang, max_tokens=max_tokens)

# ---------------- Autosummary (thread) unchanged ----------------

def _detect_lang(messages: List[Dict[str, str]]) -> Optional[str]:
    for m in reversed(messages):
        if m.get("role") == "user":
            txt = m.get("content", "").strip()
            if not txt:
                continue
            if any("а" <= ch.lower() <= "я" for ch in txt):
                return "ru"
            return "en"
    return None


def _calc_source_hash(s: str) -> str:
    import hashlib as _h
    return _h.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _trim_to_max_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    i = cut.rfind(" ")
    if i <= 0:
        i = limit
    return cut[:i].rstrip() + "…"

async def try_autosummarize(thread_id: str, messages: List[Dict[str, str]]) -> None:
    settings = get_settings()

    with session_scope() as s:
        th = s.get(Thread, thread_id)
        if not th:
            return
    now_ts = int(datetime.now(UTC).timestamp())

    source_messages: List[Dict[str, str]] = [
        m for m in messages if m.get("role") in ("user", "assistant", "tool")
    ]
    for m in source_messages:
        m["content"] = html.escape(redact_fragment(m.get("content", "")))
    source_text = build_summary_source(thread_id, messages=source_messages)["text"]

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

    if reason is None:
        return
    if th.is_summarizing:
        return
    if th.last_summary_run_at and (now_ts - th.last_summary_run_at) < settings.summary_debounce_sec:
        return

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
