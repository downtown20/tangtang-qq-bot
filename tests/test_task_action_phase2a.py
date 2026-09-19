"""ADR-005 Phase 2a：确定失败单文本的结构化人工重试 API。"""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest


def _dead_text_task(store, *, owner="1001", text="冻结后原样重发"):
    task_id = store.create_task(owner, "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, text)
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "failed", error_code="HTTP_400",
        error_detail="definite bad request", max_attempts=1,
    ) == "dead"
    return task_id, action


def _retry(store, task_id, attempt_id, request_id="retry-api-1", owner="1001"):
    return store.retry_task_generation(
        task_id, owner, expected_attempt_id=attempt_id,
        request_id=request_id, verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )


def test_dead_text_retry_clones_frozen_plan_and_queues_one_new_outbox(store):
    task_id, old = _dead_text_task(store)
    result = _retry(store, task_id, old["attempt_id"])

    assert result == {
        "ok": True,
        "code": "RETRY_QUEUED",
        "task_id": task_id,
        "current_attempt_id": result["new_attempt_id"],
        "current_generation": 1,
        "new_attempt_id": result["new_attempt_id"],
        "new_generation": 1,
        "duplicate_risk": False,
        "request_id": "retry-api-1",
    }
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        attempts = conn.execute(
            "SELECT * FROM task_action_attempts WHERE task_id=? ORDER BY generation",
            (task_id,),
        ).fetchall()
        outboxes = conn.execute(
            "SELECT o.* FROM send_outbox o JOIN task_action_attempts a "
            "ON a.id=o.task_attempt_id WHERE a.task_id=? ORDER BY a.generation",
            (task_id,),
        ).fetchall()
        request = conn.execute(
            "SELECT * FROM task_action_retry_requests WHERE request_id='retry-api-1'"
        ).fetchone()
        events = conn.execute(
            "SELECT attempt_id,event_type,actor FROM task_action_events "
            "WHERE event_type LIKE 'manual_retry_%' ORDER BY id"
        ).fetchall()

    old_plan = json.loads(attempts[0]["plan_json"])
    new_plan = json.loads(attempts[1]["plan_json"])
    old_child = old_plan["children"][0]
    new_child = new_plan["children"][0]
    assert (task["status"], task["current_attempt_id"]) == (
        "sending", attempts[1]["id"],
    )
    assert [(row["generation"], row["state"]) for row in attempts] == [
        (0, "dead"), (1, "outbox_pending"),
    ]
    assert len(outboxes) == 2
    assert (outboxes[0]["status"], outboxes[1]["status"]) == ("dead", "pending")
    assert outboxes[1]["message"] == outboxes[0]["message"] == "冻结后原样重发"
    assert new_child["payload"] == old_child["payload"]
    assert new_child["conversation_ref"] == old_child["conversation_ref"]
    assert new_child["action_id"] != old_child["action_id"]
    assert new_plan["source_id"] != old_plan["source_id"]
    assert request["result"] == "accepted"
    assert request["new_attempt_id"] == attempts[1]["id"]
    assert [(row["attempt_id"], row["event_type"], row["actor"]) for row in events] == [
        (attempts[0]["id"], "manual_retry_requested", "qq:1001:command"),
        (attempts[1]["id"], "manual_retry_created", "qq:1001:command"),
    ]
    with store._connect() as conn:
        metadata = conn.execute(
            "SELECT metadata_json FROM task_action_events "
            "WHERE event_type='manual_retry_created'"
        ).fetchone()[0]
    metadata = json.loads(metadata)
    assert metadata["verification_result"] == "NOT_REQUIRED"
    assert metadata["force_resend_ack"] is False


def test_retry_request_id_replay_returns_original_result_without_new_generation(store):
    task_id, old = _dead_text_task(store)
    first = _retry(store, task_id, old["attempt_id"], "retry-replay")
    second = _retry(store, task_id, old["attempt_id"], "retry-replay")

    assert second == first
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_retry_requests "
            "WHERE request_id='retry-replay'"
        ).fetchone()[0] == 1


def test_retry_not_found_or_not_owner_is_uniform_and_durably_rejected(store):
    task_id, old = _dead_text_task(store, owner="owner")
    hidden = _retry(store, task_id, old["attempt_id"], "retry-hidden", owner="other")
    missing = _retry(store, 999999, 888888, "retry-missing", owner="other")

    assert hidden["code"] == missing["code"] == "NOT_FOUND_OR_NOT_OWNER"
    assert hidden["ok"] is missing["ok"] is False
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT request_id,task_id,expected_attempt_id,result,reason_code "
            "FROM task_action_retry_requests "
            "WHERE request_id IN ('retry-hidden','retry-missing') ORDER BY request_id"
        ).fetchall()
    assert rows == [
        ("retry-hidden", None, None, "rejected", "NOT_FOUND_OR_NOT_OWNER"),
        ("retry-missing", None, None, "rejected", "NOT_FOUND_OR_NOT_OWNER"),
    ]


def test_real_retries_advance_monotonically_without_rewriting_generation_zero(store):
    task_id, old = _dead_text_task(store)
    first = _retry(store, task_id, old["attempt_id"], "retry-generation-1")
    with store._connect() as conn:
        first_outbox = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (first["new_attempt_id"],),
        ).fetchone()[0]
    assert store.claim_send_outbox(first_outbox)
    assert store.settle_send_outbox(
        first_outbox, "failed", error_code="HTTP_400",
        error_detail="definite second failure", max_attempts=1,
    ) == "dead"

    second = _retry(
        store, task_id, first["new_attempt_id"], "retry-generation-2",
    )
    assert (second["new_generation"], second["current_generation"]) == (2, 2)
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT generation,state,retry_request_id FROM task_action_attempts "
            "WHERE task_id=? ORDER BY generation", (task_id,),
        ).fetchall()
    assert rows == [
        (0, "dead", None),
        (1, "dead", "retry-generation-1"),
        (2, "outbox_pending", "retry-generation-2"),
    ]


def test_open_or_uncertain_attempt_is_durably_rejected(store):
    pending_task = store.create_task("1001", "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(pending_task)
    pending = store.persist_task_text_action(pending_task, "仍在队列")
    in_flight = _retry(
        store, pending_task, pending["attempt_id"], "retry-in-flight",
    )

    uncertain_task = store.create_task("1001", "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(uncertain_task)
    uncertain = store.persist_task_text_action(uncertain_task, "送达不确定")
    assert store.claim_send_outbox(uncertain["outbox_id"])
    assert store.settle_send_outbox(
        uncertain["outbox_id"], "uncertain", error_code="NETWORK_UNCERTAIN",
        error_detail="timeout after write",
    ) == "uncertain"
    unsupported = _retry(
        store, uncertain_task, uncertain["attempt_id"], "retry-uncertain",
    )

    assert in_flight["code"] == "IN_FLIGHT"
    assert unsupported["code"] == "UNSUPPORTED_STATE"
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT request_id,result,reason_code FROM task_action_retry_requests "
            "WHERE request_id IN ('retry-in-flight','retry-uncertain') "
            "ORDER BY request_id"
        ).fetchall()
    assert rows == [
        ("retry-in-flight", "rejected", "IN_FLIGHT"),
        ("retry-uncertain", "rejected", "UNSUPPORTED_STATE"),
    ]


def test_confirmed_attempt_and_phase2b_verification_modes_are_rejected(store):
    confirmed_task = store.create_task("1001", "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(confirmed_task)
    confirmed = store.persist_task_text_action(confirmed_task, "已送达")
    assert store.claim_send_outbox(confirmed["outbox_id"])
    assert store.settle_send_outbox(
        confirmed["outbox_id"], "confirmed", message_ids=(7788,),
    ) == "confirmed"
    delivered = _retry(
        store, confirmed_task, confirmed["attempt_id"], "retry-confirmed",
    )
    delivered_with_unsupported_mode = store.retry_task_generation(
        confirmed_task, "1001", expected_attempt_id=confirmed["attempt_id"],
        request_id="retry-confirmed-unsupported-mode",
        verification_result="VERIFIED_NOT_DELIVERED", force_resend_ack=False,
    )

    failed_task, failed = _dead_text_task(store)
    phase2b = store.retry_task_generation(
        failed_task, "1001", expected_attempt_id=failed["attempt_id"],
        request_id="retry-phase2b-mode",
        verification_result="VERIFIED_NOT_DELIVERED",
        force_resend_ack=False,
    )

    assert delivered["code"] == "ALREADY_DELIVERED"
    assert delivered_with_unsupported_mode["code"] == "ALREADY_DELIVERED"
    assert phase2b["code"] == "VERIFICATION_NOT_SUPPORTED"


def test_in_flight_state_precedes_unsupported_verification_mode(store):
    task_id = store.create_task("1001", "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "仍在队列")

    result = store.retry_task_generation(
        task_id, "1001", expected_attempt_id=action["attempt_id"],
        request_id="retry-inflight-unsupported-mode",
        verification_result="DELIVERY_UNKNOWN", force_resend_ack=False,
    )

    assert result["code"] == "IN_FLIGHT"


def test_bare_failed_without_durable_receipt_is_rejected_as_invariant_break(store):
    task_id, old = _dead_text_task(store)
    with store._connect() as conn:
        conn.execute(
            "DELETE FROM action_receipt_mailbox WHERE action_id=?",
            (old["domain_action_id"],),
        )

    result = _retry(store, task_id, old["attempt_id"], "retry-no-proof")

    assert result["code"] == "INVARIANT_BROKEN"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 1


def test_malformed_prior_receipt_is_durably_rejected_not_raised(store):
    task_id, old = _dead_text_task(store)
    with store._connect() as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_outbox_link_immutable'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_task_action_outbox_link_immutable")
        template = json.loads(conn.execute(
            "SELECT receipt_template FROM send_outbox WHERE action_id=?",
            (old["outbox_id"],),
        ).fetchone()[0])
        template["actual"].pop("text", None)
        conn.execute(
            "UPDATE send_outbox SET receipt_template=? WHERE action_id=?",
            (json.dumps(template, ensure_ascii=False, sort_keys=True),
             old["outbox_id"]),
        )
        conn.execute(trigger_sql)

    result = _retry(store, task_id, old["attempt_id"], "retry-bad-receipt")

    assert result["code"] == "INVARIANT_BROKEN"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT result,reason_code FROM task_action_retry_requests "
            "WHERE request_id='retry-bad-receipt'"
        ).fetchone() == ("rejected", "INVARIANT_BROKEN")


def test_request_id_conflict_never_creates_another_generation(store):
    task_id, old = _dead_text_task(store)
    accepted = _retry(store, task_id, old["attempt_id"], "retry-conflict")
    conflict = store.retry_task_generation(
        task_id, "1001", expected_attempt_id=accepted["new_attempt_id"],
        request_id="retry-conflict", verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )

    assert conflict["code"] == "REQUEST_ID_CONFLICT"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 2


def test_cross_owner_request_replay_cannot_read_original_retry_result(store):
    task_id, old = _dead_text_task(store, owner="victim")
    accepted = _retry(
        store, task_id, old["attempt_id"], "retry-owner-secret", owner="victim",
    )

    attacker = _retry(
        store, task_id, old["attempt_id"], "retry-owner-secret", owner="attacker",
    )

    assert accepted["code"] == "RETRY_QUEUED"
    assert attacker["code"] == "REQUEST_ID_CONFLICT"
    assert attacker["task_id"] is None
    assert attacker["new_attempt_id"] is None


def test_concurrent_retry_has_exactly_one_winner(store):
    task_id, old = _dead_text_task(store)

    def invoke(number):
        return _retry(
            store, task_id, old["attempt_id"], f"retry-race-{number}",
        )

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(invoke, range(25)))

    assert [row["code"] for row in results].count("RETRY_QUEUED") == 1
    assert [row["code"] for row in results].count("STALE_ATTEMPT") == 24
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_retry_requests WHERE result='accepted'"
        ).fetchone()[0] == 1


def test_retry_request_insert_failure_rolls_back_pointer_attempt_outbox_and_events(store):
    task_id, old = _dead_text_task(store)
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER inject_retry_audit_failure "
            "BEFORE INSERT ON task_action_retry_requests "
            "WHEN NEW.result='accepted' BEGIN "
            "SELECT RAISE(ABORT,'injected retry audit failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected retry audit failure"):
        _retry(store, task_id, old["attempt_id"], "retry-rollback")

    with store._connect() as conn:
        task = conn.execute(
            "SELECT status,current_attempt_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        attempts = conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,),
        ).fetchone()[0]
        linked_outboxes = conn.execute(
            "SELECT COUNT(*) FROM send_outbox o JOIN task_action_attempts a "
            "ON a.id=o.task_attempt_id WHERE a.task_id=?", (task_id,),
        ).fetchone()[0]
        manual_events = conn.execute(
            "SELECT COUNT(*) FROM task_action_events "
            "WHERE event_type LIKE 'manual_retry_%'"
        ).fetchone()[0]
        requests = conn.execute(
            "SELECT COUNT(*) FROM task_action_retry_requests"
        ).fetchone()[0]
    assert task == ("failed", old["attempt_id"])
    assert (attempts, linked_outboxes, manual_events, requests) == (1, 1, 0, 0)


def test_oversized_sqlite_ids_return_invalid_request_without_touching_db(store):
    oversized = 99_999_999_999_999_999_999

    result = store.retry_task_generation(
        oversized, "1001", expected_attempt_id=oversized,
        request_id="retry-overflow", verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )

    assert result["code"] == "INVALID_REQUEST"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_retry_requests"
        ).fetchone()[0] == 0
