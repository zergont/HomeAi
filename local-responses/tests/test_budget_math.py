from __future__ import annotations

import math
import pytest
from packages.orchestration.budget import compute_budgets
from packages.core.settings import get_settings
from packages.storage.models import ToolRun

class DummySettings:
    ctx_model_info_ttl_sec = 300
    ctx_safety_pct = 0.10
    ctx_rsys_pct = 0.05
    ctx_rsys_min = 256
    ctx_rout_pct = 0.25
    ctx_rout_default = 512
    ctx_default_context_length = 4096
    ctx_core_sys_pad_tok = 100

@pytest.mark.asyncio
async def test_budget_small_and_large(monkeypatch):
    async def fake_fetch(model_id: str):
        return {"id": model_id, "loaded_context_length": 2048, "max_context_length": 32768, "source": "lmstudio"}
    import packages.orchestration.budget as budget_mod
    monkeypatch.setattr(budget_mod, 'fetch_model_info', fake_fetch)

    s = DummySettings()
    b = await compute_budgets("lm:qwen/qwen3-14b", None, core_tokens=100, core_cap=200, settings=s)
    assert b['C_eff'] == 2048
    assert b['R_out'] == min(s.ctx_rout_default, math.floor(0.25 * 2048))
    assert b['R_sys'] >= s.ctx_rsys_min
    assert b['B_work'] >= 0
    # core reservation includes pad
    bti = b['B_total_in']
    expected_reserved = min(200 + s.ctx_core_sys_pad_tok, max(0, bti))
    assert b['core_reserved'] == expected_reserved
    assert b['core_sys_pad'] == s.ctx_core_sys_pad_tok

@pytest.mark.asyncio
async def test_budget_defaults_when_no_info(monkeypatch):
    async def fake_fetch(model_id: str):
        return {"id": model_id, "loaded_context_length": None, "max_context_length": None, "source": "default"}
    import packages.orchestration.budget as budget_mod
    monkeypatch.setattr(budget_mod, 'fetch_model_info', fake_fetch)
    monkeypatch.setattr(budget_mod, 'get_cached', lambda key: None)
    monkeypatch.setattr(budget_mod, 'set_cached', lambda key, val, ttl: None)

    s = DummySettings()
    b = await compute_budgets("qwen/qwen3-14b", 10000, core_tokens=5000, core_cap=6000, settings=s)
    assert b['C_eff'] == s.ctx_default_context_length
    assert b['R_out'] == min(10000, math.floor(s.ctx_rout_pct * s.ctx_default_context_length))
    # B_work decreased by pad
    bti = b['B_total_in']
    assert b['core_reserved'] == min(6000 + s.ctx_core_sys_pad_tok, max(0, bti))
    assert b['B_work'] == max(0, bti - b['core_reserved'])
