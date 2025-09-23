from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from packages.core.settings import get_settings
from packages.storage.repo import (
    get_or_create_memory_state,
    get_messages_since,
    insert_l2,
    insert_l3,
    trim_l3_if_over,
    update_memory_counters,
)
from packages.utils.tokens import approx_tokens


def compute_level_caps(B_work: int, tools_tokens: int = 0) -> Dict[str, int]:
    st = get_settings()
    if B_work <= 0:
        return {"l1": 0, "l2": 0, "l3": 0}
    tools_share = 0.0
    if tools_tokens > 0:
        tools_share = min(st.mem_tools_max_share, tools_tokens / max(1, B_work))
    l1 = int(math.floor((1 - tools_share) * st.mem_l1_share * B_work))
    l2 = int(math.floor(st.mem_l2_share * B_work))
    l3 = int(math.floor(st.mem_l3_share * B_work))
    return {"l1": l1, "l2": l2, "l3": l3}


def build_l1_pairs(messages: List[Any]) -> Tuple[List[Tuple[str, str]], int]:
    st = get_settings()
    pairs: List[Tuple[str, str]] = []
    # walk from end, build user->assistant pairs
    i = len(messages) - 1
    last_user_lang = None
    while i >= 0:
        if messages[i].role == 'assistant' and i - 1 >= 0 and messages[i-1].role == 'user':
            user_txt = messages[i-1].content or ''
            asst_txt = messages[i].content or ''
            # cap contributions by tokens
            if approx_tokens(user_txt) > st.cap_tok_user:
                # rough cap by characters
                max_chars = st.cap_tok_user * 4
                user_txt = user_txt[:max_chars]
            if approx_tokens(asst_txt) > st.cap_tok_assistant:
                max_chars = st.cap_tok_assistant * 4
                asst_txt = asst_txt[:max_chars]
            pairs.insert(0, (user_txt, asst_txt))
            i -= 2
        else:
            i -= 1
    total_tokens = sum(approx_tokens(u) + approx_tokens(a) for (u, a) in pairs)
    return pairs, total_tokens


def _summarize_pairs_to_bullets(pairs: List[Tuple[str, str]], max_lines_per_pair: int = 2, lang_hint: str | None = None) -> str:
    bullets: List[str] = []
    for (u, a) in pairs:
        # trivial heuristic: keep short gist of user and assistant
        u_short = u.strip().splitlines()[0][:200]
        a_short = a.strip().splitlines()[0][:200]
        bullets.append(f"- {u_short} → {a_short}")
        if max_lines_per_pair > 1 and len(u.strip().splitlines()) > 1:
            bullets.append(f"  {a_short}")
    text = "\n".join(bullets)
    return text


def promote_l1_to_l2(thread_id: str, pairs: List[Tuple[str, str]], batch_size: int) -> Tuple[int, int, str, str]:
    if not pairs:
        return 0, 0, None, None  # type: ignore[return-value]
    take = pairs[:batch_size]
    text = _summarize_pairs_to_bullets(take, max_lines_per_pair=2)
    toks = approx_tokens(text)
    # start and end message ids are not known here (we passed sanitized messages only); store placeholders
    l2 = insert_l2(thread_id, start_msg_id="auto", end_msg_id="auto", text=text, tokens=toks)
    return len(take), toks, l2.start_message_id, l2.end_message_id


def promote_l2_to_l3(thread_id: str, l2_items: List[Any], batch_size: int) -> Tuple[int, int, int, int]:
    if not l2_items:
        return 0, 0, 0, 0
    take = l2_items[:batch_size]
    # micro-theses: single-line gist per L2
    bullets = [f"• {x.text.splitlines()[0][:200]}" for x in take]
    text = "\n".join(bullets)
    toks = approx_tokens(text)
    start_id = take[0].id
    end_id = take[-1].id
    insert_l3(thread_id, start_l2_id=start_id, end_l2_id=end_id, text=text, tokens=toks)
    return len(take), toks, start_id, end_id


async def update_memory(thread_id: str, budget: Dict[str, Any], tool_results_tokens: int, now: int) -> Dict[str, Any]:
    st = get_settings()
    state = get_or_create_memory_state(thread_id)
    # fetch new messages since last_compacted_message_id
    msgs = get_messages_since(thread_id, state.last_compacted_message_id)
    pairs, l1_tokens = build_l1_pairs(msgs)

    caps = compute_level_caps(int(budget.get('B_work', 0)), int(tool_results_tokens or 0))
    actions: List[str] = []

    # compute free pct per level
    def free_pct(used: int, cap: int) -> float:
        if cap <= 0:
            return 0.0
        return max(0.0, (cap - max(0, used)) / cap)

    l1_free = free_pct(l1_tokens, caps['l1'])
    # promote L1->L2 if low free
    if caps['l1'] > 0 and l1_free < st.mem_free_threshold and pairs:
        moved, toks, s_id, e_id = promote_l1_to_l2(thread_id, pairs, st.mem_promotion_batch_size)
        actions.append(f"promoted_l1_to_l2:{moved}")
        # after promotion, reset l1_tokens for simplicity (in real impl we'd drop oldest pairs)
        pairs = pairs[moved:]
        l1_tokens = sum(approx_tokens(u) + approx_tokens(a) for (u, a) in pairs)

    # compute current L2 tokens (sum over all L2 entries)
    from packages.storage.repo import session_scope, L2Summary as _L2
    with session_scope() as s:
        l2_items = list(s.query(_L2).filter(_L2.thread_id == thread_id).order_by(_L2.id.asc()))
        l2_tokens = sum(x.tokens or 0 for x in l2_items)
    l2_free = free_pct(l2_tokens, caps['l2'])

    # promote L2->L3 if low free
    if caps['l2'] > 0 and l2_free < st.mem_free_threshold and l2_items:
        moved2, toks2, start_id, end_id = promote_l2_to_l3(thread_id, l2_items, st.mem_promotion_batch_size)
        actions.append(f"promoted_l2_to_l3:{moved2}")
        # recompute l2_tokens after promotion (we keep L2; could prune separately)
        with session_scope() as s:
            l2_items = list(s.query(_L2).filter(_L2.thread_id == thread_id).order_by(_L2.id.asc()))
            l2_tokens = sum(x.tokens or 0 for x in l2_items)

    # compute L3 tokens and trim if over cap
    from packages.storage.repo import L3MicroSummary as _L3
    with session_scope() as s:
        l3_items = list(s.query(_L3).filter(_L3.thread_id == thread_id).order_by(_L3.id.asc()))
        l3_tokens = sum(x.tokens or 0 for x in l3_items)
    if l3_tokens > caps['l3'] > 0:
        removed = trim_l3_if_over(thread_id, caps['l3'])
        if removed:
            actions.append(f"trim_l3:{removed}")
        with session_scope() as s:
            l3_items = list(s.query(_L3).filter(_L3.thread_id == thread_id).order_by(_L3.id.asc()))
            l3_tokens = sum(x.tokens or 0 for x in l3_items)

    update_memory_counters(thread_id, l1_tokens=l1_tokens, l2_tokens=l2_tokens, l3_tokens=l3_tokens)

    free = {
        'l1': free_pct(l1_tokens, caps['l1']),
        'l2': free_pct(l2_tokens, caps['l2']),
        'l3': free_pct(l3_tokens, caps['l3']),
    }
    return {
        'l1_tokens': l1_tokens,
        'l2_tokens': l2_tokens,
        'l3_tokens': l3_tokens,
        'caps': caps,
        'free_pct': free,
        'actions': actions,
    }
