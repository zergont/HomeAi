from __future__ import annotations

import pytest

from packages.orchestration.context_builder import assemble_context

@pytest.mark.asyncio
async def test_lang_follow_ru(monkeypatch):
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 4096, 'R_out': 512, 'R_sys': 256, 'Safety': 256,
            'B_total_in': 4096-512-256-256, 'B_work': 1000, 'core_sys_pad': 100,
            'core_tokens': 0, 'core_cap': 500,
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    out = await assemble_context(thread_id='thread_dummy', model_id='lm:qwen/qwen3-14b', max_output_tokens=None, tool_results_text=None, tool_results_tokens=None, last_user_lang='ru')
    text = out['system_text']
    assert 'Следуй правилам' in text
    assert 'ПРОФИЛЬ (ЯДРО)' in text
    assert '---' in text

@pytest.mark.asyncio
async def test_lang_follow_en(monkeypatch):
    async def fake_budgets(model_id, mot, core_tokens, core_cap, settings=None):
        return {
            'C_eff': 4096, 'R_out': 512, 'R_sys': 256, 'Safety': 256,
            'B_total_in': 4096-512-256-256, 'B_work': 1000, 'core_sys_pad': 100,
            'core_tokens': 0, 'core_cap': 500,
        }
    import packages.orchestration.context_builder as cb
    monkeypatch.setattr(cb, 'compute_budgets', fake_budgets)

    out = await assemble_context(thread_id='thread_dummy', model_id='lm:qwen/qwen3-14b', max_output_tokens=None, tool_results_text=None, tool_results_tokens=None, last_user_lang='en')
    text = out['system_text']
    assert 'Follow the rules' in text
    assert 'CORE PROFILE' in text
    assert '---' in text
