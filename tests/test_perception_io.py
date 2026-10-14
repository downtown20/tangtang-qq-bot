"""感知反馈持久化的事件循环边界回归测试。"""

import asyncio
import threading
import time

from agent.perception import PerceptionEngine


def test_feedback_write_does_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    class SlowStore:
        def record_feedback(self, *args, **kwargs):
            calls.append(threading.get_ident())
            time.sleep(0.05)

    async def llm(*_args, **_kwargs):
        return "neutral"

    engine = PerceptionEngine(SlowStore(), llm)

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
            await engine.evaluate(
                "糖糖的回复", "嗯", "u1", group_id="g1", reply_ms=10,
            )
        finally:
            stopped = True
            await task
        return ticks

    ticks = asyncio.run(scenario())

    assert ticks > 0
    assert calls and all(thread_id != main_thread for thread_id in calls)
