# packages/providers/base.py
from __future__ import annotations

from typing import Any, Protocol, Tuple


class Provider(Protocol):
    async def generate(
        self,
        *,
        system: str | None,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, Any] | None]:
        """Generate assistant text and optional usage.

        Returns (text, usage_dict_or_none)
        usage format example: {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
        """
        ...
