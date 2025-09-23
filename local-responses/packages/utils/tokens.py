# packages/utils/tokens.py
from __future__ import annotations

import math
from typing import Any, Dict


def approx_tokens(text: str) -> int:
    """Rough token estimate: 1 token ~= 4 chars."""
    return int(math.ceil(len(text or "") / 4))


def profile_text_view(profile: Dict[str, Any]) -> str:
    """Build a normalized textual representation of profile for core token counting.

    CoT/svc blocks should be removed by redactor at output stage. Here we just build
    human-readable, stable text.
    """
    lines = []
    add = lines.append
    def norm(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json_dumps(v)
        return str(v)

    # Ordered sections
    add(f"Name: {norm(profile.get('display_name'))}")
    add(f"Language: {norm(profile.get('preferred_language'))}")
    add(f"Tone: {norm(profile.get('tone'))}")
    add(f"Timezone: {norm(profile.get('timezone'))}")
    add(f"Region: {norm(profile.get('region_coarse'))}")
    add(f"WorkHours: {norm(profile.get('work_hours'))}")
    add(f"UI: {norm(profile.get('ui_format_prefs'))}")
    add(f"Goals/Mood: {norm(profile.get('goals_mood'))}")
    add(f"Decisions/Tasks: {norm(profile.get('decisions_tasks'))}")
    add(f"Brevity: {norm(profile.get('brevity'))}")
    add(f"FormatDefaults: {norm(profile.get('format_defaults'))}")
    add(f"Interests: {norm(profile.get('interests_topics'))}")
    add(f"WorkflowTools: {norm(profile.get('workflow_tools'))}")
    add(f"OS: {norm(profile.get('os'))}")
    add(f"Runtime: {norm(profile.get('runtime'))}")
    add(f"HW: {norm(profile.get('hardware_hint'))}")
    text = "\n".join(lines).strip()
    return text


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
