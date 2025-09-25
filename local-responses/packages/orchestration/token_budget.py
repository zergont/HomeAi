from __future__ import annotations
from typing import List, Dict, Any

from packages.core.settings import get_settings
from packages.providers import lmstudio_tokens
from packages.utils.tokens import approx_tokens_messages


def tokens_breakdown(model_id: str, messages_blocks: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int | str]:
    """
    messages_blocks = {
      'system': [...],  # 0..1 system messages
      'l3': [...], 'l2': [...], 'l1': [...], 'user': [...]
    }
    Progressive cumulative counting: T0..T4, diffs yield per-layer tokens.
    Returns dict with system,l3,l2,l1,user,total,token_count_mode.
    """
    st = get_settings()

    def _count(msgs: List[Dict[str, Any]]):
        if st.TOKEN_COUNT_MODE == 'proxy':
            return lmstudio_tokens.count_tokens_chat(model_id, msgs, st.TOKEN_CACHE_TTL_SEC)
        return approx_tokens_messages(msgs)

    sys_msgs = messages_blocks.get('system', [])
    l3_msgs = sys_msgs + messages_blocks.get('l3', [])
    l2_msgs = l3_msgs + messages_blocks.get('l2', [])
    l1_msgs = l2_msgs + messages_blocks.get('l1', [])
    all_msgs = l1_msgs + messages_blocks.get('user', [])

    T0 = _count(sys_msgs)
    T1 = _count(l3_msgs)
    T2 = _count(l2_msgs)
    T3 = _count(l1_msgs)
    T4 = _count(all_msgs)

    return {
        'system': T0,
        'l3': T1 - T0,
        'l2': T2 - T1,
        'l1': T3 - T2,
        'user': T4 - T3,
        'total': T4,
        'token_count_mode': 'proxy' if st.TOKEN_COUNT_MODE == 'proxy' else 'approx'
    }
