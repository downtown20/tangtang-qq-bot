"""健康调度和 fire-and-forget 异常回收回归。"""

import asyncio
import builtins
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def test_startup_health_check_does_not_immediately_repeat_hourly_item():
    from agent.health_check import SystemHealth

    calls = 0

    async def check():
        nonlocal calls
        calls += 1
        return {"status": "ok", "message": "ok"}

    health = SystemHealth(None)
    health.register("hourly-probe", check, "hourly")

    async def run():
        await health.run_startup()
        await health.run_hourly()
        assert calls == 1
        health._checks["hourly-probe"]["last_run"] -= timedelta(hours=1, seconds=1)
        await health.run_hourly()

    asyncio.run(run())
    assert calls == 2


def test_busy_health_reports_persistently_queued_unflushed_values():
    from agent.health_check import _check_extract_busy

    values = {"extract_attempts": 10, "extract_busy_queued": 6}
    metrics = type("Metrics", (), {
        "get_current": lambda self, name: values.get(name, 0),
    })()
    handler = type("Handler", (), {"metrics": metrics})()

    result = asyncio.run(_check_extract_busy(handler))

    assert result["status"] == "ok"
    assert "60%" in result["message"]


def test_safe_task_retrieves_extraction_exception_and_records_lifecycle():
    from agent.handler import MessageHandler

    class Metrics:
        def __init__(self):
            self.values = {}

        def incr(self, name, delta=1):
            self.values[name] = self.values.get(name, 0) + delta

    async def run():
        handler = object.__new__(MessageHandler)
        handler._bg_tasks = set()
        handler.metrics = Metrics()
        unhandled = []
        loop = asyncio.get_running_loop()
        old_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        async def explode():
            raise RuntimeError("probe failure")

        try:
            handler._safe_task(explode(), name="memory_extract:test-user")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(old_handler)

        assert handler._bg_tasks == set()
        assert unhandled == []
        assert handler.metrics.values["extract_tasks_scheduled"] == 1
        assert handler.metrics.values["extract_tasks_failed"] == 1

    asyncio.run(run())


def test_extraction_worker_refills_bounded_queue_before_consuming(monkeypatch, caplog):
    from agent.handler_autonomy import AutonomyMixin

    class Store:
        @staticmethod
        def get_extraction_queue_health():
            return {"total_open": 0}

        @staticmethod
        def requeue_dead_extraction_jobs(limit=2):
            return 0

    handler = AutonomyMixin()
    handler._shutting_down = False
    handler._busy = False
    handler._llm_lock = SimpleNamespace(locked=lambda: False)
    handler.memory = SimpleNamespace(
        store=Store(), ensure_memory_embeddings=lambda *_args: 0,
    )
    handler.embed_engine = None
    handler._maybe_extract_stale = AsyncMock(return_value=1)
    handler._resume_extraction_jobs = AsyncMock(return_value=1)

    sleeps = 0

    async def finite_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            handler._shutting_down = True

    monkeypatch.setattr("agent.handler_autonomy.asyncio.sleep", finite_sleep)
    async def run_in_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("agent.handler_autonomy.asyncio.to_thread", run_in_thread)

    with caplog.at_level("INFO", logger="糖糖.Autonomy"):
        asyncio.run(handler._extraction_worker_loop())

    handler._maybe_extract_stale.assert_awaited_once_with(
        max_per_cycle=8, max_open=8,
    )
    handler._resume_extraction_jobs.assert_awaited_once_with(limit=1)
    assert any("🧠 提取 worker 周期" in record.message
               and "idle_reason=active" in record.message
               for record in caplog.records)


def test_extraction_worker_yields_while_live_reply_is_busy(monkeypatch):
    from agent.handler_autonomy import AutonomyMixin

    class Store:
        @staticmethod
        def get_extraction_queue_health():
            return {"total_open": 1}

        @staticmethod
        def requeue_dead_extraction_jobs(limit=2):
            return 0

    handler = AutonomyMixin()
    handler._shutting_down = False
    handler._busy = True
    handler._llm_lock = SimpleNamespace(locked=lambda: False)
    handler.memory = SimpleNamespace(
        store=Store(), ensure_memory_embeddings=lambda *_args: 0,
    )
    handler.embed_engine = None
    handler._maybe_extract_stale = AsyncMock(return_value=0)
    handler._resume_extraction_jobs = AsyncMock(return_value=0)

    sleeps = 0

    async def finite_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            handler._shutting_down = True

    monkeypatch.setattr("agent.handler_autonomy.asyncio.sleep", finite_sleep)
    async def run_in_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("agent.handler_autonomy.asyncio.to_thread", run_in_thread)

    asyncio.run(handler._extraction_worker_loop())

    handler._maybe_extract_stale.assert_not_awaited()
    handler._resume_extraction_jobs.assert_not_awaited()


def test_health_store_probe_does_not_block_event_loop():
    """健康检查的同步 Store 读取必须在线程边界执行。"""
    from agent.health_check import _check_stale_extraction

    class Store:
        @staticmethod
        def get_extraction_backlog_snapshot():
            time.sleep(0.05)
            return {
                "total_user_messages": 1,
                "backlog_messages": 1,
                "backlog_users": 1,
                "over_30_users": 0,
                "eligible_messages": 0,
                "deferred_messages": 1,
                "oldest_at": "2026-08-30 16:00:00",
            }

    handler = type("Handler", (), {
        "memory": type("Memory", (), {"store": Store()})(),
    })()

    async def run():
        ticks = 0
        finished = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not finished.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            result = await _check_stale_extraction(handler)
        finally:
            finished.set()
            await task
        assert result["status"] == "ok"
        assert ticks >= 3

    asyncio.run(run())


def test_gateway_health_reports_ws_qq_queue_and_outbox_without_network_io():
    from agent.health_check import _check_gateway_health

    class Store:
        def get_send_outbox_health(self):
            return {"open": 2, "dead": 1, "uncertain": 1}

    client = type("Client", (), {
        "ready_to_send": False,
        "_ws_connected": True,
        "_qq_online": False,
        "_event_inflight": 7,
        "_event_workers": {"group:1": object(), "private:2": object()},
        "_outbox_store": Store(),
    })()
    result = asyncio.run(_check_gateway_health(type("Handler", (), {
        "napcat": client,
    })()))

    assert result["status"] == "warn"
    assert "WS=on" in result["message"]
    assert "QQ=off" in result["message"]
    assert "inflight=7" in result["message"]
    assert "review=2" in result["message"]


def test_gateway_health_warns_when_outbox_is_still_open_even_if_ready():
    from agent.health_check import _check_gateway_health

    class Store:
        def get_send_outbox_health(self):
            return {
                "open": 1, "pending": 0, "sending": 1,
                "dead": 0, "uncertain": 0,
                "confirmed_unaccounted": 0, "confirmed_conflict": 0,
                "needs_review": 0,
            }

    client = type("Client", (), {
        "runtime_snapshot": {
            "ready": True, "ws_connected": True, "qq_online": True,
            "event_inflight": 0, "active_scopes": 0,
        },
        "ready_to_send": True,
        "_outbox_store": Store(),
    })()
    result = asyncio.run(_check_gateway_health(type("Handler", (), {
        "napcat": client,
    })()))

    assert result["status"] == "warn"
    assert "outbox_open=1" in result["message"]


def test_gateway_health_exposes_bare_sending_task_claim():
    from agent.health_check import _check_gateway_health

    class Store:
        def get_send_outbox_health(self):
            return {
                "open": 0, "dead": 0, "uncertain": 0,
                "confirmed_unaccounted": 0, "confirmed_conflict": 0,
                "bare_sending_tasks": 1, "needs_review": 1,
            }

    client = type("Client", (), {
        "runtime_snapshot": {
            "ready": True, "ws_connected": True, "qq_online": True,
            "event_inflight": 0, "active_scopes": 0,
        },
        "ready_to_send": True,
        "_outbox_store": Store(),
    })()
    result = asyncio.run(_check_gateway_health(type("Handler", (), {
        "napcat": client,
    })()))

    assert result["status"] == "warn"
    assert "bare_tasks=1" in result["message"]


def test_local_tts_health_warns_when_configured_port_is_down():
    from agent.health_check import _check_local_tts_health

    checker = AsyncMock(return_value=False)
    manager = type("Manager", (), {
        "_check_port": checker,
        "_gpt_sovits_proc": type("Proc", (), {"returncode": None})(),
    })()
    handler = type("Handler", (), {
        "config": {"voice": {"enabled": True, "provider": "gpt-sovits"}},
        "_service_mgr": manager,
    })()

    result = asyncio.run(_check_local_tts_health(handler))

    assert result["status"] == "warn"
    assert "9880 不可达" in result["message"]
    checker.assert_awaited_once_with(9880)


def test_local_tts_health_accepts_external_live_service():
    from agent.health_check import _check_local_tts_health

    checker = AsyncMock(return_value=True)
    manager = type("Manager", (), {
        "_check_port": checker,
        "_gpt_sovits_proc": None,
    })()
    handler = type("Handler", (), {
        "config": {"voice": {"enabled": True, "provider": "gpt_sovits"}},
        "_service_mgr": manager,
    })()

    result = asyncio.run(_check_local_tts_health(handler))

    assert result == {"status": "ok", "message": "GPT-SoVITS 端口 9880 在线"}


def test_local_tts_health_skips_disabled_voice_without_probe():
    from agent.health_check import _check_local_tts_health

    checker = AsyncMock(return_value=False)
    handler = type("Handler", (), {
        "config": {"voice": {"enabled": False, "provider": "gpt-sovits"}},
        "_service_mgr": type("Manager", (), {"_check_port": checker})(),
    })()

    result = asyncio.run(_check_local_tts_health(handler))

    assert result == {"status": "ok", "message": "GPT-SoVITS 未启用"}
    checker.assert_not_awaited()


def test_extraction_state_health_read_does_not_block_event_loop(tmp_path):
    from agent.health_check import _check_extraction_state_file

    state = tmp_path / "extraction_state.json"
    state.write_text('{"cursor": 1}', encoding="utf-8")
    handler = type("Handler", (), {
        "_EXTRACTION_STATE_FILE": str(state),
    })()
    main_thread = threading.get_ident()
    reads = []
    original_open = builtins.open

    class SlowFile:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self._wrapped.__exit__(*args)

        def read(self, *args, **kwargs):
            reads.append(threading.get_ident())
            time.sleep(0.05)
            return self._wrapped.read(*args, **kwargs)

    def slow_open(*args, **kwargs):
        return SlowFile(original_open(*args, **kwargs))

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
            result = await _check_extraction_state_file(handler)
        finally:
            stopped = True
            await task
        return ticks, result

    with patch("builtins.open", slow_open):
        ticks, result = asyncio.run(scenario())

    assert result["status"] == "ok"
    assert ticks > 0
    assert reads and all(thread_id != main_thread for thread_id in reads)
