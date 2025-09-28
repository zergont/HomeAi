from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
import time
import logging

from packages.core.settings import get_settings
from packages.orchestration.token_budget import tokens_breakdown
from packages.storage import repo

log = logging.getLogger("after_reply")

# Canonical helpers (ASC pairs)

def _build_pairs_asc(items):
    pairs = []
    last_user = None
    for m in items:  # ASC
        if getattr(m, 'role', None) == 'user':
            last_user = m
        elif getattr(m, 'role', None) == 'assistant' and last_user is not None:
            pairs.append((last_user, m))
            last_user = None
    return pairs

def _flatten_pairs_asc(pairs):
    out = []
    for u, a in pairs:
        out.append({'role': 'user', 'content': u.content, 'id': u.id})
        out.append({'role': 'assistant', 'content': a.content, 'id': a.id})
    return out

async def _recompute_blocks_fill_to_cap(model_id: str,
                                        thread_id: str,
                                        system_msg: Optional[Dict[str, Any]],
                                        lang: str,
                                        caps: Dict[str, int]) -> Tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
    """Return (msgs_system, msgs_l3, msgs_l2, l1_tail) after refilling L1 to cap."""
    st = get_settings()
    l3_records = repo.get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
    l2_records = repo.get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
    msgs_system = [system_msg] if system_msg else []
    msgs_l3 = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
    hist = repo.get_thread_messages_for_l1(thread_id, exclude_message_id=None, max_items=2000)
    pairs_all = _build_pairs_asc(hist)

    # Fill-to-cap (greedy newest->oldest)
    chosen: List[Tuple[Any, Any]] = []
    C_eff = caps.get('C_eff', 0)
    bd0 = tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': [], 'user': []})
    stg = get_settings()
    for (u, a) in reversed(pairs_all):
        trial = [(u, a)] + chosen
        trial_l1 = _flatten_pairs_asc(trial)
        bd_try = tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': trial_l1, 'user': []})
        if bd_try['l1'] <= caps.get('l1', 0):
            chosen = trial
        else:
            break
    need_min = max(0, stg.L1_MIN_PAIRS - len(chosen))
    for _ in range(need_min):
        idx = len(pairs_all) - len(chosen) - 1
        if idx < 0:
            break
        chosen = [pairs_all[idx]] + chosen
    l1_tail = _flatten_pairs_asc(chosen)
    return msgs_system, msgs_l3, msgs_l2, l1_tail

def _pct(used: int, cap: int) -> int:
    return int(round(100 * used / cap)) if cap > 0 else 0

async def normalize_after_reply(*,
                                model_id: str,
                                thread_id: str,
                                system_msg: Optional[Dict[str, Any]],
                                lang: str,
                                caps: Dict[str, int],
                                l3_msgs: Optional[List[Dict[str, Any]]] = None,
                                l2_msgs: Optional[List[Dict[str, Any]]] = None,
                                l1_tail: Optional[List[Dict[str, Any]]] = None,
                                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """HF-32E / HF-34.1: Post-reply normalization with batched compaction + L3 quality control.
    Adds retry + skip for empty L3 outputs.
    """
    st = get_settings()
    if l3_msgs is None or l2_msgs is None or l1_tail is None:
        msgs_system, l3_msgs, l2_msgs, l1_tail = await _recompute_blocks_fill_to_cap(model_id, thread_id, system_msg, lang, caps)
    else:
        msgs_system = [system_msg] if system_msg else []

    def _bd():
        return tokens_breakdown(model_id, {'system': msgs_system, 'l3': l3_msgs, 'l2': l2_msgs, 'l1': l1_tail, 'user': []})

    bd = _bd()
    steps: List[str] = []
    sc = meta.setdefault('context_assembly', {}).setdefault('summary_counters', {}) if meta is not None else {}
    guard = 0

    from packages.orchestration import summarizer  # local import to avoid cycle at module load

    while guard < 12:
        guard += 1
        l1_pct = _pct(bd.get('l1', 0), caps.get('l1', 0))
        l2_pct = _pct(bd.get('l2', 0), caps.get('l2', 0))
        l3_pct = _pct(bd.get('l3', 0), caps.get('l3', 0))
        did = False

        # Batched L1 -> L2
        if l1_pct > st.L1_HIGH:
            pair_count = len(l1_tail) // 2
            if pair_count > 0:
                K = min(st.L2_GROUP_SIZE, pair_count)
                chunk_msgs = l1_tail[:2*K]
                pairs_ids: List[Tuple[str,str]] = []
                pairs_texts: List[Tuple[str,str]] = []
                valid = True
                for j in range(0, len(chunk_msgs), 2):
                    u = chunk_msgs[j]; a = chunk_msgs[j+1]
                    if u.get('role') != 'user' or a.get('role') != 'assistant':
                        valid = False; break
                    pairs_ids.append((u['id'], a['id']))
                    pairs_texts.append((u.get('content',''), a.get('content','')))
                if valid and pairs_ids:
                    try:
                        l2_text = await summarizer.summarize_pairs_group_to_l2(pairs_ids, pairs_texts, lang, st.L2_GROUP_MAX_TOKENS or None)
                    except Exception:
                        bullets = []
                        for (u_txt,a_txt) in pairs_texts[:2]:
                            bullets.append(f"- {(u_txt.splitlines() or [''])[0][:120]} → {(a_txt.splitlines() or [''])[0][:120]}")
                        l2_text = '\n'.join(bullets) if bullets else '(empty)'
                    repo.insert_l2_summary(thread_id, pairs_ids[0][0], pairs_ids[-1][1], l2_text, int(time.time()))
                    del l1_tail[:2*K]
                    steps.append(f"l1_to_l2_group:{K}->1")
                    sc['l1_to_l2_groups'] = sc.get('l1_to_l2_groups', 0) + 1
                    sc['l1_to_l2_pairs'] = sc.get('l1_to_l2_pairs', 0) + K
                    did = True
        # Batched L2 -> L3 with quality control (HF-34.1)
        elif l2_pct > st.L2_HIGH:
            block = repo.pick_oldest_l2_block(thread_id, max_items=st.L3_GROUP_SIZE)
            if block:
                try:
                    l2_texts = [x.text or '' for x in block]
                    l3_txt = await summarizer.summarize_l2_block_to_l3(l2_texts, lang, max_tokens=st.L3_GROUP_MAX_TOKENS or None)
                    if not summarizer._is_meaningful(l3_txt):
                        # retry with truncated inputs (last chance)
                        l3_txt = await summarizer.summarize_l2_block_to_l3([t[:200] for t in l2_texts], lang, max_tokens=st.L3_GROUP_MAX_TOKENS or None)
                    if summarizer._is_meaningful(l3_txt):
                        repo.insert_l3_summary(thread_id, [x.id for x in block], l3_txt, int(time.time()))
                        repo.delete_l2_batch([x.id for x in block])
                        steps.append(f"l2_to_l3_group:{len(block)}->1")
                        sc['l2_to_l3_groups'] = sc.get('l2_to_l3_groups', 0) + 1
                        did = True
                    else:
                        log.warning("Skip empty L3 summary for thread=%s (block ids=%s)", thread_id, [x.id for x in block])
                        # do not delete L2 so it can be retried later
                except Exception as e:
                    log.warning("L2->L3 summarization error thread=%s: %s", thread_id, e)
        # L3 eviction (unchanged)
        elif l3_pct > st.L3_HIGH:
            ev = repo.evict_l3_oldest(thread_id, count=3)
            if ev:
                steps.append(f"l3_evict:{ev}")
                did = True

        if not did:
            break

        # Reload L2/L3 after any changes
        l2_records = repo.get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
        l3_records = repo.get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
        l2_msgs = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
        l3_msgs = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
        bd = _bd()
        if (_pct(bd.get('l1',0), caps.get('l1',0)) <= st.L1_LOW and
            _pct(bd.get('l2',0), caps.get('l2',0)) <= st.L2_LOW and
            _pct(bd.get('l3',0), caps.get('l3',0)) <= st.L3_LOW):
            break

    result = {'compaction_steps': steps, 'tokens_breakdown': bd, 'l1_tail': l1_tail, 'l2_msgs': l2_msgs, 'l3_msgs': l3_msgs, 'summary_counters': sc}
    if meta is not None:
        ctx_asm = meta.setdefault('context_assembly', {})
        ctx_asm['compaction_steps'] = (ctx_asm.get('compaction_steps') or []) + steps
    return result
