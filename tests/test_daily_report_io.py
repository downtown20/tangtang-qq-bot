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

def _scheduler(config):
    return DailyReportScheduler(
        config=config,
        llm_caller=None,
        send_group_msg=None,
        get_group_ids=lambda: [],
        get_stats=lambda: {},
        get_weather=None,
    )


def test_daily_report_time_is_normalized_to_hh_mm():
    """审计 RC3 回归：config 写 '9:00'，而 _tick 拿 strftime('%H:%M') 得到
    '09:00'——两者不相等时定时器**永不触发**（生产 config.yaml 正是 '9:00'）。"""
    assert _scheduler({"time": "9:00"})._time == "09:00"
    assert _scheduler({"time": "09:00"})._time == "09:00"
    assert _scheduler({"time": "23:5"})._time == "23:05"
    assert _scheduler({})._time == "09:00"
    assert _scheduler({"time": "垃圾"})._time == "09:00"  # 坏配置不静默乱算


def test_daily_report_tick_fires_at_the_normalized_minute(monkeypatch):
    """阳性对照：规范化之后 tick 真的会在到点那一分钟广播。"""
    import agent.daily_report as daily_report

    scheduler = _scheduler({"time": "9:00"})
    fired = []

    async def fake_broadcast():
        fired.append(True)

    async def fake_reset(_today):
        return None

    scheduler._broadcast = fake_broadcast
    scheduler._reset_daily_async = fake_reset

    real_datetime = daily_report.datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 9, 25, 9, 0, 0)

    monkeypatch.setattr(daily_report, "datetime", _FrozenDatetime)
    asyncio.run(scheduler._tick())
    assert fired == [True]
