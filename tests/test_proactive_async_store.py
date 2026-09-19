"""主动事件持久化边界：SQLite 等待不能阻塞自治事件循环。"""

import asyncio
import threading
import time
import types

from agent.handler_autonomy import AutonomyMixin
from agent.interaction_contract import ProactiveEvent
from agent.scheduler import CronScheduler
from napcat.ws_client import SendResult


def _event():
    return ProactiveEvent(
        event_id="autonomy:async:g1:1",
        source="autonomy",
        scope_id="group:g1",
        channel="group",
        target="g1",
        payload={"kind": "group_initiative"},
    )


def test_proactive_claim_decision_finish_are_ordered_off_event_loop():
    """入口→租约→决策绑定→终态的每个 Store 边界都在线程中按序完成。"""
    class Store:
        def __init__(self):
            self.calls = []

        def record_proactive_event(self, event):
            self.calls.append(("record", threading.get_ident(), event.event_id))
            time.sleep(0.03)
            return {"status": "pending"}

        def get_proactive_event(self, event_id):
            self.calls.append(("lookup", threading.get_ident(), event_id))
            time.sleep(0.03)
            return {"event_id": event_id, "status": "pending"}

        def claim_proactive_event(self, event_id, lease):
            self.calls.append(("claim", threading.get_ident(), event_id, lease))
            time.sleep(0.03)
            return True

        def mark_proactive_event_executing(self, event_id, lease):
            self.calls.append(("executing", threading.get_ident(), event_id, lease))
            time.sleep(0.03)
            return True

        def record_decision_run(self, run):
            self.calls.append(("decision", threading.get_ident(), run.run_id))
            time.sleep(0.03)
            return {"run_id": run.run_id, "status": "completed"}

        def mark_proactive_event_decided(self, event_id, lease, run_id):
            self.calls.append(("bind", threading.get_ident(), event_id, run_id))
            time.sleep(0.03)
            return True

        def finish_proactive_event(self, event_id, lease, status, *, error_code=""):
            self.calls.append(("finish", threading.get_ident(), event_id, status))
            time.sleep(0.03)
            return True

    from agent.proactive_decision import start_proactive_decision

    store = Store()
    handler = object.__new__(AutonomyMixin)
    handler.memory = types.SimpleNamespace(store=store)
    handler._proactive_event_sink = store.record_proactive_event
    handler._run_store_io = AutonomyMixin._run_store_io.__get__(handler)
    main_thread = threading.get_ident()
    event = _event()

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await handler._emit_proactive_event_async(event)
            enabled, lease = await handler._claim_event_for_execution_async(event)
            assert enabled and lease
            run = start_proactive_decision(event)
            _, bound = await handler._finalize_proactive_decision_async(
                event, lease, run, reply="想和大家聊聊", responded=True,
            )
            assert bound
            await handler._finish_event_async(event, lease, "confirmed")
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks > 0
    assert store.calls
    assert [call[0] for call in store.calls] == [
        "record", "lookup", "claim", "executing", "decision", "bind", "finish",
    ]
    assert all(call[1] != main_thread for call in store.calls)


def test_emit_async_keeps_legacy_sink_compatible_and_nonblocking():
    """无拆分记录器的离线测试适配器仍可使用，且 sink 不在事件循环线程执行。"""
    calls = []
    main_thread = threading.get_ident()
    fake = types.SimpleNamespace(
        _proactive_event_sink=lambda event: calls.append(
            (event.event_id, threading.get_ident())
        ),
    )

    asyncio.run(AutonomyMixin._emit_proactive_event_async(fake, _event()))

    assert calls == [("autonomy:async:g1:1", calls[0][1])]
    assert calls[0][1] != main_thread


def test_scheduler_store_coordination_does_not_run_on_event_loop():
    """定时任务的事件持久化/租约/终态写入必须离开事件循环。"""
    main_thread = threading.get_ident()

    class SlowStore:
        def __init__(self):
            self.calls = []

        def record_proactive_event(self, event):
            self.calls.append(("record", threading.get_ident()))
            time.sleep(0.03)
            return {"event_id": event.event_id, "status": "pending"}

        def get_proactive_event(self, event_id):
            self.calls.append(("lookup", threading.get_ident()))
            time.sleep(0.03)
            return {"event_id": event_id, "status": "pending"}

        def claim_proactive_event(self, event_id, lease):
            self.calls.append(("claim", threading.get_ident()))
            time.sleep(0.03)
            return True

        def mark_proactive_event_executing(self, event_id, lease):
            self.calls.append(("executing", threading.get_ident()))
            time.sleep(0.03)
            return True

        def record_decision_run(self, run):
            self.calls.append(("decision", threading.get_ident()))
            time.sleep(0.03)
            return {"run_id": run.run_id, "status": "completed"}

        def mark_proactive_event_decided(self, event_id, lease, run_id):
            self.calls.append(("bind", threading.get_ident()))
            time.sleep(0.03)
            return True

        def finish_proactive_event(self, event_id, lease, status, *, error_code=""):
            self.calls.append(("finish", threading.get_ident()))
            time.sleep(0.03)
            return True

    async def llm(*_args, **_kwargs):
        return "提醒一下～"

    async def send(*_args, **_kwargs):
        return SendResult(True, True, message_id=1)

    store = SlowStore()
    scheduler = CronScheduler(
        send_group_msg=send,
        send_private_msg=send,
        llm_caller=llm,
        get_group_ids=lambda: [],
        proactive_event_sink=store.record_proactive_event,
        proactive_event_store=store,
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
            result = await scheduler._fire({
                "id": 99,
                "type": "once",
                "text": "喝水",
                "group_id": "g1",
                "last_attempt_at": "2026-08-29 17:00",
            })
        finally:
            stopped = True
            await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result == "confirmed"
    assert ticks > 0
    assert store.calls
    assert all(thread_id != main_thread for _name, thread_id in store.calls)
