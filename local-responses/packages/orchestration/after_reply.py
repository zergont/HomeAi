from __future__ import annotations
from typing import List, Dict, Any
from packages.orchestration.token_budget import tokens_breakdown
from packages.core.settings import get_settings

async def normalize_after_reply(model_id: str, thread_id: str, system_msg: dict,
                                l3_msgs: List[Dict[str, Any]], l2_msgs: List[Dict[str, Any]],
                                l1_tail: List[Dict[str, Any]], current_user_msg: dict,
                                now: int, lang: str, repo, summarizer) -> dict:
    st = get_settings()
    steps: List[str] = []

    def blocks():
        return {
            'system': [system_msg] if system_msg else [],
            'l3': l3_msgs,
            'l2': l2_msgs,
            'l1': l1_tail,
            'user': [current_user_msg] if current_user_msg else []
        }

    br = tokens_breakdown(model_id, blocks())

    def pct(used, cap):
        return int(round(100 * used / cap)) if cap > 0 else 0

    def over(level, used_pct):
        H = {'l1': st.L1_HIGH, 'l2': st.L2_HIGH, 'l3': st.L3_HIGH}[level]
        return used_pct > H

    def under(level, used_pct):
        L = {'l1': st.L1_LOW, 'l2': st.L2_LOW, 'l3': st.L3_LOW}[level]
        return used_pct <= L

    # Helpers for token counts per level from breakdown
    def layer_tokens(name: str) -> int:
        return int(br.get(name, 0))

    # Need caps — caller should supply via repo or external context; assume repo has get_caps
    try:
        caps = repo.get_memory_caps(thread_id)  # must return dict l1,l2,l3
    except Exception:
        caps = {'l1': 1, 'l2': 1, 'l3': 1}

    guard = 0
    while guard < 10:
        guard += 1
        l1_pct = pct(layer_tokens('l1'), caps.get('l1', 1))
        l2_pct = pct(layer_tokens('l2'), caps.get('l2', 1))
        l3_pct = pct(layer_tokens('l3'), caps.get('l3', 1))
        did = False
        # L1 -> L2
        if over('l1', l1_pct):
            # Oldest pair from tail (first two entries user,assistant) -> L2 summary
            if len(l1_tail) >= 2 and l1_tail[0]['role'] == 'user' and l1_tail[1]['role'] == 'assistant':
                u = l1_tail[0]; a = l1_tail[1]
                try:
                    txt = await summarizer.summarize_pair_to_l2(u['content'], a['content'], lang)
                except Exception:
                    txt = f"- {u['content'][:120]} → {a['content'][:120]}"
                repo.insert_l2_summary(thread_id, u.get('id','u'), a.get('id','a'), txt, now)
                del l1_tail[0:2]
                steps.append('l1_to_l2:1')
                did = True
        # L2 -> L3
        elif over('l2', l2_pct):
            block = repo.pick_oldest_l2_block(thread_id, max_items=5)
            if block:
                try:
                    l3_txt = await summarizer.summarize_l2_block_to_l3([x.text for x in block], lang)
                except Exception:
                    l3_txt = '\n'.join([f"• {x.text.splitlines()[0][:160]}" for x in block[:2]])
                repo.insert_l3_summary(thread_id, [x.id for x in block], l3_txt, now)
                repo.delete_l2_batch([x.id for x in block])
                steps.append(f"l2_to_l3:{len(block)}")
                did = True
        # Evict L3 oldest
        elif over('l3', l3_pct):
            ev = repo.evict_l3_oldest(thread_id, count=3)
            if ev:
                steps.append(f"l3_evict:{ev}")
                did = True
        if not did:
            break
        br = tokens_breakdown(model_id, blocks())
        l1_pct = pct(layer_tokens('l1'), caps.get('l1', 1))
        l2_pct = pct(layer_tokens('l2'), caps.get('l2', 1))
        l3_pct = pct(layer_tokens('l3'), caps.get('l3', 1))
        if under('l1', l1_pct) and under('l2', l2_pct) and under('l3', l3_pct):
            break

    return {'compaction_steps_post': steps, 'tokens_breakdown_post': br, 'l1_tail': l1_tail}
