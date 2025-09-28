from __future__ import annotations

import math, time, logging
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.redactor import sanitize_for_memory
from packages.storage.repo import (
    get_profile, get_thread_messages_for_l1,
    get_l2_for_thread, get_l3_for_thread,
)
from packages.storage import repo  # needed for compactor mutations
from packages.storage.models import Message
from packages.utils.tokens import approx_tokens, profile_text_view
from packages.utils.i18n import pick_lang, t
from packages.orchestration.token_budget import tokens_breakdown
from packages.orchestration import summarizer

logger = logging.getLogger("app.context")

# --- Helpers ---

def build_pairs_asc(items: List[Message]) -> List[Tuple[Message, Message]]:
    pairs: List[Tuple[Message, Message]] = []
    last_user: Optional[Message] = None
    for m in items:  # ASC
        if m.role == 'user':
            last_user = m
        elif m.role == 'assistant' and last_user is not None:
            pairs.append((last_user, m))
            last_user = None
    return pairs  # ASC


def flatten_pairs_asc(pairs: List[Tuple[Message, Message]]):
    out: List[Dict[str, str]] = []
    for u, a in pairs:  # ASC
        out.append({'role': 'user', 'content': sanitize_for_memory(u.content or ''), 'id': u.id})
        out.append({'role': 'assistant', 'content': sanitize_for_memory(a.content or ''), 'id': a.id})
    return out

# --- HF-33 Preflight Compactor ---
async def compact_to_budget(model_id: str,
                            thread_id: str,
                            lang: str,
                            caps: Dict[str, int],
                            blocks: Dict[str, List[Dict[str, Any]]],
                            meta: Dict[str, Any]) -> Tuple[Dict[str, int], List[str], Dict[str, int]]:
    """Preflight compaction to satisfy HIGH/LOW and free output window targets.
    Mutates blocks in place (l1/l2/l3). Returns (breakdown, steps, counters).
    """
    st = get_settings()
    steps: List[str] = []
    counters = {"l1_to_l2_groups": 0, "l1_to_l2_pairs": 0, "l2_to_l3_groups": 0}

    def bd():
        return tokens_breakdown(model_id, blocks)

    breakdown = bd()
    guard = 0
    while guard < 20:
        guard += 1
        used_l1 = breakdown.get("l1", 0)
        used_l2 = breakdown.get("l2", 0)
        used_l3 = breakdown.get("l3", 0)
        l1_pct = (100 * used_l1 // max(1, caps.get('l1', 1)))
        l2_pct = (100 * used_l2 // max(1, caps.get('l2', 1)))
        l3_pct = (100 * used_l3 // max(1, caps.get('l3', 1)))
        C_eff = meta['context_budget']['C_eff']
        R_sys = meta['context_budget']['R_sys']
        Safety = meta['context_budget']['Safety']
        total = breakdown['total']
        free_out_cap = max(0, C_eff - total - R_sys - Safety)
        need_more_room = free_out_cap < st.R_OUT_MIN
        over_any = (l1_pct > st.L1_HIGH) or (l2_pct > st.L2_HIGH) or (l3_pct > st.L3_HIGH)
        if not over_any and not need_more_room:
            break
        did = False
        # First: L2 -> L3 grouping if L2 high OR need more room and L2 has content
        if l2_pct > st.L2_HIGH or (need_more_room and used_l2 > 0):
            block = repo.pick_oldest_l2_block(thread_id, max_items=st.L3_GROUP_SIZE)
            if block:
                try:
                    l3_txt = await summarizer.summarize_l2_block_to_l3_text([x.text or '' for x in block], lang, st.L3_GROUP_MAX_TOKENS)
                except Exception:
                    l3_txt = '\n'.join([f"• {(x.text or '').splitlines()[0][:160]}" for x in block[:2]])
                repo.insert_l3_summary(thread_id, [x.id for x in block], l3_txt, int(time.time()))
                repo.delete_l2_batch([x.id for x in block])
                # reload L2/L3
                l2_recs = repo.get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
                l3_recs = repo.get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
                blocks['l2'] = [{"role": "assistant", "content": r.text, "id": f"l2#{r.id}:{r.start_message_id}->{r.end_message_id}"} for r in l2_recs]
                blocks['l3'] = [{"role": "assistant", "content": r.text, "id": f"l3#{r.id}"} for r in l3_recs]
                steps.append(f"l2_to_l3_group:{len(block)}->1")
                counters['l2_to_l3_groups'] += 1
                breakdown = bd(); did = True
        # Second: L1 -> L2 grouping of oldest pairs
        if not did and (l1_pct > st.L1_HIGH or (need_more_room and len(blocks['l1']) >= 2 * st.L1_MIN_PAIRS)):
            pair_count = len(blocks['l1']) // 2
            if pair_count > 0:
                K = min(st.L2_GROUP_SIZE, max(1, pair_count - st.L1_MIN_PAIRS))
                chunk = blocks['l1'][:2*K]
                pairs_ids: List[Tuple[str, str]] = []
                pairs_texts: List[Tuple[str, str]] = []
                valid = True
                for i in range(0, len(chunk), 2):
                    u = chunk[i]; a = chunk[i+1]
                    if u.get('role') != 'user' or a.get('role') != 'assistant':
                        valid = False; break
                    pairs_ids.append((u['id'], a['id']))
                    pairs_texts.append((u.get('content', ''), a.get('content', '')))
                if valid and pairs_ids:
                    try:
                        l2_text = await summarizer.summarize_pairs_group_to_l2(pairs_ids, pairs_texts, lang=lang, max_tokens=st.L2_GROUP_MAX_TOKENS)
                    except Exception:
                        bullets = []
                        for (u_txt, a_txt) in pairs_texts[:2]:
                            bullets.append(f"- {(u_txt.splitlines() or [''])[0][:120]} → {(a_txt.splitlines() or [''])[0][:120]}")
                        l2_text = '\n'.join(bullets) if bullets else '(empty)'
                    repo.insert_l2_summary(thread_id, pairs_ids[0][0], pairs_ids[-1][1], l2_text, int(time.time()))
                    del blocks['l1'][:2*K]
                    l2_recs = repo.get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
                    blocks['l2'] = [{"role": "assistant", "content": r.text, "id": f"l2#{r.id}:{r.start_message_id}->{r.end_message_id}"} for r in l2_recs]
                    steps.append(f"l1_to_l2_group:{K}->1")
                    counters['l1_to_l2_groups'] += 1
                    counters['l1_to_l2_pairs'] += K
                    breakdown = bd(); did = True
        # Third: L3 eviction if still needed
        if not did and (l3_pct > st.L3_HIGH or (need_more_room and used_l3 > 0)):
            ev = repo.evict_l3_oldest(thread_id, count=3)
            if ev:
                l3_recs = repo.get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
                blocks['l3'] = [{"role": "assistant", "content": r.text, "id": f"l3#{r.id}"} for r in l3_recs]
                steps.append(f"l3_evict:{ev}")
                breakdown = bd(); did = True
        if not did:
            break
    return breakdown, steps, counters

# --- Main assembler ---
async def assemble_context(
    thread_id: str,
    model_id: str,
    max_output_tokens: Optional[int] = None,
    tool_results_text: Optional[str] = None,
    tool_results_tokens: Optional[int] = None,
    last_user_lang: Optional[str] = None,
    current_user_text: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    st = get_settings()
    prof = get_profile()
    lang = pick_lang(last_user_lang, prof.preferred_language)

    prof_dict = {
        'display_name': prof.display_name,
        'preferred_language': prof.preferred_language,
        'tone': prof.tone,
        'timezone': prof.timezone,
        'region_coarse': prof.region_coarse,
        'work_hours': prof.work_hours,
        'ui_format_prefs': prof.ui_format_prefs,
        'goals_mood': prof.goals_mood,
        'decisions_tasks': prof.decisions_tasks,
        'brevity': prof.brevity,
        'format_defaults': prof.format_defaults,
        'interests_topics': prof.interests_topics,
        'workflow_tools': prof.workflow_tools,
        'os': prof.os,
        'runtime': prof.runtime,
        'hardware_hint': prof.hardware_hint,
    }
    core_text_full = profile_text_view(prof_dict)
    core_tokens = approx_tokens(core_text_full)
    core_cap = int(math.ceil(core_tokens * 1.10))

    budgets = await compute_budgets(model_id, max_output_tokens, core_tokens=core_tokens, core_cap=core_cap, settings=st)
    B_work = int(budgets.get('B_work', 0))
    tools_cap = int(min(st.mem_tools_max_share * B_work, B_work))
    if tool_results_text and tool_results_tokens is None:
        tool_results_tokens = approx_tokens(tool_results_text)
    tools_src_txt = tool_results_text or ''
    tools_used = min(int(tool_results_tokens or approx_tokens(tools_src_txt)), tools_cap)
    work_left = max(0, B_work - tools_used)
    L1_cap = int(st.mem_l1_share * work_left)
    L2_cap = int(st.mem_l2_share * work_left)
    L3_cap = int(st.mem_l3_share * work_left)

    l3_records = get_l3_for_thread(thread_id, limit=getattr(st, 'L3_FETCH_LIMIT', 200))
    l2_records = get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))

    hist = get_thread_messages_for_l1(thread_id, exclude_message_id=current_user_id, max_items=2000)
    pairs_all = build_pairs_asc(hist)

    D = t(lang, 'divider')
    def build_system(core_text: str, tools_text: str) -> str:
        blocks = [t(lang, 'instruction'), D, t(lang, 'core_profile'), core_text]
        if tools_text:
            blocks += [D, t(lang, 'tool_results'), tools_text]
        return '\n'.join(b for b in blocks if b)
    tools_text = sanitize_for_memory(tools_src_txt[:tools_cap*4]) if tools_used > 0 and tools_src_txt else ''
    system_text = build_system(core_text_full, tools_text)

    msgs_system = [{'role': 'system', 'content': system_text}] if system_text else []
    msgs_tools = ([{'role': 'system', 'content': tools_text}] if tools_text else [])
    msgs_l3 = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]

    # Fill L1 newest->oldest within cap & free out constraint approximation
    C_eff = int(budgets.get('C_eff', 0)); R_sys = int(budgets.get('R_sys', 0)); Safety = int(budgets.get('Safety', 0))
    chosen_pairs: List[Tuple[Message, Message]] = []
    for (u, a) in reversed(pairs_all):
        trial = [(u, a)] + chosen_pairs
        trial_l1 = flatten_pairs_asc(trial)
        bd_try = tokens_breakdown(model_id, {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': trial_l1, 'user': ([{'role':'user','content': current_user_text or ''}] if current_user_text else [])})
        if bd_try['l1'] <= L1_cap and (C_eff - bd_try['total'] - R_sys - Safety) >= 0:
            chosen_pairs = trial
        else:
            break
    # Minimum guarantee
    need_min = max(0, st.L1_MIN_PAIRS - len(chosen_pairs))
    for _ in range(need_min):
        idx = len(pairs_all) - len(chosen_pairs) - 1
        if idx < 0: break
        chosen_pairs = [pairs_all[idx]] + chosen_pairs
    l1_msgs_out = flatten_pairs_asc(chosen_pairs)

    # Eager grouped L2 for old pairs
    summary_counters: Dict[str, int] = {}
    if pairs_all and len(chosen_pairs) < len(pairs_all) and getattr(st, 'SUMMARIZE_INSTEAD_OF_TRIM', True):
        old_pairs = pairs_all[:max(0, len(pairs_all) - len(chosen_pairs))]
        if old_pairs:
            try:
                res_group = await repo.ensure_l2_for_pairs_grouped(
                    thread_id=thread_id,
                    pairs_seq=[(u.id, a.id) for (u, a) in old_pairs],
                    lang=last_user_lang or 'ru',
                    now_ts=int(time.time()),
                    group_size=getattr(st, 'L2_GROUP_SIZE', 4),
                    max_group_tokens=getattr(st, 'L2_GROUP_MAX_TOKENS', 0) or None
                )
                if res_group.get('groups') or res_group.get('pairs'):
                    l2_records = get_l2_for_thread(thread_id, limit=getattr(st, 'L2_FETCH_LIMIT', 500))
                    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
                    summary_counters['l1_to_l2_groups'] = res_group.get('groups', 0)
                    summary_counters['l1_to_l2_pairs'] = res_group.get('pairs', 0)
            except Exception as exc:
                logger.warning("group eager L2 summarization failed: %s", exc)

    # Preflight compactor (HF-33)
    blocks = {
        'system': msgs_system,
        'l3': msgs_l3,
        'l2': msgs_l2,
        'l1': l1_msgs_out,
        'user': ([{'role': 'user', 'content': current_user_text or '', 'id': current_user_id or 'current_user'}] if current_user_text else []),
    }
    caps_levels = {'l1': L1_cap, 'l2': L2_cap, 'l3': L3_cap}
    meta_stub = {'context_budget': {'C_eff': C_eff, 'R_sys': R_sys, 'Safety': Safety}}
    bd_final, comp_steps, counters_added = await compact_to_budget(model_id, thread_id, last_user_lang or 'ru', caps_levels, blocks, meta_stub)
    # Free out cap after compaction
    free_out_cap = max(0, C_eff - bd_final['total'] - R_sys - Safety)

    # Compose provider messages after compaction
    provider_messages = blocks['system'] + msgs_tools + blocks['l3'] + blocks['l2'] + blocks['l1'] + blocks['user']

    stats = {
        'order': ['core','tools','l3','l2','l1'],
        'tokens': {
            'core': approx_tokens(core_text_full),
            'tools': approx_tokens(tools_text) if tools_text else 0,
            'l3': bd_final.get('l3', 0),
            'l2': bd_final.get('l2', 0),
            'l1': bd_final.get('l1', 0),
            'total_in': bd_final.get('total', 0),
        },
        'caps': {'core_cap': core_cap, 'tools_cap': tools_cap, 'l1': L1_cap, 'l2': L2_cap, 'l3': L3_cap},
        'budget': {k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens')},
        'free_out_cap': free_out_cap,
        'l1_pairs_count': len(chosen_pairs),
        'compaction_steps': comp_steps,
        'prompt_tokens_precise': bd_final.get('total', 0),
        'token_count_mode': bd_final.get('token_count_mode'),
        'includes': {
            'l3_ids': [int(m['id'].split('#')[1]) for m in blocks['l3']],
            'l2_pairs': [{'id': int(m['id'].split('#')[1].split(':')[0]), 'u': m['id'].split(':')[1].split('->')[0], 'a': m['id'].split('->')[1]} for m in blocks['l2']],
            'l1_pairs': [{'u': blocks['l1'][i]['id'], 'a': blocks['l1'][i+1]['id']} for i in range(0, len(blocks['l1']), 2) if i+1 < len(blocks['l1'])],
        },
        # HF-34.2: L3 quality (length in characters) for UI indicator
        'l3_quality': [{'id': r.id, 'chars': len((r.text or '').strip())} for r in l3_records],
        'l1_order_preview': (
            ([f"{p[0].id}->{p[1].id}" for p in chosen_pairs[:3]] + ['...'] + [f"{p[0].id}->{p[1].id}" for p in chosen_pairs[-3:]])
            if len(chosen_pairs) > 6 else [f"{p[0].id}->{p[1].id}" for p in chosen_pairs]
        ),
    }
    if summary_counters:
        sc_all = summary_counters.copy()
    else:
        sc_all = {}
    for k,v in counters_added.items():
        sc_all[k] = sc_all.get(k,0)+v
    if sc_all:
        stats['summary_counters'] = sc_all

    def pct(v: int, cap: int): return int(round(100 * v / cap)) if cap > 0 else 0
    stats['fill_pct'] = {
        'l1': pct(stats['tokens']['l1'], L1_cap),
        'l2': pct(stats['tokens']['l2'], L2_cap),
        'l3': pct(stats['tokens']['l3'], L3_cap),
    }
    stats['free_pct'] = {k: 100 - v for k, v in stats['fill_pct'].items()}

    return {
        'system_text': system_text,
        'messages': blocks['l1'],  # L1 after compaction (list of message dicts)
        'provider_messages': provider_messages,
        'stats': stats,
        'context_budget': budgets,
    }
