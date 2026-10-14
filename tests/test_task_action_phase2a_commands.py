"""ADR-005 Phase 2a：人工重试的 TaskManager、命令与开关接线。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent.handler_commands import CommandRouter
from agent.tasks import TaskManager


def _dead_text_task(store, owner="1001"):
    task_id = store.create_task(owner, "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "冻结正文")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "failed", error_code="HTTP_400",
        error_detail="definite failure", max_attempts=1,
    ) == "dead"
    return task_id, action


def _router(*, enabled, send_claims=True, retry_result=None, tasks=None):
    manager = SimpleNamespace(
        retry_generation=Mock(return_value=retry_result),
        retry=Mock(return_value=False),
        cancel=Mock(return_value=False),
        list_for=Mock(return_value=list(tasks or [])),
    )
    handler = SimpleNamespace(
        config={"tasks": {
            "manual_retry_generation_enabled": enabled,
            "send_outbox_claims_enabled": send_claims,
        }},
        task_manager=manager,
    )
    return CommandRouter(handler), manager


def test_list_tasks_includes_failed_attempt_token_and_delivery_state(store):
    task_id, action = _dead_text_task(store)

    rows = store.list_tasks("1001")

    task = next(row for row in rows if row["id"] == task_id)
    assert task["status"] == "failed"
    assert task["current_attempt_id"] == action["attempt_id"]
    assert task["current_generation"] == 0
    assert task["attempt_state"] == "dead"
    assert task["accounting_state"] == "none"
    assert task["outbox_status"] == "dead"


def test_task_manager_generation_wrapper_freezes_phase2a_verification_contract(store):
    task_id, action = _dead_text_task(store)
    manager = TaskManager(store, SimpleNamespace())

    result = manager.retry_generation(
        task_id, "1001", expected_attempt_id=action["attempt_id"],
        request_id="manager-retry",
    )

    assert result["code"] == "RETRY_QUEUED"
    with store._connect() as conn:
        request = conn.execute(
            "SELECT verification_result,force_resend_ack,actor "
            "FROM task_action_retry_requests WHERE request_id='manager-retry'"
        ).fetchone()
    assert request == ("NOT_REQUIRED", 0, "qq:1001:command")


def test_versioned_retry_command_is_disabled_by_default_gate():
    router, manager = _router(enabled=False)

    reply = asyncio.run(router._cmd_task("1001", "重试 42@107"))

    assert "未启用" in reply
    manager.retry_generation.assert_not_called()


def test_versioned_retry_command_uses_exact_attempt_token_and_trusted_actor():
    result = {
        "ok": True, "code": "RETRY_QUEUED", "task_id": 42,
        "current_attempt_id": 108, "current_generation": 1,
        "new_attempt_id": 108, "new_generation": 1,
        "duplicate_risk": False, "request_id": "ignored-by-command",
    }
    router, manager = _router(enabled=True, retry_result=result)

    reply = asyncio.run(router._cmd_task("1001", "重试 42@107"))

    assert "重新排队" in reply
    call = manager.retry_generation.call_args
    assert call.args == (42, "1001")
    assert call.kwargs["expected_attempt_id"] == 107
    assert isinstance(call.kwargs["request_id"], str)
    assert len(call.kwargs["request_id"]) == 32


def test_versioned_retry_command_derives_request_id_from_inbound_event():
    """同一条持久入站事件重放必须复用同一个请求 ID。"""
    result = {
        "ok": False, "code": "STALE_ATTEMPT", "task_id": 42,
        "current_attempt_id": 108, "current_generation": 1,
        "new_attempt_id": None, "new_generation": None,
        "duplicate_risk": False, "request_id": "ignored",
    }
    router, manager = _router(enabled=True, retry_result=result)
    event_key = "v2:private:1001:1787940000:1001:12345"

    asyncio.run(router.handle(
        "1001", "/任务 重试 42@107", event_key=event_key,
    ))
    asyncio.run(router.handle(
        "1001", "/任务 重试 42@107", event_key=event_key,
    ))

    calls = manager.retry_generation.call_args_list
    assert len(calls) == 2
    first_id = calls[0].kwargs["request_id"]
    assert first_id == calls[1].kwargs["request_id"]
    assert len(first_id) == 64
    assert all(ch in "0123456789abcdef" for ch in first_id)


def test_versioned_retry_command_converts_internal_error_to_stable_reply():
    router, manager = _router(enabled=True)
    manager.retry_generation.side_effect = RuntimeError(
        "injected internal database detail"
    )

    reply = asyncio.run(router._cmd_task("1001", "重试 42@107"))

    assert "未完成" in reply
    assert "injected internal database detail" not in reply
    assert "重新排队" not in reply


def test_versioned_retry_command_interlocks_with_outbox_worker_gate():
    router, manager = _router(enabled=True, send_claims=False)

    reply = asyncio.run(router._cmd_task("1001", "重试 42@107"))

    assert "发送队列已暂停" in reply
    manager.retry_generation.assert_not_called()


def test_versioned_retry_command_rejects_ambiguous_syntax_without_falling_to_list():
    router, manager = _router(enabled=True)

    reply = asyncio.run(router._cmd_task("1001", "重试 42@107 未收到"))

    assert "用法" in reply
    manager.retry_generation.assert_not_called()
    manager.list_for.assert_not_called()


def test_versioned_retry_command_rejects_sqlite_integer_overflow_token():
    router, manager = _router(enabled=True)

    reply = asyncio.run(router._cmd_task(
        "1001", "重试 99999999999999999999@99999999999999999999",
    ))

    assert "用法" in reply
    manager.retry_generation.assert_not_called()


def test_task_list_renders_versioned_token_and_phase2a_help():
    router, _manager = _router(enabled=True, tasks=[{
        "id": 42, "remind_at": "2026-08-29 09:00",
        "description": "冻结正文", "status": "failed",
        "current_attempt_id": 107, "attempt_state": "dead",
    }])

    reply = asyncio.run(router._cmd_task("1001", ""))

    assert "42@107" in reply
    assert "/任务 重试 编号@attempt" in reply


def test_manual_retry_gate_is_default_off_in_config_and_qt_console():
    import ast
    import yaml

    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    console = (root / "糖糖控制台_qt.py").read_text(encoding="utf-8")
    console_tree = ast.parse(console)
    task_section = next(
        node for node in ast.walk(console_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_add_section"
        and ast.literal_eval(node.args[0]) == "tasks"
    )
    field_paths = {
        ast.literal_eval(field.elts[0]) for field in task_section.args[2].elts
    }
    assert "tasks.manual_retry_generation_enabled" not in field_paths
    assert "manual_retry_generation_enabled" in config["tasks"]
    assert isinstance(config["tasks"]["manual_retry_generation_enabled"], bool)

    main_tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "manual_retry_generation_enabled"
        for node in ast.walk(main_tree)
    )
