"""ADR-005 Phase 0：定时动作 attempt/outbox 持久契约。"""

import json
import sqlite3

import pytest

from agent.store import Store


def _plan_document(task_id, generation, plan_id, text="你好"):
    return {
        "plan_id": plan_id,
        "source_id": f"task:{task_id}:attempt:{generation}",
        "scope_id": "_private_u1",
        "channel": "private",
        "target": "u1",
        "created_at": "2026-08-28T18:00:00+08:00",
        "role_id": "default",
        "library_id": "",
        "schema_version": 1,
        "children": [{
            "action_id": f"act-{task_id}-{generation}-0",
            "kind": "text",
            "channel": "private",
            "target": "u1",
            "payload": {"text": text},
            "source_id": f"task:{task_id}:attempt:{generation}",
            "scope_id": "_private_u1",
            "ordinal": 0,
            "schema_version": 1,
        }],
    }


def _create_attempt(store, task_id, *, generation=0, plan_id=None,
                    text="你好"):
    plan_id = plan_id or f"plan-{task_id}-{generation}"
    retry_request_id = (
        None if generation == 0 else f"retry-{task_id}-{generation}"
    )
    plan_json = json.dumps(
        _plan_document(task_id, generation, plan_id, text),
        ensure_ascii=False, sort_keys=True,
    )
    with store._connect() as conn:
        current = conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (task_id,))
        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,retry_request_id,state,"
            "created_at,updated_at) "
            "VALUES (?,?,?,?,?,'persisted','2026-08-28 18:00:00',"
            "'2026-08-28 18:00:00')",
            (task_id, generation, plan_id, plan_json, retry_request_id),
        )
        attempt_id = int(cur.lastrowid)
        if generation > 0:
            conn.execute(
                "UPDATE tasks SET current_attempt_id=? "
                "WHERE id=? AND current_attempt_id=? AND status='sending'",
                (attempt_id, task_id, current),
            )
            conn.execute(
                "INSERT INTO task_action_retry_requests "
                "(request_id,requested_task_id,requested_attempt_id,task_id,"
                "expected_attempt_id,expected_generation,new_attempt_id,new_generation,"
                "actor,verification_result,"
                "force_resend_ack,selected_ordinals_json,skipped_ordinals_json,"
                "result,reason_code,evidence_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'test:schema','NOT_REQUIRED',0,'[0]','[]',"
                "'accepted','RETRY_QUEUED','{}','2026-08-28 18:00:00')",
                (retry_request_id, str(task_id), str(current), task_id, current,
                 generation - 1, attempt_id, generation),
            )
        return attempt_id, plan_json


def _activate_attempt(store, task_id, attempt_id, *, state="persisted"):
    with store._connect() as conn:
        conn.execute(
            "UPDATE tasks SET current_attempt_id=?,status='sending' WHERE id=?",
            (attempt_id, task_id),
        )
        if state != "persisted":
            conn.execute(
                "UPDATE task_action_attempts SET state=? WHERE id=?",
                (state, attempt_id),
            )


def _insert_linked_outbox(store, attempt_id, *, action_id="out-0",
                          domain_action_id="act-0", ordinal=0,
                          status="pending", attempts=0):
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,"
            "status,attempts,next_retry_at,last_error,created_at,updated_at,"
            "domain_action_id,task_attempt_id,ordinal,retry_owner) "
            "VALUES (?,'private','100','',?,'',?,?,'','',?,?,?,?,?,'outbox')",
            (action_id, "hello", status, attempts,
             "2026-08-28 18:00:00", "2026-08-28 18:00:00",
             domain_action_id, attempt_id, ordinal),
        )


def test_legacy_database_migrates_without_rewriting_existing_rows(tmp_path):
    db = tmp_path / "legacy-task-actions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "owner_qq TEXT NOT NULL,description TEXT NOT NULL,remind_at TEXT NOT NULL,"
            "created_at TEXT NOT NULL,status TEXT DEFAULT 'pending')"
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id,owner_qq,description,remind_at,created_at,status) "
            "VALUES (7,'u1','旧提醒','2026-08-29 09:00','2026-08-28 18:00:00','pending')"
        )
        conn.execute(
            "CREATE TABLE send_outbox ("
            "action_id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,"
            "group_id TEXT NOT NULL DEFAULT '',message TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,"
            "next_retry_at TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO send_outbox VALUES "
            "('legacy-out','private','u1','','旧正文','pending',0,'','',"
            "'2026-08-28 18:00:00','2026-08-28 18:00:00')"
        )

    Store(str(db))

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM tasks WHERE id=7").fetchone()
        outbox = conn.execute(
            "SELECT * FROM send_outbox WHERE action_id='legacy-out'"
        ).fetchone()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert task["owner_qq"] == "u1"
    assert task["description"] == "旧提醒"
    assert task["status"] == "pending"
    assert task["current_attempt_id"] is None
    assert outbox["message"] == "旧正文"
    assert outbox["status"] == "pending"
    assert outbox["task_attempt_id"] is None
    assert outbox["retry_owner"] == "outbox"
    assert {"task_action_attempts", "task_action_events"} <= tables


def test_task_action_schema_migration_is_idempotent(tmp_path):
    db = tmp_path / "idempotent.db"
    Store(str(db))
    with sqlite3.connect(db) as conn:
        before = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'trg_task_action_%' "
            "OR name LIKE 'idx_task_action_%' "
            "OR name LIKE 'idx_send_outbox_task_%' "
            "OR name='idx_send_outbox_owner_due' "
            "OR name IN ('task_action_attempts','task_action_events') "
            "ORDER BY type,name"
        ).fetchall()
    Store(str(db))

    with sqlite3.connect(db) as conn:
        after = conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name LIKE 'trg_task_action_%' "
            "OR name LIKE 'idx_task_action_%' "
            "OR name LIKE 'idx_send_outbox_task_%' "
            "OR name='idx_send_outbox_owner_due' "
            "OR name IN ('task_action_attempts','task_action_events') "
            "ORDER BY type,name"
        ).fetchall()
        triggers = {
            row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_task_action_%' "
                "AND name NOT LIKE 'trg_task_action_projection_anchor_%' "
                "ORDER BY name"
            )
        }
        assert triggers == {
            "trg_task_action_accounting_event",
            "trg_task_action_attempt_accounting_transition",
            "trg_task_action_attempt_cancel_guard",
            "trg_task_action_attempt_created",
            "trg_task_action_attempt_generation",
            "trg_task_action_attempt_identity_immutable",
            "trg_task_action_attempt_insert_state",
            "trg_task_action_attempt_plan_children",
            "trg_task_action_attempt_retry_business_guard",
            "trg_task_action_attempt_retry_link_guard",
            "trg_task_action_attempt_retry_link_immutable",
            "trg_task_action_attempt_state_transition",
            "trg_task_action_attempt_terminal_guard",
            "trg_task_action_current_attempt_insert",
            "trg_task_action_current_attempt_owner",
            "trg_task_action_current_attempt_transition",
            "trg_task_action_delivery_event",
            "trg_task_action_event_immutable_delete",
            "trg_task_action_event_immutable_update",
            "trg_task_action_outbox_insert_guard",
            "trg_task_action_outbox_link_immutable",
            "trg_task_action_outbox_open_guard",
            "trg_task_action_outbox_status_guard",
            "trg_task_action_phase2a_receipt_outbox_guard",
            "trg_task_action_phase2a_receipt_request_guard",
            "trg_task_action_retry_request_immutable_delete",
            "trg_task_action_retry_request_immutable_update",
            "trg_task_action_retry_request_insert_guard",
            "trg_task_action_task_pending_guard",
            "trg_task_action_task_status_guard",
                "trg_task_action_item_immutable_update",
                "trg_task_action_item_immutable_delete",
                "trg_task_action_child_identity_immutable",
                "trg_task_action_child_outbox_insert_guard",
                "trg_task_action_child_outbox_update_guard",
                "trg_task_action_child_state_guard",
                "trg_task_action_child_immutable_delete",
                "trg_task_action_confirmation_identity_guard",
                "trg_task_action_confirmation_evidence_guard",
            "trg_task_action_confirmation_immutable_update",
            "trg_task_action_confirmation_immutable_delete",
            "trg_task_action_confirmation_duplicate_event",
            "trg_task_action_outbox_delete_requires_confirmation",
        }
        indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND (name LIKE 'idx_task_action_%' "
                "OR name LIKE 'idx_send_outbox_task_%' "
                "OR name='idx_send_outbox_owner_due') "
                "AND name NOT LIKE 'idx_task_action_projection_anchors_%'"
            )
        }
        assert indexes == {
            "idx_task_action_attempts_task_state",
            "idx_task_action_attempts_retry_request",
            "idx_task_action_events_attempt_time",
            "idx_task_action_retry_requests_accepted_expected",
            "idx_task_action_retry_requests_task_time",
            "idx_send_outbox_task_ordinal",
                "idx_send_outbox_task_status",
                "idx_send_outbox_owner_due",
                "idx_task_action_items_task_ordinal",
                "idx_task_action_children_attempt_ordinal",
                "idx_task_action_children_item_generation",
                "idx_task_action_confirmations_task_ordinal",
                "idx_task_action_confirmations_attempt",
            }
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations "
            "WHERE version='20260828_task_action_phase0_v1'"
        ).fetchone()
        phase2a_marker = conn.execute(
            "SELECT checksum FROM schema_migrations "
            "WHERE version='20260829_task_action_phase2a_v1'"
        ).fetchone()
        assert marker is not None
        assert len(marker[0]) == 64
        assert phase2a_marker is not None
        assert len(phase2a_marker[0]) == 64
        assert before == after
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        expected_fks = {
            "task_action_attempts": {
                ("tasks", "task_id", "id", "RESTRICT"),
                ("task_action_retry_requests", "retry_request_id",
                 "request_id", "RESTRICT"),
            },
            "task_action_events": {
                ("task_action_attempts", "attempt_id", "id", "RESTRICT")
            },
            "tasks": {
                ("task_action_attempts", "current_attempt_id", "id", "RESTRICT")
            },
            "send_outbox": {
                ("task_action_attempts", "task_attempt_id", "id", "RESTRICT")
            },
            "task_action_retry_requests": {
                ("tasks", "task_id", "id", "RESTRICT"),
                ("task_action_attempts", "expected_attempt_id", "id", "RESTRICT"),
                ("task_action_attempts", "new_attempt_id", "id", "RESTRICT"),
            },
        }
        for table, expected in expected_fks.items():
            actual = {
                (row[2], row[3], row[4], row[6])
                for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            }
            assert expected <= actual


def test_unversioned_or_tampered_phase0_schema_is_rejected(tmp_path):
    unversioned = tmp_path / "unversioned.db"
    Store(str(unversioned))
    with sqlite3.connect(unversioned) as conn:
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE version='20260828_task_action_phase0_v1'"
        )
    with pytest.raises(sqlite3.OperationalError, match="task action"):
        Store(str(unversioned))

    tampered = tmp_path / "tampered.db"
    Store(str(tampered))
    with sqlite3.connect(tampered) as conn:
        conn.execute("DROP TRIGGER trg_task_action_delivery_event")
    with pytest.raises(sqlite3.OperationalError, match="task action"):
        Store(str(tampered))

    extra_trigger = tmp_path / "extra-trigger.db"
    Store(str(extra_trigger))
    with sqlite3.connect(extra_trigger) as conn:
        conn.execute(
            "CREATE TRIGGER stale_side_effect AFTER UPDATE "
            "ON task_action_attempts BEGIN SELECT 1; END"
        )
    with pytest.raises(sqlite3.OperationalError, match="task action"):
        Store(str(extra_trigger))


def test_attempt_plan_is_immutable_and_generation_identity_is_unique(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, plan_json = _create_attempt(store, task_id)
    other_plan = json.dumps(
        _plan_document(task_id, 0, "plan-other", "不同正文"),
        ensure_ascii=False, sort_keys=True,
    )

    with store._connect() as conn:
        conn.execute(
            "UPDATE tasks SET current_attempt_id=?,status='sending' WHERE id=?",
            (attempt_id, task_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_attempts SET plan_json='{}' WHERE id=?",
                (attempt_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,?,?,?,'persisted','now','now')",
                (task_id, 0, "plan-other", other_plan),
            )
        conn.execute(
            "UPDATE task_action_attempts SET state='failed' WHERE id=?",
            (attempt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,?,?,?,'persisted','now','now')",
                (task_id, 1, "plan-0", plan_json),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,?,?,'not-json','persisted','now','now')",
                (task_id, 1, "plan-invalid"),
            )

    with store._connect() as conn:
        row = conn.execute(
            "SELECT plan_json FROM task_action_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    assert row[0] == plan_json


@pytest.mark.parametrize("bad_plan", [
    "null",
    "[]",
    "{}",
    json.dumps({"plan_id": "different", "children": [{}]}),
    json.dumps({
        "plan_id": "plan-column", "source_id": "task:1:attempt:0",
        "scope_id": "_private_u1", "channel": "private", "target": "u1",
        "schema_version": 1, "children": [],
    }),
    json.dumps({
        "plan_id": "plan-column", "source_id": "task:1:attempt:0",
        "scope_id": "_private_u1", "channel": "private", "target": "u1",
        "schema_version": 1, "children": "not-an-array",
    }),
    json.dumps({
        "plan_id": "plan-column", "source_id": "task:999:attempt:77",
        "scope_id": "_private_u1", "channel": "private", "target": "u1",
        "schema_version": 1, "children": [17],
    }),
    json.dumps({
        "plan_id": "plan-column", "source_id": "task:1:attempt:0",
        "scope_id": "_private_u1", "channel": "private", "target": "u1",
        "schema_version": 1, "children": [17],
    }),
    json.dumps({
        "plan_id": "plan-column", "source_id": "task:1:attempt:0",
        "scope_id": "_private_u1", "channel": "private", "target": "u1",
        "schema_version": 1, "children": ["not-an-action"],
    }),
])
def test_attempt_plan_rejects_non_action_plan_shapes(store, bad_plan):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    with store._connect() as conn:
        conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (task_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,0,'plan-column',?,'persisted','now','now')",
                (task_id, bad_plan),
            )


def test_attempt_can_only_be_created_after_task_is_claimed(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    plan_id = f"plan-{task_id}-0"
    plan_json = json.dumps(
        _plan_document(task_id, 0, plan_id), ensure_ascii=False, sort_keys=True,
    )
    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,0,?,?,'persisted','now','now')",
                (task_id, plan_id, plan_json),
            )


def test_current_attempt_pointer_cannot_cross_task_ownership(store):
    first = store.create_task("u1", "提醒1", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, first)
    second = store.create_task("u2", "提醒2", "2026-08-29 10:00")

    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET current_attempt_id=? WHERE id=?",
                (attempt_id, second),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks "
                "(id,owner_qq,description,remind_at,created_at,status,current_attempt_id) "
                "VALUES (999,'u3','提醒3','2026-08-29 11:00','now','pending',?)",
                (attempt_id,),
            )


def _reach_attempt_state(conn, attempt_id, state):
    paths = {
        "persisted": (),
        "outbox_pending": ("outbox_pending",),
        "sending": ("outbox_pending", "sending"),
        "confirmed": ("outbox_pending", "sending", "confirmed"),
        "uncertain": ("uncertain",),
        "failed": ("failed",),
        "dead": ("outbox_pending", "dead"),
        "partial": ("outbox_pending", "sending", "partial"),
        "cancelled": ("cancelled",),
    }
    for next_state in paths[state]:
        conn.execute(
            "UPDATE task_action_attempts SET state=? WHERE id=?",
            (next_state, attempt_id),
        )


def test_attempt_delivery_state_machine_allows_only_forward_edges(store):
    allowed = {
        "persisted": {"outbox_pending", "failed", "uncertain", "cancelled"},
        "outbox_pending": {"sending", "failed", "dead", "uncertain", "cancelled"},
        "sending": {"outbox_pending", "confirmed", "uncertain", "failed", "dead", "partial"},
    }
    all_states = {
        "persisted", "outbox_pending", "sending", "confirmed", "uncertain",
        "failed", "dead", "partial", "cancelled",
    }

    for source, targets in allowed.items():
        for target in targets:
            task_id = store.create_task(
                "u1", f"允许 {source}->{target}", "2026-08-29 09:00"
            )
            attempt_id, _ = _create_attempt(store, task_id)
            with store._connect() as conn:
                _reach_attempt_state(conn, attempt_id, source)
                conn.execute(
                    "UPDATE task_action_attempts SET state=? WHERE id=?",
                    (target, attempt_id),
                )

    forbidden = {
        "persisted": all_states - allowed["persisted"] - {"persisted"},
        "outbox_pending": all_states - allowed["outbox_pending"] - {"outbox_pending"},
        "sending": all_states - allowed["sending"] - {"sending"},
        "confirmed": all_states - {"confirmed"},
        "uncertain": all_states - {"uncertain"},
        "failed": all_states - {"failed"},
        "dead": all_states - {"dead"},
        "partial": all_states - {"partial"},
        "cancelled": all_states - {"cancelled"},
    }
    for source, targets in forbidden.items():
        for target in targets:
            task_id = store.create_task(
                "u1", f"拒绝 {source}->{target}", "2026-08-29 09:00"
            )
            attempt_id, _ = _create_attempt(store, task_id)
            with store._connect() as conn:
                _reach_attempt_state(conn, attempt_id, source)
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE task_action_attempts SET state=? WHERE id=?",
                        (target, attempt_id),
                    )


def test_generation_cannot_advance_without_audited_retry_closure(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    first, _ = _create_attempt(store, task_id, generation=0)
    _activate_attempt(store, task_id, first)

    with store._connect() as conn:
        conn.execute(
            "UPDATE task_action_attempts SET state='failed' WHERE id=?", (first,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        _create_attempt(store, task_id, generation=1)

    with store._connect() as conn:
        assert conn.execute(
            "SELECT current_attempt_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()[0] == first
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,)
        ).fetchone()[0] == 1


def test_open_outbox_cannot_be_detached_or_create_a_second_owner(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, task_id)
    _activate_attempt(store, task_id, attempt_id, state="outbox_pending")
    _insert_linked_outbox(store, attempt_id)

    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET current_attempt_id=NULL,status='pending' WHERE id=?",
                (task_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET status='pending' WHERE id=?", (task_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_attempts SET state='cancelled' WHERE id=?",
                (attempt_id,),
            )

    other_task = store.create_task("u1", "另一个提醒", "2026-08-29 10:00")
    other_attempt, _ = _create_attempt(store, other_task)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_linked_outbox(
            store, other_attempt, action_id="out-orphan-owner",
            domain_action_id="act-orphan-owner",
        )

    # Feature-off 的唯一安全回退：未尝试 outbox 先取消，再取消 attempt，
    # 最后同一事务解除 current 指针并恢复 legacy pending。
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='cancelled' WHERE action_id='out-0'"
        )
        conn.execute(
            "UPDATE task_action_attempts SET state='cancelled' WHERE id=?",
            (attempt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET current_attempt_id=NULL,status='done' WHERE id=?",
                (task_id,),
            )
        conn.execute(
            "UPDATE tasks SET current_attempt_id=NULL,status='pending' WHERE id=?",
            (task_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE send_outbox SET status='pending' WHERE action_id='out-0'"
            )

        task = conn.execute(
            "SELECT status,current_attempt_id FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert task == ("pending", None)


def test_terminal_attempt_and_task_status_require_closed_outbox(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, task_id)
    _activate_attempt(store, task_id, attempt_id, state="outbox_pending")
    _insert_linked_outbox(store, attempt_id)
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_action_attempts SET state='sending' WHERE id=?",
            (attempt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_attempts SET state='confirmed' WHERE id=?",
                (attempt_id,),
            )
        for status in ("done", "uncertain", "failed", "completed"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tasks SET status=? WHERE id=?", (status, task_id)
                )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE send_outbox SET status='bogus' WHERE action_id='out-0'"
            )


def test_attempt_state_event_is_atomic_and_events_are_append_only(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, task_id)

    with store._connect() as conn:
        created = conn.execute(
            "SELECT event_type,to_state,actor,reason,metadata_json "
            "FROM task_action_events WHERE attempt_id=? ORDER BY id", (attempt_id,),
        ).fetchall()
        assert len(created) == 1
        assert created[0][:4] == (
            "attempt_created", "persisted", "system", "attempt_persisted"
        )
        assert isinstance(json.loads(created[0][4]), dict)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (attempt_id,),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_events WHERE attempt_id=? "
            "AND event_type='delivery_state_changed'",
            (attempt_id,),
        ).fetchone()[0] == 1
        conn.rollback()

        state = conn.execute(
            "SELECT state FROM task_action_attempts WHERE id=?", (attempt_id,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT id FROM task_action_events WHERE attempt_id=? "
            "AND event_type='delivery_state_changed'", (attempt_id,),
        ).fetchall()
        assert state == "persisted"
        assert events == []

        before_same_state = conn.execute(
            "SELECT COUNT(*) FROM task_action_events WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE task_action_attempts SET state=state WHERE id=?", (attempt_id,)
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_events WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0] == before_same_state

        event_id = conn.execute(
            "SELECT id FROM task_action_events WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_events SET reason='tamper' WHERE id=?", (event_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM task_action_events WHERE id=?", (event_id,))

        for event_type, actor, reason, metadata in (
            ("", "operator", "manual_retry", "{}"),
            ("manual_retry", "", "manual_retry", "{}"),
            ("manual_retry", "operator", "", "{}"),
            ("manual_retry", "operator", "manual_retry", "not-json"),
            ("manual_retry", "operator", "manual_retry", "[]"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO task_action_events "
                    "(attempt_id,event_type,actor,reason,metadata_json) "
                    "VALUES (?,?,?,?,?)",
                    (attempt_id, event_type, actor, reason, metadata),
                )


def test_attempt_accounting_state_cannot_rewind(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, task_id)
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_action_attempts SET accounting_state='pending_repair' "
            "WHERE id=?", (attempt_id,),
        )
        conn.execute(
            "UPDATE task_action_attempts SET accounting_state='clean' WHERE id=?",
            (attempt_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_attempts SET accounting_state='pending_repair' "
                "WHERE id=?", (attempt_id,),
            )


def test_outbox_attempt_link_enforces_fk_and_unique_ordinal(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    attempt_id, _ = _create_attempt(store, task_id)
    _activate_attempt(store, task_id, attempt_id, state="outbox_pending")
    _insert_linked_outbox(store, attempt_id)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_linked_outbox(
            store, attempt_id, action_id="out-duplicate",
            domain_action_id="act-duplicate", ordinal=0,
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_linked_outbox(
            store, attempt_id + 9999, action_id="out-orphan",
            domain_action_id="act-orphan", ordinal=1,
        )

    with store._connect() as conn:
        for sql in (
            "UPDATE send_outbox SET task_attempt_id=NULL WHERE action_id='out-0'",
            "UPDATE send_outbox SET ordinal=1 WHERE action_id='out-0'",
            "UPDATE send_outbox SET retry_owner='task' WHERE action_id='out-0'",
            "UPDATE send_outbox SET domain_action_id='other' WHERE action_id='out-0'",
            "UPDATE send_outbox SET message='tampered' WHERE action_id='out-0'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM task_action_attempts WHERE id=?", (attempt_id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def test_failed_phase0_ddl_rolls_back_task_action_columns(tmp_path):
    db = tmp_path / "broken-attempt-schema.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "owner_qq TEXT NOT NULL,description TEXT NOT NULL,remind_at TEXT NOT NULL,"
            "created_at TEXT NOT NULL,status TEXT DEFAULT 'pending')"
        )
        conn.execute(
            "CREATE TABLE send_outbox ("
            "action_id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,"
            "group_id TEXT NOT NULL DEFAULT '',message TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,"
            "next_retry_at TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        # 模拟一次不完整/损坏的旧迁移；Phase 0 必须整体失败且不留下半截新列。
        conn.execute("CREATE TABLE task_action_attempts (id INTEGER PRIMARY KEY)")

    with sqlite3.connect(db) as conn:
        before_attempt_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='task_action_attempts'"
        ).fetchone()[0]

    with pytest.raises(sqlite3.OperationalError):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        task_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)")
        }
        outbox_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")
        }
        after_attempt_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='task_action_attempts'"
        ).fetchone()[0]
        phase0_objects = conn.execute(
            "SELECT type,name FROM sqlite_master WHERE "
            "name='task_action_events' OR name LIKE 'trg_task_action_%' "
            "OR name LIKE 'idx_task_action_%' "
            "OR name LIKE 'idx_send_outbox_task_%' "
            "OR name='idx_send_outbox_owner_due'"
        ).fetchall()
        marker = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE version='20260828_task_action_phase0_v1'"
        ).fetchone()
    assert after_attempt_sql == before_attempt_sql
    assert phase0_objects == []
    assert marker is None
    assert "current_attempt_id" not in task_columns
    assert "task_attempt_id" not in outbox_columns
