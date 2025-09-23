from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.memory_manager import compute_level_caps, build_l1_pairs
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
    *,
    thread_id: str,
    model_id: str,
    max_output_tokens: Optional[int],
    tool_results_text: Optional[str],
    tool_results_tokens: Optional[int],
    last_user_lang: Optional[str],
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
    caps = compute_level_caps(work_left, tools_used)

    # L3 bullets newest first
    l3_txt = ''
    l3_tok = 0
    with session_scope() as s:
        l3_items = list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()))
    if l3_items and caps['l3'] > 0:
        bullets = [f"• {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l3_items]
        acc: List[str] = []
        for b in bullets:
            if approx_tokens("\n".join(acc + [b])) > caps['l3']:
                break
            acc.append(b)
        l3_txt = "\n".join(acc)
        l3_tok = approx_tokens(l3_txt)

    # L2 bullets newest first
    l2_txt = ''
    l2_tok = 0
    with session_scope() as s:
        l2_items = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
    if l2_items and caps['l2'] > 0:
        bullets2 = [f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items]
        acc2: List[str] = []
        for b in bullets2:
            if approx_tokens("\n".join(acc2 + [b])) > caps['l2']:
                break
            acc2.append(b)
        l2_txt = "\n".join(acc2)
        l2_tok = approx_tokens(l2_txt)

    # L1 pairs with secondary caps
    from packages.storage.repo import get_messages_since, get_or_create_memory_state
    state = get_or_create_memory_state(thread_id)
    msgs = get_messages_since(thread_id, state.last_compacted_message_id)
    pairs, _ = build_l1_pairs(msgs)

    cap_user = getattr(st, 'cap_tok_user', 120)
    cap_asst = getattr(st, 'cap_tok_assistant', 80)
    l1_msgs: List[Dict[str, str]] = []
    l1_tok = 0
    lang_ru = (lang == 'ru')
    for (u,a) in reversed(pairs):
        u_txt = u
        a_txt = a
        if approx_tokens(u_txt) > cap_user:
            u_txt = _cap_text_by_tokens(u_txt, cap_user)
        if approx_tokens(a_txt) > cap_asst:
            a_txt = _cap_text_by_tokens(_condense_assistant(a_txt, lang_ru), cap_asst)
        pair_tok = approx_tokens(u_txt) + approx_tokens(a_txt)
        if l1_tok + pair_tok > caps['l1']:
            a_txt2 = _cap_text_by_tokens(_condense_assistant(a_txt, lang_ru), max(20, int(cap_asst/2)))
            pair_tok = approx_tokens(u_txt) + approx_tokens(a_txt2)
            if l1_tok + pair_tok > caps['l1']:
                continue
            a_txt = a_txt2
        l1_msgs.append({'role': 'user', 'content': sanitize_for_memory(u_txt)})
        l1_msgs.append({'role': 'assistant', 'content': sanitize_for_memory(a_txt)})
        l1_tok += approx_tokens(u_txt) + approx_tokens(a_txt)

    # Localized single system_text
    D = t(lang, 'divider')
    blocks: List[str] = [
        t(lang, 'instruction'),
        D,
        t(lang, 'core_profile'),
        core_text_full,
    ]
    if tools_used > 0 and tools_src_txt:
        blocks += [D, t(lang, 'tool_results'), sanitize_for_memory(tools_src_txt[:tools_cap*4])]
    if l3_tok > 0:
        blocks += [D, t(lang, 'recap_l3'), l3_txt]
    if l2_tok > 0:
        blocks += [D, t(lang, 'recap_l2'), l2_txt]
    system_text = "\n".join(b for b in blocks if b)

    # Dry token accounting by final text
    core_tok = approx_tokens(core_text_full)
    tools_tok = approx_tokens(tools_src_txt[:tools_cap*4]) if tools_used > 0 and tools_src_txt else 0
    total_in = core_tok + tools_tok + l3_tok + l2_tok + l1_tok + approx_tokens(current_user_text or "")

    squeezed: List[str] = []
    B_total_in = int(budgets.get('B_total_in', 0))

    def total() -> int:
        return core_tok + tools_tok + l3_tok + l2_tok + l1_tok + approx_tokens(current_user_text or "")

    # Squeeze in order
    def drop_oldest_L1() -> bool:
        nonlocal l1_msgs, l1_tok
        i = len(l1_msgs) - 1
        changed = False
        while i >= 1 and total() > B_total_in:
            if l1_msgs[i]['role'] == 'assistant' and l1_msgs[i-1]['role'] == 'user':
                tok = approx_tokens(l1_msgs[i]['content']) + approx_tokens(l1_msgs[i-1]['content'])
                l1_msgs.pop(i); l1_msgs.pop(i-1)
                l1_tok -= tok
                squeezed.append(f"drop_l1:{tok}")
                changed = True
                i -= 2
            else:
                i -= 1
        return changed

    def drop_oldest_L2() -> bool:
        nonlocal l2_txt, l2_tok
        if not l2_txt:
            return False
        lines = l2_txt.splitlines()
        if not lines:
            return False
        last = lines.pop()
        rm = approx_tokens(last)
        l2_txt = "\n".join(lines)
        l2_tok -= rm
        squeezed.append(f"drop_l2:{rm}")
        return True

    def shrink_assistant_in_L1() -> bool:
        nonlocal l1_msgs, l1_tok
        changed = False
        for i in range(len(l1_msgs)-1, -1, -1):
            if l1_msgs[i]['role'] == 'assistant':
                before = approx_tokens(l1_msgs[i]['content'])
                new_txt = _cap_text_by_tokens(_condense_assistant(l1_msgs[i]['content'], lang_ru), max(20, int(cap_asst/2)))
                after = approx_tokens(new_txt)
                if after < before:
                    l1_tok -= (before - after)
                    l1_msgs[i]['content'] = new_txt
                    squeezed.append(f"shrink_l1_asst:{before-after}")
                    changed = True
                if total() <= B_total_in:
                    break
        return changed

    def shrink_user_in_L1() -> bool:
        nonlocal l1_msgs, l1_tok
        changed = False
        for i in range(len(l1_msgs)-1, -1, -1):
            if l1_msgs[i]['role'] == 'user':
                before = approx_tokens(l1_msgs[i]['content'])
                new_txt = _cap_text_by_tokens(l1_msgs[i]['content'], max(40, int(cap_user/2)))
                after = approx_tokens(new_txt)
                if after < before:
                    l1_tok -= (before - after)
                    l1_msgs[i]['content'] = new_txt
                    squeezed.append(f"shrink_l1_user:{before-after}")
                    changed = True
                if total() <= B_total_in:
                    break
        return changed

    def drop_tools() -> bool:
        nonlocal tools_tok, tools_used, tools_src_txt
        if tools_used <= 0 or not tools_src_txt:
            return False
        rm = min(tools_tok, max(0, total() - B_total_in))
        if rm <= 0:
            return False
        keep_tok = max(0, tools_tok - rm)
        tools_src_txt = tools_src_txt[:keep_tok * 4]
        tools_tok = approx_tokens(tools_src_txt)
        tools_used = tools_tok
        squeezed.append(f"drop_tools:{rm}")
        return True

    def shrink_core_to_min() -> bool:
        nonlocal core_tok
        min_core = int(st.context_min_core_skeleton_tok)
        if core_tok <= min_core:
            return False
        core_tok = min_core
        squeezed.append(f"shrink_core:{min_core}")
        return True

    if total_in > B_total_in:
        if drop_oldest_L1() and total() <= B_total_in:
            pass
        if total() > B_total_in and drop_oldest_L2() and total() <= B_total_in:
            pass
        if total() > B_total_in and shrink_assistant_in_L1() and total() <= B_total_in:
            pass
        if total() > B_total_in and shrink_user_in_L1() and total() <= B_total_in:
            pass
        if total() > B_total_in and drop_tools() and total() <= B_total_in:
            pass
        if total() > B_total_in:
            shrink_core_to_min()

    stats = {
        'order': ["core","tools","l3","l2","l1"],
        'tokens': { 'core': core_tok, 'tools': tools_tok, 'l3': l3_tok, 'l2': l2_tok, 'l1': l1_tok },
        'caps':   { 'core_cap': core_cap, 'tools_cap': tools_cap, 'l1': caps['l1'], 'l2': caps['l2'], 'l3': caps['l3'] },
        'budget': { k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens') },
        'squeezes': squeezed,
        'squeezed': bool(len(squeezed) > 0),
    }

    return { 'system_text': system_text, 'messages': l1_msgs, 'stats': stats, 'context_budget': budgets }
