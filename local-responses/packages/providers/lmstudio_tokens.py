import os
import time, hashlib, json
import logging
import httpx
from typing import List, Dict, Tuple

from packages.utils.tokens import approx_tokens, approx_tokens_messages

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234").rstrip("/")
log = logging.getLogger(__name__)

_cache: dict[str, dict] = {}


def _key(model: str, payload: dict) -> str:
    s = json.dumps({"model": model, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------- HTTP Chat Token Counting (prompt usage) ---------------- #

def count_tokens_chat(model_id: str, messages: List[Dict], timeout: float = 3.0, cache_ttl: int = 60) -> Tuple[int, str]:
    """Return (prompt_tokens, mode) using LM Studio HTTP /v1/chat/completions.
    mode is 'proxy-http' on success else 'approx'.
    Caches successful values for cache_ttl seconds.
    """
    k = _key(model_id, {"messages": messages})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < cache_ttl:
        entry = _cache[k]
        return entry["n"], entry.get("mode", "proxy-http")
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": False,
        "max_tokens": 1,  # minimal generation, we only need usage.prompt_tokens
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{LMSTUDIO_BASE_URL}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage") or {}
        n = int(usage.get("prompt_tokens", 0))
        if n <= 0:
            raise RuntimeError("usage.prompt_tokens missing or zero")
        _cache[k] = {"n": n, "t": now, "mode": "proxy-http"}
        return n, "proxy-http"
    except Exception as e:  # noqa: BLE001
        log.warning("LMStudio HTTP tokenization failed (approx): %s", e)
        n = approx_tokens_messages(messages)
        _cache[k] = {"n": n, "t": now, "mode": "approx"}
        return n, "approx"


# ---------------- HTTP Text Token Counting (wrap as single user message) ---------------- #

def count_tokens_text(model_id: str, text: str, timeout: float = 3.0, cache_ttl: int = 60) -> int:
    """Count tokens for plain text by wrapping into a single user message via HTTP.
    Returns integer tokens (for backward compatibility with existing callers).
    Falls back to approx on error.
    """
    k = _key(model_id, {"text": text})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < cache_ttl:
        return _cache[k]["n"]
    messages = [{"role": "user", "content": text}]
    try:
        n, mode = count_tokens_chat(model_id, messages, timeout=timeout, cache_ttl=cache_ttl)
        # Re-cache with explicit mode for transparency
        _cache[k] = {"n": n, "t": now, "mode": mode}
        return n
    except Exception:
        n = approx_tokens(text)
        _cache[k] = {"n": n, "t": now, "mode": "approx"}
        return n


# Utility to clear cache (optional)

def clear_token_cache():
    _cache.clear()
