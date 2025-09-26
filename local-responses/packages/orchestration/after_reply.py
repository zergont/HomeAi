from __future__ import annotations
from typing import List, Dict, Any, Tuple
import time

from packages.orchestration.token_budget import tokens_breakdown
from packages.core.settings import get_settings
from packages.storage.repo import (
    ensure_l2_for_pairs, get_l2_for_thread, get_l3_for_thread,
)

# Helpers to manipulate L1 tail (list of chat messages alternating user/assistant)

def _iterate_pairs(l1_tail: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    pairs = []
    i = 0
    while i < len(l1_tail) - 1:
        u = l1_tail[i]; a = l1_tail[i+1]
        if u.get('role') == 'user' and a.get('role') == 'assistant':
            pairs.append((u, a))
            i += 2
        else:
            i += 1
    return pairs

def _pick_oldest_pair_from_tail(l1_tail: List[Dict[str,Any]]):
    pairs = _iterate_pairs(l1_tail)
    return pairs[0] if pairs else (None, None)

def _remove_pair_from_tail(l1_tail: List[Dict[str,Any]], uid: str, aid: str):
    i = 0
    while i < len(l1_tail) - 1:
        if l1_tail[i].get('id') == uid and l1_tail[i+1].get('id') == aid:
            del l1_tail[i:i+2]
            return True
        i += 1
    return False

# HF-29B helpers

def _pct(used, cap):
    return int(round(100 * used / cap)) if cap > 0 else 0

def _levels_used(breakdown: dict):
    return {
        'l1': int(breakdown.get('l1', 0)),
        'l2': int(breakdown.get('l2', 0)),
        'l3': int(breakdown.get('l3', 0)),
    }

def _caps(meta_caps: dict):
    return {
        'l1': int(meta_caps.get('l1', 0)),
        'l2': int(meta_caps.get('l2', 0)),
        'l3': int(meta_caps.get('l3', 0)),
    }

async def normalize_after_reply(
    *,
    model_id: str,
    thread_id: str,
    system_msg: Dict[str, Any] | None,
    l3_msgs: List[Dict[str, Any]],
    l2_msgs: List[Dict[str, Any]],
    l1_tail: List[Dict[str, Any]],
    current_user_msg: Dict[str, Any],
    meta: Dict[str, Any],
    lang: str,
    summarizer,
    repo,
) -> Dict[str, Any]:
    """Post-reply normalization based on real tokens & caps.
    Accepts existing provider-layer message partitions and metadata (with caps) and ensures layers are within High watermarks.
    """
    st = get_settings()
    now = int(time.time())

    # Build message layer sets for breakdown
    msgs_system = [system_msg] if system_msg else []
    msgs_l3 = list(l3_msgs)
    msgs_l2 = list(l2_msgs)

    bd = tokens_breakdown(model_id, {
        'system': msgs_system,
        'l3': msgs_l3,
        'l2': msgs_l2,
        'l1': l1_tail,
        'user': [current_user_msg] if current_user_msg else []
    })
    meta_caps = (meta.get('context_assembly', {}).get('caps') if meta.get('context_assembly') else {})
    caps = _caps(meta_caps)
    used = _levels_used(bd)

    steps: List[str] = []
    guard = 0

    while guard < 10:
        guard += 1
        l1_pct = _pct(used['l1'], caps['l1'])
        l2_pct = _pct(used['l2'], caps['l2'])
        l3_pct = _pct(used['l3'], caps['l3'])
        did = False

        # L1 → L2
        if l1_pct > st.L1_HIGH:
            u,a = _pick_oldest_pair_from_tail(l1_tail)
            if u and a:
                try:
                    txt = await summarizer.summarize_pair_to_l2(u.get('content',''), a.get('content',''), lang or 'ru')
                except Exception:
                    txt = f"- {u.get('content','')[:120]} → {a.get('content','')[:120]}"
                repo.insert_l2_summary(thread_id, u.get('id','u'), a.get('id','a'), txt, now)
                _remove_pair_from_tail(l1_tail, u.get('id'), a.get('id'))
                steps.append('l1_to_l2:1')
                did = True
        # L2 → L3
        elif l2_pct > st.L2_HIGH:
            block = repo.pick_oldest_l2_block(thread_id, max_items=5)
            if block:
                try:
                    l3_txt = await summarizer.summarize_l2_block_to_l3([x.text for x in block], lang or 'ru')
                except Exception:
                    l3_txt = '\n'.join([f"• {(x.text or '').splitlines()[0][:160]}" for x in block[:2]])
                repo.insert_l3_summary(thread_id, [x.id for x in block], l3_txt, now)
                repo.delete_l2_batch([x.id for x in block])
                steps.append(f"l2_to_l3:{len(block)}")
                did = True
        # L3 eviction
        elif l3_pct > st.L3_HIGH:
            ev = repo.evict_l3_oldest(thread_id, count=3)
            if ev:
                steps.append(f"l3_evict:{ev}")
                did = True
        if not did:
            break

        # reload L2/L3 after any mutation
        l2_records = get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
        l3_records = get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
        msgs_l2 = [{'role':'assistant','content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
        msgs_l3 = [{'role':'assistant','content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]

        bd = tokens_breakdown(model_id, {
            'system': msgs_system,
            'l3': msgs_l3,
            'l2': msgs_l2,
            'l1': l1_tail,
            'user': [current_user_msg] if current_user_msg else []
        })
        used = _levels_used(bd)

    # Update meta counters
    ctx_asm = meta.setdefault('context_assembly', {})
    ctx_asm.setdefault('summary_counters', {'l1_to_l2':0,'l2_to_l3':0})
    ctx_asm['summary_counters']['l1_to_l2'] += sum(1 for s in steps if s.startswith('l1_to_l2'))
    ctx_asm['summary_counters']['l2_to_l3'] += sum(int(s.split(':')[1]) for s in steps if s.startswith('l2_to_l3'))
    ctx_asm['compaction_steps'] = (ctx_asm.get('compaction_steps') or []) + steps

    return {
        'steps': steps,
        'tokens_breakdown_post': bd,
        'l1_tail': l1_tail,
        'l2_msgs': msgs_l2,
        'l3_msgs': msgs_l3,
    }
