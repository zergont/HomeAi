# tests/test_context_budgeter.py
from __future__ import annotations

from packages.storage.repo import create_thread, append_message
from packages.orchestration.context_manager import build_context


def test_context_budgeter_limits_tokens() -> None:
    th = create_thread(None)
    # Create many messages to exceed budget
    for i in range(50):
        append_message(th.id, "user", f"msg {i} " + ("x" * 50))
        append_message(th.id, "assistant", f"resp {i} " + ("y" * 50))

    ctx = build_context(th.id)
    total_chars = len(ctx["system"]) + sum(len(m["content"]) for m in ctx["messages"])
    # Roughly should be within ~4*budget chars; allow slack
    from packages.core.settings import get_settings

    budget = get_settings().ctx_max_input_tokens
    assert total_chars <= budget * 6


def test_context_uses_summary_if_present() -> None:
    th = create_thread(None)
    append_message(th.id, "user", "hello")
    # simulate stored summary
    from packages.storage.repo import update_thread_summary

    update_thread_summary(th.id, "This is summary")

    ctx = build_context(th.id)
    assert ctx["system"].startswith("This is summary")
