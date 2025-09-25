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
from packages.orchestration.token_budget import tokens_breakdown

logger = logging.getLogger("app.context")

# --- HF-28 canonical helpers ---

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


def flatten_pairs_asc(pairs: List[Tuple[Message, Message]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for u, a in pairs:  # ASC
        out.append({'role': 'user', 'content': sanitize_for_memory(u.content or ''), 'id': u.id})
        out.append({'role': 'assistant', 'content': sanitize_for_memory(a.content or ''), 'id': a.id})
    return out

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

    # Fetch existing summaries ASC
    l3_records = get_l3_for_thread(thread_id, limit=200)
    l2_records = get_l2_for_thread(thread_id, limit=500)

    # History for L1
    hist = get_thread_messages_for_l1(thread_id, exclude_message_id=current_user_id, max_items=2000)
    pairs_all = build_pairs_asc(hist)  # ASC

    # System & tools
    D = t(lang, 'divider')
    def build_system(core_text: str, tools_text: str) -> str:
        blocks = [t(lang, 'instruction'), D, t(lang, 'core_profile'), core_text]
        if tools_text:
            blocks += [D, t(lang, 'tool_results'), tools_text]
        return '\n'.join(b for b in blocks if b)
    tools_text = sanitize_for_memory(tools_src_txt[:tools_cap*4]) if tools_used > 0 and tools_src_txt else ''
    system_text = build_system(core_text_cur, tools_text)

    msgs_system = [{'role': 'system', 'content': system_text}] if system_text else []
    msgs_tools = ([{'role': 'system', 'content': tools_text}] if tools_text else [])
    msgs_l3 = [{'role': 'assistant', 'content': r.text, 'id': f'l3#{r.id}'} for r in l3_records]
    msgs_l2 = [{'role': 'assistant', 'content': r.text, 'id': f'l2#{r.id}:{r.start_message_id}->{r.end_message_id}'} for r in l2_records]

    base_blocks = {'system': msgs_system, 'l3': msgs_l3, 'l2': msgs_l2}
    bd0 = tokens_breakdown(model_id, {**base_blocks, 'l1': [], 'user': []})

    C_eff = int(budgets.get('C_eff', 0))
    R_sys = int(budgets.get('R_sys', 0))
    Safety = int(budgets.get('Safety', 0))

    chosen_pairs: List[Tuple[Message, Message]] = []  # ASC

    # Fill-to-cap: newest→oldest trial insertion
    for (u, a) in reversed(pairs_all):
        trial_pairs = [(u, a)] + chosen_pairs  # new oldest candidate at front to maintain ASC when assigned
        trial_l1 = flatten_pairs_asc(trial_pairs)
        bd_try = tokens_breakdown(model_id, {**base_blocks, 'l1': trial_l1, 'user': ([{'role': 'user', 'content': current_user_text or ''}] if current_user_text else [])})
        l1_used = bd_try['l1']
        total = bd_try['total']
        free_out_cap = max(0, C_eff - total - R_sys - Safety)
        if l1_used <= L1_cap and free_out_cap >= 0:
            chosen_pairs = trial_pairs  # accept
        else:
            break

    # Guaranteed minimum
    need_min = max(0, st.L1_MIN_PAIRS - len(chosen_pairs))
    for _ in range(need_min):
        idx = len(pairs_all) - len(chosen_pairs) - 1
        if idx < 0:
            break
        chosen_pairs = [pairs_all[idx]] + chosen_pairs

    l1_msgs_out = flatten_pairs_asc(chosen_pairs)

    # Remaining pairs become old_pairs for future compaction (not executed here; compactor later)
    old_pairs = pairs_all[:-len(chosen_pairs)] if chosen_pairs else pairs_all

    # Token breakdown after L1
    bd_final = tokens_breakdown(model_id, {**base_blocks, 'l1': l1_msgs_out, 'user': ([{'role': 'user', 'content': current_user_text or ''}] if current_user_text else [])})
    free_out_cap = max(0, C_eff - bd_final['total'] - R_sys - Safety)

    compaction_steps: List[str] = []  # HF-28: compaction loop could be added here if free_out unsafe (omitted for brevity)

    # Diagnostics
    if chosen_pairs:
        first_pair = chosen_pairs[0]
        last_pair = chosen_pairs[-1]
        logger.debug("L1.order: pairs=%d first u#%s->a#%s last u#%s->a#%s", len(chosen_pairs), first_pair[0].id, first_pair[1].id, last_pair[0].id, last_pair[1].id)

    # Build provider messages
    msgs_user = ([{'role':'user','content': current_user_text or '', 'id': current_user_id or 'current_user'}] if current_user_text else [])
    provider_messages = msgs_system + msgs_tools + msgs_l3 + msgs_l2 + l1_msgs_out + msgs_user

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
        },
        'l1_order_preview': (
            ([f"{p[0].id}->{p[1].id}" for p in chosen_pairs[:3]] + ['...'] + [f"{p[0].id}->{p[1].id}" for p in chosen_pairs[-3:]])
            if len(chosen_pairs) > 6 else [f"{p[0].id}->{p[1].id}" for p in chosen_pairs]
        ),
    }

    # Fill pct diagnostics
    def pct(v: int, cap: int): return int(round(100 * v / cap)) if cap > 0 else 0
    stats['fill_pct'] = {
        'l1': pct(stats['tokens']['l1'], L1_cap),
        'l2': pct(stats['tokens']['l2'], L2_cap),
        'l3': pct(stats['tokens']['l3'], L3_cap),
    }
    stats['free_pct'] = {k: 100 - v for k, v in stats['fill_pct'].items()}

    return {
        'system_text': system_text,
        'messages': l1_msgs_out,
        'provider_messages': provider_messages,
        'stats': stats,
        'context_budget': budgets,
    }
