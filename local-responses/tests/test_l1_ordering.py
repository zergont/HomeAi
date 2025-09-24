import pytest
from packages.orchestration.context_builder import assemble_context
from packages.storage.repo import session_scope, Message, create_thread
import time

@pytest.mark.asyncio
async def test_l1_ordering():
    # Подготовка: u1,a1,u2,a2 (старые→новые)
    with session_scope() as s:
        th = create_thread(title=None)
        thread_id = th.id
        now = int(time.time())
        m1 = Message(thread_id=thread_id, role="user", content="u1", created_at=now)
        m2 = Message(thread_id=thread_id, role="assistant", content="a1", created_at=now+1)
        m3 = Message(thread_id=thread_id, role="user", content="u2", created_at=now+2)
        m4 = Message(thread_id=thread_id, role="assistant", content="a2", created_at=now+3)
        s.add_all([m1, m2, m3, m4])
        s.commit()
    assembled = await assemble_context(thread_id, 'lm:qwen/qwen3-14b', max_output_tokens=128, tool_results_text=None, tool_results_tokens=None, last_user_lang='en', current_user_text="Q?")
    msgs = assembled["messages"]
    # Проверяем порядок: u1,a1,u2,a2
    assert [m["content"] for m in msgs] == ["u1","a1","u2","a2"]
    # Последний перед текущим user — a2
    assert msgs[-1]["content"] == "a2"
