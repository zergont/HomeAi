import os
import time, hashlib, json
import logging
import httpx
try:
    import lmstudio as lms  # still used for text fallback
except Exception:  # noqa: BLE001
    lms = None

from packages.utils.tokens import approx_tokens, approx_tokens_messages

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234")
log = logging.getLogger(__name__)

_cache: dict[str, dict] = {}


def _key(model: str, payload: dict) -> str:
    s = json.dumps({"model": model, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# --- New HF-30 HTTP-based chat token counting ---

def count_tokens_chat(model_id: str, messages: list[dict], timeout: float = 3.0, use_cache: bool = True) -> tuple[int, str]:
    """Return (prompt_tokens, mode).
    mode = 'proxy-http' when usage.prompt_tokens received from LM Studio HTTP; 'approx' on fallback.
    Caching keeps only numeric value; mode recalculated (proxy-http if cache hit originally proxy-http else approx).
    """
    k = _key(model_id, {"messages": messages})
    now = time.time()
    if use_cache and k in _cache and now - _cache[k]["t"] < 60:  # short cache to keep fresher accounting
        entry = _cache[k]
        return entry["n"], entry.get("mode", "proxy-http")
    try:
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": False,
            "max_tokens": 1,
            "temperature": 0,
        }
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{LMSTUDIO_BASE_URL}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        usage = data.get("usage") or {}
        n = int(usage.get("prompt_tokens", 0))
        if n > 0:
            _cache[k] = {"n": n, "t": now, "mode": "proxy-http"}
            return n, "proxy-http"
        raise RuntimeError("no usage.prompt_tokens")
    except Exception as e:  # noqa: BLE001
        log.warning("LMStudio HTTP tokenization failed (approx): %s", e)
        n = approx_tokens_messages(messages)
        _cache[k] = {"n": n, "t": now, "mode": "approx"}
        return n, "approx"


# Text path keeps SDK (optional) - still returns int only for legacy callers

def _get_model(model_id: str):
    if lms is None:
        raise RuntimeError("LM Studio SDK not available; install 'lmstudio'")
    try:
        client = getattr(lms, "Client", None)
        if client is not None:
            cl = client(base_url=LMSTUDIO_BASE_URL)
            return cl.llm(model_id)
    except Exception as e:  # noqa: BLE001
        log.warning("LMStudio Client base_url failed, falling back: %s", e)
    os.environ["LMSTUDIO_BASE_URL"] = LMSTUDIO_BASE_URL
    return lms.llm(model_id)


def count_tokens_text(model_id: str, text: str, ttl: int = 300) -> int:
    k = _key(model_id, {"text": text})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < ttl:
        return _cache[k]["n"]
    try:
        m = _get_model(model_id)
        n = len(m.tokenize(text))
    except Exception as e:  # noqa: BLE001
        log.warning("LMStudio tokenization failed (using approx): %s", e)
        n = approx_tokens(text)
    _cache[k] = {"n": n, "t": now, "mode": "sdk"}
    return n
