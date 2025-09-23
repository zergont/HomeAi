from __future__ import annotations

import pytest

from packages.orchestration.context_builder import assemble_context

@pytest.mark.asyncio
async def test_context_small_window_squeezes(monkeypatch):
    # monkeypatch budgets to small window
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 2048, 'R_out': 256, 'R_sys': 256, 'Safety': 128,
            'B_total_in': 2048-256-256-128, 'B_work': 800, 'core_sys_pad': 100,
            'core_tokens': 0, 'core_cap': 200,
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    out = await assemble_context('thread_dummy', 'lm:qwen/qwen3-14b', max_output_tokens=128, tool_results_text='T'*5000, tool_results_tokens=None, last_user_lang='ru')
    stats = out['stats']
    assert stats['tokens']['total_in'] <= stats['budget']['B_total_in']
    assert isinstance(stats['squeezes'], list)

@pytest.mark.asyncio
async def test_context_large_window_no_squeeze(monkeypatch):
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 32768, 'R_out': 1024, 'R_sys': 256, 'Safety': 256,
            'B_total_in': 32768-1024-256-256, 'B_work': 10000, 'core_sys_pad': 100,
            'core_tokens': 0, 'core_cap': 2000,
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    out = await assemble_context('thread_dummy', 'lm:qwen/qwen3-14b', max_output_tokens=128, tool_results_text='tools', tool_results_tokens=None, last_user_lang='en')
    assert out['stats']['tokens']['total_in'] <= out['stats']['budget']['B_total_in']
