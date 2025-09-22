# tests/test_cost_usage.py
from __future__ import annotations

from decimal import Decimal

from packages.core.pricing import price_for


def test_cost_calculation_default_price() -> None:
    price = price_for("lmstudio", "any", {"lmstudio:__default__": 0.5})
    total_tokens = 1500
    cost = Decimal((total_tokens / 1000) * price).quantize(Decimal("0.000001"))
    assert cost == Decimal("0.750000")
