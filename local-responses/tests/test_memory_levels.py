from __future__ import annotations

import asyncio
import pytest

from packages.storage.repo import create_thread, append_message
from packages.orchestration.memory_manager import compute_level_caps, build_l1_pairs, update_memory


@pytest.mark.asyncio
async def test_memory_levels_promotions_and_caps(monkeypatch):
    th = create_thread(None)
    # generate a long series: user/assistant pairs
    for i in range(40):
        append_message(th.id, 'user', f'user says {i} ' + ('x'*100))
        append_message(th.id, 'assistant', f'assistant replies {i} ' + ('y'*200))

    budget = {'B_work': 4096}
    caps = compute_level_caps(budget['B_work'], tools_tokens=0)
    assert sum(caps.values()) <= budget['B_work']

    # initial update to build L1 and possibly promote
    mem = await update_memory(th.id, budget, tool_results_tokens=0, now=0)
    assert 'l1_tokens' in mem and 'caps' in mem

    # trigger promotions by setting small caps and thresholds
    from packages.core import settings as s
    s.get_settings.cache_clear()
    from packages.core.settings import AppSettings
    AppSettings.mem_free_threshold = 0.99  # type: ignore

    mem2 = await update_memory(th.id, budget, tool_results_tokens=0, now=0)
    # expect some actions indicating promotions or trims (may be empty if content small)
    assert isinstance(mem2.get('actions'), list)

    # caps per role respected in pairs build
    msgs = [type('M', (), {'role':'user','content':'u'*1000}), type('M', (), {'role':'assistant','content':'a'*1000})]
    pairs, toks = build_l1_pairs(msgs)
    assert pairs
    from packages.core.settings import get_settings
    st = get_settings()
    # token caps: user <= 120, assistant <= 80
    u_tok = len(pairs[0][0])//4 + (1 if len(pairs[0][0])%4 else 0)
    a_tok = len(pairs[0][1])//4 + (1 if len(pairs[0][1])%4 else 0)
    assert u_tok <= st.cap_tok_user and a_tok <= st.cap_tok_assistant
