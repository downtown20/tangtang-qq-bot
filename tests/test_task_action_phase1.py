"""ADR-005 Phase 1：纯文本定时任务的 durable ActionPlan/outbox 闭环。"""

import asyncio
import json
import sqlite3
import types
from unittest.mock import AsyncMock

import pytest

from agent.store import ConfirmedProjectionConflict, ConfirmedProjectionError
from agent.tasks import TaskManager
from onebot.ws_client import NapCatClient


def _run(coro):
    return asyncio.run(coro)


def _rows(store, task_id):
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        attempts = conn.execute(
            "SELECT * FROM task_action_attempts WHERE task_id=? ORDER BY generation",
            (task_id,),
        ).fetchall()
        outboxes = conn.execute(
            "SELECT o.* FROM send_outbox o JOIN task_action_attempts a "
            "ON a.id=o.task_attempt_id WHERE a.task_id=? ORDER BY o.ordinal",
            (task_id,),
        ).fetchall()
    return (
        dict(task) if task else None,
        [dict(row) for row in attempts],
        [dict(row) for row in outboxes],
    )


def _nap_stub():
    return types.SimpleNamespace(
        send_private_message=AsyncMock(),
        send_group_message=AsyncMock(),
    )


def _persist_due_text(store, *, owner="1001", group_id="", payload=None,
                      llm_call=None, description="内部提醒备忘"):
    nap = _nap_stub()
    tm = TaskManager(store, nap, llm_call=llm_call)
    task_id = store.create_task(
        owner, description, "2020-01-01 00:00", group_id,
        action_payload=payload,
    )
    _run(tm._check_and_send())
    return task_id, tm, nap


def test_legacy_text_is_composed_once_then_frozen_before_any_network(store):
    llm = AsyncMock(return_value="该喝水啦，忙完也要照顾自己哦～")

    task_id, tm, nap = _persist_due_text(store, llm_call=llm)
    _run(tm._check_and_send())

    llm.assert_awaited_once()
    nap.send_private_message.assert_not_awaited()
    nap.send_group_message.assert_not_awaited()
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], task["current_attempt_id"]) == (
        "sending", attempts[0]["id"],
    )
    assert len(attempts) == len(outboxes) == 1
    assert attempts[0]["state"] == "outbox_pending"
    assert outboxes[0]["status"] == "pending"
    assert outboxes[0]["message"] == "该喝水啦，忙完也要照顾自己哦～"
    assert outboxes[0]["retry_owner"] == "outbox"
    plan = json.loads(attempts[0]["plan_json"])
    child = plan["children"][0]
    assert plan["source_id"] == f"task:{task_id}:attempt:0"
    assert child["action_id"] == outboxes[0]["domain_action_id"]
    assert child["payload"] == {"text": outboxes[0]["message"]}


def test_text_claim_gate_pauses_new_plans_without_claim_or_compose(store):
    llm = AsyncMock(return_value="不应生成")
    nap = _nap_stub()
    nap._task_text_outbox_enabled = False
    tm = TaskManager(store, nap, llm_call=llm)
    task_id = store.create_task(
        "1001", "暂停期间保留", "2020-01-01 00:00",
    )

    _run(tm._check_and_send())

    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], task["current_attempt_id"]) == ("pending", None)
    assert attempts == []
    assert outboxes == []
    llm.assert_not_awaited()
    nap.send_private_message.assert_not_awaited()


def test_outbox_claim_gate_leaves_pending_row_unclaimed(store):
    outbox_id = store.enqueue_send_outbox("private", "1001", "等待安全放行")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client._outbox_claims_enabled = False
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 909},
    })

    assert _run(client.process_send_outbox()) == 0
    assert store.get_send_outbox(outbox_id)["status"] == "pending"
    client._call_api.assert_not_awaited()


def test_task_action_rollback_gates_are_in_config_and_qt_console():
    import ast
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    config_gate_paths = {
        "tasks.text_action_outbox_enabled",
        "tasks.media_action_outbox_enabled",
        "tasks.send_outbox_claims_enabled",
        "tasks.manual_retry_generation_enabled",
    }
    expected_ui_defaults = {
        "tasks.text_action_outbox_enabled": True,
        "tasks.media_action_outbox_enabled": False,
    }
    task_gates = config["tasks"]
    for path in config_gate_paths:
        key = path.removeprefix("tasks.")
        assert key in task_gates
        assert isinstance(task_gates[key], bool)

    console = (root / "糖糖控制台_qt.py").read_text(encoding="utf-8")
    tree = ast.parse(console)
    task_section = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_add_section"
        and ast.literal_eval(node.args[0]) == "tasks"
    )
    field_paths = {
        ast.literal_eval(field.elts[0]) for field in task_section.args[2].elts
    }
    assert field_paths == set(expected_ui_defaults)

    assignment = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "TASK_GATE_DEFAULTS"
            for target in node.targets
        )
    )
    assert ast.literal_eval(assignment.value) == expected_ui_defaults
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_get_nested"
        and len(node.args) >= 3
        and ast.unparse(node.args[2]) == "TASK_GATE_DEFAULTS.get(path, False)"
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "owner,group_id,channel,target,scope",
    [
        ("1001", "", "private", "1001", "_private_1001"),
        ("1001", "g9", "group", "g9", "g9"),
    ],
)
def test_typed_text_freezes_verbatim_identity_without_llm(
        store, owner, group_id, channel, target, scope):
    llm = AsyncMock(return_value="不应该被调用")
    task_id, _tm, nap = _persist_due_text(
        store, owner=owner, group_id=group_id,
        payload={"text": "到点就说这一句"}, llm_call=llm,
    )

    llm.assert_not_awaited()
    nap.send_private_message.assert_not_awaited()
    nap.send_group_message.assert_not_awaited()
    _task, attempts, outboxes = _rows(store, task_id)
    plan = json.loads(attempts[0]["plan_json"])
    child = plan["children"][0]
    template = json.loads(outboxes[0]["receipt_template"])
    assert (plan["channel"], plan["target"], plan["scope_id"]) == (
        channel, target, scope,
    )
    assert child["conversation_ref"] == {
        "projection_kind": "conversation_reply",
        "conversation_user_id": owner,
        "group_id": group_id,
        "source_chat_id": None,
        "self_memory_eligible": False,
    }
    assert outboxes[0]["message"] == "到点就说这一句"
    assert template["identity_payload"] == {"text": "到点就说这一句"}


def test_plan_attempt_and_outbox_insert_roll_back_as_one_unit(store):
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER phase1_injected_outbox_failure BEFORE INSERT ON send_outbox "
            "BEGIN SELECT RAISE(ABORT,'injected outbox failure'); END"
        )

    task_id, _tm, nap = _persist_due_text(
        store, payload={"text": "不要发出去"},
    )

    nap.send_private_message.assert_not_awaited()
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], task["current_attempt_id"]) == ("pending", None)
    assert attempts == []
    assert outboxes == []


def test_legacy_claim_release_cannot_steal_a_linked_outbox_owner(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "已有 durable owner"},
    )

    assert store.release_task_claim(task_id) is False
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "sending", "outbox_pending", "pending",
    )


def test_concurrent_due_scans_create_one_plan_and_compose_once(store):
    calls = 0

    async def compose(_system, _user):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "唯一冻结消息"

    nap = _nap_stub()
    tm = TaskManager(store, nap, llm_call=compose)
    task_id = store.create_task("1001", "喝水", "2020-01-01 00:00")

    async def scenario():
        await asyncio.gather(tm._check_and_send(), tm._check_and_send())

    _run(scenario())
    _task, attempts, outboxes = _rows(store, task_id)
    assert calls == 1
    assert len(attempts) == len(outboxes) == 1
    assert outboxes[0]["message"] == "唯一冻结消息"


def test_outbox_worker_is_first_sender_and_confirmed_settles_task(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "确认送达"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 321},
    })

    assert _run(client.process_send_outbox()) == 1

    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["state"] == "confirmed"
    assert attempts[0]["accounting_state"] == "clean"
    assert outboxes == []
    client._call_api.assert_awaited_once()
    with store._connect() as conn:
        fact = conn.execute(
            "SELECT domain_action_id,chat_log_id FROM confirmed_action_facts"
        ).fetchone()
        window_count = conn.execute(
            "SELECT COUNT(*) FROM conversation_window_events"
        ).fetchone()[0]
        bot_chat_count = conn.execute(
            "SELECT COUNT(*) FROM chat_log WHERE is_bot_reply=1 AND message='确认送达'"
        ).fetchone()[0]
    assert fact and fact[1]
    assert window_count == bot_chat_count == 1


def test_task_linked_bare_boolean_send_is_uncertain_not_confirmed(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "旧适配器只返回布尔值"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client.send_private_message = AsyncMock(return_value=True)

    assert _run(client.process_send_outbox()) == 1

    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "uncertain"
    assert attempts[0]["state"] == "uncertain"
    assert len(outboxes) == 1
    assert outboxes[0]["status"] == "uncertain"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM confirmed_action_facts"
        ).fetchone()[0] == 0
        receipt = conn.execute(
            "SELECT action_status FROM action_receipt_mailbox"
        ).fetchone()
    assert receipt[0] == "uncertain"


def test_retryable_failure_keeps_one_frozen_action_then_confirms(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "重试仍是同一句"},
    )
    _task, attempts, outboxes = _rows(store, task_id)
    first_action = outboxes[0]["domain_action_id"]
    first_outbox = outboxes[0]["action_id"]
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })

    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "sending"
    assert attempts[0]["state"] == "outbox_pending"
    assert len(outboxes) == 1
    assert (outboxes[0]["action_id"], outboxes[0]["domain_action_id"]) == (
        first_outbox, first_action,
    )
    assert outboxes[0]["message"] == "重试仍是同一句"

    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 322},
    })
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET next_retry_at='' WHERE action_id=?",
            (first_outbox,),
        )
    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["state"] == "confirmed"
    assert outboxes == []


def test_uncertain_and_dead_are_terminal_for_task_owner(store):
    uncertain_id, _tm, _nap = _persist_due_text(
        store, owner="1001", payload={"text": "可能送达"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 0},
    })
    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, uncertain_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "uncertain", "uncertain", "uncertain",
    )

    dead_id, _tm, _nap = _persist_due_text(
        store, owner="1002", payload={"text": "确定失败"},
    )
    client._call_api = AsyncMock(return_value={
        "status": "failed", "retcode": 400, "msg": "bad request",
    })
    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, dead_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "failed", "dead", "dead",
    )
    assert not any(
        task["id"] in {uncertain_id, dead_id} for task in store.get_due_tasks()
    )


def test_linked_uncertain_retry_waits_for_phase2_verification_contract(store):
    task_id, tm, _nap = _persist_due_text(
        store, payload={"text": "请再发同一句"},
    )
    _task, _attempts, outboxes = _rows(store, task_id)
    old = outboxes[0]
    assert store.claim_send_outbox(old["action_id"])
    assert store.settle_send_outbox(
        old["action_id"], "uncertain", error_code="SEND_RESULT_LOST",
    ) == "uncertain"

    assert tm.retry(task_id, "1001") is False
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], task["current_attempt_id"]) == (
        "uncertain", attempts[0]["id"],
    )
    assert [(row["generation"], row["state"]) for row in attempts] == [
        (0, "uncertain"),
    ]
    assert len(outboxes) == 1
    assert outboxes[0]["action_id"] == old["action_id"]
    assert outboxes[0]["message"] == "请再发同一句"
    with store._connect() as conn:
        retry_events = conn.execute(
            "SELECT COUNT(*) FROM task_action_events "
            "WHERE attempt_id=? AND event_type='manual_retry_requested'",
            (attempts[0]["id"],),
        ).fetchone()[0]
    assert retry_events == 0


def test_restart_quarantines_claimed_row_but_keeps_pending_row_owned(store):
    claimed_id, _tm, _nap = _persist_due_text(
        store, owner="1001", payload={"text": "正在发送"},
    )
    pending_id, _tm, _nap = _persist_due_text(
        store, owner="1002", payload={"text": "尚未领取"},
    )
    _task, _attempts, outboxes = _rows(store, claimed_id)
    assert store.claim_send_outbox(outboxes[0]["action_id"])

    assert store.recover_send_outbox_after_restart() == 1
    assert store.recover_sending_tasks() == 0

    task, attempts, outboxes = _rows(store, claimed_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "uncertain", "uncertain", "uncertain",
    )
    task, attempts, outboxes = _rows(store, pending_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "sending", "outbox_pending", "pending",
    )


def test_known_confirmed_projection_failure_finishes_delivery_then_repairs_db(
        store, monkeypatch):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "平台已经确认"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 323},
    })
    original = store._commit_confirmed_action_conn

    def fail_projection(*_args, **_kwargs):
        raise ConfirmedProjectionError("INJECTED_PROJECTION_FAILURE")

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", fail_projection)
    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["state"] == "confirmed"
    assert attempts[0]["accounting_state"] == "pending_repair"
    assert outboxes[0]["status"] == "confirmed_unaccounted"

    with store._connect() as conn:
        conn.execute(
            "UPDATE task_action_attempts SET finalized_at='2000-01-01 00:00:00' "
            "WHERE id=?", (attempts[0]["id"],),
        )

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", original)
    assert store.repair_confirmed_projection(outboxes[0]["action_id"]) in {
        "confirmed", "confirmed_duplicate",
    }
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["state"] == "confirmed"
    assert attempts[0]["accounting_state"] == "clean"
    assert attempts[0]["finalized_at"] == "2000-01-01 00:00:00"
    assert outboxes == []
    client._call_api.assert_awaited_once()


def test_confirmed_double_db_failure_stays_non_replayable_and_visible(
        store, monkeypatch):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "平台确认后双写失败"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 326},
    })

    def fail_settle(*_args, **_kwargs):
        raise sqlite3.OperationalError("INJECTED_SETTLE_FAILURE")

    def fail_fence(*_args, **_kwargs):
        raise sqlite3.OperationalError("INJECTED_FENCE_FAILURE")

    monkeypatch.setattr(store, "settle_send_outbox", fail_settle)
    monkeypatch.setattr(store, "mark_send_outbox_confirmed_unaccounted", fail_fence)

    assert _run(client.process_send_outbox()) == 1
    assert _run(client.process_send_outbox()) == 0
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "sending", "sending", "sending",
    )
    client._call_api.assert_awaited_once()

    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET updated_at='2000-01-01 00:00:00' "
            "WHERE action_id=?", (outboxes[0]["action_id"],),
        )
    health = store.get_send_outbox_health()
    assert health["open"] == 1
    assert health["stale_sending"] == 1
    assert health["needs_review"] >= 1


def test_bare_sending_task_claim_is_visible_for_safe_restart_recovery(store):
    task_id = store.create_task(
        "1001", "冻结前数据库连续失败", "2020-01-01 00:00",
    )
    assert store.claim_task_for_send(task_id)

    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], task["current_attempt_id"]) == ("sending", None)
    assert attempts == []
    assert outboxes == []
    health = store.get_send_outbox_health()
    assert health["bare_sending_tasks"] == 1
    assert health["needs_review"] >= 1


def test_linked_outbox_cannot_be_deleted_without_confirmation(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "不能因 outbox 被异常删除而假绿"},
    )
    _task, _attempts, outboxes = _rows(store, task_id)
    with pytest.raises(sqlite3.IntegrityError, match="requires confirmation"):
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM send_outbox WHERE action_id=?",
                (outboxes[0]["action_id"],),
            )


def test_known_confirmed_projection_conflict_is_terminal_not_retryable(
        store, monkeypatch):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "冲突也不能重发"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 324},
    })

    def conflict(*_args, **_kwargs):
        raise ConfirmedProjectionConflict('{"old":1}', '{"new":1}')

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", conflict)
    assert _run(client.process_send_outbox()) == 1
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["state"] == "confirmed"
    assert attempts[0]["accounting_state"] == "conflict"
    assert outboxes[0]["status"] == "confirmed_conflict"
    assert store.list_due_send_outbox() == []
    assert store.get_send_outbox_health()["linked_invariant_violations"] == 0


def test_projection_repair_failure_preserves_existing_conflict(
        store, monkeypatch):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "人工冲突不能降级为自动修复"},
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 325},
    })

    def conflict(*_args, **_kwargs):
        raise ConfirmedProjectionConflict('{"old":2}', '{"new":2}')

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", conflict)
    assert _run(client.process_send_outbox()) == 1
    _task, _attempts, outboxes = _rows(store, task_id)
    outbox_id = outboxes[0]["action_id"]
    with store._connect() as conn:
        conflict_before = tuple(conn.execute(
            "SELECT existing_json,incoming_json FROM action_projection_conflicts "
            "WHERE outbox_id=?", (outbox_id,),
        ).fetchone())

    def still_broken(*_args, **_kwargs):
        raise ConfirmedProjectionError("REPAIR_STILL_BROKEN")

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", still_broken)
    assert store.repair_confirmed_projection(outbox_id) == "confirmed_conflict"
    task, attempts, outboxes = _rows(store, task_id)
    assert task["status"] == "done"
    assert attempts[0]["accounting_state"] == "conflict"
    assert outboxes[0]["status"] == "confirmed_conflict"
    with store._connect() as conn:
        conflict_after = tuple(conn.execute(
            "SELECT existing_json,incoming_json FROM action_projection_conflicts "
            "WHERE outbox_id=?", (outbox_id,),
        ).fetchone())
    assert conflict_after == conflict_before


def test_legacy_outbox_mutators_delegate_or_refuse_for_linked_rows(store):
    uncertain_id, _tm, _nap = _persist_due_text(
        store, owner="1001", payload={"text": "不能裸删"},
    )
    _task, _attempts, outboxes = _rows(store, uncertain_id)
    outbox_id = outboxes[0]["action_id"]
    assert store.claim_send_outbox(outbox_id)
    assert store.complete_send_outbox(outbox_id) is False
    assert store.mark_send_outbox_uncertain(outbox_id, "lost response") is True
    task, attempts, outboxes = _rows(store, uncertain_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "uncertain", "uncertain", "uncertain",
    )

    retry_id, _tm, _nap = _persist_due_text(
        store, owner="1002", payload={"text": "统一失败结算"},
    )
    _task, _attempts, outboxes = _rows(store, retry_id)
    outbox_id = outboxes[0]["action_id"]
    assert store.claim_send_outbox(outbox_id)
    assert store.fail_send_outbox(outbox_id, "network", max_attempts=1) is True
    task, attempts, outboxes = _rows(store, retry_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "failed", "dead", "dead",
    )


def test_cancel_linked_uncertain_returns_false_without_breaking_audit(store):
    task_id, _tm, _nap = _persist_due_text(
        store, payload={"text": "送达不确定时不能伪装成已取消"},
    )
    _task, _attempts, outboxes = _rows(store, task_id)
    outbox_id = outboxes[0]["action_id"]
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "uncertain", error_code="LOST_RESPONSE",
    ) == "uncertain"

    assert store.cancel_task(task_id, "1001") is False
    task, attempts, outboxes = _rows(store, task_id)
    assert (task["status"], attempts[0]["state"], outboxes[0]["status"]) == (
        "uncertain", "uncertain", "uncertain",
    )


def test_legacy_sticker_retry_has_only_task_manager_owner(store):
    class Stickers:
        def match_by_emotion_text(self, _emotion, count=1):
            return ["[CQ:image,file=s.jpg]"][:count]

    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })
    tm = TaskManager(store, client, stickers=Stickers())
    task_id = store.create_task(
        "1001", "贴图提醒", "2020-01-01 00:00",
        action_payload={"sticker_emotion": "开心"},
    )

    _run(tm._check_and_send())

    with store._connect() as conn:
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM send_outbox"
        ).fetchone()[0]
    assert task_status == "pending"
    assert outbox_count == 0
    assert client._call_api.await_count == 1


@pytest.mark.parametrize("group_id", ["", "10007"])
def test_legacy_voice_retry_has_only_task_manager_owner(
        store, group_id):
    from agent.handler import MessageHandler

    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })

    handler = MessageHandler.__new__(MessageHandler)
    handler.napcat = client
    handler.voice_enabled = True
    handler.voice = types.SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=["voice.wav"]),
        to_cq=lambda _path: "[CQ:record,file=voice.wav]",
    )
    handler.mood = None
    handler._voice_emotion_history = {}
    handler._find_last_group = lambda _user_id: ""
    handler._is_voice_blocked = lambda _scope: False
    handler._text_to_voice_script = AsyncMock(return_value="语音提醒")

    tm = TaskManager(store, client, voice_sender=handler._task_voice_sender)
    task_id = store.create_task(
        "1001", "语音提醒", "2020-01-01 00:00", group_id,
        action_payload={"voice_text": "记得喝水"},
    )

    _run(tm._check_and_send())

    with store._connect() as conn:
        task_status = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM send_outbox"
        ).fetchone()[0]
    assert task_status == "pending"
    assert outbox_count == 0
    assert client._call_api.await_count == 1


def test_outbox_worker_does_not_suppress_another_targets_retryable_enqueue(store):
    original_id = store.enqueue_send_outbox("group", "100", "worker message")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    worker_started = asyncio.Event()
    release_worker = asyncio.Event()

    async def call_api(_action, payload):
        if str(payload.get("group_id")) == "100":
            worker_started.set()
            await release_worker.wait()
        return {"status": "failed", "_transport_error": "NETWORK"}

    client._call_api = call_api

    async def scenario():
        worker = asyncio.create_task(client.process_send_outbox())
        await worker_started.wait()
        normal = await client.send_group_message("200", "normal message")
        release_worker.set()
        await worker
        return normal

    result = _run(scenario())
    assert result.retryable is True
    with store._connect() as conn:
        persisted_targets = {
            row[0] for row in conn.execute(
                "SELECT target_id FROM send_outbox WHERE status='pending'"
            ).fetchall()
        }
    assert persisted_targets == {"100", "200"}
    assert store.get_send_outbox(original_id)["attempts"] == 1
