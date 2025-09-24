import json
import hashlib

def canon_args(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def args_hash(obj, algo="sha256") -> str:
    s = canon_args(obj).encode("utf-8")
    return hashlib.new(algo, s).hexdigest()

def is_valid_tool_json(s: str) -> tuple[bool, dict|None]:
    try:
        data = json.loads(s)
        return (isinstance(data, dict) and "name" in data and "arguments" in data, data)
    except Exception:
        return (False, None)
