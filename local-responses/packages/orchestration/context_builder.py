from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.redactor import sanitize_for_memory
from packages.storage.repo import get_profile, session_scope
from packages.storage.models import L2Summary, L3MicroSummary
from packages.utils.tokens import approx_tokens, profile_text_view
from packages.utils.i18n import pick_lang, t


def _cap_text_by_tokens(txt: str, cap: int) -> str:
    if cap <= 0:
        return ''
    if approx_tokens(txt) <= cap:
        return txt
    return txt[:cap * 4]


def _condense_assistant(text: str, lang_ru: bool) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ''
    first = lines[0][:400]
    numbers = ''
    for l in lines[1:5]:
        if any(ch.isdigit() for ch in l):
            numbers = l[:200]
            break
    return (first if not numbers else f"{first}\n{'Ключевые числа/параметры' if lang_ru else 'Key numbers/params'}: {numbers}")


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
    # track mutable core text used in system block
    core_text_cur = core_text_full
    core_tokens = approx_tokens(core_text_full)
    core_cap = int(math.ceil(core_tokens * 1.10))

    # Budgets from model window (R_out subtracted before B_work/core_reserved)
    budgets = await compute_budgets(model_id, max_output_tokens, core_tokens=core_tokens, core_cap=core_cap, settings=st)

    B_work = int(budgets.get('B_work', 0))
    # Tools caps and usage
    tools_cap = int(math.floor(min(st.mem_tools_max_share * B_work, B_work)))
    if tool_results_text and tool_results_tokens is None:
        tool_results_tokens = approx_tokens(tool_results_text)
    tools_src_txt = tool_results_text or ''
    tools_used = min(int(tool_results_tokens or approx_tokens(tools_src_txt)), tools_cap)

    # Distribute level caps from remaining work
    work_left = max(0, B_work - tools_used)
    L1_cap = int(math.floor(st.mem_l1_share * work_left))
    L2_cap = int(math.floor(st.mem_l2_share * work_left))
    L3_cap = int(math.floor(st.mem_l3_share * work_left))

    # L3 bullets newest first
    l3_txt = ''
    with session_scope() as s:
        l3_items = list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()))
    if l3_items and L3_cap > 0:
        bullets = [f"• {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l3_items]
        acc: List[str] = []
        for b in bullets:
            if approx_tokens("\n".join(acc + [b])) > L3_cap:
                break
            acc.append(b)
        l3_txt = "\n".join(acc)

    # L2 bullets newest first
    l2_txt = ''
    with session_scope() as s:
        l2_items = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
    if l2_items and L2_cap > 0:
        bullets2 = [f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items]
        acc2: List[str] = []
        for b in bullets2:
            if approx_tokens("\n".join(acc2 + [b])) > L2_cap:
                break
            acc2.append(b)
        l2_txt = "\n".join(acc2)

    # L1 pairs with secondary caps
    from packages.storage.repo import get_messages_since, get_or_create_memory_state
    state = get_or_create_memory_state(thread_id)
    msgs = get_messages_since(thread_id, state.last_compacted_message_id)
    # Build newest->oldest constrained by L1_cap
    cap_user = getattr(st, 'cap_tok_user', 120)
    cap_asst = getattr(st, 'cap_tok_assistant', 80)
    l1_msgs: List[Dict[str, str]] = []
    lang_ru = (lang == 'ru')
    def l1_total_tokens() -> int:
        return sum(approx_tokens(m['content']) for m in l1_msgs)
    i = len(msgs) - 1
    while i >= 1:
        if msgs[i].role == 'assistant' and msgs[i-1].role == 'user':
            u_txt = msgs[i-1].content or ''
            a_txt = msgs[i].content or ''
            if approx_tokens(u_txt) > cap_user:
                u_txt = _cap_text_by_tokens(u_txt, cap_user)
            if approx_tokens(a_txt) > cap_asst:
                a_txt = _cap_text_by_tokens(_condense_assistant(a_txt, lang_ru), cap_asst)
            # try fit into L1_cap
            if l1_total_tokens() + approx_tokens(u_txt) + approx_tokens(a_txt) > L1_cap:
                a_try = _cap_text_by_tokens(_condense_assistant(a_txt, lang_ru), max(20, int(cap_asst/2)))
                if l1_total_tokens() + approx_tokens(u_txt) + approx_tokens(a_try) <= L1_cap:
                    a_txt = a_try
                else:
                    i -= 2
                    continue
            l1_msgs.append({'role': 'user', 'content': sanitize_for_memory(u_txt)})
            l1_msgs.append({'role': 'assistant', 'content': sanitize_for_memory(a_txt)})
            i -= 2
        else:
            i -= 1
    l1_msgs_out = list(reversed(l1_msgs))  # хронологически: старые → новые

    # Build system and tokens
    D = t(lang, 'divider')
    def build_system(core_text: str, tools_text: str, l3_text: str, l2_text: str) -> str:
        blocks: List[str] = [
            t(lang, 'instruction'),
            D,
            t(lang, 'core_profile'),
            core_text,
        ]
        if tools_text:
            blocks += [D, t(lang, 'tool_results'), tools_text]
        if l3_text:
            blocks += [D, t(lang, 'recap_l3'), l3_text]
        if l2_text:
            blocks += [D, t(lang, 'recap_l2'), l2_text]
        return "\n".join(b for b in blocks if b)

    tools_text = sanitize_for_memory(tools_src_txt[:tools_cap*4]) if tools_used > 0 and tools_src_txt else ''
    system_text = build_system(core_text_cur, tools_text, l3_txt, l2_txt)

    # Token accounting by final text (без current user в компонентах, но добавим отдельно)
    core_tok = approx_tokens(core_text_cur)
    tools_tok = approx_tokens(tools_text) if tools_text else 0
    l3_tok = approx_tokens(l3_txt) if l3_txt else 0
    l2_tok = approx_tokens(l2_txt) if l2_txt else 0
    l1_tok = sum(approx_tokens(m['content']) for m in l1_msgs)
    current_user_tokens = approx_tokens(current_user_text or "")
    total_in = core_tok + tools_tok + l3_tok + l2_tok + l1_tok + current_user_tokens
    squeezed: List[str] = []
    B_total_in = int(budgets.get('B_total_in', 0))
    def total() -> int:
        return core_tok + tools_tok + l3_tok + l2_tok + l1_tok + current_user_tokens

    # Final budget view and free_out_cap (squeeze omitted for brevity)
    budget_view = { k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens') }
    free_out_cap = max(0, int(budgets.get('C_eff', 0)) - total_in - int(budgets.get('R_sys', 0)) - int(budgets.get('Safety', 0)))
    current_user_only_mode = False
    original_current_user_tokens = current_user_tokens

    def _pct(used: int, cap: int) -> int:
        return int(round(100 * used / cap)) if cap > 0 else 0

    stats = {
        'order': ["core","tools","l3","l2","l1"],
        'tokens': { 'core': core_tok, 'tools': tools_tok, 'l3': l3_tok, 'l2': l2_tok, 'l1': l1_tok, 'total_in': total_in },
        'caps':   { 'core_cap': core_cap, 'tools_cap': tools_cap, 'l1': L1_cap, 'l2': L2_cap, 'l3': L3_cap },
        'budget': budget_view,
        'squeezes': squeezed,
        'squeezed': bool(len(squeezed) > 0),
        'current_user_tokens': current_user_tokens,
        'current_user_only_mode': current_user_only_mode,
        'original_current_user_tokens': original_current_user_tokens,
        'free_out_cap': free_out_cap,
        'l1_order': 'chronological',
    }
    stats["fill_pct"] = {
        "l1": _pct(stats["tokens"]["l1"], stats["caps"]["l1"]),
        "l2": _pct(stats["tokens"]["l2"], stats["caps"]["l2"]),
        "l3": _pct(stats["tokens"]["l3"], stats["caps"]["l3"]),
    }
    stats["free_pct"] = {
        "l1": 100 - stats["fill_pct"]["l1"],
        "l2": 100 - stats["fill_pct"]["l2"],
        "l3": 100 - stats["fill_pct"]["l3"],
    }
    last_assistant = next((m for m in reversed(l1_msgs_out) if m["role"]=="assistant"), None)
    if last_assistant:
        prev = (last_assistant.get("content") or "")[:160]
        stats["last_assistant_before_user"] = {
            "message_id": last_assistant.get("id"),
            "preview": prev
        }
    else:
        stats["last_assistant_before_user"] = None

    return { 'system_text': system_text, 'messages': l1_msgs_out, 'stats': stats, 'context_budget': budgets }
