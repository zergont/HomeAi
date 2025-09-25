from __future__ import annotations

import math, time, logging
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.redactor import sanitize_for_memory
from packages.storage.repo import (
    get_profile, session_scope, get_thread_messages_for_l1,
    get_l2_for_thread, get_l3_for_thread, ensure_l2_for_pairs, promote_l2_to_l3
)
from packages.storage.models import L2Summary, L3MicroSummary, Message
from packages.utils.tokens import approx_tokens, profile_text_view
from packages.utils.i18n import pick_lang, t
from packages.providers import lmstudio_tokens
from packages.orchestration.token_budget import tokens_breakdown

logger = logging.getLogger("app.context")

# --------- helpers (HF-27A dynamic fill) ---------

def _build_pairs(items: List[Message]) -> List[Tuple[Message, Message]]:
    pairs: List[Tuple[Message, Message]] = []
    last_user: Optional[Message] = None
    for m in items:
        if m.role == 'user':
            last_user = m
        elif m.role == 'assistant' and last_user is not None:
            pairs.append((last_user, m))
            last_user = None
    return pairs  # ASC


def _append_pair_msgs(buf: List[Dict[str, str]], u: Message, a: Message):
    buf.append({'role': 'user', 'content': sanitize_for_memory(u.content or ''), 'id': u.id})
    buf.append({'role': 'assistant', 'content': sanitize_for_memory(a.content or ''), 'id': a.id})


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
    core_text_cur = core_text_full
    core_tokens = approx_tokens(core_text_full)
    core_cap = int(math.ceil(core_tokens * 1.10))

    budgets = await compute_budgets(model_id, max_output_tokens, core_tokens=core_tokens, core_cap=core_cap, settings=st)
    B_work = int(budgets.get('B_work', 0))
    tools_cap = int(math.floor(min(st.mem_tools_max_share * B_work, B_work)))
    if tool_results_text and tool_results_tokens is None:
        tool_results_tokens = approx_tokens(tool_results_text)
    tools_src_txt = tool_results_text or ''
    tools_used = min(int(tool_results_tokens or approx_tokens(tools_src_txt)), tools_cap)
    work_left = max(0, B_work - tools_used)
    L1_cap = int(math.floor(st.mem_l1_share * work_left))
    L2_cap = int(math.floor(st.mem_l2_share * work_left))
    L3_cap = int(math.floor(st.mem_l3_share * work_left))

    # Summaries for inclusion (ASC order)
    l3_records = get_l3_for_thread(thread_id, limit=200)
    l2_records = get_l2_for_thread(thread_id, limit=500)

    # History (exclude current user msg if passed)
    hist = get_thread_messages_for_l1(thread_id, exclude_message_id=current_user_id, max_items=2000)
    pairs_all = _build_pairs(hist)
    logger.debug("L1.dynamic: total_pairs=%d", len(pairs_all))

    # System & tools text
    D = t(lang, 'divider')
    def build_system(core_text: str, tools_text: str) -> str:
        blocks = [t(lang, 'instruction'), D, t(lang, 'core_profile'), core_text]
        if tools_text:
            blocks += [D, t(lang, 'tool_results'), tools_text]
        return "\n".join(b for b in blocks if b)

    tools_text = ''
    if tools_used > 0 and tools_src_txt:
        tools_text = sanitize_for_memory(tools_src_txt[:tools_cap*4])
    system_text = build_system(core_text_cur, tools_text)

    # Base blocks (without L1 and user)
    msgs_system = [{'role': 'system', 'content': system_text}] if system_text else []
    msgs_tools = ([{'role': 'system', 'content': tools_text}] if tools_text else [])
    msgs_l3 = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]

    base_blocks = {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': [], 'user': []}
    bd_base = tokens_breakdown(model_id, base_blocks)

    C_eff = int(budgets.get('C_eff', 0))
    R_sys = int(budgets.get('R_sys', 0))
    Safety = int(budgets.get('Safety', 0))
    R_out = int(budgets.get('R_out', 0))

    l1_msgs_out: List[Dict[str, str]] = []
    chosen_pairs: List[Tuple[Message, Message]] = []

    # Dynamic fill from newest backwards
    for (u, a) in reversed(pairs_all):
        trial_l1 = list(l1_msgs_out)
        _append_pair_msgs(trial_l1, u, a)
        bd_try = tokens_breakdown(model_id, { 'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': trial_l1, 'user': ([{'role':'user','content': current_user_text or ''}] if current_user_text else []) })
        l1_used = bd_try['l1']
        total = bd_try['total']
        free_out_cap = max(0, C_eff - total - R_sys - Safety)
        if l1_used <= L1_cap and free_out_cap >= 0:
            _append_pair_msgs(l1_msgs_out, u, a)
            chosen_pairs.insert(0, (u, a))  # maintain ASC
        else:
            break

    # Guarantee minimum L1_MIN_PAIRS
    need_min = int(getattr(st, 'L1_MIN_PAIRS', 2)) - (len(l1_msgs_out)//2)
    while need_min > 0 and len(chosen_pairs) < len(pairs_all):
        # take next oldest not chosen
        target_index = len(pairs_all) - len(chosen_pairs) - 1
        if target_index < 0:
            break
        u, a = pairs_all[target_index]
        _append_pair_msgs(l1_msgs_out, u, a)
        chosen_pairs.insert(0, (u, a))
        need_min -= 1

    # Remaining old pairs become candidates for L2 compaction
    old_pairs = pairs_all[:-len(chosen_pairs)] if chosen_pairs else pairs_all

    # tokens after final L1 fill
    bd_final = tokens_breakdown(model_id, { 'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': l1_msgs_out, 'user': ([{'role':'user','content': current_user_text or ''}] if current_user_text else []) })
    free_out_cap = max(0, C_eff - bd_final['total'] - R_sys - Safety)

    compaction_steps: List[str] = []
    summary_created_l2 = 0
    summary_created_l3 = 0
    now_ts = int(time.time())

    # Auto-compaction if output buffer negative (or insufficient vs R_OUT)
    R_OUT_MIN = int(getattr(st, 'R_OUT_MIN', 256))
    L2_HIGH = int(getattr(st, 'L2_HIGH', 90))

    def l2_fill_pct() -> float:
        return (bd_final['l2'] / L2_cap * 100) if L2_cap > 0 else 0.0

    loop_guard = 20
    while (free_out_cap < 0 or free_out_cap < R_OUT_MIN) and loop_guard > 0:
        loop_guard -= 1
        if old_pairs:
            batch_ids = [(u.id, a.id) for (u, a) in old_pairs]
            created = await ensure_l2_for_pairs(thread_id, batch_ids, lang, now_ts)
            if created:
                summary_created_l2 += created
                compaction_steps.append(f"l1_to_l2:{created}")
            old_pairs = []
            l2_records = get_l2_for_thread(thread_id, limit=500)
            msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
        elif l2_fill_pct() > L2_HIGH:
            l2_records_all = get_l2_for_thread(thread_id, limit=500)
            if l2_records_all:
                ids2 = [x.id for x in l2_records_all[:5]]
                made = await promote_l2_to_l3(thread_id, ids2, lang, now_ts)
                if made:
                    summary_created_l3 += made
                    compaction_steps.append(f"l2_to_l3:{len(ids2)}")
                l2_records = get_l2_for_thread(thread_id, limit=500)
                l3_records = get_l3_for_thread(thread_id, limit=200)
                msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
                msgs_l3 = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
        else:
            # reduce L1 tail to minimum
            min_pairs = int(getattr(st, 'L1_MIN_PAIRS', 2))
            if len(chosen_pairs) > min_pairs:
                # remove oldest excess pairs into L2 summaries
                excess = chosen_pairs[:-min_pairs]
                chosen_pairs = chosen_pairs[-min_pairs:]
                l1_msgs_out = []
                for (u,a) in chosen_pairs:
                    _append_pair_msgs(l1_msgs_out, u, a)
                if excess:
                    ex_ids = [(u.id, a.id) for (u,a) in excess]
                    created2 = await ensure_l2_for_pairs(thread_id, ex_ids, lang, now_ts)
                    if created2:
                        summary_created_l2 += created2
                        compaction_steps.append(f"tail_reduce:{len(excess)}→{min_pairs}")
                    l2_records = get_l2_for_thread(thread_id, limit=500)
                    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]
            else:
                break
        bd_final = tokens_breakdown(model_id, { 'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2, 'l1': l1_msgs_out, 'user': ([{'role':'user','content': current_user_text or ''}] if current_user_text else []) })
        free_out_cap = max(0, C_eff - bd_final['total'] - R_sys - Safety)
        if free_out_cap >= 0 and free_out_cap >= R_OUT_MIN:
            break

    # Build final provider messages
    msgs_user = ([{'role':'user','content': current_user_text or '', 'id': current_user_id or 'current_user'}] if current_user_text else [])
    provider_messages = msgs_system + msgs_tools + msgs_l3 + msgs_l2 + l1_msgs_out + msgs_user

    # Stats
    def pct(v: int, cap: int): return int(round(100 * v / cap)) if cap > 0 else 0
    stats = {
        'order': ['core','tools','l3','l2','l1'],
        'tokens': {
            'core': approx_tokens(core_text_cur),
            'tools': approx_tokens(tools_text) if tools_text else 0,
            'l3': bd_final['l3'],
            'l2': bd_final['l2'],
            'l1': bd_final['l1'],
            'total_in': bd_final['total'],
        },
        'caps': {'core_cap': core_cap, 'tools_cap': tools_cap, 'l1': L1_cap, 'l2': L2_cap, 'l3': L3_cap},
        'budget': {k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens')},
        'free_out_cap': free_out_cap,
        'l1_pairs_count': len(chosen_pairs),
        'compaction_steps': compaction_steps,
        'prompt_tokens_precise': bd_final['total'],
        'token_count_mode': bd_final['token_count_mode'],
        'includes': {
            'l3_ids': [r.id for r in l3_records],
            'l2_pairs': [{'id': r.id, 'u': r.start_message_id, 'a': r.end_message_id} for r in l2_records],
            'l1_pairs': [{'u': u.id, 'a': a.id} for (u,a) in chosen_pairs],
        }
    }
    stats['fill_pct'] = {
        'l1': pct(stats['tokens']['l1'], L1_cap),
        'l2': pct(stats['tokens']['l2'], L2_cap),
        'l3': pct(stats['tokens']['l3'], L3_cap),
    }
    stats['free_pct'] = {k: 100 - v for k, v in stats['fill_pct'].items()}

    logger.debug("L1.dynamic.final: pairs=%d free_out=%d steps=%s", len(chosen_pairs), free_out_cap, compaction_steps)

    return {
        'system_text': system_text,
        'messages': l1_msgs_out,
        'provider_messages': provider_messages,
        'stats': stats,
        'context_budget': budgets,
    }
