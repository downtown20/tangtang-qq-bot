"""反思整合的 SQLite 写入事件循环边界回归测试。"""

import asyncio
import threading
import time
from types import SimpleNamespace

from agent.reflection import DailyDigest, ReflectionEngine, ReflectionResult


def test_reflection_persistence_does_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    class SlowStore:
        def insert_daily_digest(self, **_kwargs):
            calls.append(("digest", threading.get_ident()))
            time.sleep(0.05)

        def insert_tangtang_journal(self, **_kwargs):
            calls.append(("journal", threading.get_ident()))
            time.sleep(0.05)

    self_state = SimpleNamespace(
        self_narrative=SimpleNamespace(summary="", recent_experiences=[]),
        values=SimpleNamespace(to_tendency_text=lambda: ""),
        update_self_narrative=lambda **_kwargs: None,
        update_values=lambda *_args, **_kwargs: None,
        update_relationship=lambda *_args, **_kwargs: None,
        clear_experience_buffer=lambda: None,
        get_recent_experiences=lambda limit=200: [{"message": "测试"}],
        _save=lambda: None,
    )
    result = ReflectionResult(
        success=True,
        digests=[DailyDigest(date="2026-08-31", group_id="g1", summary="今天聊了测试")],
        journal_entry="今天完成了测试。",
        journal_mood="warm",
    )
    engine = ReflectionEngine(
        llm_call=lambda *_args, **_kwargs: None,
        self_state=self_state,
        store=SlowStore(),
        min_interactions_before_reflect=1,
    )
    engine._last_reflection = None

    async def fake_reflect(_experiences):
        return result

    engine._reflect = fake_reflect

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
            reflected = await engine.maybe_reflect()
        finally:
            stopped = True
            await task
        return ticks, reflected

    ticks, reflected = asyncio.run(scenario())

    assert reflected and reflected.success
    assert ticks > 0
    assert calls and all(thread_id != main_thread for _name, thread_id in calls)
