from __future__ import annotations

import math
import asyncio
from typing import Any, Dict, Optional

from packages.core.settings import get_settings
from packages.providers.lmstudio_cache import get_cached, set_cached, _get_lock
from packages.providers.lmstudio_model_info import fetch_model_info


def _strip_provider_prefix(model_id: str) -> str:
    return model_id.split(":", 1)[1] if model_id.startswith("lm:") else model_id


async def _get_model_info_cached(model_id: str) -> Dict[str, Any]:
    settings = get_settings()
    mid = _strip_provider_prefix(model_id)
    key = f"lmstudio:model:{mid}"
    cached = get_cached(key)
    if cached is not None:
        return cached
    # prevent thundering herd
    lock = _get_lock(key)
    async with lock:
        cached2 = get_cached(key)
        if cached2 is not None:
            return cached2
        data = await fetch_model_info(mid)
        # If model isn't loaded yet or we only have defaults/max, cache briefly to allow quick refresh after load
        provisional = (
            data.get("source") == "default"
            or data.get("state") != "loaded"
            or not isinstance(data.get("loaded_context_length"), int)
        )
        ttl = 2 if provisional else int(settings.ctx_model_info_ttl_sec)
        set_cached(key, data, ttl)
        return data


async def compute_budgets(model_id: str, max_output_tokens: Optional[int], core_tokens: int, core_cap: int, settings=None) -> Dict[str, Any]:
    settings = settings or get_settings()
    info = await _get_model_info_cached(model_id)

    # If model is not loaded yet, wait briefly for it to load to get accurate loaded_context_length
    if (info.get("state") != "loaded") or (not isinstance(info.get("loaded_context_length"), int)):
        mid = _strip_provider_prefix(model_id)
        key = f"lmstudio:model:{mid}"
        for _ in range(10):  # up to ~6s
            await asyncio.sleep(0.6)
            latest = await fetch_model_info(mid)
            if latest.get("state") == "loaded" and isinstance(latest.get("loaded_context_length"), int):
                info = latest
                set_cached(key, latest, int(settings.ctx_model_info_ttl_sec))
                break

    loaded = info.get("loaded_context_length")
    mx = info.get("max_context_length")

    C_loaded = int(loaded) if isinstance(loaded, int) and loaded > 0 else None
    C_max = int(mx) if isinstance(mx, int) and mx > 0 else None

    # Budget base MUST be loaded window when available; never exceed it
    if C_loaded is not None:
        C_base = C_loaded
        source = "lmstudio.loaded_context_length"
    elif C_max is not None:
        C_base = C_max
        source = "lmstudio.max_context_length"
    else:
        C_base = int(settings.ctx_default_context_length)
        source = "default"

    # Derive reservations strictly from the chosen base
    R_out = min(int(max_output_tokens or settings.ctx_rout_default), int(math.floor(settings.ctx_rout_pct * C_base)))
    R_sys = max(int(settings.ctx_rsys_min), int(math.floor(settings.ctx_rsys_pct * C_base)))
    Safety = int(math.ceil(settings.ctx_safety_pct * C_base))
    B_total_in = int(C_base - R_out - R_sys - Safety)

    core_sys_pad = int(settings.ctx_core_sys_pad_tok)
    core_reserved = min(int(core_cap) + core_sys_pad, max(0, B_total_in))
    B_work = max(0, B_total_in - core_reserved)

    return {
        "model_id": _strip_provider_prefix(model_id),
        "source": source,
        "C_eff": C_base,  # kept for backward-compat in UI/tests
        "C_loaded": C_loaded,
        "C_max": C_max,
        "R_out": R_out,
        "R_sys": R_sys,
        "Safety": Safety,
        "B_total_in": B_total_in,
        "core_tokens": int(core_tokens),
        "core_cap": int(core_cap),
        "core_reserved": core_reserved,
        "core_sys_pad": core_sys_pad,
        "B_work": B_work,
        "effective_max_output_tokens": R_out,
    }
