"""ADR-005 Phase 2b：逻辑 item、确认事实与跨 generation 归约红测。"""

import json
import sqlite3

import pytest

from agent.store import (
    ConfirmedProjectionConflict,
    ConfirmedProjectionError,
    _LATE_CONFIRMATION_CAPABILITY,
)


def _plan(task_id, generation, texts):
    from agent.action_contract import ActionEnvelope, ConversationRef, derive_action_id
    from agent.action_plan import ActionPlan
    source_id = f"task:{task_id}:attempt:{generation}"
    children = []
    for ordinal, text in enumerate(texts):
        action_id = derive_action_id(
            source_id=source_id, scope_id="_private_u1", kind="text",
            channel="private", target="u1", payload={"text": text},
            ordinal=ordinal, schema_version=2, identity_version=1,
        )
        children.append({
            "action_id": action_id,
            "kind": "text",
            "channel": "private",
            "target": "u1",
            "payload": {"text": text},
            "review": False,
            "source_id": source_id,
            "scope_id": "_private_u1",
            "ordinal": ordinal,
            "schema_version": 2,
            "identity_version": 1,
            "conversation_ref": {
                "projection_kind": "conversation_reply",
                "conversation_user_id": "u1",
                "group_id": "",
                "source_chat_id": None,
                "self_memory_eligible": False,
            },
        })
    envelopes = tuple(ActionEnvelope(
        action_id=child["action_id"], kind=child["kind"],
        channel=child["channel"], target=child["target"],
        payload=child["payload"], review=child["review"],
        schema_version=child["schema_version"], source_id=child["source_id"],
        scope_id=child["scope_id"], ordinal=child["ordinal"],
        identity_version=child["identity_version"],
        conversation_ref=ConversationRef.from_value(child["conversation_ref"]),
    ) for child in children)
    plan_id = ActionPlan.create(
        source_id=source_id, scope_id="_private_u1", channel="private",
        target="u1", children=envelopes, created_at="2026-08-29T00:00:00+08:00",
        role_id="default", library_id="", schema_version=2,
    ).plan_id
    return {
        "plan_id": plan_id,
        "source_id": source_id,
        "scope_id": "_private_u1",
        "channel": "private",
        "target": "u1",
        "created_at": "2026-08-29T00:00:00+08:00",
        "role_id": "default",
        "library_id": "",
        "schema_version": 2,
        "children": children,
    }


def _insert_attempt(store, task_id, generation, texts):
    """Insert a synthetic multi-child attempt for reducer tests."""
    plan = _plan(task_id, generation, texts)
    now = "2026-08-29 00:00:00"
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        if generation == 0:
            conn.execute(
                "UPDATE tasks SET status='sending' WHERE id=?", (task_id,)
            )
        else:
            conn.execute(
                "UPDATE tasks SET status='sending' WHERE id=?", (task_id,)
            )
        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
            "VALUES (?,?,?,?, 'persisted',?,?)",
            (task_id, generation, plan["plan_id"], json.dumps(plan, ensure_ascii=False),
             now, now),
        )
        attempt_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_attempt_id=? WHERE id=?",
            (attempt_id, task_id),
        )
        outboxes = []
        for child in plan["children"]:
            outbox_id = f"out-{task_id}-{generation}-{child['ordinal']}"
            template = {
                "schema_version": 2,
                "action_id": child["action_id"],
                "kind": "text",
                "channel": "private",
                "target": "u1",
                "source_id": plan["source_id"],
                "scope_id": "_private_u1",
                "ordinal": child["ordinal"],
                "actual": {"requested": child["payload"]["text"],
                           "text": child["payload"]["text"]},
                "identity_version": 1,
                "identity_payload": child["payload"],
                "conversation_ref": child["conversation_ref"],
            }
            conn.execute(
                "INSERT INTO send_outbox "
                "(action_id,target_type,target_id,group_id,message,receipt_template,"
                "status,attempts,next_retry_at,last_error,created_at,updated_at,"
                "domain_action_id,task_attempt_id,ordinal,retry_owner) "
                "VALUES (?, 'private','u1','',?,?,'pending',0,'','',?,?,?, ?,?,'outbox')",
                (outbox_id, child["payload"]["text"], json.dumps(template, ensure_ascii=False),
                 now, now, child["action_id"], attempt_id, child["ordinal"]),
            )
            outboxes.append(outbox_id)
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (attempt_id,),
        )
        conn.commit()
    return attempt_id, outboxes


def test_phase2b_confirmation_schema_and_delete_guard(store):
    with store._connect() as conn:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "task_action_confirmations" in names
        assert "task_action_items" in names
        assert "task_action_children" in names

    task_id = store.create_task("u1", "多子动作", "2020-01-01 00:00")
    _attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])
    with pytest.raises(sqlite3.IntegrityError, match="confirmation"):
        with store._connect() as conn:
            conn.execute("DELETE FROM send_outbox WHERE action_id=?", (outboxes[0],))


def test_persist_materializes_child_before_worker_claim_and_restart(tmp_path):
    from agent.store import Store

    db = tmp_path / "persist-child-before-claim.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "重启窗口", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    restarted = Store(str(db))
    with restarted._connect() as conn:
        child = conn.execute(
            "SELECT action_id,outbox_id,state FROM task_action_children "
            "WHERE attempt_id=?", (action["attempt_id"],),
        ).fetchone()
    assert tuple(child) == (action["domain_action_id"], action["outbox_id"], "pending")


def test_retry_materializes_child_before_worker_claim_and_restart(tmp_path):
    from agent.store import Store

    db = tmp_path / "retry-child-before-claim.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "重试重启窗口", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action0 = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action0["outbox_id"])
    assert store.settle_send_outbox(
        action0["outbox_id"], "failed", error_code="HTTP_400", max_attempts=1,
    ) == "dead"
    retry = store.retry_task_generation(
        task_id, "u1", expected_attempt_id=action0["attempt_id"],
        request_id="retry-restart-child", verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )
    assert retry["ok"] is True
    restarted = Store(str(db))
    with restarted._connect() as conn:
        child = conn.execute(
            "SELECT action_id,outbox_id,state FROM task_action_children "
            "WHERE attempt_id=?", (retry["new_attempt_id"],),
        ).fetchone()
    assert child is not None
    assert child[0].startswith("act-")
    assert child[1]
    assert child[2] == "pending"


def test_multi_child_first_confirmation_keeps_attempt_open_and_second_claimable(store):
    task_id = store.create_task("u1", "多子动作", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])

    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(101,)) == "confirmed"

    with store._connect() as conn:
        row = conn.execute(
            "SELECT state FROM task_action_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        task = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row[0] == "sending"
    assert task[0] == "sending"
    assert store.get_send_outbox_health()["linked_invariant_violations"] == 0
    assert store.claim_send_outbox(outboxes[1])


def test_multi_child_late_confirmation_keeps_pending_repair_until_all_projection_is_clean(store):
    """兄弟动作完成不能覆盖仍未归账的迟到确认。"""
    task_id = store.create_task("u1", "多子动作迟到待修复", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])

    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(
        outboxes[0], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"
    assert store._record_task_confirmation_from_platform(
        outboxes[0], message_ids=(124,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    assert store.claim_send_outbox(outboxes[1])
    assert store.settle_send_outbox(outboxes[1], "confirmed", message_ids=(125,)) == "confirmed"

    with store._connect() as conn:
        accounting = conn.execute(
            "SELECT state,accounting_state FROM task_action_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        task = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert tuple(accounting) == ("confirmed", "pending_repair")
    assert task[0] == "done"
    assert store.get_send_outbox_health()["needs_review"] > 0

    assert store.repair_confirmed_projection(outboxes[0]) == "confirmed"
    with store._connect() as conn:
        accounting = conn.execute(
            "SELECT state,accounting_state FROM task_action_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    assert tuple(accounting) == ("confirmed", "clean")
    health = store.get_send_outbox_health()
    assert health["linked_invariant_violations"] == 0
    assert health["needs_review"] == 0


def test_projection_failure_confirmation_survives_restart_and_repeat_repair_failure(
        tmp_path, monkeypatch):
    """known-confirmed 冻结态可重启，重复投影失败不因重复 confirmation 崩溃。"""
    from agent.store import Store

    db = tmp_path / "confirmed-unaccounted-restart.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "投影失败重启", "2020-01-01 00:00")
    _attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    assert store.claim_send_outbox(outboxes[0])

    def fail_projection(*_args, **_kwargs):
        raise ConfirmedProjectionError("INJECTED_PROJECTION_FAILURE")

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", fail_projection)
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(126,)) == (
        "confirmed_unaccounted"
    )
    assert store.get_send_outbox_health()["linked_invariant_violations"] == 0

    restarted = Store(str(db))
    health = restarted.get_send_outbox_health()
    assert health["linked_invariant_violations"] == 0
    assert health["confirmed_unaccounted"] == 1

    original_commit = restarted._commit_confirmed_action_conn
    monkeypatch.setattr(restarted, "_commit_confirmed_action_conn", fail_projection)
    assert restarted.repair_confirmed_projection(outboxes[0]) == "confirmed_unaccounted"
    assert restarted.get_send_outbox(outboxes[0])["status"] == "confirmed_unaccounted"

    monkeypatch.setattr(restarted, "_commit_confirmed_action_conn", original_commit)
    assert restarted.repair_confirmed_projection(outboxes[0]) == "confirmed"
    assert restarted.get_send_outbox(outboxes[0]) is None


def test_multi_child_conflict_with_sibling_confirmation_is_health_consistent(
        store, monkeypatch):
    """冲突冻结的 child 与已确认兄弟并存时，只保留人工复核，不误报断链。"""
    task_id = store.create_task("u1", "多子动作冲突健康", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])
    original = store._commit_confirmed_action_conn

    def selective_conflict(conn, *args, **kwargs):
        if kwargs.get("outbox_id") == outboxes[0]:
            raise ConfirmedProjectionConflict('{"old":1}', '{"new":1}')
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", selective_conflict)
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(127,)) == (
        "confirmed_conflict"
    )
    assert store.claim_send_outbox(outboxes[1])
    assert store.settle_send_outbox(outboxes[1], "confirmed", message_ids=(128,)) == (
        "confirmed"
    )

    with store._connect() as conn:
        accounting = conn.execute(
            "SELECT state,accounting_state FROM task_action_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
    assert tuple(accounting) == ("confirmed", "conflict")
    health = store.get_send_outbox_health()
    assert health["linked_invariant_violations"] == 0
    assert health["confirmed_conflict"] == 1


def test_multi_child_late_repair_does_not_flag_pending_sibling_health(store):
    """已归账的子动作与仍待发送兄弟并存时，健康检查不应误报。"""
    task_id = store.create_task("u1", "多子动作迟到归账", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])

    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(
        outboxes[0], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"
    assert store._record_task_confirmation_from_platform(
        outboxes[0], message_ids=(123,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    assert store.repair_confirmed_projection(outboxes[0]) == "confirmed"

    with store._connect() as conn:
        attempt = conn.execute(
            "SELECT state FROM task_action_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        pending = conn.execute(
            "SELECT status FROM send_outbox WHERE action_id=?", (outboxes[1],)
        ).fetchone()
    assert attempt[0] == "sending"
    assert pending[0] == "pending"
    health = store.get_send_outbox_health()
    assert health["linked_invariant_violations"] == 0
    assert health["needs_review"] == 0


def test_late_confirmation_is_audited_and_cancels_unclaimed_descendant(store):
    task_id = store.create_task("u1", "迟到确认", "2020-01-01 00:00")
    _attempt0, outboxes0 = _insert_attempt(store, task_id, 0, ["甲"])
    assert store.claim_send_outbox(outboxes0[0])
    assert store.settle_send_outbox(
        outboxes0[0], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"

    result = store._record_task_confirmation_from_platform(
        outboxes0[0], message_ids=(202,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    )
    assert result == "confirmed"
    with store._connect() as conn:
        confirmation = conn.execute(
            "SELECT generation,ordinal,action_id,evidence_source,message_ids_json "
            "FROM task_action_confirmations"
        ).fetchone()
    assert confirmation[0:4] == (0, 0, confirmation[2], "late_settle")
    assert json.loads(confirmation[4]) == [202]


def test_public_late_confirmation_requires_terminal_outbox_and_evidence(store):
    task_id = store.create_task("u1", "迟到确认门槛", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    with pytest.raises(ValueError, match="trusted platform callback"):
        store.record_task_confirmation(
            action["outbox_id"], message_ids=(202,)
        )
    assert store._record_task_confirmation_from_platform(
        action["outbox_id"], message_ids=(202,),
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "not_late_confirmable"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_confirmations"
        ).fetchone()[0] == 0
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"
    with pytest.raises(ValueError, match="message id evidence"):
        store._record_task_confirmation_from_platform(
            action["outbox_id"], _capability=_LATE_CONFIRMATION_CAPABILITY,
        )
    assert store._record_task_confirmation_from_platform(
        action["outbox_id"], message_ids=(-7, "203", True, 203, 203), actor="audit",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT message_ids_json FROM task_action_confirmations"
        ).fetchone()[0] == "[-7,203]"


def test_confirmed_settle_without_message_id_stays_uncertain(store):
    """没有平台消息号时不能把发送写成永久 confirmed。"""
    task_id = store.create_task("u1", "空消息号确认", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "confirmed", message_ids=()
    ) == "uncertain"
    job = store.get_send_outbox(action["outbox_id"])
    assert job["status"] == "uncertain"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_confirmations"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM confirmed_action_facts"
        ).fetchone()[0] == 0


def test_confirmed_unaccounted_fence_without_message_id_is_rejected(store):
    """settle 异常兜底也不能用空 message_ids 伪造 known-confirmed。"""
    task_id = store.create_task("u1", "空消息号兜底", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.mark_send_outbox_confirmed_unaccounted(
        action["outbox_id"], message_ids=()
    ) is False
    job = store.get_send_outbox(action["outbox_id"])
    assert job["status"] == "uncertain"
    assert store.list_confirmed_projection_repairs() == []


def test_projection_repair_with_empty_message_ids_never_creates_fact(store):
    """历史损坏的空证据 known-confirmed 只能留待审查。"""
    task_id = store.create_task("u1", "空消息号修复", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_unaccounted',"
            "confirmed_message_ids='[]',confirmed_at='2026-08-29 00:00:01' "
            "WHERE action_id=?", (action["outbox_id"],),
        )
        conn.commit()
    assert store.repair_confirmed_projection(action["outbox_id"]) == (
        "confirmed_unaccounted"
    )
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM confirmed_action_facts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM send_outbox WHERE action_id=?",
            (action["outbox_id"],),
        ).fetchone()[0] == "confirmed_unaccounted"


def test_restart_rejects_legacy_empty_confirmation_evidence(tmp_path):
    """旧 confirmation 被污染为空证据时，启动校验必须 fail-closed。"""
    from agent.store import Store

    db = tmp_path / "legacy-empty-confirmation.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "旧空 confirmation", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "confirmed", message_ids=(777,)
    ) == "confirmed"
    with store._connect() as conn:
        conn.execute("DROP TRIGGER trg_task_action_confirmation_immutable_update")
        conn.execute(
            "UPDATE task_action_confirmations SET message_ids_json='[]'"
        )
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_immutable_update
            BEFORE UPDATE ON task_action_confirmations
            BEGIN SELECT RAISE(ABORT,'task action confirmation is append-only'); END
        """)
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="confirmation evidence is invalid"):
        Store(str(db))


@pytest.mark.parametrize("terminal_state,error_code", [
    ("uncertain", "NETWORK_UNCERTAIN"),
    ("failed", "HTTP_400"),
])
def test_late_confirmation_db_only_repair_upgrades_negative_mailbox(
        store, terminal_state, error_code):
    task_id = store.create_task("u1", "迟到确认归账", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], terminal_state, error_code=error_code,
        max_attempts=1,
    ) == ("dead" if terminal_state == "failed" else "uncertain")
    assert store._record_task_confirmation_from_platform(
        action["outbox_id"], message_ids=(204,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    assert store.repair_confirmed_projection(action["outbox_id"]) == "confirmed"
    with store._connect() as conn:
        fact = conn.execute(
            "SELECT message_ids_json FROM confirmed_action_facts "
            "WHERE domain_action_id=?", (action["domain_action_id"],),
        ).fetchone()
        mailbox = conn.execute(
            "SELECT action_status,receipt_json FROM action_receipt_mailbox "
            "WHERE action_id=?", (action["domain_action_id"],),
        ).fetchone()
    assert fact[0] == "[204]"
    assert mailbox[0] == "confirmed"
    assert json.loads(mailbox[1])["message_ids"] == [204]


def test_late_confirmation_rejects_out_of_range_message_id(store):
    task_id = store.create_task("u1", "消息号范围", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "uncertain", error_code="NETWORK_UNCERTAIN",
    ) == "uncertain"
    with pytest.raises(ValueError, match="message id evidence"):
        store._record_task_confirmation_from_platform(
            action["outbox_id"], message_ids=(2**31,), evidence_source="late_settle",
            _capability=_LATE_CONFIRMATION_CAPABILITY,
        )


def test_late_confirmation_after_dead_retry_finishes_logical_task(store):
    """旧代迟到确认应取消未领取后代，并把逻辑任务收敛为 done。"""
    task_id = store.create_task("u1", "迟到确认重试", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action0 = store.persist_task_text_action(task_id, "甲")
    attempt0 = action0["attempt_id"]
    assert store.claim_send_outbox(action0["outbox_id"])
    assert store.settle_send_outbox(
        action0["outbox_id"], "failed", error_code="HTTP_400", max_attempts=1
    ) == "dead"

    retry = store.retry_task_generation(
        task_id, "u1", expected_attempt_id=attempt0,
        request_id="retry-late-confirm", verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )
    assert retry["ok"] is True
    attempt1 = retry["new_attempt_id"]
    with store._connect() as conn:
        outbox1 = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (attempt1,),
        ).fetchone()[0]

    assert store._record_task_confirmation_from_platform(
        action0["outbox_id"], message_ids=(203,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    with store._connect() as conn:
        task = conn.execute(
            "SELECT status,current_attempt_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        descendant = conn.execute(
            "SELECT state FROM task_action_attempts WHERE id=?", (attempt1,)
        ).fetchone()
        outbox = conn.execute(
            "SELECT status,attempts FROM send_outbox WHERE action_id=?", (outbox1,)
        ).fetchone()
    assert task[0:2] == ("done", attempt1)
    assert descendant[0] == "cancelled"
    assert outbox[0:2] == ("cancelled", 0)


def test_partial_attempt_cannot_be_promoted_to_confirmed_by_raw_sql(store):
    """confirmed 必须覆盖本代全部 child，不能只凭一条 confirmation。"""
    task_id = store.create_task("u1", "部分动作", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲", "乙"])
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(204,)) == "confirmed"
    assert store.claim_send_outbox(outboxes[1])
    assert store.settle_send_outbox(
        outboxes[1], "failed", error_code="HTTP_400", max_attempts=1
    ) == "dead"

    with pytest.raises(sqlite3.IntegrityError, match="invalid task action delivery transition"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_attempts SET state='confirmed' WHERE id=?",
                (attempt_id,),
            )


def test_phase2b_validator_rejects_self_consistent_delete_guard_tamper(tmp_path):
    """即使攻击者同步改 marker，关键删除护栏语义仍须由代码校验。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_TRIGGERS,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "tampered-phase2b-trigger.db"
    store = Store(str(db))
    with store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER trg_task_action_outbox_delete_requires_confirmation")
        conn.execute("""
            CREATE TRIGGER trg_task_action_outbox_delete_requires_confirmation
            BEFORE DELETE ON send_outbox
            BEGIN SELECT 1; END
        """)
        marker = store._task_action_phase2b_checksum(conn)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (marker, _TASK_ACTION_PHASE2B_VERSION),
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="semantic contract"):
        Store(str(db))


def test_phase2b_backfill_reduces_known_confirmed_outbox_to_done(tmp_path):
    """旧库 known-confirmed 回填后必须立即完成 attempt/task 投影。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_INDEXES,
        _TASK_ACTION_PHASE2B_TRIGGERS,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "known-confirmed-backfill.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "已确认回填", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_unaccounted',"
            "confirmed_message_ids='[205]',confirmed_at='2026-08-29 00:00:01' "
            "WHERE action_id=?",
            (action["outbox_id"],),
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for name in sorted(_TASK_ACTION_PHASE2B_TRIGGERS):
            conn.execute(f'DROP TRIGGER "{name}"')
        for name in sorted(_TASK_ACTION_PHASE2B_INDEXES):
            conn.execute(f'DROP INDEX "{name}"')
        conn.execute("DROP TABLE task_action_confirmations")
        conn.execute("DROP TABLE task_action_children")
        conn.execute("DROP TABLE task_action_items")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        )
        conn.commit()

    Store(str(db))
    with sqlite3.connect(db) as conn:
        attempt = conn.execute(
            "SELECT state,accounting_state FROM task_action_attempts WHERE id=?",
            (action["attempt_id"],),
        ).fetchone()
        task = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        confirmation = conn.execute(
            "SELECT evidence_source,message_ids_json FROM task_action_confirmations"
        ).fetchone()
        outbox = conn.execute(
            "SELECT status FROM send_outbox WHERE action_id=?", (action["outbox_id"],)
        ).fetchone()
    assert attempt == ("confirmed", "pending_repair")
    assert task == ("done",)
    assert confirmation == ("known_confirmed_backfill", "[205]")
    assert outbox == ("confirmed_unaccounted",)


def test_late_confirmation_after_claim_preserves_duplicate_risk_evidence(store):
    task_id = store.create_task("u1", "重复风险", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action0 = store.persist_task_text_action(task_id, "甲")
    _attempt0 = action0["attempt_id"]
    outboxes0 = [action0["outbox_id"]]
    assert store.claim_send_outbox(outboxes0[0])
    assert store.settle_send_outbox(
        outboxes0[0], "failed", error_code="HTTP_400", max_attempts=1
    ) == "dead"
    retry = store.retry_task_generation(
        task_id, "u1", expected_attempt_id=_attempt0,
        request_id="retry-duplicate", verification_result="NOT_REQUIRED",
        force_resend_ack=False,
    )
    assert retry["ok"] is True
    _attempt1 = retry["new_attempt_id"]
    with store._connect() as conn:
        outboxes1 = [row[0] for row in conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?", (_attempt1,)
        )]
    assert store.claim_send_outbox(outboxes1[0])
    assert store.settle_send_outbox(
        outboxes1[0], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"

    assert store._record_task_confirmation_from_platform(
        outboxes0[0], message_ids=(301,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    assert store._record_task_confirmation_from_platform(
        outboxes1[0], message_ids=(302,), evidence_source="late_settle",
        _capability=_LATE_CONFIRMATION_CAPABILITY,
    ) == "confirmed"
    with store._connect() as conn:
        confirmations = conn.execute(
            "SELECT generation,message_ids_json FROM task_action_confirmations "
            "ORDER BY generation"
        ).fetchall()
        duplicate_events = conn.execute(
            "SELECT COUNT(*) FROM task_action_events "
            "WHERE event_type='duplicate_delivery_detected'"
        ).fetchone()[0]
        task = conn.execute(
            "SELECT status,current_attempt_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert [row[0] for row in confirmations] == [0, 1]
    assert [json.loads(row[1])[0] for row in confirmations] == [301, 302]
    assert duplicate_events == 1
    assert task[0] == "done"
    assert task[1] == _attempt1


def test_confirmation_facts_are_append_only_and_normal_settle_is_fact_first(store):
    task_id = store.create_task("u1", "事实不可变", "2020-01-01 00:00")
    _attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(401,)) == "confirmed"
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action_id,outbox_id,evidence_source FROM task_action_confirmations"
        ).fetchone()
    assert row[1] == outboxes[0]
    assert row[2] == "outbox_settle"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_confirmations SET actor='tamper' WHERE action_id=?",
                (row[0],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM task_action_confirmations WHERE action_id=?", (row[0],)
            )
    with store._connect() as conn:
        fact = conn.execute(
            "SELECT domain_action_id FROM confirmed_action_facts"
        ).fetchone()
    assert fact is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE confirmed_action_facts SET actual_json='{}' "
                "WHERE domain_action_id=?", (fact[0],)
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM confirmed_action_facts WHERE domain_action_id=?",
                (fact[0],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            columns = [row[1] for row in conn.execute(
                "PRAGMA table_info(confirmed_action_facts)"
            )]
            existing = conn.execute(
                "SELECT * FROM confirmed_action_facts WHERE domain_action_id=?",
                (fact[0],),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO confirmed_action_facts ("
                + ",".join(columns) + ") VALUES ("
                + ",".join("?" for _ in columns) + ")", tuple(existing),
            )


def test_child_outbox_identity_is_immutable_and_domain_bound(store):
    task_id = store.create_task("u1", "子动作关联", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    with store._connect() as conn:
        store._materialize_task_action_attempt_conn(conn, attempt_id)
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="outbox identity"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_children SET outbox_id='' "
                "WHERE outbox_id=?", (outboxes[0],)
            )
    with pytest.raises(sqlite3.IntegrityError, match="outbox identity"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_children SET outbox_id='forged' "
                "WHERE outbox_id=?", (outboxes[0],)
            )


def test_confirmed_fact_requires_typed_outbox_anchor(store):
    outbox_id = store.enqueue_send_outbox("private", "u1", "legacy")
    with pytest.raises(sqlite3.IntegrityError, match="strict anchor"):
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO confirmed_action_facts ("
                "domain_action_id,outbox_id,schema_version,identity_version,"
                "source_id,scope_id,kind,channel,target,actor_kind,projection_kind,"
                "conversation_user_id,group_id,source_chat_id,self_memory_eligible,"
                "actual_json,message_ids_json,immutable_json,confirmed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'bot','none','','',NULL,0,? ,?,?,?)",
                (
                    "forged-action", outbox_id, 2, 1, "forged-source",
                    "_private_u1", "text", "private", "u1", "{}", "[]",
                    '{"action_id":"forged-action"}', "2026-08-29 00:00:00",
                ),
            )


def test_confirmed_fact_rejects_failed_mailbox_anchor(store):
    """失败/不确定回执不能成为永久 confirmed fact 的唯一锚点。"""
    task_id = store.create_task("u1", "邮箱锚点状态", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(402,)) == "confirmed"
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM confirmed_action_facts"
        ).fetchone()
        assert fact is not None
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(confirmed_action_facts)"
        )]
        values = tuple(fact[column] for column in columns)
        domain_action_id = str(fact["domain_action_id"])
        scope_id = str(fact["scope_id"])

    with pytest.raises(sqlite3.IntegrityError, match="strict anchor"):
        with store._connect() as conn:
            # 仅为构造恶意历史样本，事务回滚会恢复不可变触发器与事实行。
            conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_delete")
            conn.execute(
                "DELETE FROM conversation_window_events WHERE domain_action_id=?",
                (domain_action_id,),
            )
            conn.execute(
                "DELETE FROM confirmed_action_facts WHERE domain_action_id=?",
                (domain_action_id,),
            )
            conn.execute(
                "UPDATE action_receipt_mailbox SET action_status='failed',"
                "receipt_json=json_set(receipt_json,'$.status','failed') "
                "WHERE scope_id=? AND action_id=?",
                (scope_id, domain_action_id),
            )
            conn.execute(
                "INSERT INTO confirmed_action_facts (" + ",".join(columns) + ") VALUES ("
                + ",".join("?" for _ in columns) + ")",
                values,
            )


def test_confirmed_fact_health_rejects_existing_failed_mailbox_anchor(store):
    """既有污染即使绕过写入触发器，重启健康检查也必须拒绝。"""
    task_id = store.create_task("u1", "邮箱锚点健康", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(outboxes[0], "confirmed", message_ids=(403,)) == "confirmed"
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        fact = conn.execute(
            "SELECT * FROM confirmed_action_facts"
        ).fetchone()
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(confirmed_action_facts)"
        )]
        values = tuple(fact[column] for column in columns)
        domain_action_id = str(fact["domain_action_id"])
        scope_id = str(fact["scope_id"])
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_delete")
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_strict_anchor")
        conn.execute(
            "DELETE FROM conversation_window_events WHERE domain_action_id=?",
            (domain_action_id,),
        )
        conn.execute(
            "DELETE FROM confirmed_action_facts WHERE domain_action_id=?",
            (domain_action_id,),
        )
        conn.execute(
            "UPDATE action_receipt_mailbox SET action_status='failed',"
            "receipt_json=json_set(receipt_json,'$.status','failed') "
            "WHERE scope_id=? AND action_id=?",
            (scope_id, domain_action_id),
        )
        conn.execute(
            "INSERT INTO confirmed_action_facts (" + ",".join(columns) + ") VALUES ("
            + ",".join("?" for _ in columns) + ")",
            values,
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="confirmed fact strict anchor trigger drifted"):
        type(store)(store.db_path)


def test_child_state_cannot_diverge_from_outbox_or_confirmation(store):
    task_id = store.create_task("u1", "子动作状态", "2020-01-01 00:00")
    attempt_id, outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    with store._connect() as conn:
        store._materialize_task_action_attempt_conn(conn, attempt_id)
        conn.commit()
    assert store.claim_send_outbox(outboxes[0])
    assert store.settle_send_outbox(
        outboxes[0], "uncertain", error_code="NETWORK_UNCERTAIN"
    ) == "uncertain"
    with pytest.raises(sqlite3.IntegrityError, match="state disagrees"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_children SET state='confirmed' "
                "WHERE attempt_id=?", (attempt_id,)
            )
    with pytest.raises(sqlite3.IntegrityError, match="state disagrees"):
        with store._connect() as conn:
            conn.execute(
                "UPDATE task_action_children SET state='dead' "
                "WHERE attempt_id=?", (attempt_id,)
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store._connect() as conn:
            conn.execute(
                "DELETE FROM task_action_children WHERE attempt_id=?",
                (attempt_id,),
            )


def test_confirmation_outbox_id_must_match_child(store):
    task_id = store.create_task("u1", "确认关联", "2020-01-01 00:00")
    attempt_id, _outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    with store._connect() as conn:
        store._materialize_task_action_attempt_conn(conn, attempt_id)
        child = conn.execute(
            "SELECT item_id,ordinal,action_id FROM task_action_children "
            "WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="confirmation identity"):
            conn.execute(
                "INSERT INTO task_action_confirmations "
                "(task_id,attempt_id,item_id,generation,ordinal,action_id,outbox_id,"
                "evidence_source,message_ids_json,evidence_json,confirmed_at,recorded_at,actor) "
                "VALUES (?,?,?,?,?,?,?,'late_settle','[999]','{}',?,?,?)",
                (task_id, attempt_id, child[0], 0, child[1], child[2],
                 "forged-outbox", "2026-08-29 00:00:00",
                 "2026-08-29 00:00:00", "forged-user"),
            )


def test_confirmation_guard_rejects_pending_outbox(store):
    """未进入发送/终局状态的 outbox 不能直接制造确认事实。"""
    task_id = store.create_task("u1", "未发送确认", "2020-01-01 00:00")
    attempt_id, _outboxes = _insert_attempt(store, task_id, 0, ["甲"])
    with store._connect() as conn:
        store._materialize_task_action_attempt_conn(conn, attempt_id)
        child = conn.execute(
            "SELECT item_id,ordinal,action_id,outbox_id FROM task_action_children "
            "WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="confirmation identity"):
            conn.execute(
                "INSERT INTO task_action_confirmations "
                "(task_id,attempt_id,item_id,generation,ordinal,action_id,outbox_id,"
                "evidence_source,message_ids_json,evidence_json,confirmed_at,recorded_at,actor) "
                "VALUES (?,?,?,?,?,?,?,'late_settle','[999]','{}',?,?,?)",
                (task_id, attempt_id, child[0], 0, child[1], child[2], child[3],
                 "2026-08-29 00:00:00", "2026-08-29 00:00:00", "forged-user"),
            )


def test_task_linked_signed_int32_message_id_is_confirmed(store):
    """OneBot 合法的负 signed-int32 message_id 不能被 Phase2b 误拦截。"""
    task_id = store.create_task("u1", "负消息号", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "confirmed", message_ids=(-7,),
    ) == "confirmed"
    with store._connect() as conn:
        confirmation = conn.execute(
            "SELECT message_ids_json FROM task_action_confirmations "
            "WHERE action_id=?", (action["domain_action_id"],)
        ).fetchone()
    assert confirmation[0] == "[-7]"
    assert store.get_send_outbox_health()["needs_review"] == 0


def test_confirmed_fact_field_drift_is_rejected_on_restart(tmp_path):
    from agent.action_contract import (
        ConversationRef, build_action_receipt_template, ActionEnvelope,
        derive_action_id,
    )
    from agent.store import Store

    db = tmp_path / "tampered-confirmed-fact.db"
    store = Store(str(db))
    source_chat_id = store.insert_chat("u1", "原消息", "")
    action_id = derive_action_id(
        source_id="source-fact-drift", scope_id="_private_u1", kind="text",
        channel="private", target="u1", payload={"text": "回复"}, ordinal=0,
        schema_version=2, identity_version=1,
    )
    envelope = ActionEnvelope(
        action_id=action_id, kind="text", channel="private", target="u1",
        payload={"text": "回复"}, source_id="source-fact-drift",
        scope_id="_private_u1", schema_version=2, ordinal=0,
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply", conversation_user_id="u1",
            source_chat_id=source_chat_id,
        ),
    )
    template = build_action_receipt_template(envelope, {
        "requested": "回复", "text": "回复", "delivery_kind": "text",
    })
    outbox_id = store.enqueue_send_outbox(
        "private", "u1", "回复", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(outbox_id, "confirmed", message_ids=(601,)) == "confirmed"
    with store._connect() as conn:
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_update")
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_delete")
        conn.execute(
            "UPDATE confirmed_action_facts SET actual_json='{}' "
            "WHERE domain_action_id=?", (template["action_id"],)
        )
        conn.commit()
    with pytest.raises(sqlite3.OperationalError, match="fact integrity violations"):
        Store(str(db))


def test_confirmed_fact_identity_anchor_rejects_self_consistent_tamper(tmp_path):
    """同时改事实列和快照时，v2 action_id 锚点仍必须拒绝启动。"""
    from agent.action_contract import (
        ConversationRef, build_action_receipt_template, ActionEnvelope,
        derive_action_id,
    )
    from agent.store import Store

    db = tmp_path / "tampered-confirmed-identity.db"
    store = Store(str(db))
    source_chat_id = store.insert_chat("u1", "原消息", "")
    action_id = derive_action_id(
        source_id="source-fact-identity", scope_id="_private_u1", kind="text",
        channel="private", target="u1", payload={"text": "回复"}, ordinal=0,
        schema_version=2, identity_version=1,
    )
    envelope = ActionEnvelope(
        action_id=action_id, kind="text", channel="private", target="u1",
        payload={"text": "回复"}, source_id="source-fact-identity",
        scope_id="_private_u1", schema_version=2, ordinal=0,
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply", conversation_user_id="u1",
            source_chat_id=source_chat_id,
        ),
    )
    template = build_action_receipt_template(envelope, {
        "requested": "回复", "text": "回复", "delivery_kind": "text",
    })
    outbox_id = store.enqueue_send_outbox(
        "private", "u1", "回复", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(outbox_id, "confirmed", message_ids=(602,)) == "confirmed"
    with store._connect() as conn:
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_update")
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_immutable_delete")
        snapshot = json.loads(conn.execute(
            "SELECT immutable_json FROM confirmed_action_facts "
            "WHERE domain_action_id=?", (template["action_id"],)
        ).fetchone()[0])
        snapshot["target"] = "u2"
        conn.execute(
            "UPDATE confirmed_action_facts SET target=?,immutable_json=? "
            "WHERE domain_action_id=?",
            ("u2", json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")), template["action_id"]),
        )
        conn.commit()
    with pytest.raises(sqlite3.OperationalError, match="fact integrity violations"):
        Store(str(db))


def test_phase2b_backfill_rejects_unproved_confirmed_attempt(tmp_path):
    """旧库只写 confirmed 状态、没有事实证据时必须拒绝启动迁移。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_INDEXES,
        _TASK_ACTION_PHASE2B_TRIGGERS,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "unproved-confirmed-backfill.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "无证据确认", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='dead' WHERE action_id=?",
            (action["outbox_id"],),
        )
        conn.execute(
            "UPDATE task_action_attempts SET state='confirmed' WHERE id=?",
            (action["attempt_id"],),
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for name in sorted(_TASK_ACTION_PHASE2B_TRIGGERS):
            conn.execute(f'DROP TRIGGER "{name}"')
        for name in sorted(_TASK_ACTION_PHASE2B_INDEXES):
            conn.execute(f'DROP INDEX "{name}"')
        conn.execute("DROP TABLE task_action_confirmations")
        conn.execute("DROP TABLE task_action_children")
        conn.execute("DROP TABLE task_action_items")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="confirmed.*evidence"):
        Store(str(db))


def test_phase2b_backfill_restores_fact_backed_child_after_outbox_cleanup(tmp_path):
    """生产旧库已删 confirmed outbox 时，永久 fact 必须能恢复 child 关联。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_INDEXES,
        _TASK_ACTION_PHASE2B_TRIGGERS,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "fact-backed-confirmed-backfill.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "历史确认", "2020-01-01 00:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "甲")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "confirmed", message_ids=(701,),
    ) == "confirmed"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM send_outbox WHERE action_id=?",
            (action["outbox_id"],),
        ).fetchone() is None
        conn.execute("BEGIN IMMEDIATE")
        for name in sorted(_TASK_ACTION_PHASE2B_TRIGGERS):
            conn.execute(f'DROP TRIGGER "{name}"')
        for name in sorted(_TASK_ACTION_PHASE2B_INDEXES):
            conn.execute(f'DROP INDEX "{name}"')
        conn.execute("DROP TABLE task_action_confirmations")
        conn.execute("DROP TABLE task_action_children")
        conn.execute("DROP TABLE task_action_items")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        )
        conn.commit()

    migrated = Store(str(db))
    with migrated._connect() as conn:
        child = conn.execute(
            "SELECT outbox_id,state FROM task_action_children WHERE attempt_id=?",
            (action["attempt_id"],),
        ).fetchone()
        confirmation = conn.execute(
            "SELECT outbox_id,evidence_source FROM task_action_confirmations "
            "WHERE attempt_id=?", (action["attempt_id"],),
        ).fetchone()
        task = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
    assert tuple(child) == (action["outbox_id"], "confirmed")
    assert tuple(confirmation) == (
        action["outbox_id"], "known_confirmed_backfill",
    )
    assert task[0] == "done"


def test_phase2b_migration_rejects_tampered_phase2a_marker(tmp_path):
    """Phase 2b 扩展前不能静默覆盖已漂移的 Phase 2a 标记。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_INDEXES,
        _TASK_ACTION_PHASE2B_TRIGGERS,
        _TASK_ACTION_PHASE2B_VERSION,
        _TASK_ACTION_PHASE2A_VERSION,
    )

    db = tmp_path / "tampered-phase2b.db"
    Store(str(db))
    with Store(str(db))._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name in sorted(_TASK_ACTION_PHASE2B_TRIGGERS):
            conn.execute(f'DROP TRIGGER "{name}"')
        for name in sorted(_TASK_ACTION_PHASE2B_INDEXES):
            conn.execute(f'DROP INDEX "{name}"')
        conn.execute("DROP TABLE task_action_confirmations")
        conn.execute("DROP TABLE task_action_children")
        conn.execute("DROP TABLE task_action_items")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        )
        conn.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=?",
            (_TASK_ACTION_PHASE2A_VERSION,),
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="Phase 2a"):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2A_VERSION,),
        ).fetchone()
        phase2b_objects = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ("
            + ",".join("?" for _ in (
                "task_action_items", "task_action_children",
                "task_action_confirmations",
            ))
            + ")",
            ("task_action_items", "task_action_children",
             "task_action_confirmations"),
        ).fetchone()[0]
    assert marker[0] == "tampered"
    assert phase2b_objects == 0


def test_phase2b_pre_guard_marker_is_migrated(tmp_path):
    """已发布的 pre-guard marker 走受控升级，不被误判为未知漂移。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "pre-guard-phase2b.db"
    Store(str(db))
    with Store(str(db))._connect() as conn:
        conn.execute("DROP TRIGGER trg_task_action_confirmation_identity_guard")
        conn.execute("DROP TRIGGER trg_task_action_confirmation_evidence_guard")
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_identity_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN NOT EXISTS (
                SELECT 1 FROM task_action_attempts a
                JOIN task_action_children ch ON ch.attempt_id=a.id
                    AND ch.ordinal=NEW.ordinal AND ch.action_id=NEW.action_id
                JOIN task_action_items i ON i.id=ch.item_id
                WHERE a.id=NEW.attempt_id AND a.task_id=NEW.task_id
                  AND a.generation=NEW.generation AND i.id=NEW.item_id
            )
            BEGIN SELECT RAISE(ABORT,'task action confirmation identity differs'); END
        """)
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_evidence_guard
            BEFORE INSERT ON task_action_confirmations
            WHEN EXISTS (
                SELECT 1 FROM json_each(NEW.message_ids_json) m
                WHERE m.type!='integer' OR m.value<=0
            )
            BEGIN SELECT RAISE(ABORT,'task action confirmation evidence is invalid'); END
        """)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (_TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM,
             _TASK_ACTION_PHASE2B_VERSION),
        )
        conn.commit()

    migrated = Store(str(db))
    with migrated._connect() as conn:
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        ).fetchone()[0]
        checksum = migrated._task_action_phase2b_checksum(conn)
        evidence_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_confirmation_evidence_guard'"
        ).fetchone()[0]
    assert marker == checksum
    assert "m.value<=0" not in evidence_sql
    assert "m.value<-(2147483648)" in evidence_sql


def test_confirmed_fact_anchor_v3_marker_is_migrated(tmp_path):
    """生产已存在的旧安全 JSON-guard marker 必须可受控升级。"""
    from agent.store import (
        Store,
        _CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V3,
        _CONFIRMED_FACT_ANCHOR_VERSION,
    )

    db = tmp_path / "legacy-anchor-v3.db"
    Store(str(db))
    with Store(str(db))._connect() as conn:
        conn.execute("DROP TRIGGER trg_confirmed_action_fact_strict_anchor")
        conn.execute("""
            CREATE TRIGGER trg_confirmed_action_fact_strict_anchor
            BEFORE INSERT ON confirmed_action_facts
            WHEN NOT EXISTS (
                SELECT 1 FROM send_outbox o
                WHERE o.action_id=NEW.outbox_id
                  AND o.domain_action_id=NEW.domain_action_id
                  AND o.domain_action_id!=''
                  AND o.status IN ('sending','confirmed','confirmed_unaccounted',
                                   'confirmed_conflict')
                  AND json_valid(o.receipt_template)
                  AND json_extract(o.receipt_template,'$.action_id')
                      =NEW.domain_action_id
            ) AND NOT EXISTS (
                SELECT 1 FROM action_receipt_mailbox m
                WHERE m.scope_id=NEW.scope_id
                  AND m.action_id=NEW.domain_action_id
                  AND m.action_status='confirmed'
                  AND json_valid(m.receipt_json)
                  AND json_extract(m.receipt_json,'$.action_id')
                      =NEW.domain_action_id
                  AND json_extract(m.receipt_json,'$.status')='confirmed'
            )
            BEGIN SELECT RAISE(ABORT,'confirmed action fact lacks strict anchor'); END
        """)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (_CONFIRMED_FACT_ANCHOR_LEGACY_CHECKSUM_V3,
             _CONFIRMED_FACT_ANCHOR_VERSION),
        )
        conn.commit()

    migrated = Store(str(db))
    with migrated._connect() as conn:
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_CONFIRMED_FACT_ANCHOR_VERSION,),
        ).fetchone()[0]
    assert marker == migrated._confirmed_fact_anchor_expected_checksum()


def test_phase2b_legacy_marker_does_not_overwrite_ddl_drift(tmp_path):
    """旧 marker 白名单不应掩盖已篡改的 Phase 2b DDL。"""
    from agent.store import (
        Store,
        _TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM,
        _TASK_ACTION_PHASE2B_VERSION,
    )

    db = tmp_path / "legacy-drift-phase2b.db"
    Store(str(db))
    with Store(str(db))._connect() as conn:
        conn.execute("DROP TRIGGER trg_task_action_confirmation_identity_guard")
        conn.execute("""
            CREATE TRIGGER trg_task_action_confirmation_identity_guard
            BEFORE INSERT ON task_action_confirmations
            BEGIN SELECT 1; END
        """)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (_TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM,
             _TASK_ACTION_PHASE2B_VERSION),
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="legacy schema manifest differs"):
        Store(str(db))
    with sqlite3.connect(db) as conn:
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (_TASK_ACTION_PHASE2B_VERSION,),
        ).fetchone()[0]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_confirmation_identity_guard'"
        ).fetchone()[0]
    assert marker == _TASK_ACTION_PHASE2B_LEGACY_V1_PRE_GUARD_CHECKSUM
    assert "BEGIN SELECT 1; END" in trigger_sql
