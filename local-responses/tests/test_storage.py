# tests/test_storage.py
from __future__ import annotations

from packages.storage.repo import create_thread, append_message, get_thread, session_scope
from packages.storage.models import Thread, Message, Response


def test_thread_create_and_cascade() -> None:
    th = create_thread("Title")
    assert th.id

    append_message(th.id, "user", "hi")
    append_message(th.id, "assistant", "hello")

    # Ensure messages exist
    with session_scope() as s:
        msgs = s.query(Message).filter(Message.thread_id == th.id).all()
        assert len(msgs) == 2

    # Delete thread and check cascade
    with session_scope() as s:
        t = s.get(Thread, th.id)
        s.delete(t)
        s.commit()
        msgs = s.query(Message).filter(Message.thread_id == th.id).all()
        assert len(msgs) == 0
