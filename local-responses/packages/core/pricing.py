# packages/core/pricing.py
from __future__ import annotations

from typing import Dict

PRICES: Dict[str, Dict[str, float]] = {
    "lmstudio": {"__default__": 0.0},
}


def price_for(provider: str, model: str, overrides: Dict[str, float] | None = None) -> float:
    prov = provider.lower()
    mdl = model.lower()
    base = PRICES.get(prov, {}).get("__default__", 0.0)
    if overrides:
        # exact model override wins
        key_exact = f"{prov}:{mdl}"
        if key_exact in overrides:
            return overrides[key_exact]
        # provider default override
        key_def = f"{prov}:__default__"
        if key_def in overrides:
            return overrides[key_def]
    return base
