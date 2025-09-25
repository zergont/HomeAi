from __future__ import annotations

import math, time, logging
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.redactor import sanitize_for_memory
from packages.storage.repo import get_profile, session_scope, get_thread_messages_for_l1
from packages.storage.models import L2Summary, L3MicroSummary, Message
from packages.utils.tokens import approx_tokens, profile_text_view
from packages.utils.i18n import pick_lang, t
from packages.providers import lmstudio_tokens
from packages.orchestration.token_budget import tokens_breakdown

logger = logging.getLogger("app.context")

# ---------------- pairing helpers ----------------

def build_pairs(msgs: List[Message]) -> List[Tuple[Message, Message]]:
    pairs: List[Tuple[Message, Message]] = []
    i = 0
    while i < len(msgs) - 1:
        u, a = msgs[i], msgs[i + 1]
        if u.role == 'user' and a.role == 'assistant':
            pairs.append((u, a))
            i += 2
        else:
            i += 1
    return pairs


def flatten_pairs(pairs: List[Tuple[Message, Message]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for (u, a) in pairs:
        out.append({'role': 'user', 'content': sanitize_for_memory(u.content or '')})
        out.append({'role': 'assistant', 'content': sanitize_for_memory(a.content or '')})
    return out

# ---------------- main assemble -------------------
async def assemble_context(
    thread_id: str,
    model_id: str,
    max_output_tokens: Optional[int] = None,
    tool_results_text: Optional[str] = None,
    tool_results_tokens: Optional[int] = None,
    last_user_lang: Optional[str] = None,
    current_user_text: Optional[str] = None,
) -> Dict[str, Any]:
    st = get_settings()

    # Profile & language
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

    # Budgets baseline
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

    # Load summaries (L3 newest first, L2 newest first)
    with session_scope() as s:
        l3_items_db = list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()))
        l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
    l3_txt = "\n".join([f"• {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l3_items_db]) if l3_items_db else ''
    l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db]) if l2_items_db else ''

    # Full message history ASC (user/assistant) for pairing (exclude current user if id passed via budgets maybe later)
    # Replaced previous history acquisition with full-history function
    # NOTE: current_user_text not yet inserted into DB when assembling; if needed pass exclude id.
    history_msgs = get_thread_messages_for_l1(thread_id, exclude_message_id=None, max_items=2000)
    all_pairs = build_pairs(history_msgs)
    tail_min = int(getattr(st, 'L1_TAIL_MIN_PAIRS', 4))
    tail_n = min(tail_min, len(all_pairs))
    tail_pairs = all_pairs[-tail_n:]
    old_pairs = all_pairs[:-tail_n]
    logger.debug("L1.build: total_pairs=%d, tail_pairs=%d, old_pairs=%d", len(all_pairs), len(tail_pairs), len(old_pairs))
    logger.debug("L1.tail.ids: %s", [f"{p[0].id[-6:]}->{p[1].id[-6:]}" for p in tail_pairs])

    # L1 from tail only
    l1_msgs_out = flatten_pairs(tail_pairs)

    # System blocks
    D = t(lang, 'divider')
    def build_system(core_text: str, tools_text: str, l3_text: str, l2_text: str) -> str:
        blocks: List[str] = [t(lang, 'instruction'), D, t(lang, 'core_profile'), core_text]
        if tools_text:
            blocks += [D, t(lang, 'tool_results'), tools_text]
        if l3_text:
            blocks += [D, t(lang, 'recap_l3'), l3_text]
        if l2_text:
            blocks += [D, t(lang, 'recap_l2'), l2_text]
        return "\n".join(b for b in blocks if b)

    tools_text = sanitize_for_memory(tools_src_txt[:tools_cap*4]) if tools_used > 0 and tools_src_txt else ''
    system_text = build_system(core_text_cur, tools_text, l3_txt, l2_txt)

    # Build token layers blocks for progressive counting
    def make_blocks() -> Dict[str, List[Dict[str,str]]]:
        blk: Dict[str, List[Dict[str,str]]] = {
            'system': [{'role':'system','content': system_text}],
            'l3': ([{'role':'system','content': l3_txt}] if l3_txt else []),
            'l2': ([{'role':'system','content': l2_txt}] if l2_txt else []),
            'l1': l1_msgs_out,
            'user': ([{'role':'user','content': current_user_text or ''}] if current_user_text is not None else []),
        }
        return blk

    blocks = make_blocks()
    breakdown = tokens_breakdown(model_id, blocks)

    def compute_free_out(total_prompt: int) -> int:
        C_eff = int(budgets.get('C_eff', 0))
        R_sys = int(budgets.get('R_sys', 0))
        safety = int(getattr(st, 'SAFETY_TOK', 64))
        return max(0, C_eff - total_prompt - R_sys - safety)

    free_out_cap = compute_free_out(int(breakdown['total']))

    compaction_steps: List[str] = []
    summary_created_l2 = 0
    summary_created_l3 = 0

    from packages.storage.repo import ensure_l2_for_pairs as repo_ensure_l2, promote_l2_to_l3 as repo_promote_l2

    # Helper refresh after changes
    def refresh_layers():
        nonlocal system_text, blocks, breakdown, free_out_cap, l2_txt, l3_txt, l1_msgs_out
        system_text = build_system(core_text_cur, tools_text, l3_txt, l2_txt)
        blocks = make_blocks()
        breakdown.update(tokens_breakdown(model_id, blocks))
        free_out_cap = compute_free_out(int(breakdown['total']))

    # Fill pct helpers
    def fill_pct(layer_tokens: int, cap: int) -> float:
        if cap <= 0:
            return 0.0
        return (layer_tokens / cap) * 100.0

    # Auto-compaction loop if недостаточно свободного выхода
    emergency_tail_min = int(getattr(st, 'L1_TAIL_EMERGENCY_PAIRS', 2))
    R_OUT_MIN = int(getattr(st, 'R_OUT_MIN', 256))
    L2_HIGH = int(getattr(st, 'L2_HIGH', 90))

    loop_guard = 20
    while free_out_cap < R_OUT_MIN and loop_guard > 0:
        loop_guard -= 1
        # Step 1: convert old_pairs to L2 summaries (if any)
        if old_pairs:
            ids = [(u.id, a.id) for (u,a) in old_pairs]
            created = repo_ensure_l2(thread_id, ids, lang, int(time.time()))
            if created:
                summary_created_l2 += created
                compaction_steps.append(f"l1_to_l2:{created}")
            old_pairs = []
            # Reload L2 text
            with session_scope() as s:
                l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
            l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
            refresh_layers()
            if free_out_cap >= R_OUT_MIN:
                break
            continue
        # Step 2: promote L2->L3 if over high watermark
        l2_tokens_layer = int(breakdown['l2'])
        if fill_pct(l2_tokens_layer, L2_cap) > L2_HIGH:
            with session_scope() as s:
                l2_oldest = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.asc()))
            if l2_oldest:
                ids2 = [x.id for x in l2_oldest[:5]]
                made = repo_promote_l2(thread_id, ids2, lang, int(time.time()))
                if made:
                    summary_created_l3 += made
                    compaction_steps.append(f"l2_to_l3:{len(ids2)}")
                with session_scope() as s:
                    l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
                    l3_items_db = list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()))
                l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
                l3_txt = "\n".join([f"• {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l3_items_db])
                refresh_layers()
                if free_out_cap >= R_OUT_MIN:
                    break
                continue
        # Step 3: shrink tail to emergency if still not enough
        if len(tail_pairs) > emergency_tail_min:
            keep = tail_pairs[-emergency_tail_min:]
            removed = tail_pairs[:-emergency_tail_min]
            if removed:
                ids3 = [(u.id, a.id) for (u,a) in removed]
                created2 = repo_ensure_l2(thread_id, ids3, lang, int(time.time()))
                if created2:
                    summary_created_l2 += created2
            tail_pairs = keep
            l1_msgs_out = flatten_pairs(tail_pairs)
            compaction_steps.append(f"tail_reduce:{len(removed)+len(keep)}→{emergency_tail_min}")
            # reload L2 after ensuring
            with session_scope() as s:
                l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
            l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
            refresh_layers()
            if free_out_cap >= R_OUT_MIN:
                break
            continue
        # Step 4: drop tools
        if tools_text:
            tools_text = ''
            compaction_steps.append("drop_tools")
            refresh_layers()
            if free_out_cap >= R_OUT_MIN:
                break
            continue
        # Step 5: shrink core
        min_core = int(getattr(st, 'context_min_core_skeleton_tok', 60))
        if approx_tokens(core_text_cur) > min_core:
            # heuristic char cap (4 chars/token)
            core_text_cur = core_text_cur[:min_core*4]
            compaction_steps.append("shrink_core")
            refresh_layers()
            if free_out_cap >= R_OUT_MIN:
                break
            continue
        # Step 6: current_user_only_mode
        l1_msgs_out.clear(); l2_txt=''; l3_txt=''; tools_text=''
        compaction_steps.append("current_user_only_mode")
        refresh_layers()
        break

    # Final stats
    # Fill pct for layers (tokens vs caps)
    def pct(tok: int, cap: int) -> int:
        return int(round((tok / cap) * 100)) if cap > 0 else 0
    fill_pct_map = {
        'l1': pct(int(breakdown['l1']), L1_cap),
        'l2': pct(int(breakdown['l2']), L2_cap),
        'l3': pct(int(breakdown['l3']), L3_cap),
    }

    stats = {
        'order': ["core","tools","l3","l2","l1"],
        'tokens': {
            'core': approx_tokens(core_text_cur),
            'tools': approx_tokens(tools_text) if tools_text else 0,
            'l3': int(breakdown['l3']),
            'l2': int(breakdown['l2']),
            'l1': int(breakdown['l1']),
            'total_in': int(breakdown['total']),
        },
        'caps': {
            'core_cap': core_cap,
            'tools_cap': tools_cap,
            'l1': L1_cap,
            'l2': L2_cap,
            'l3': L3_cap,
        },
        'budget': { k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens') },
        'squeezes': [],
        'squeezed': False,
        'current_user_tokens': approx_tokens(current_user_text or ''),
        'current_user_only_mode': ('current_user_only_mode' in compaction_steps),
        'original_current_user_tokens': approx_tokens(current_user_text or ''),
        'free_out_cap': free_out_cap,
        'l1_order': 'chronological',
        'tail_pairs': len(tail_pairs),
        'l1_pairs_count': len(tail_pairs),
        'compaction_steps': compaction_steps,
        'prompt_tokens_precise': int(breakdown['total']),
        'token_count_mode': breakdown['token_count_mode'],
    }
    stats['fill_pct'] = fill_pct_map
    stats['free_pct'] = { k: 100 - v for k,v in fill_pct_map.items() }

    last_assistant = next((m for m in reversed(l1_msgs_out) if m["role"]=="assistant"), None)
    if last_assistant:
        stats['last_assistant_before_user'] = { 'message_id': last_assistant.get('id'), 'preview': (last_assistant.get('content') or '')[:160] }
    else:
        stats['last_assistant_before_user'] = None

    logger.debug("compact.steps: %s", compaction_steps)

    return {
        'system_text': system_text,
        'messages': l1_msgs_out,
        'stats': stats,
        'context_budget': budgets,
    }
