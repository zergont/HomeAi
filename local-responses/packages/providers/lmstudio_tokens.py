import time, hashlib, json
try:
    import lmstudio as lms
except Exception:  # optional import guard for test environments
    lms = None

_cache: dict[str, dict] = {}

def _key(model: str, payload: dict) -> str:
    s = json.dumps({"model": model, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def count_tokens_chat(model_id: str, messages: list[dict], ttl: int = 300) -> int:
    k = _key(model_id, {"messages": messages})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < ttl:
        return _cache[k]["n"]
    if lms is None:
        # Fallback approximate: join as text for tests without SDK
        text = "\n".join((m.get("content") or "") for m in messages)
        n = max(1, len(text) // 4)
    else:
        m = lms.llm(model_id)
        formatted = m.apply_prompt_template({"messages": messages})
        n = len(m.tokenize(formatted))
    _cache[k] = {"n": n, "t": now}
    return n

def count_tokens_text(model_id: str, text: str, ttl: int = 300) -> int:
    k = _key(model_id, {"text": text})
    now = time.time()
    if k in _cache and now - _cache[k]["t"] < ttl:
        return _cache[k]["n"]
    if lms is None:
        n = max(1, len(text) // 4)
    else:
        m = lms.llm(model_id)
        n = len(m.tokenize(text))
    _cache[k] = {"n": n, "t": now}
    return n
