"""优雅退出时的指标持久化回归测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.handler import MessageHandler


def _bare_shutdown_handler():
    handler = MessageHandler.__new__(MessageHandler)
    handler._save_extraction_state = MagicMock()
    handler.self_state = SimpleNamespace(save=MagicMock())
    handler.metrics = MagicMock()
    handler._bg_tasks = []
    handler.llm = None
    handler._service_mgr = SimpleNamespace(stop_all=AsyncMock())
    handler.catch_up = None
    return handler


def test_stop_services_flushes_metrics_before_closing_services():
    handler = _bare_shutdown_handler()

    asyncio.run(handler.stop_services())

    handler.metrics.flush.assert_called_once_with()
    handler._service_mgr.stop_all.assert_awaited_once_with()


def test_stop_services_stops_task_manager_before_state_flush():
    handler = _bare_shutdown_handler()
    events = []

    async def stop_tasks():
        events.append("tasks_stop")

    handler.task_manager = SimpleNamespace(stop=stop_tasks)
    handler._save_extraction_state.side_effect = lambda: events.append("state")
    handler.self_state.save.side_effect = lambda: events.append("self")

    asyncio.run(handler.stop_services())

    assert events[:3] == ["tasks_stop", "state", "self"]


def test_stop_services_flushes_pending_self_state_before_sync_fallback():
    handler = _bare_shutdown_handler()
    events = []

    async def flush_save():
        events.append("self_async")

    handler.self_state.flush_pending_save = flush_save
    handler.self_state.save.side_effect = lambda: events.append("self_sync")

    asyncio.run(handler.stop_services())

    assert events == ["self_async", "self_sync"]


def test_scheduled_restart_flushes_metrics_before_exit():
    handler = _bare_shutdown_handler()

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(handler._do_scheduled_restart())

    assert exc_info.value.code == 42
    handler.metrics.flush.assert_called_once_with()
