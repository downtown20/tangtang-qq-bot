"""自然动作中记忆查询的事件循环边界回归测试。"""

import asyncio
import threading
import time
from types import SimpleNamespace

from agent.handler import MessageHandler
from agent.handler_autonomy import AutonomyMixin


def _run(coro):
    return asyncio.run(coro)


def test_natural_intimacy_stats_do_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    def slow_stats(_owner_id):
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return {
            "intimacy": 10,
            "intimacy_grade": "熟悉",
            "relationship": "朋友",
            "total_chats": 3,
            "memory_count": 2,
        }

    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(get_stats=slow_stats)
    handler._run_store_io = AutonomyMixin._run_store_io.__get__(handler)

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
            result = await handler._execute_natural_action(
                {"action": "intimacy"}, "u1",
            )
        finally:
            stopped = True
            await task
        return ticks, result

    ticks, result = _run(scenario())

    assert "羁绊" in result
    assert ticks > 0
    assert calls and all(thread_id != main_thread for thread_id in calls)


def test_natural_memory_recall_does_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    def slow_recall(_owner_id, *, limit=10, source_group_id=None):
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return "记得草莓"

    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(recall_formatted=slow_recall)
    handler._run_store_io = AutonomyMixin._run_store_io.__get__(handler)

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
            result = await handler._execute_natural_action(
                {"action": "memory"}, "u1",
            )
        finally:
            stopped = True
            await task
        return ticks, result

    ticks, result = _run(scenario())

    assert "草莓" in result
    assert ticks > 0
    assert calls and all(thread_id != main_thread for thread_id in calls)
