from __future__ import annotations
from typing import List, Dict, Any, Tuple
import time

from packages.orchestration.token_budget import tokens_breakdown
from packages.core.settings import get_settings
from packages.storage.repo import get_thread_messages_for_l1, ensure_l2_for_pairs, promote_l2_to_l3, get_l2_for_thread, get_l3_for_thread
from packages.orchestration.redactor import sanitize_for_memory

# Helpers mirror context_builder dynamic logic

def _build_pairs(items):
    pairs = []
    last_user = None
    for m in items:
        if m.role == 'user':
            last_user = m
        elif m.role == 'assistant' and last_user is not None:
            pairs.append((last_user, m))
            last_user = None
    return pairs  # ASC

def _append_pair_msgs(buf: List[Dict[str,Any]], u, a):
    buf.append({'role':'user','content':sanitize_for_memory(u.content or ''),'id':u.id})
    buf.append({'role':'assistant','content':sanitize_for_memory(a.content or ''),'id':a.id})

async def normalize_after_reply(model_id: str, thread_id: str, system_msg: dict,
                                l3_msgs: List[Dict[str, Any]], l2_msgs: List[Dict[str, Any]],
                                l1_tail: List[Dict[str, Any]], current_user_msg: dict,
                                now: int, lang: str, repo, summarizer) -> dict:
    """Post-reply normalization with dynamic fill-to-cap (HF-27B).
    1. Rebuild full history, fill L1 from end until cap or free budget.
    2. Cascade compaction (L1→L2, L2→L3, L3 eviction) until all layers <= Low watermarks.
    Returns compaction steps and updated token breakdown.
    NOTE: We approximate caps from current token breakdown if explicit caps API is absent.
    """
    st = get_settings()
    steps: List[str] = []
    summary_l2 = 0
    summary_l3 = 0

    # Rebuild history (exclude current user message id if provided in current_user_msg)
    exclude_id = current_user_msg.get('id') if current_user_msg else None
    hist = get_thread_messages_for_l1(thread_id, exclude_message_id=exclude_id, max_items=2000)
    pairs_all = _build_pairs(hist)

    # Existing l1_tail (list of dicts) may not have ids for reconstruction -> rebuild chosen_pairs from ids
    existing_ids = []
    for i in range(0, len(l1_tail), 2):
        try:
            u = l1_tail[i]; a = l1_tail[i+1]
            if u['role']=='user' and a['role']=='assistant':
                existing_ids.append((u.get('id'), a.get('id')))
        except Exception:
            break

    # Start fresh dynamic L1
    msgs_system = [system_msg] if system_msg else []
    msgs_l3 = list(l3_msgs)
    msgs_l2 = list(l2_msgs)
    l1_dynamic: List[Dict[str,Any]] = []
    chosen_pairs: List[Tuple[Any,Any]] = []

    # Initial breakdown without L1/user
    base_br = tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': [], 'user': []})
    C_eff = None  # not passed here; free_out not strictly enforced post-reply

    # Derive approximate caps (if repo has get_memory_caps use it)
    try:
        caps = repo.get_memory_caps(thread_id)
        L1_cap = caps.get('l1', 0) or 0
        L2_cap = caps.get('l2', 0) or 0
        L3_cap = caps.get('l3', 0) or 0
    except Exception:
        # Approximate using shares of current prompt tokens (base) scaled
        total_base = base_br['total'] or 1
        L1_cap = int(total_base * st.mem_l1_share)
        L2_cap = int(total_base * st.mem_l2_share)
        L3_cap = int(total_base * st.mem_l3_share)

    # Fill to cap (newest -> oldest)
    for (u,a) in reversed(pairs_all):
        trial = list(l1_dynamic)
        _append_pair_msgs(trial, u, a)
        br_try = tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': trial, 'user': [current_user_msg] if current_user_msg else []})
        if br_try['l1'] <= L1_cap or len(chosen_pairs) < st.L1_MIN_PAIRS:
            _append_pair_msgs(l1_dynamic, u, a)
            chosen_pairs.insert(0, (u,a))
        else:
            break

    # Ensure minimum pairs
    need_min = st.L1_MIN_PAIRS - len(chosen_pairs)
    while need_min > 0 and len(chosen_pairs) < len(pairs_all):
        idx = len(pairs_all) - len(chosen_pairs) - 1
        if idx < 0:
            break
        u,a = pairs_all[idx]
        _append_pair_msgs(l1_dynamic, u, a)
        chosen_pairs.insert(0, (u,a))
        need_min -= 1

    def rebuild_breakdown():
        return tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': l1_dynamic, 'user': [current_user_msg] if current_user_msg else []})

    br = rebuild_breakdown()

    def pct(v, cap):
        return (v / cap * 100) if cap > 0 else 0

    # Cascade compaction until all layers <= Low
    HIGH = {'l1': st.L1_HIGH, 'l2': st.L2_HIGH, 'l3': st.L3_HIGH}
    LOW = {'l1': st.L1_LOW, 'l2': st.L2_LOW, 'l3': st.L3_LOW}

    def levels_over():
        return (
            pct(br['l1'], L1_cap) > HIGH['l1'] or
            pct(br['l2'], L2_cap) > HIGH['l2'] or
            pct(br['l3'], L3_cap) > HIGH['l3']
        )
    def levels_under():
        return (
            pct(br['l1'], L1_cap) <= LOW['l1'] and
            pct(br['l2'], L2_cap) <= LOW['l2'] and
            pct(br['l3'], L3_cap) <= LOW['l3']
        )

    loop_guard = 20
    while levels_over() and loop_guard > 0:
        loop_guard -= 1
        did = False
        # L1 -> L2 (oldest pair)
        if pct(br['l1'], L1_cap) > HIGH['l1'] and len(chosen_pairs) > st.L1_MIN_PAIRS:
            # oldest chosen pair is at index 0
            oldest = chosen_pairs.pop(0)
            # rebuild l1_dynamic from remaining
            l1_dynamic = []
            for (u,a) in chosen_pairs:
                _append_pair_msgs(l1_dynamic, u, a)
            # summarize removed pair
            created = await ensure_l2_for_pairs(thread_id, [(oldest[0].id, oldest[1].id)], lang, now)
            if created:
                summary_l2 += created
                steps.append(f"l1_to_l2:{created}")
            # reload L2 messages
            l2_records = get_l2_for_thread(thread_id, limit=500)
            msgs_l2 = [{'role':'assistant','content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
            did = True
        # L2 -> L3
        elif pct(br['l2'], L2_cap) > HIGH['l2']:
            l2_records_all = get_l2_for_thread(thread_id, limit=500)
            if l2_records_all:
                ids2 = [x.id for x in l2_records_all[:5]]
                made = await promote_l2_to_l3(thread_id, ids2, lang, now)
                if made:
                    summary_l3 += made
                    steps.append(f"l2_to_l3:{len(ids2)}")
                l2_records = get_l2_for_thread(thread_id, limit=500)
                l3_records = get_l3_for_thread(thread_id, limit=200)
                msgs_l2 = [{'role':'assistant','content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
                msgs_l3 = [{'role':'assistant','content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
                did = True
        # Evict L3 oldest (simple batch of 3) if still over
        elif pct(br['l3'], L3_cap) > HIGH['l3']:
            ev = repo.evict_l3_oldest(thread_id, count=3)
            if ev:
                steps.append(f"l3_evict:{ev}")
            l3_records = get_l3_for_thread(thread_id, limit=200)
            msgs_l3 = [{'role':'assistant','content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
            did = True
        if not did:
            break
        br = rebuild_breakdown()
        if levels_under():
            break

    return {
        'compaction_steps_post': steps,
        'tokens_breakdown_post': br,
        'l1_tail': l1_dynamic,
        'summary_counters_post': {'l1_to_l2': summary_l2, 'l2_to_l3': summary_l3}
    }
