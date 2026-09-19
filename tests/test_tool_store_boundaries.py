"""工具执行器剩余同步 Store/Memory 边界的线程与心跳回归。"""

import asyncio
import threading
import time
from types import SimpleNamespace

from agent.handler import MessageHandler


def _heartbeat_runner(coro):
    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await coro
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return result, ticks

    return asyncio.run(scenario())


def test_correct_memory_store_work_is_off_event_loop():
    class Memory:
        def __init__(self):
            self.thread_ids = []

        def correct_memory(self, *_args, **_kwargs):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.08)
            return {
                "matched": 1, "retracted": 1, "superseded": 0,
                "notes_dirty": False,
            }

    memory = Memory()
    handler = object.__new__(MessageHandler)
    handler.owner_qq = "owner"
    handler.memory = memory
    handler.embed_engine = None
    main_thread = threading.get_ident()

    result, ticks = _heartbeat_runner(handler._execute_tool(
        "correct_memory",
        {"subject_qq": "owner", "wrong_fact": "旧事实"},
        scope_id="_private_owner",
        current_user="owner",
        turn_actions={},
    ))

    assert "已忘掉" in result
    assert ticks > 0
    assert memory.thread_ids and all(tid != main_thread for tid in memory.thread_ids)


def test_set_reminder_task_store_work_is_off_event_loop():
    class TaskManager:
        def __init__(self):
            self.thread_ids = []

        def add(self, **_kwargs):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.08)
            return 123

    task_manager = TaskManager()
    handler = object.__new__(MessageHandler)
    handler.task_manager = task_manager
    handler._allowed_groups = set()
    main_thread = threading.get_ident()

    result, ticks = _heartbeat_runner(handler._execute_tool(
        "set_reminder",
        {"time": "5分钟", "description": "喝水"},
        scope_id="_private_owner",
        current_user="owner",
        action_source_id="private:1",
    ))

    assert "任务#123" in result
    assert ticks > 0
    assert task_manager.thread_ids and all(
        tid != main_thread for tid in task_manager.thread_ids
    )


def test_relation_nickname_resolution_is_off_event_loop():
    class Store:
        def __init__(self):
            self.thread_ids = []

        def find_qq_by_nickname(self, _name, fuzzy=False):
            assert fuzzy is False
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.08)
            return "30001"

        def find_qq_by_alias(self, _name, fuzzy=False):
            assert fuzzy is False
            self.thread_ids.append(threading.get_ident())
            return ""

    class Memory:
        def __init__(self, store):
            self.store = store
            self.relation_threads = []

        def search_relation_triples(self, *_args, **_kwargs):
            self.relation_threads.append(threading.get_ident())
            time.sleep(0.08)
            return ["小红是朋友"]

    store = Store()
    memory = Memory(store)
    handler = object.__new__(MessageHandler)
    handler.owner_qq = "owner"
    handler.memory = memory
    main_thread = threading.get_ident()

    result, ticks = _heartbeat_runner(handler._execute_tool(
        "search_relations",
        {"name": "小红"},
        scope_id="_private_owner",
        current_user="owner",
    ))

    assert "小红是朋友" in result
    assert ticks > 0
    assert store.thread_ids and all(tid != main_thread for tid in store.thread_ids)
    assert memory.relation_threads and all(
        tid != main_thread for tid in memory.relation_threads
    )
