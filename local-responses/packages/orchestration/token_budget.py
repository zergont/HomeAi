from __future__ import annotations
from typing import List, Dict, Any, Tuple

from packages.core.settings import get_settings
from packages.providers import lmstudio_tokens
from packages.utils.tokens import approx_tokens_messages


def tokens_breakdown(model_id: str, messages_blocks: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int | str]:
    """
    Progressive cumulative counting across blocks: system -> +l3 -> +l2 -> +l1 -> +user.
    For proxy mode uses LM Studio HTTP counting (HF-30) which returns (tokens, mode).
    Diffs between cumulative counts yield per-level token usage.
    Returns dict with system, l3, l2, l1, user, total, token_count_mode.
    """
    st = get_settings()

    def _count(msgs: List[Dict[str, Any]]) -> Tuple[int, str]:
        if st.TOKEN_COUNT_MODE == 'proxy':
            tokens, mode = lmstudio_tokens.count_tokens_chat(model_id, msgs)  # no use_cache kw for compatibility
            return int(tokens), mode
        # approx mode
        return approx_tokens_messages(msgs), 'approx'

    sys_msgs = messages_blocks.get('system', [])
    l3_msgs_full = sys_msgs + messages_blocks.get('l3', [])
    l2_msgs_full = l3_msgs_full + messages_blocks.get('l2', [])
    l1_msgs_full = l2_msgs_full + messages_blocks.get('l1', [])
    all_msgs = l1_msgs_full + messages_blocks.get('user', [])

    T0, m0 = _count(sys_msgs)
    T1, m1 = _count(l3_msgs_full)
    T2, m2 = _count(l2_msgs_full)
    T3, m3 = _count(l1_msgs_full)
    T4, m4 = _count(all_msgs)

    # Determine final mode precedence: if any step was approx -> approx, else last mode
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
