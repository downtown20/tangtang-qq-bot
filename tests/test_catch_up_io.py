"""离线补读的 SQLite 边界回归测试。"""

import asyncio
import threading
import time
from collections import deque
from types import SimpleNamespace

from agent.catch_up import CatchUpManager


def _manager(store, *, self_state=None):
    return CatchUpManager(
        store=store,
        short_term={"g1": deque()},
        llm_caller=lambda *_args, **_kwargs: None,
        config={"summarize_threshold": 30},
        get_allowed_groups=lambda: ["g1"],
        self_state=self_state,
    )


def test_catch_up_store_reads_do_not_block_event_loop():
    """补读的群消息计数/读取必须在线程边界执行。"""
    main_thread = threading.get_ident()
    calls = []

    class SlowStore:
        def get_group_message_count_since(self, group_id, since):
            calls.append(("count", threading.get_ident()))
            time.sleep(0.05)
            return 1

        def get_group_messages_since(self, group_id, since, limit):
            calls.append(("messages", threading.get_ident()))
            time.sleep(0.05)
            return [{"qq_id": "u1", "nickname": "小明", "message": "在吗", "timestamp": since}]

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            result = await _manager(SlowStore())._catch_up_group(
                "g1", "2026-08-30 12:00:00", "2026-08-30 13:00:00"
            )
        finally:
            stopped = True
            await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result and result.message_count == 1
    assert ticks > 0
    assert [name for name, _thread_id in calls] == ["count", "messages"]
    assert all(thread_id != main_thread for _name, thread_id in calls)


def test_missed_reply_memory_read_does_not_block_event_loop():
    """错过点名补回复读取记忆时也不能卡住其它消息。"""
    main_thread = threading.get_ident()
    calls = []

    class SlowStore:
        def query_memories(self, *args, **kwargs):
            calls.append(threading.get_ident())
            time.sleep(0.05)
            return [{"key": "like", "value": "草莓"}]

    rel = SimpleNamespace(
        closeness=0.5,
        familiarity_level="熟悉",
        my_feeling="相处舒服",
        learned=[],
        unfinished=[],
    )
    self_state = SimpleNamespace(
        relationships={"u1": rel},
        group_atmospheres={"g1": "热闹"},
    )

    async def llm(*_args, **_kwargs):
        await asyncio.sleep(0)
        return "看到啦～"

    manager = CatchUpManager(
        store=SlowStore(),
        short_term={"g1": deque()},
        llm_caller=llm,
        config={},
        get_allowed_groups=lambda: ["g1"],
        self_state=self_state,
    )

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            reply = await manager._generate_missed_reply(
                "g1",
                {"qq_id": "u1", "nickname": "小明", "message": "在吗", "timestamp": "12:00:00"},
                "大家聊了聊天",
            )
        finally:
            stopped = True
            await task
        return ticks, reply

    ticks, reply = asyncio.run(scenario())

    assert reply == "看到啦～"
    assert ticks > 0
    assert calls and all(thread_id != main_thread for thread_id in calls)
