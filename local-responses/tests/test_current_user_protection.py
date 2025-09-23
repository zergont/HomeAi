from __future__ import annotations

import pytest

from packages.orchestration.context_builder import assemble_context

@pytest.mark.asyncio
async def test_current_user_only_mode(monkeypatch):
    # Fake budgets to have small input capacity to trigger minimal context mode
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 2048, 'R_out': 128, 'R_sys': 128, 'Safety': 128,
            'B_total_in': 256, 'B_work': 64, 'core_sys_pad': 32,
            'core_tokens': core_tokens, 'core_cap': core_cap,
            'core_reserved': 64, 'effective_max_output_tokens': 128,
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    huge_text = "X" * 10000
    out = await assemble_context(
        thread_id='thread_dummy',
        model_id='lm:qwen/qwen3-14b',
        max_output_tokens=None,
        tool_results_text=None,
        tool_results_tokens=None,
        last_user_lang='en',
        current_user_text=huge_text,
    )
    stats = out['stats']
    assert stats.get('current_user_only_mode') is True
    # tools/l1/l2/l3 must be zero
    assert stats['tokens']['tools'] == 0
    assert stats['tokens']['l1'] == 0
    assert stats['tokens']['l2'] == 0
    assert stats['tokens']['l3'] == 0
    # total_in must include current_user_tokens and minimal core
    assert stats['tokens']['total_in'] >= stats['current_user_tokens']
