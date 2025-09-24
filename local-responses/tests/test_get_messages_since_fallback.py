import pytest
from packages.storage.repo import get_messages_since, session_scope, Message, create_thread
import time

@pytest.mark.asyncio
async def test_get_messages_since_fallback():
    with session_scope() as s:
        th = create_thread(title=None)
        thread_id = th.id
        now = int(time.time())
        m1 = Message(thread_id=thread_id, role="user", content="u1", created_at=now)
        m2 = Message(thread_id=thread_id, role="assistant", content="a1", created_at=now+1)
        s.add_all([m1, m2])
        s.commit()
    # last_id несуществующий
    msgs = get_messages_since(thread_id, last_id="nonexistent")
    # Должны вернуть все user/assistant
    assert [m.content for m in msgs] == ["u1", "a1"]
