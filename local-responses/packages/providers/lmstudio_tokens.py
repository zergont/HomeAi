import os
import time, hashlib, json
import logging
try:
    import lmstudio as lms
except Exception:  # optional import guard for test environments
    lms = None

from packages.utils.tokens import approx_tokens, approx_tokens_messages

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://192.168.0.111:1234")

log = logging.getLogger(__name__)

_cache: dict[str, dict] = {}


def _key(model: str, payload: dict) -> str:
    s = json.dumps({"model": model, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _get_model(model_id: str):
    if lms is None:
        raise RuntimeError("LM Studio SDK not available")
    try:
        client = getattr(lms, "Client", None)
        if client is not None:
            cl = client(base_url=LMSTUDIO_BASE_URL)
            return cl.llm(model_id)
    except Exception:
        # fall through to env-based init
        pass
    # fallback: some SDK versions read base_url from ENV
    os.environ["LMSTUDIO_BASE_URL"] = LMSTUDIO_BASE_URL
    return lms.llm(model_id)


def count_tokens_chat(model_id: str, messages: list[dict], ttl: int = 300) -> int:
    k = _key(model_id, {"messages": messages})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < ttl:
        return _cache[k]["n"]
    try:
        m = _get_model(model_id)  # example: "qwen/qwen3-14b"
        formatted = m.apply_prompt_template({"messages": messages})
        n = len(m.tokenize(formatted))
    except Exception as e:
        log.warning("LMStudio tokenization failed (using approx): %s", e)
        n = approx_tokens_messages(messages)
    _cache[k] = {"n": n, "t": now}
    return n


def count_tokens_text(model_id: str, text: str, ttl: int = 300) -> int:
    k = _key(model_id, {"text": text})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < ttl:
        return _cache[k]["n"]
    try:
        m = _get_model(model_id)
        n = len(m.tokenize(text))
    except Exception as e:
        log.warning("LMStudio tokenization failed (using approx): %s", e)
        n = approx_tokens(text)
    _cache[k] = {"n": n, "t": now}
    return n
