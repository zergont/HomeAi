from __future__ import annotations

import pytest

from packages.orchestration.context_builder import assemble_context

@pytest.mark.asyncio
async def test_core_capped_not_below_min(monkeypatch):
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 4096, 'R_out': 512, 'R_sys': 256, 'Safety': 256,
            'B_total_in': 4096-512-256-256, 'B_work': 2000, 'core_sys_pad': 100,
            'core_tokens': 0, 'core_cap': 80,  # cap < min skeleton
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    out = await assemble_context('thread_dummy', 'lm:qwen/qwen3-14b', max_output_tokens=None, tool_results_text=None, tool_results_tokens=None, last_user_lang='en')
    # core tokens should be at least min skeleton (60)
    assert out['stats']['tokens']['core'] >= 60
