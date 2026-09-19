"""P1-4a：主动事件来源持久化与幂等边界。"""

import pytest


def _event(*, event_id="pro-1", target="g1", idem="idem-1"):
    from agent.interaction_contract import ProactiveEvent

    return ProactiveEvent(
        event_id=event_id,
        source="scheduler",
        scope_id=f"group:{target}",
        channel="group",
        target=target,
        payload={"task_id": "task-1", "text": "提醒一下"},
        created_at="2026-08-29 18:00:00",
        idempotency_key=idem,
    )


def test_proactive_event_is_durable_and_idempotent(tmp_path):
    from agent.store import Store

    db = tmp_path / "proactive.db"
    first_store = Store(str(db))
    first = first_store.record_proactive_event(_event())
    duplicate = first_store.record_proactive_event(_event())

    assert first["event_id"] == "pro-1"
    assert first["status"] == "pending"
    assert first["attempts"] == 0
    assert duplicate == first

    restarted = Store(str(db))
    loaded = restarted.get_proactive_event("pro-1")
    assert loaded == first
    assert restarted.get_proactive_event_health()["total"] == 1


def test_proactive_event_idempotency_conflict_fails_closed(tmp_path):
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())

    with pytest.raises(ValueError, match="PROACTIVE_EVENT_CONFLICT"):
        store.record_proactive_event(
            _event(event_id="pro-2", target="g2", idem="idem-1")
        )


def test_proactive_event_requires_typed_contract(tmp_path):
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    with pytest.raises(TypeError, match="ProactiveEvent"):
        store.record_proactive_event({"event_id": "not-typed"})


def test_proactive_event_health_reports_only_known_states(tmp_path):
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    health = store.get_proactive_event_health()

    assert health["pending"] == 1
    assert health["claimed"] == 0
    assert health["unknown"] == 0
    assert health["open"] == 1


def test_failed_claim_to_executing_can_be_released_without_waiting_for_lease(tmp_path):
    """claim 成功但 executing 推进失败时，同一租约应立即回到 pending。"""
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.mark_proactive_event_executing("pro-1", "wrong") is False
    assert store.release_proactive_event_claim("pro-1", "lease-a") is True
    saved = store.get_proactive_event("pro-1")
    assert saved["status"] == "pending"
    assert saved["lease_token"] == ""


def test_handler_sink_persists_proactive_event_without_claiming_delivery(tmp_path):
    from types import SimpleNamespace

    from agent.handler import MessageHandler
    from agent.store import Store

    class Metrics:
        def incr(self, *_args, **_kwargs):
            return None

    store = Store(str(tmp_path / "proactive.db"))
    handler = object.__new__(MessageHandler)
    handler.metrics = Metrics()
    handler.memory = SimpleNamespace(store=store)

    MessageHandler._record_proactive_event(handler, _event())

    saved = store.get_proactive_event("pro-1")
    assert saved["status"] == "pending"
    assert saved["payload"] == {"task_id": "task-1", "text": "提醒一下"}
    assert saved["decision_run_id"] == ""


def test_readonly_runtime_snapshot_exposes_proactive_health(tmp_path):
    from datetime import datetime

    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "proactive.db"
    Store(str(db)).record_proactive_event(_event())

    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime(2026, 8, 29, 18, 0, 0),
    )

    assert snapshot["proactive"]["total"] == 1
    assert snapshot["proactive"]["pending"] == 1
    assert snapshot["proactive"]["schema_missing"] is False


def test_proactive_claim_is_single_owner_and_claimed_recovery_is_safe(tmp_path):
    from agent.store import Store

    db = tmp_path / "proactive.db"
    store = Store(str(db))
    store.record_proactive_event(_event())

    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.claim_proactive_event("pro-1", "lease-b") is False
    recovered = Store(str(db)).recover_proactive_events_after_restart()

    assert recovered == {"pending": 1, "uncertain": 0}
    state = Store(str(db)).get_proactive_event("pro-1")
    assert state["status"] == "pending"
    assert state["attempts"] == 1
    assert state["lease_token"] == ""


def test_expired_claimed_lease_can_be_reclaimed_before_external_side_effect(tmp_path):
    """进程未重启但 claim→executing 之间取消时，过期 claimed 不应永久卡死。"""
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    with store._connect() as conn:
        conn.execute(
            "UPDATE proactive_event_state SET lease_until=? WHERE event_id=?",
            ("2000-01-01 00:00:00", "pro-1"),
        )
        conn.commit()

    assert store.claim_proactive_event("pro-1", "lease-b") is True
    state = store.get_proactive_event("pro-1")
    assert state["status"] == "claimed"
    assert state["attempts"] == 2
    assert state["lease_token"] == "lease-b"
    assert state["error_code"] == "PROACTIVE_LEASE_EXPIRED"


def test_proactive_executing_recovery_is_uncertain_and_not_replayed(tmp_path):
    from agent.store import Store

    db = tmp_path / "proactive.db"
    store = Store(str(db))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.mark_proactive_event_executing("pro-1", "lease-a") is True

    recovered = Store(str(db)).recover_proactive_events_after_restart()

    assert recovered == {"pending": 0, "uncertain": 1}
    state = Store(str(db)).get_proactive_event("pro-1")
    assert state["status"] == "uncertain"
    assert state["error_code"] == "PROCESS_RESTARTED_DURING_EVENT"


def test_proactive_claim_and_execution_require_matching_lease(tmp_path):
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())

    assert store.mark_proactive_event_executing("pro-1", "wrong") is False
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.mark_proactive_event_executing("pro-1", "wrong") is False
    assert store.mark_proactive_event_executing("pro-1", "lease-a") is True


def test_handler_startup_recovery_delegates_to_store(tmp_path):
    from types import SimpleNamespace

    from agent.handler import MessageHandler
    from agent.store import Store

    db = tmp_path / "proactive.db"
    store = Store(str(db))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True

    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(store=store)

    MessageHandler._recover_proactive_events_after_restart(handler)

    assert store.get_proactive_event("pro-1")["status"] == "pending"


def test_proactive_decision_and_terminal_state_are_lease_bound(tmp_path):
    from agent.interaction_contract import DecisionRun
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.mark_proactive_event_executing("pro-1", "lease-a") is True
    decision = DecisionRun.start(
        run_id="decision-1", event_key="pro-1", scope_id="group:g1",
        correlation_id="cid-1", model="test",
    ).finish(decision="reply")
    store.record_decision_run(decision)

    assert store.mark_proactive_event_decided(
        "pro-1", "lease-a", "decision-1", action_plan_id="plan-1",
    ) is True
    state = store.get_proactive_event("pro-1")
    assert state["status"] == "decided"
    assert state["decision_run_id"] == "decision-1"
    assert state["action_plan_id"] == "plan-1"
    assert store.get_proactive_event_health()["bound"] == 1

    assert store.finish_proactive_event(
        "pro-1", "lease-a", "confirmed",
    ) is True
    assert store.get_proactive_event("pro-1")["status"] == "confirmed"
    assert store.finish_proactive_event(
        "pro-1", "lease-a", "failed", error_code="late",
    ) is False


def test_proactive_decision_binding_rejects_missing_or_cross_scope_run(tmp_path):
    from agent.interaction_contract import DecisionRun
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True
    assert store.mark_proactive_event_executing("pro-1", "lease-a") is True

    with pytest.raises(ValueError, match="PROACTIVE_DECISION_RUN_NOT_FOUND"):
        store.mark_proactive_event_decided("pro-1", "lease-a", "missing")

    wrong_scope = DecisionRun.start(
        run_id="decision-wrong", event_key="pro-1", scope_id="group:g2",
        correlation_id="cid-2", model="test",
    ).finish(decision="reply")
    store.record_decision_run(wrong_scope)
    with pytest.raises(ValueError, match="PROACTIVE_DECISION_RUN_SCOPE_MISMATCH"):
        store.mark_proactive_event_decided("pro-1", "lease-a", "decision-wrong")

    wrong_event = DecisionRun.start(
        run_id="decision-event", event_key="other-event", scope_id="group:g1",
        correlation_id="cid-3", model="test",
    ).finish(decision="reply")
    store.record_decision_run(wrong_event)
    with pytest.raises(ValueError, match="PROACTIVE_DECISION_RUN_EVENT_MISMATCH"):
        store.mark_proactive_event_decided("pro-1", "lease-a", "decision-event")


def test_proactive_terminal_transition_cannot_use_wrong_lease_or_status(tmp_path):
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    store.record_proactive_event(_event())
    assert store.claim_proactive_event("pro-1", "lease-a") is True

    assert store.mark_proactive_event_decided(
        "pro-1", "wrong", "decision-1",
    ) is False
    assert store.finish_proactive_event(
        "pro-1", "lease-a", "confirmed",
    ) is False
    assert store.mark_proactive_event_executing("pro-1", "lease-a") is True
    with pytest.raises(ValueError, match="PROACTIVE_TERMINAL_STATUS_INVALID"):
        store.finish_proactive_event("pro-1", "lease-a", "pending")


def test_terminal_decision_run_is_durable_idempotent_and_conflict_safe(tmp_path):
    from agent.interaction_contract import DecisionRun
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    run = DecisionRun.start(
        run_id="run-1", event_key="event-1", scope_id="group:g1",
        correlation_id="cid-1", model="test-model",
    ).finish(decision="reply", tool_calls=("send_voice",))

    first = store.record_decision_run(run)
    assert first["run_id"] == "run-1"
    assert first["status"] == "completed"
    assert first["tool_calls"] == ["send_voice"]
    assert store.record_decision_run(run) == first
    assert store.get_decision_run("run-1") == first

    with pytest.raises(ValueError, match="DECISION_RUN_CONFLICT"):
        store.record_decision_run(
            DecisionRun(
                run_id="run-1", event_key="event-2", scope_id="group:g2",
                correlation_id="cid-2", status="completed", decision="skip",
            )
        )

    with pytest.raises(ValueError, match="DECISION_RUN_NOT_TERMINAL"):
        store.record_decision_run(
            DecisionRun.start(
                run_id="run-running", event_key="event-3", scope_id="group:g3",
                correlation_id="cid-3",
            )
        )


def test_readonly_snapshot_exposes_decision_run_health(tmp_path):
    from datetime import datetime

    from agent.interaction_contract import DecisionRun
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "decision.db"
    store = Store(str(db))
    run = DecisionRun.start(
        run_id="run-1", event_key="event-1", scope_id="private:u1",
        correlation_id="cid-1",
    ).finish(decision="skip", tool_calls=("skip_response",))
    store.record_decision_run(run)

    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime(2026, 8, 29, 18, 0, 0),
    )
    assert snapshot["decision_runs"]["total"] == 1
    assert snapshot["decision_runs"]["completed"] == 1
    assert snapshot["decision_runs"]["corrupt"] == 0
    assert snapshot["decision_runs"]["schema_missing"] is False


def test_readonly_snapshot_scopes_proactive_decision_runs(tmp_path):
    from datetime import datetime

    from agent.interaction_contract import DecisionRun, ProactiveEvent
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "scoped-decision.db"
    store = Store(str(db))
    event = ProactiveEvent(
        event_id="proactive-1", idempotency_key="source-1", source="test",
        scope_id="private:u1", channel="private", target="u1",
        payload={"text": "hello"},
    )
    store.record_proactive_event(event)
    proactive_run = DecisionRun.start(
        run_id="proactive-run-1", event_key=event.event_id,
        scope_id=event.scope_id, correlation_id="cid-proactive",
    ).finish(decision="reply")
    store.record_decision_run(proactive_run)
    foreground_run = DecisionRun.start(
        run_id="foreground-run-1", event_key="chat-event-1",
        scope_id="private:u1", correlation_id="cid-foreground",
    ).finish(decision="reply")
    store.record_decision_run(foreground_run)

    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime(2026, 8, 29, 18, 0, 0),
    )
    assert snapshot["decision_runs"]["total"] == 2
    assert snapshot["proactive"]["proactive_decision_runs"] == 1
    assert snapshot["proactive"]["proactive_decision_runs_corrupt"] == 0
    assert snapshot["proactive"]["proactive_decision_runs_unknown"] == 0


def test_scheduler_claims_and_finishes_persisted_event_before_delivery(tmp_path):
    import asyncio

    from agent.scheduler import CronScheduler
    from agent.store import Store
    from napcat.ws_client import SendResult

    store = Store(str(tmp_path / "proactive.db"))
    sent = []

    async def send_group(group_id, message):
        sent.append((group_id, message))
        return SendResult(True, True, message_id=101)

    async def llm(*_args, **_kwargs):
        return "提醒一下～"

    scheduler = CronScheduler(
        send_group_msg=send_group,
        send_private_msg=send_group,
        llm_caller=llm,
        get_group_ids=lambda: [],
        proactive_event_sink=store.record_proactive_event,
        proactive_event_store=store,
    )

    result = asyncio.run(scheduler._fire({
        "id": 42,
        "type": "once",
        "text": "喝水",
        "group_id": "g1",
        "last_attempt_at": "2026-08-29 18:00",
    }))

    assert result == "confirmed"
    assert sent == [("g1", "提醒一下～")]
    event = store.list_proactive_events()[0]
    assert event["status"] == "confirmed"
    assert event["attempts"] == 1
    assert event["decision_run_id"]
    decision = store.get_decision_run(event["decision_run_id"])
    assert decision["event_key"] == event["event_id"]
    assert decision["scope_id"] == event["scope_id"]
    assert decision["decision"] == "reply"


def test_scheduler_reentry_does_not_replay_terminal_event(tmp_path):
    import asyncio

    from agent.scheduler import CronScheduler
    from agent.store import Store
    from napcat.ws_client import SendResult

    store = Store(str(tmp_path / "proactive.db"))
    sent = []
    llm_calls = []

    async def send_group(group_id, message):
        sent.append((group_id, message))
        return SendResult(True, True, message_id=102)

    async def llm(*_args, **_kwargs):
        llm_calls.append(True)
        return "提醒一下～"

    scheduler = CronScheduler(
        send_group_msg=send_group,
        send_private_msg=send_group,
        llm_caller=llm,
        get_group_ids=lambda: [],
        proactive_event_sink=store.record_proactive_event,
        proactive_event_store=store,
    )
    task = {
        "id": 43,
        "type": "once",
        "text": "喝水",
        "group_id": "g1",
        "last_attempt_at": "2026-08-29 18:01",
    }

    assert asyncio.run(scheduler._fire(task)) == "confirmed"
    assert asyncio.run(scheduler._fire(task)) == "failed"
    assert sent == [("g1", "提醒一下～")]
    assert len(llm_calls) == 1
    assert store.list_proactive_events()[0]["attempts"] == 1


def test_scheduler_llm_failure_is_failed_without_sending_fallback_text(tmp_path):
    import asyncio

    from agent.scheduler import CronScheduler
    from agent.store import Store

    store = Store(str(tmp_path / "proactive.db"))
    sent = []

    async def send_group(*args, **kwargs):
        sent.append((args, kwargs))

    async def llm(*_args, **_kwargs):
        raise RuntimeError("llm down")

    scheduler = CronScheduler(
        send_group_msg=send_group,
        send_private_msg=send_group,
        llm_caller=llm,
        get_group_ids=lambda: [],
        proactive_event_sink=store.record_proactive_event,
        proactive_event_store=store,
    )

    task = {
        "id": 44,
        "type": "once",
        "text": "喝水",
        "group_id": "g1",
        "last_attempt_at": "2026-08-29 18:02",
    }
    assert asyncio.run(scheduler._fire(task)) == "failed"
    assert sent == []
    event = store.list_proactive_events()[0]
    assert event["status"] == "failed"
    assert event["decision_run_id"] == ""
    assert store.get_decision_run_health()["failed"] == 1
