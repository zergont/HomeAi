from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

_cache: Dict[str, Dict[str, Any]] = {}
_locks: Dict[str, asyncio.Lock] = {}

def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]

def get_cached(key: str) -> Optional[Dict[str, Any]]:
    entry = _cache.get(key)
    if not entry:
        return None
    if entry["exp"] < time.time():
        _cache.pop(key, None)
        return None
    return entry["val"]

def set_cached(key: str, value: Dict[str, Any], ttl_sec: int) -> None:
    _cache[key] = {"val": value, "exp": time.time() + max(1, int(ttl_sec))}
