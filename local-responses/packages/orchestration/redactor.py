# packages/orchestration/redactor.py
from __future__ import annotations

import re
from typing import Any, Dict


# Only strip <think>...</think> blocks (Chain-of-Thought). Nothing else.
_THINK_RX = re.compile(r"(?is)<think>.*?</think>")
_JSON_RX = re.compile(r"(?is)\{\s*\"tool.*?\}\s*$")


def redact_fragment(text: str) -> str:
    """Redact verbose internal reasoning blocks from model output.

    - Removes only <think>...</think>
    - Preserves original line breaks
    """
    if not text:
        return text
    cleaned = _THINK_RX.sub("", text)
    return cleaned


def sanitize_for_memory(text: str) -> str:
    """Sanitize text for L2/L3 memory: strip CoT and trailing tool/service JSON blobs."""
    if not text:
        return text or ""
    t = redact_fragment(text)
    t = _JSON_RX.sub("", t).strip()
    return t


def safe_profile_output(data: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize profile dict for output: remove any CoT/service blocks in strings.
    We only apply redact_fragment to string fields, leaving structure intact.
    """
    out: Dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = redact_fragment(v)
        else:
            out[k] = v
    return out
