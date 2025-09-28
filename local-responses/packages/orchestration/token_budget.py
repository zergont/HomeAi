from __future__ import annotations
from typing import List, Dict, Any, Tuple

from packages.core.settings import get_settings
from packages.providers import lmstudio_tokens
from packages.utils.tokens import approx_tokens_messages


def tokens_breakdown(model_id: str, messages_blocks: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int | str]:
    st = get_settings()

    def _count(msgs: List[Dict[str, Any]]) -> Tuple[int, str]:
        if st.TOKEN_COUNT_MODE == 'proxy':
            try:
                n, mode = lmstudio_tokens.count_tokens_chat(model_id, msgs)
                return int(n), mode
            except Exception:
                return approx_tokens_messages(msgs), 'approx'
        return approx_tokens_messages(msgs), 'approx'

    sys_msgs = messages_blocks.get('system', [])
    l3_full = sys_msgs + messages_blocks.get('l3', [])
    l2_full = l3_full + messages_blocks.get('l2', [])
    l1_full = l2_full + messages_blocks.get('l1', [])
    all_full = l1_full + messages_blocks.get('user', [])

    T0, m0 = _count(sys_msgs)
    T1, m1 = _count(l3_full)
    T2, m2 = _count(l2_full)
    T3, m3 = _count(l1_full)
    T4, m4 = _count(all_full)

    modes = {m0, m1, m2, m3, m4}
    final_mode = 'approx' if 'approx' in modes else (m4 or m3 or m2 or m1 or m0)

    return {
        'system': T0,
        'l3': T1 - T0,
        'l2': T2 - T1,
        'l1': T3 - T2,
        'user': T4 - T3,
        'total': T4,
        'token_count_mode': final_mode,
    }
