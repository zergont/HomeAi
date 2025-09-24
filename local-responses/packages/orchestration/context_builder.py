from __future__ import annotations

import math, time, logging
from typing import Any, Dict, List, Optional, Tuple

from packages.core.settings import get_settings
from packages.orchestration.budget import compute_budgets
from packages.orchestration.redactor import sanitize_for_memory
from packages.storage.repo import get_profile, session_scope
from packages.storage.models import L2Summary, L3MicroSummary, Message
from packages.utils.tokens import approx_tokens, profile_text_view
from packages.utils.i18n import pick_lang, t
from packages.providers import lmstudio_tokens

logger = logging.getLogger("app.context")


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


def build_pairs(msgs: List[Message]) -> List[Tuple[Message, Message]]:
    """Build user→assistant pairs in chronological order (ASC). Only valid u→a pairs."""
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


def _flatten_tail_pairs(tail_pairs: List[Tuple[Message, Message]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for (u, a) in tail_pairs:
        out.append({'role': 'user', 'content': sanitize_for_memory(u.content or '')})
        out.append({'role': 'assistant', 'content': sanitize_for_memory(a.content or '')})
    return out


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

    # L3 newest first from DB
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

    # L2 newest first from DB
    l2_txt = ''
    with session_scope() as s:
        l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
    if l2_items_db and L2_cap > 0:
        bullets2 = [f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db]
        acc2: List[str] = []
        for b in bullets2:
            if approx_tokens("\n".join(acc2 + [b])) > L2_cap:
                break
            acc2.append(b)
        l2_txt = "\n".join(acc2)

    # Build full message history ASC (user/assistant only)
    with session_scope() as s:
        history_msgs = [m for m in s.query(Message).filter(Message.thread_id == thread_id).order_by(Message.created_at.asc()) if m.role in ("user","assistant")]
    pairs_all = build_pairs(history_msgs)
    tail_n = min(int(getattr(st, 'L1_TAIL_UNCLIPPED_PAIRS', 4)), len(pairs_all))
    tail_pairs = pairs_all[-tail_n:]
    old_pairs = pairs_all[:-tail_n]

    # DEBUG logs
    logger.debug("L1.build: total_pairs=%d, tail_pairs=%d, old_pairs=%d", len(pairs_all), len(tail_pairs), len(old_pairs))
    def _short(mid: str) -> str:
        return (mid or '')[-6:]
    logger.debug("L1.tail.ids: %s", [f"u#{_short(u.id)}->a#{_short(a.id)}" for (u,a) in tail_pairs])

    # Compose L1 from tail only (ASC)
    l1_msgs_out: List[Dict[str, str]] = _flatten_tail_pairs(tail_pairs)

    # Build system and tokens
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

    def recalc_tokens() -> Dict[str, int]:
        core_tok = approx_tokens(core_text_cur)
        tools_tok = approx_tokens(tools_text) if tools_text else 0
        l3_tok = approx_tokens(l3_txt) if l3_txt else 0
        l2_tok = approx_tokens(l2_txt) if l2_txt else 0
        l1_tok = sum(approx_tokens(m['content']) for m in l1_msgs_out)
        current_user_tokens = approx_tokens(current_user_text or "")
        total_in = core_tok + tools_tok + l3_tok + l2_tok + l1_tok + current_user_tokens
        return {'core_tok': core_tok, 'tools_tok': tools_tok, 'l3_tok': l3_tok, 'l2_tok': l2_tok, 'l1_tok': l1_tok, 'current_user_tokens': current_user_tokens, 'total_in': total_in}

    def preflight_and_adjust(messages_for_provider: List[Dict[str,str]]) -> Optional[int]:
        try:
            mid = model_id.split(":", 1)[1] if model_id.startswith("lm:") else model_id
            n = lmstudio_tokens.count_tokens_chat(mid, messages_for_provider, getattr(st, 'TOKEN_CACHE_TTL_SEC', 300))
            C_eff = int(budgets.get('C_eff', 0)); R_sys = int(budgets.get('R_sys', 0)); Safety = int(budgets.get('Safety', 0))
            requested = int(max_output_tokens or getattr(st, 'ctx_rout_default', 512))
            eff_out = max(0, min(requested, C_eff - n - R_sys - Safety))
            budgets['effective_max_output_tokens'] = eff_out
            return int(n)
        except Exception:
            return None

    toks = recalc_tokens()
    messages_for_provider = ([{"role": "system", "content": system_text}] + l1_msgs_out + [{"role": "user", "content": current_user_text or ''}])
    prompt_tokens_precise = preflight_and_adjust(messages_for_provider)
    token_count_mode = 'proxy' if prompt_tokens_precise is not None else None

    def overflows(total_in: int) -> bool:
        return total_in + int(budgets.get('R_out', 0)) + int(budgets.get('R_sys', 0)) + int(budgets.get('Safety', 0)) > int(budgets.get('C_eff', 0))

    def rebuild():
        nonlocal system_text, messages_for_provider, toks
        system_text = build_system(core_text_cur, tools_text, l3_txt, l2_txt)
        messages_for_provider = ([{"role": "system", "content": system_text}] + l1_msgs_out + [{"role": "user", "content": current_user_text or ''}])
        toks.update(recalc_tokens())

    compaction_steps: List[str] = []
    summary_created_l2 = 0
    summary_created_l3 = 0
    now_ts = int(time.time())

    # Helpers to call repo ops
    from packages.storage.repo import ensure_l2_for_pairs as repo_ensure_l2, promote_l2_to_l3 as repo_promote_l2

    max_cycles = 15
    while overflows(toks['total_in']) and max_cycles > 0:
        max_cycles -= 1
        # 1) Ensure L2 for old_pairs (do not touch tail)
        if old_pairs:
            ids = [(u.id, a.id) for (u, a) in old_pairs]
            created = repo_ensure_l2(thread_id, ids, lang, now_ts)
            if created:
                summary_created_l2 += created
                compaction_steps.append(f"l1_to_l2:{created}")
            # refresh L2 view
            with session_scope() as s:
                l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
            l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
            old_pairs = []
            rebuild()
            prompt_tokens_precise = preflight_and_adjust(messages_for_provider) or prompt_tokens_precise
            if not overflows(toks['total_in']):
                break
            continue
        # 2) Promote L2 -> L3 (oldest first)
        with session_scope() as s:
            l2_oldest = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.asc()))
        if l2_oldest:
            ids2 = [x.id for x in l2_oldest[:5]]
            made = repo_promote_l2(thread_id, ids2, lang, now_ts)
            if made:
                summary_created_l3 += made
                compaction_steps.append(f"l2_to_l3:{len(ids2)}")
            # refresh L2/L3 views
            with session_scope() as s:
                l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
                l3_items = list(s.query(L3MicroSummary).filter(L3MicroSummary.thread_id == thread_id).order_by(L3MicroSummary.id.desc()))
            l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
            l3_txt = "\n".join([f"• {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l3_items])
            rebuild()
            prompt_tokens_precise = preflight_and_adjust(messages_for_provider) or prompt_tokens_precise
            if not overflows(toks['total_in']):
                break
            continue
        # 3) Reduce tail to fallback (move removed pairs to L2)
        fallback = int(getattr(st, 'L1_TAIL_FALLBACK_PAIRS', 2))
        if len(tail_pairs) > fallback:
            keep = tail_pairs[-fallback:]
            removed = tail_pairs[:-fallback]
            if removed:
                ids3 = [(u.id, a.id) for (u, a) in removed]
                created2 = repo_ensure_l2(thread_id, ids3, lang, now_ts)
                if created2:
                    summary_created_l2 += created2
            tail_pairs = keep
            compaction_steps.append(f"tail_reduce:{len(removed)+len(keep)}→{fallback}")
            l1_msgs_out = _flatten_tail_pairs(tail_pairs)
            # refresh L2 view
            with session_scope() as s:
                l2_items_db = list(s.query(L2Summary).filter(L2Summary.thread_id == thread_id).order_by(L2Summary.id.desc()))
            l2_txt = "\n".join([f"- {sanitize_for_memory(x.text or '').splitlines()[0][:200]}" for x in l2_items_db])
            rebuild()
            prompt_tokens_precise = preflight_and_adjust(messages_for_provider) or prompt_tokens_precise
            if not overflows(toks['total_in']):
                break
            continue
        # 4) Drop tools
        if tools_text:
            tools_text = ''
            compaction_steps.append("drop_tools")
            rebuild()
            prompt_tokens_precise = preflight_and_adjust(messages_for_provider) or prompt_tokens_precise
            if not overflows(toks['total_in']):
                break
            continue
        # 5) Shrink core
        min_core = int(getattr(st, 'context_min_core_skeleton_tok', 60))
        if approx_tokens(core_text_cur) > min_core:
            core_text_cur = _cap_text_by_tokens(core_text_cur, min_core)
            compaction_steps.append("shrink_core")
            rebuild()
            prompt_tokens_precise = preflight_and_adjust(messages_for_provider) or prompt_tokens_precise
            if not overflows(toks['total_in']):
                break
            continue
        # 6) current_user_only_mode
        l1_msgs_out.clear(); l2_txt = ''; l3_txt = ''; tools_text = ''
        compaction_steps.append("current_user_only_mode")
        rebuild()
        break

    # finalize tokens and stats
    toks = recalc_tokens()
    budget_view = { k: budgets.get(k) for k in ('C_eff','R_out','R_sys','Safety','B_total_in','B_work','core_sys_pad','core_reserved','effective_max_output_tokens') }
    free_out_cap = max(0, int(budgets.get('C_eff', 0)) - toks['total_in'] - int(budgets.get('R_sys', 0)) - int(budgets.get('Safety', 0)))

    def _pct(used: int, cap: int) -> int:
        return int(round(100 * used / cap)) if cap > 0 else 0

    stats = {
        'order': ["core","tools","l3","l2","l1"],
        'tokens': { 'core': toks['core_tok'], 'tools': toks['tools_tok'], 'l3': toks['l3_tok'], 'l2': toks['l2_tok'], 'l1': toks['l1_tok'], 'total_in': toks['total_in'] },
        'caps':   { 'core_cap': core_cap, 'tools_cap': tools_cap, 'l1': L1_cap, 'l2': L2_cap, 'l3': L3_cap },
        'budget': budget_view,
        'squeezes': [],
        'squeezed': False,
        'current_user_tokens': toks['current_user_tokens'],
        'current_user_only_mode': ('current_user_only_mode' in compaction_steps),
        'original_current_user_tokens': toks['current_user_tokens'],
        'free_out_cap': free_out_cap,
        'l1_order': 'chronological',
        'tail_pairs': len(tail_pairs),
        'compaction_steps': compaction_steps,
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
    if prompt_tokens_precise is not None:
        stats['prompt_tokens_precise'] = int(prompt_tokens_precise)
        stats['token_count_mode'] = token_count_mode or 'proxy'

    last_assistant = next((m for m in reversed(l1_msgs_out) if m["role"]=="assistant"), None)
    if last_assistant:
        prev = (last_assistant.get("content") or "")[:160]
        stats["last_assistant_before_user"] = {"message_id": last_assistant.get("id"), "preview": prev}
    else:
        stats["last_assistant_before_user"] = None

    logger.debug("compact.steps: %s", compaction_steps)

    return { 'system_text': system_text, 'messages': l1_msgs_out, 'stats': stats, 'context_budget': budgets }
