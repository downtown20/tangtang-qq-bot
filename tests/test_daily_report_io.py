"""每日播报状态文件 I/O 的事件循环边界回归测试。"""

import asyncio
import threading
import time

from agent.daily_report import DailyReportScheduler


def test_daily_report_state_save_does_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    scheduler = DailyReportScheduler(
        config={},
        llm_caller=None,
        send_group_msg=None,
        get_group_ids=lambda: [],
        get_stats=lambda: {},
        get_weather=None,
    )

    def slow_save():
        calls.append(threading.get_ident())
        time.sleep(0.03)
        return True

    scheduler._save_state = slow_save

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
            result = await scheduler._save_state_async()
        finally:
            stopped = True
            await task
        return result, ticks

    result, ticks = asyncio.run(scenario())

    assert result is True
    assert ticks > 0
    assert calls and calls[0] != main_thread
