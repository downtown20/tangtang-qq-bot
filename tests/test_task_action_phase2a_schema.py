"""ADR-005 Phase 2a：人工重试 lineage 的持久 schema 合同。"""

import hashlib
import json
import sqlite3

import pytest

from agent.store import Store


PHASE2A_VERSION = "20260829_task_action_phase2a_v1"
PHASE0_VERSION = "20260828_task_action_phase0_v1"
PHASE2A_RECEIPT_V1_VERSION = "20260829_task_action_phase2a_receipt_v1"
PHASE2A_RECEIPT_V2_VERSION = "20260829_task_action_phase2a_receipt_v2"
PHASE2A_RECEIPT_TRIGGERS = (
    "trg_task_action_phase2a_receipt_outbox_guard",
    "trg_task_action_phase2a_receipt_request_guard",
)
PHASE2A_TRIGGERS = (
    "trg_task_action_attempt_retry_business_guard",
    "trg_task_action_attempt_retry_link_guard",
    "trg_task_action_attempt_retry_link_immutable",
    "trg_task_action_retry_request_immutable_delete",
    "trg_task_action_retry_request_immutable_update",
    "trg_task_action_retry_request_insert_guard",
)
PHASE2A_INDEXES = (
    "idx_task_action_attempts_retry_request",
    "idx_task_action_retry_requests_accepted_expected",
    "idx_task_action_retry_requests_task_time",
)
PHASE2B_OBJECTS = (
    "task_action_items", "task_action_children", "task_action_confirmations",
)
PHASE2B_VERSION = "20260829_task_action_phase2b_v1"
PHASE2B_INDEXES = (
    "idx_task_action_items_task_ordinal",
    "idx_task_action_children_attempt_ordinal",
    "idx_task_action_children_item_generation",
    "idx_task_action_confirmations_task_ordinal",
    "idx_task_action_confirmations_attempt",
)
PHASE2B_TRIGGERS = (
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
)
RECEIPT_VERSIONS = (
    PHASE2A_RECEIPT_V1_VERSION, PHASE2A_RECEIPT_V2_VERSION,
)


def _task_action_schema_snapshot(conn):
    """捕获所有动作迁移对象和冻结 marker，供回滚后逐字比较。"""
    rows = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE type IN ('table','index','trigger') "
        "AND name NOT LIKE 'sqlite_%' AND ("
        "name LIKE 'task_action_%' OR name LIKE 'idx_task_action_%' "
        "OR name LIKE 'trg_task_action_%' OR name IN ("
        "'idx_send_outbox_task_ordinal','idx_send_outbox_task_status',"
        "'idx_send_outbox_owner_due')) ORDER BY type,name"
    ).fetchall()
    versions = (
        PHASE0_VERSION, PHASE2A_VERSION, PHASE2A_RECEIPT_V1_VERSION,
        PHASE2A_RECEIPT_V2_VERSION, PHASE2B_VERSION,
        "20260829_task_action_phase2b_floor_v1",
    )
    markers = conn.execute(
        "SELECT version,checksum FROM schema_migrations WHERE version IN ("
        + ",".join("?" for _ in versions) + ") ORDER BY version", versions,
    ).fetchall()
    return (tuple(tuple(row) for row in rows), tuple(tuple(row) for row in markers),
            tuple(tuple(row) for row in conn.execute("PRAGMA foreign_key_check")))


def _task_action_data_snapshot(conn):
    """捕获迁移 reducer 可能触碰的业务行，验证 savepoint 也回滚数据。"""
    snapshots = []
    for table in ("tasks", "task_action_attempts", "task_action_events", "send_outbox"):
        snapshots.append((table, tuple(tuple(row) for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY rowid"
        ))))
    return tuple(snapshots)


def _restore_phase0_state_guards(conn):
    """恢复 Phase 2b 改写前的两个 Phase 0 状态触发器。"""
    conn.execute("DROP TRIGGER IF EXISTS trg_task_action_attempt_state_transition")
    conn.execute("""
        CREATE TRIGGER trg_task_action_attempt_state_transition
        BEFORE UPDATE OF state ON task_action_attempts
        WHEN NEW.state!=OLD.state AND NOT (
          (OLD.state='persisted' AND NEW.state IN (
            'outbox_pending','failed','uncertain','cancelled'
          ))
          OR (OLD.state='outbox_pending' AND NEW.state IN (
            'sending','failed','dead','uncertain','cancelled'
          ))
          OR (OLD.state='sending' AND NEW.state IN (
            'outbox_pending','confirmed','uncertain','failed','dead','partial'
          ))
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid task action delivery transition');
        END
    """)
    conn.execute("DROP TRIGGER IF EXISTS trg_task_action_task_status_guard")
    conn.execute("""
        CREATE TRIGGER trg_task_action_task_status_guard
        BEFORE UPDATE OF status,current_attempt_id ON tasks
        WHEN NEW.current_attempt_id IS NOT NULL AND (
          NEW.status NOT IN ('sending','done','uncertain','failed','partial')
          OR (
            NEW.status!='sending'
            AND EXISTS (
              SELECT 1 FROM task_action_attempts a
              JOIN send_outbox o ON o.task_attempt_id=a.id
              WHERE a.task_id=NEW.id AND o.status IN ('pending','sending')
            )
          )
          OR (
            NEW.status='done'
            AND NOT EXISTS (
              SELECT 1 FROM task_action_attempts a
              WHERE a.id=NEW.current_attempt_id
                AND a.task_id=NEW.id AND a.state='confirmed'
            )
          )
          OR (
            NEW.status='uncertain'
            AND NOT EXISTS (
              SELECT 1 FROM task_action_attempts a
              WHERE a.id=NEW.current_attempt_id
                AND a.task_id=NEW.id AND a.state='uncertain'
            )
          )
          OR (
            NEW.status='failed'
            AND NOT EXISTS (
              SELECT 1 FROM task_action_attempts a
              WHERE a.id=NEW.current_attempt_id
                AND a.task_id=NEW.id AND a.state IN ('failed','dead')
            )
          )
          OR (
            NEW.status='partial'
            AND NOT EXISTS (
              SELECT 1 FROM task_action_attempts a
              WHERE a.id=NEW.current_attempt_id
                AND a.task_id=NEW.id AND a.state='partial'
            )
          )
        )
        BEGIN
            SELECT RAISE(ABORT, 'task status disagrees with current attempt');
        END
    """)


def _current_phase0_checksum(conn):
    """按 Store 的冻结算法重算 Phase 0 marker（测试仅用于造迁移前快照）。"""
    expected_indexes = {
        "idx_task_action_attempts_task_state",
        "idx_task_action_events_attempt_time",
        "idx_send_outbox_task_ordinal",
        "idx_send_outbox_task_status",
        "idx_send_outbox_owner_due",
    }
    expected_triggers = {
        "trg_task_action_accounting_event",
        "trg_task_action_attempt_accounting_transition",
        "trg_task_action_attempt_cancel_guard",
        "trg_task_action_attempt_created",
        "trg_task_action_attempt_generation",
        "trg_task_action_attempt_identity_immutable",
        "trg_task_action_attempt_insert_state",
        "trg_task_action_attempt_plan_children",
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
        "trg_task_action_task_pending_guard",
        "trg_task_action_task_status_guard",
    }
    names = {"task_action_attempts", "task_action_events"} | expected_indexes | expected_triggers
    placeholders = ",".join("?" for _ in names)
    objects = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        f"WHERE name IN ({placeholders}) ORDER BY type,name", tuple(sorted(names)),
    ).fetchall()
    columns = {}
    for table, wanted in (
        ("tasks", {"current_attempt_id"}),
        ("send_outbox", {"task_attempt_id", "ordinal", "retry_owner"}),
    ):
        columns[table] = [tuple(row) for row in conn.execute(
            f"PRAGMA table_info({table})") if row[1] in wanted]
        columns[f"{table}:fk"] = [tuple(row) for row in conn.execute(
            f"PRAGMA foreign_key_list({table})") if row[3] in wanted]
    payload = json.dumps(
        {"objects": objects, "columns": columns},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remove_phase2b_objects(conn):
    """删除 Phase 2b 对象，留下可重复执行的 Phase 2a/receipt 合同。"""
    for name in (
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
        "trg_task_action_projection_anchor_immutable_update",
        "trg_task_action_projection_anchor_immutable_delete",
        "trg_task_action_projection_anchor_insert_guard",
    ):
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for name in (
        "idx_task_action_items_task_ordinal",
        "idx_task_action_children_attempt_ordinal",
        "idx_task_action_children_item_generation",
        "idx_task_action_confirmations_task_ordinal",
        "idx_task_action_confirmations_attempt",
        "idx_task_action_projection_anchors_attempt",
    ):
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
    for table in reversed(PHASE2B_OBJECTS + ("task_action_projection_anchors",)):
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    conn.execute(
        "DELETE FROM schema_migrations WHERE version IN (?,?)",
        (PHASE2B_VERSION, "20260829_task_action_phase2b_floor_v1"),
    )


def _downgrade_to_phase0_for_migration_fault(conn):
    """把一份全新数据库还原成可进入 Phase 2a 的纯 Phase 0 快照。

    当前测试库默认一次初始化会完成所有阶段；为了真正调用 Phase 2a
    安装 DDL，而不是被 ``retry_request_id`` 的“半安装”护栏提前拒绝，
    这里按 SQLite 的历史迁移方式重建唯一新增列所在的表。测试数据只
    保留 tasks 行，所有 attempt 触发器 SQL 则按当前已冻结的 Phase 0
    对象原样恢复。
    """
    attempt_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='task_action_attempts'"
    ).fetchone()[0]
    phase0_attempt_sql = attempt_sql.replace(
        "finalized_at TEXT NOT NULL DEFAULT '', retry_request_id TEXT DEFAULT NULL "
        "REFERENCES task_action_retry_requests(request_id) ON DELETE RESTRICT "
        "DEFERRABLE INITIALLY DEFERRED,\n                    UNIQUE",
        "finalized_at TEXT NOT NULL DEFAULT '',\n                    UNIQUE",
    )
    assert phase0_attempt_sql != attempt_sql
    trigger_rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='task_action_attempts' ORDER BY name"
    ).fetchall()
    # 调用方可能刚更新过 tasks，先提交结束事务，否则 SQLite 会静默忽略
    # foreign_keys=OFF，导致重建表时约束状态与夹具意图不一致。
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    # 完整初始化会同时留下 Phase 2b 与 receipt 合同；先全部撤掉，
    # 否则故障后虽能回滚，数据库仍是无法安全重试的混合状态。
    _remove_phase2b_objects(conn)
    for name in PHASE2A_RECEIPT_TRIGGERS:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute(
        "DELETE FROM schema_migrations WHERE version IN (?,?,?,?)",
        (PHASE2A_RECEIPT_V1_VERSION, PHASE2A_RECEIPT_V2_VERSION,
         PHASE2B_VERSION, "20260829_task_action_phase2b_floor_v1"),
    )
    conn.execute("DROP TABLE IF EXISTS task_action_retry_requests")
    for name in PHASE2A_TRIGGERS:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for name in PHASE2A_INDEXES:
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
    for name, _sql in trigger_rows:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    conn.execute("DROP TABLE task_action_attempts")
    conn.execute(phase0_attempt_sql)
    conn.execute(
        "CREATE INDEX idx_task_action_attempts_task_state "
        "ON task_action_attempts(task_id,state)"
    )
    for name, sql in trigger_rows:
        if name in PHASE2A_TRIGGERS:
            continue
        conn.execute(sql)
    # Phase 2b 会改写这两个同名 Phase 0 trigger；这里只保留真实的
    # Phase 0 状态合同，避免把残留 trigger 伪装成历史快照。
    _restore_phase0_state_guards(conn)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def _receipt_checksum(conn, version):
    placeholders = ",".join("?" for _ in PHASE2A_RECEIPT_TRIGGERS)
    triggers = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') "
        "FROM sqlite_master WHERE type='trigger' AND name IN ("
        + placeholders + ") ORDER BY name",
        PHASE2A_RECEIPT_TRIGGERS,
    ).fetchall()
    payload = json.dumps(
        {"version": version, "triggers": [tuple(row) for row in triggers]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plan(task_id, generation, *, text="冻结正文", target="u1",
          conversation_user_id="u1"):
    source_id = f"task:{task_id}:attempt:{generation}"
    return {
        "plan_id": f"plan-{task_id}-{generation}",
        "source_id": source_id,
        "scope_id": f"_private_{target}",
        "channel": "private",
        "target": target,
        "created_at": f"2026-08-29T00:00:0{generation}+08:00",
        "role_id": "default",
        "library_id": "",
        "schema_version": 2,
        "children": [{
            "action_id": f"act-{task_id}-{generation}-0",
            "kind": "text",
            "channel": "private",
            "target": target,
            "payload": {"text": text},
            "review": False,
            "source_id": source_id,
            "scope_id": f"_private_{target}",
            "ordinal": 0,
            "schema_version": 2,
            "identity_version": 1,
            "conversation_ref": {
                "projection_kind": "conversation_reply",
                "conversation_user_id": conversation_user_id,
                "group_id": "",
                "source_chat_id": None,
                "self_memory_eligible": False,
            },
        }],
    }


def _failed_generation_zero(store):
    task_id = store.create_task("u1", "提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "冻结正文")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "failed", error_code="HTTP_400",
        error_detail="definite bad request", max_attempts=1,
    ) == "dead"
    return task_id, action["attempt_id"]


def _mutate_v2_plan_conversation_ref(store, attempt_id, outbox_id, ref):
    """仅用于启动校验红测：同步篡改 plan/receipt，恢复写入触发器。"""
    with store._connect() as conn:
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?)",
                ("trg_task_action_attempt_identity_immutable",
                 "trg_task_action_outbox_link_immutable"),
            )
        }
        plan = json.loads(conn.execute(
            "SELECT plan_json FROM task_action_attempts WHERE id=?",
            (int(attempt_id),),
        ).fetchone()[0])
        plan["children"][0]["conversation_ref"] = ref
        receipt = json.loads(conn.execute(
            "SELECT receipt_template FROM send_outbox WHERE action_id=?",
            (str(outbox_id),),
        ).fetchone()[0])
        receipt["conversation_ref"] = ref
        for name in trigger_sql:
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(
            "UPDATE task_action_attempts SET plan_json=? WHERE id=?",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True), int(attempt_id)),
        )
        conn.execute(
            "UPDATE send_outbox SET receipt_template=? WHERE action_id=?",
            (json.dumps(receipt, ensure_ascii=False, sort_keys=True), str(outbox_id)),
        )
        for sql in trigger_sql.values():
            conn.execute(sql)
        conn.commit()


def _bare_failed_generation_zero(store):
    task_id = store.create_task("u1", "损坏提醒", "2026-08-29 09:00")
    plan = _plan(task_id, 0)
    with store._connect() as conn:
        conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (task_id,))
        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
            "VALUES (?,0,?,?,'persisted','now','now')",
            (task_id, plan["plan_id"], json.dumps(plan, ensure_ascii=False,
                                                  sort_keys=True)),
        )
        attempt_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_attempt_id=? WHERE id=?",
            (attempt_id, task_id),
        )
        conn.execute("UPDATE task_action_attempts SET state='failed' WHERE id=?",
                     (attempt_id,))
        conn.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    return task_id, attempt_id


def _retry_plan_from_root(conn, task_id, request_id, generation=1):
    plan = json.loads(conn.execute(
        "SELECT plan_json FROM task_action_attempts "
        "WHERE task_id=? AND generation=0",
        (task_id,),
    ).fetchone()[0])
    source_id = f"task:{task_id}:attempt:{generation}"
    plan["plan_id"] = f"plan-{task_id}-{generation}-{request_id}"
    plan["source_id"] = source_id
    plan["created_at"] = "2026-08-29T00:00:01+08:00"
    plan["children"][0]["action_id"] = (
        f"act-{task_id}-{generation}-{request_id}"
    )
    plan["children"][0]["source_id"] = source_id
    return plan


def _insert_retry_attempt(conn, task_id, expected_attempt_id, request_id,
                          *, plan=None, selected="[0]", result="accepted",
                          link_outbox=True, receipt_actual=None):
    generation = 1
    if plan is None:
        plan = _retry_plan_from_root(conn, task_id, request_id, generation)
    conn.execute(
        "UPDATE tasks SET status='sending' "
        "WHERE id=? AND current_attempt_id=? AND status='failed'",
        (task_id, expected_attempt_id),
    )
    cur = conn.execute(
        "INSERT INTO task_action_attempts "
        "(task_id,generation,plan_id,plan_json,retry_request_id,state,"
        "created_at,updated_at) VALUES (?,?,?,?,?,'persisted','now','now')",
        (task_id, generation, plan["plan_id"],
         json.dumps(plan, ensure_ascii=False, sort_keys=True), request_id),
    )
    new_attempt_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE tasks SET current_attempt_id=? "
        "WHERE id=? AND current_attempt_id=? AND status='sending'",
        (new_attempt_id, task_id, expected_attempt_id),
    )
    if link_outbox:
        child = plan["children"][0]
        actual = receipt_actual if receipt_actual is not None else {
            "requested": child["payload"]["text"],
            "text": child["payload"]["text"],
        }
        template = {
            "schema_version": child["schema_version"],
            "action_id": child["action_id"],
            "kind": child["kind"],
            "channel": child["channel"],
            "target": child["target"],
            "source_id": child["source_id"],
            "scope_id": child["scope_id"],
            "ordinal": child["ordinal"],
            "actual": actual,
            "identity_version": child["identity_version"],
            "identity_payload": child["payload"],
            "conversation_ref": child["conversation_ref"],
        }
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,"
            "status,attempts,next_retry_at,last_error,created_at,updated_at,"
            "domain_action_id,task_attempt_id,ordinal,retry_owner) "
            "VALUES (?,? ,?,'',?,?,'pending',0,'','', 'now','now',?,?,0,'outbox')",
            (f"out-{request_id}", child["channel"], child["target"],
             child["payload"]["text"], json.dumps(template, ensure_ascii=False,
                                                    sort_keys=True),
             child["action_id"], new_attempt_id),
        )
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (new_attempt_id,),
        )
    conn.execute(
        "INSERT INTO task_action_retry_requests "
        "(request_id,requested_task_id,requested_attempt_id,task_id,"
        "expected_attempt_id,expected_generation,new_attempt_id,new_generation,"
        "actor,verification_result,"
        "force_resend_ack,selected_ordinals_json,skipped_ordinals_json,"
        "result,reason_code,evidence_json,created_at) "
        "VALUES (?,?,?,?,?,0,?,1,'qq:u1:command','NOT_REQUIRED',0,?,'[]',"
        "?,?,'{}','now')",
        (request_id, str(task_id), str(expected_attempt_id), task_id,
         expected_attempt_id, new_attempt_id, selected, result,
         "RETRY_QUEUED" if result == "accepted" else "REJECTED"),
    )
    return new_attempt_id


def _mixed_version_attempt(store, versions=(1, 2)):
    """建立 child schema 混合的 generation 0，验证 guard 按 ordinal 判定。"""
    task_id = store.create_task("u1", "混合版本提醒", "2026-08-29 09:00")
    assert store.claim_task_for_send(task_id)
    plan = _plan(task_id, 0)
    template = plan["children"][0]
    children = []
    for ordinal, version in enumerate(versions):
        child = json.loads(json.dumps(template, ensure_ascii=False))
        child["action_id"] = f"act-{task_id}-0-{ordinal}"
        child["ordinal"] = ordinal
        child["schema_version"] = version
        child["payload"] = {"text": f"冻结正文-{ordinal}"}
        if version == 1:
            child.pop("identity_version", None)
            child.pop("conversation_ref", None)
        children.append(child)
    plan["children"] = children
    with store._connect() as conn:
        cur = conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
            "VALUES (?,0,?,?,'persisted','now','now')",
            (task_id, plan["plan_id"], json.dumps(
                plan, ensure_ascii=False, sort_keys=True,
            )),
        )
        attempt_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE tasks SET current_attempt_id=? WHERE id=? AND status='sending'",
            (attempt_id, task_id),
        )
    return task_id, attempt_id, plan


def _insert_mixed_outbox(store, attempt_id, child, *, actual=None):
    receipt = ""
    if actual is not None:
        receipt_data = {
            "schema_version": child["schema_version"],
            "action_id": child["action_id"],
            "kind": child["kind"],
            "channel": child["channel"],
            "target": child["target"],
            "source_id": child["source_id"],
            "scope_id": child["scope_id"],
            "ordinal": child["ordinal"],
            "actual": actual,
        }
        if child["schema_version"] >= 2:
            receipt_data.update({
                "identity_version": child["identity_version"],
                "identity_payload": child["payload"],
                "conversation_ref": child["conversation_ref"],
            })
        receipt = json.dumps(receipt_data, ensure_ascii=False, sort_keys=True)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,"
            "status,attempts,next_retry_at,last_error,created_at,updated_at,"
            "domain_action_id,task_attempt_id,ordinal,retry_owner) "
            "VALUES (?,? ,?,'',?,?,'pending',0,'','', 'now','now',?,?,?,'outbox')",
            (
                f"out-mixed-{attempt_id}-{child['ordinal']}",
                child["channel"], child["target"], child["payload"]["text"],
                receipt, child["action_id"], attempt_id, child["ordinal"],
            ),
        )


def _phase2a_objects(conn):
    return conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name='task_action_retry_requests' "
        "OR name LIKE 'idx_task_action_retry_%' "
        "OR name LIKE 'idx_task_action_attempts_retry_%' "
        "OR name LIKE 'trg_task_action_retry_%' "
        "OR name LIKE 'trg_task_action_attempt_retry_%' "
        "ORDER BY type,name"
    ).fetchall()


def test_phase2a_schema_installs_versioned_retry_lineage(tmp_path):
    db = tmp_path / "phase2a-schema.db"
    Store(str(db))

    with sqlite3.connect(db) as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        attempt_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(task_action_attempts)"
            )
        }
        request_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(task_action_retry_requests)"
            )
        }
        attempt_fks = {
            (row[2], row[3], row[4], row[6]) for row in conn.execute(
                "PRAGMA foreign_key_list(task_action_attempts)"
            )
        }
        request_fks = {
            (row[2], row[3], row[4], row[6]) for row in conn.execute(
                "PRAGMA foreign_key_list(task_action_retry_requests)"
            )
        }
        marker = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (PHASE2A_VERSION,),
        ).fetchone()

    assert "task_action_retry_requests" in tables
    assert "retry_request_id" in attempt_columns
    assert request_columns == {
        "request_id", "requested_task_id", "requested_attempt_id", "task_id",
        "expected_attempt_id", "expected_generation", "new_attempt_id",
        "new_generation", "actor", "verification_result", "force_resend_ack",
        "selected_ordinals_json", "skipped_ordinals_json", "result",
        "reason_code", "evidence_json", "created_at",
    }
    assert (
        "task_action_retry_requests", "retry_request_id", "request_id", "RESTRICT"
    ) in attempt_fks
    assert {
        ("tasks", "task_id", "id", "RESTRICT"),
        ("task_action_attempts", "expected_attempt_id", "id", "RESTRICT"),
        ("task_action_attempts", "new_attempt_id", "id", "RESTRICT"),
    } <= request_fks
    assert marker is not None
    assert len(marker[0]) == 64


def test_phase2a_schema_migration_is_idempotent(tmp_path):
    db = tmp_path / "phase2a-idempotent.db"
    Store(str(db))
    with sqlite3.connect(db) as conn:
        before = _phase2a_objects(conn)

    Store(str(db))
    with sqlite3.connect(db) as conn:
        after = _phase2a_objects(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert before
    assert before == after


def test_receipt_v1_marker_is_upgraded_to_dynamic_ordinal_v2(tmp_path):
    db = tmp_path / "phase2a-receipt-v1-upgrade.db"
    Store(str(db))

    with sqlite3.connect(db) as conn:
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?) ORDER BY name",
                PHASE2A_RECEIPT_TRIGGERS,
            )
        }
        for name in PHASE2A_RECEIPT_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        for sql in trigger_sql.values():
            conn.execute(sql.replace("NEW.ordinal", "0")
                         .replace("linked.ordinal", "0"))
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V2_VERSION,),
        )
        v1_checksum = _receipt_checksum(conn, PHASE2A_RECEIPT_V1_VERSION)
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) "
            "VALUES (?,?,datetime('now')) "
            "ON CONFLICT(version) DO UPDATE SET checksum=excluded.checksum",
            (PHASE2A_RECEIPT_V1_VERSION, v1_checksum),
        )

    Store(str(db))

    with sqlite3.connect(db) as conn:
        markers = dict(conn.execute(
            "SELECT version,checksum FROM schema_migrations "
            "WHERE version IN (?,?)",
            (PHASE2A_RECEIPT_V1_VERSION, PHASE2A_RECEIPT_V2_VERSION),
        ))
        upgraded_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?) ORDER BY name",
                PHASE2A_RECEIPT_TRIGGERS,
            )
        }

    assert markers[PHASE2A_RECEIPT_V1_VERSION] == v1_checksum
    assert len(markers[PHASE2A_RECEIPT_V2_VERSION]) == 64
    assert "NEW.ordinal" in upgraded_sql[
        "trg_task_action_phase2a_receipt_outbox_guard"
    ]
    assert "linked.ordinal" in upgraded_sql[
        "trg_task_action_phase2a_receipt_request_guard"
    ]


def test_receipt_v2_rejects_fixed_ordinal_with_self_consistent_checksum(tmp_path):
    db = tmp_path / "phase2a-receipt-v2-drift.db"
    Store(str(db))

    with sqlite3.connect(db) as conn:
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?) ORDER BY name",
                PHASE2A_RECEIPT_TRIGGERS,
            )
        }
        for name in PHASE2A_RECEIPT_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        for sql in trigger_sql.values():
            conn.execute(sql.replace("NEW.ordinal", "0")
                         .replace("linked.ordinal", "0"))
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (
                _receipt_checksum(conn, PHASE2A_RECEIPT_V2_VERSION),
                PHASE2A_RECEIPT_V2_VERSION,
            ),
        )

    with pytest.raises(sqlite3.OperationalError, match="ordinal contract"):
        Store(str(db))


def test_retry_generation_requires_bidirectional_request_link(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)

    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            plan = _plan(task_id, 1)
            conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (task_id,))
            conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,state,created_at,updated_at) "
                "VALUES (?,1,?,?,'persisted','now','now')",
                (task_id, plan["plan_id"], json.dumps(plan, ensure_ascii=False,
                                                      sort_keys=True)),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "missing-request"
            )
            conn.execute(
                "DELETE FROM task_action_retry_requests "
                "WHERE request_id='missing-request'"
            )

    with store._connect() as conn:
        new_attempt_id = _insert_retry_attempt(
            conn, task_id, expected_attempt_id, "retry-ok"
        )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT retry_request_id FROM task_action_attempts WHERE id=?",
            (new_attempt_id,),
        ).fetchone()[0] == "retry-ok"
        assert conn.execute(
            "SELECT new_attempt_id FROM task_action_retry_requests "
            "WHERE request_id='retry-ok'"
        ).fetchone()[0] == new_attempt_id


@pytest.mark.parametrize(
    "path,value",
    [
        (("children", 0, "payload", "text"), "偷换正文"),
        (("target",), "u2"),
        (("children", 0, "conversation_ref", "conversation_user_id"), "other"),
    ],
    ids=("payload", "target", "conversation-ref"),
)
def test_retry_generation_rejects_business_payload_drift(store, path, value):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            baseline = _retry_plan_from_root(conn, task_id, "retry-drift")
            plan = json.loads(json.dumps(baseline, ensure_ascii=False))
            target = plan
            for part in path[:-1]:
                target = target[part]
            original = target[path[-1]]
            target[path[-1]] = value
            assert original != value
            restored = json.loads(json.dumps(plan, ensure_ascii=False))
            target = restored
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = original
            assert restored == baseline
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "retry-drift",
                plan=plan,
            )


def test_retry_request_selected_ordinals_must_equal_new_plan(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "retry-wrong-ordinal",
                selected="[1]",
            )


def test_accepted_retry_request_requires_the_new_frozen_outbox(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "retry-no-outbox",
                link_outbox=False,
            )


def test_retry_rejects_bare_failed_attempt_without_delivery_proof(store):
    task_id, expected_attempt_id = _bare_failed_generation_zero(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "retry-bare-failed"
            )


def test_retry_requests_are_append_only_and_rejected_has_no_attempt(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_action_retry_requests "
            "(request_id,requested_task_id,requested_attempt_id,task_id,"
            "expected_attempt_id,expected_generation,actor,verification_result,"
            "force_resend_ack,selected_ordinals_json,"
            "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
            "VALUES ('retry-rejected',?,?,?,?,0,'qq:u1:command','NOT_REQUIRED',0,"
            "'[]','[]','rejected','UNSUPPORTED_STATE','{}','now')",
            (str(task_id), str(expected_attempt_id), task_id, expected_attempt_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE task_action_retry_requests SET reason_code='TAMPERED' "
                "WHERE request_id='retry-rejected'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM task_action_retry_requests "
                "WHERE request_id='retry-rejected'"
            )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT new_attempt_id,new_generation FROM task_action_retry_requests "
            "WHERE request_id='retry-rejected'"
        ).fetchone() == (None, None)


def test_retry_attempt_cannot_point_to_a_rejected_request(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_action_retry_requests "
            "(request_id,requested_task_id,requested_attempt_id,task_id,"
            "expected_attempt_id,expected_generation,actor,verification_result,"
            "force_resend_ack,selected_ordinals_json,"
            "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
            "VALUES ('retry-rejected-link',?,?,?,?,0,'qq:u1:command','NOT_REQUIRED',"
            "0,'[]','[]','rejected','UNSUPPORTED_STATE','{}','now')",
            (str(task_id), str(expected_attempt_id), task_id, expected_attempt_id),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            plan = _plan(task_id, 1)
            conn.execute("UPDATE tasks SET status='sending' WHERE id=?", (task_id,))
            cur = conn.execute(
                "INSERT INTO task_action_attempts "
                "(task_id,generation,plan_id,plan_json,retry_request_id,state,"
                "created_at,updated_at) VALUES (?,1,?,?,'retry-rejected-link',"
                "'persisted','now','now')",
                (task_id, plan["plan_id"], json.dumps(
                    plan, ensure_ascii=False, sort_keys=True,
                )),
            )
            conn.execute(
                "UPDATE tasks SET current_attempt_id=? WHERE id=?",
                (int(cur.lastrowid), task_id),
            )


def test_retry_request_replace_cannot_bypass_append_only_guard(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    values = (str(task_id), str(expected_attempt_id), task_id, expected_attempt_id)
    insert_sql = (
        "INSERT INTO task_action_retry_requests "
        "(request_id,requested_task_id,requested_attempt_id,task_id,"
        "expected_attempt_id,expected_generation,actor,verification_result,"
        "force_resend_ack,selected_ordinals_json,"
        "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
        "VALUES ('retry-replace',?,?,?,?,0,'qq:u1:command','NOT_REQUIRED',0,'[]',"
        "'[]','rejected','UNSUPPORTED_STATE','{\"original\":true}','now')"
    )
    with store._connect() as conn:
        conn.execute(insert_sql, values)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert_sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")
                .replace("qq:u1:command", "tampered:actor")
                .replace("{\"original\":true}", "{\"original\":false}"),
                values,
            )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT actor,evidence_json FROM task_action_retry_requests "
            "WHERE request_id='retry-replace'"
        ).fetchone() == ("qq:u1:command", '{"original":true}')


def test_rejected_unknown_target_is_durably_audited_without_fake_fk(store):
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_action_retry_requests "
            "(request_id,requested_task_id,requested_attempt_id,actor,"
            "verification_result,force_resend_ack,selected_ordinals_json,"
            "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
            "VALUES ('retry-not-found','999999','888888','qq:u1:command',"
            "'NOT_REQUIRED',0,'[]','[]','rejected','NOT_FOUND_OR_NOT_OWNER',"
            "'{\"source\":\"command\"}','now')"
        )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT requested_task_id,requested_attempt_id,task_id,"
            "expected_attempt_id,expected_generation,new_attempt_id "
            "FROM task_action_retry_requests WHERE request_id='retry-not-found'"
        ).fetchone() == ("999999", "888888", None, None, None, None)


def test_rejected_random_stale_token_can_resolve_only_the_owned_task(store):
    task_id, _attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO task_action_retry_requests "
            "(request_id,requested_task_id,requested_attempt_id,task_id,actor,"
            "verification_result,force_resend_ack,selected_ordinals_json,"
            "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
            "VALUES ('retry-stale-random',?,'999999',?,'qq:u1:command',"
            "'NOT_REQUIRED',0,'[]','[]','rejected','STALE_ATTEMPT','{}','now')",
            (str(task_id), task_id),
        )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT task_id,expected_attempt_id,expected_generation "
            "FROM task_action_retry_requests "
            "WHERE request_id='retry-stale-random'"
        ).fetchone() == (task_id, None, None)


def test_rejected_reason_must_match_resolution_state(store):
    task_id, _attempt_id = _failed_generation_zero(store)
    base = (
        "INSERT INTO task_action_retry_requests "
        "(request_id,requested_task_id,requested_attempt_id,task_id,actor,"
        "verification_result,force_resend_ack,selected_ordinals_json,"
        "skipped_ordinals_json,result,reason_code,evidence_json,created_at) "
    )
    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                base + "VALUES ('bad-unresolved-stale','404','505',NULL,"
                "'qq:u1:command','NOT_REQUIRED',0,'[]','[]','rejected',"
                "'STALE_ATTEMPT','{}','now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                base + "VALUES ('bad-task-only-reason',?,'505',?,"
                "'qq:u1:command','NOT_REQUIRED',0,'[]','[]','rejected',"
                "'UNSUPPORTED_STATE','{}','now')",
                (str(task_id), task_id),
            )


@pytest.mark.parametrize(
    "column,value",
    [
        ("domain_action_id", "act-tampered"),
        ("message", "被篡改的正文"),
        ("target_id", "different-target"),
    ],
    ids=("action-id", "text", "target"),
)
def test_boot_rejects_linked_outbox_identity_drift(store, column, value):
    _task_id, action = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (action,),
        ).fetchone()[0]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_outbox_link_immutable'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_task_action_outbox_link_immutable")
        conn.execute(
            f"UPDATE send_outbox SET {column}=? WHERE action_id=?",
            (value, outbox_id),
        )
        conn.execute(trigger_sql)

    with pytest.raises(sqlite3.OperationalError, match="outbox identity"):
        Store(store.db_path)


def test_boot_rejects_linked_outbox_actual_text_drift(store):
    _task_id, action = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (action,),
        ).fetchone()[0]
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_outbox_link_immutable'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_task_action_outbox_link_immutable")
        template = json.loads(conn.execute(
            "SELECT receipt_template FROM send_outbox WHERE action_id=?",
            (outbox_id,),
        ).fetchone()[0])
        template["actual"]["text"] = "被篡改的回执正文"
        conn.execute(
            "UPDATE send_outbox SET receipt_template=? WHERE action_id=?",
            (json.dumps(template, ensure_ascii=False, sort_keys=True), outbox_id),
        )
        conn.execute(trigger_sql)

    with pytest.raises(sqlite3.OperationalError, match="outbox identity"):
        Store(store.db_path)


def test_boot_rejects_plan_child_scope_drift(store):
    """停用写入触发器后篡改 child 范围，重启仍必须 fail closed。"""
    _task_id, attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_attempt_identity_immutable'"
        ).fetchone()[0]
        plan = json.loads(conn.execute(
            "SELECT plan_json FROM task_action_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()[0])
        plan["children"][0]["scope_id"] = "_private_other-user"
        conn.execute("DROP TRIGGER trg_task_action_attempt_identity_immutable")
        conn.execute(
            "UPDATE task_action_attempts SET plan_json=? WHERE id=?",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True), attempt_id),
        )
        conn.execute(trigger_sql)

    with pytest.raises(sqlite3.OperationalError, match="plan child identity"):
        Store(store.db_path)


def test_boot_rejects_self_consistent_weak_retry_trigger(tmp_path):
    """同名弱 trigger + 自洽 checksum 不能伪装成完整 Phase 2a。"""
    db = tmp_path / "phase2a-weak-trigger.db"
    store = Store(str(db))
    trigger_name = "trg_task_action_retry_request_insert_guard"
    with store._connect() as conn:
        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON "
            "task_action_retry_requests BEGIN SELECT 1; END"
        )
        checksum = store._task_action_phase2a_checksum(conn)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (checksum, PHASE2A_VERSION),
        )
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="semantic contract"):
        Store(str(db))


@pytest.mark.parametrize(
    "ref",
    [
        {"projection_kind": "conversation_reply", "conversation_user_id": "other", "group_id": "", "source_chat_id": None, "self_memory_eligible": False},
        {"projection_kind": "conversation_reply", "conversation_user_id": "", "group_id": "", "source_chat_id": None, "self_memory_eligible": False},
        {"projection_kind": "conversation_reply", "conversation_user_id": "u1", "group_id": "g1", "source_chat_id": None, "self_memory_eligible": False},
        {"projection_kind": "evil", "conversation_user_id": "", "group_id": "", "source_chat_id": None, "self_memory_eligible": False},
        {"projection_kind": "none", "conversation_user_id": "", "group_id": "", "source_chat_id": -1, "self_memory_eligible": False},
        {"projection_kind": "none", "conversation_user_id": "", "group_id": "", "source_chat_id": None, "self_memory_eligible": True},
        {"projection_kind": "none", "conversation_user_id": "", "group_id": "", "source_chat_id": None, "self_memory_eligible": False, "extra": "x"},
    ],
    ids=("cross-private-user", "empty-user", "private-group", "unknown-kind",
         "invalid-source-chat", "invalid-self-memory", "unknown-field"),
)
def test_boot_rejects_semantically_invalid_v2_conversation_ref(tmp_path, ref):
    db = tmp_path / "phase2a-conversation-ref-invalid.db"
    store = Store(str(db))
    _task_id, attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
    _mutate_v2_plan_conversation_ref(store, attempt_id, outbox_id, ref)

    with pytest.raises(sqlite3.OperationalError, match="conversation_ref contract"):
        Store(str(db))


def test_boot_rejects_v2_logical_item_canonical_drift(tmp_path):
    db = tmp_path / "phase2a-conversation-ref-canonical-drift.db"
    store = Store(str(db))
    _task_id, attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
    # source_chat_id=7 对私聊仍是合法 ConversationRef；故障只能由
    # item canonical 与 plan child 分叉触发，而不是被语义校验遮蔽。
    _mutate_v2_plan_conversation_ref(
        store, attempt_id, outbox_id,
        {"projection_kind": "conversation_reply", "conversation_user_id": "u1",
         "group_id": "", "source_chat_id": 7, "self_memory_eligible": False},
    )

    with pytest.raises(sqlite3.OperationalError, match="logical item canonical drift"):
        Store(str(db))


def test_boot_rejects_projected_v2_child_downgrade_to_v1(tmp_path):
    """Phase 2b 投影存在时，plan/item 同步降级也不能绕过 v2 语义校验。"""
    db = tmp_path / "phase2a-v2-child-downgrade.db"
    store = Store(str(db))
    task_id, attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        item_id = conn.execute(
            "SELECT id FROM task_action_items WHERE task_id=? AND ordinal=0",
            (task_id,),
        ).fetchone()[0]
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?,?)",
                ("trg_task_action_attempt_identity_immutable",
                 "trg_task_action_item_immutable_update",
                 "trg_task_action_child_identity_immutable"),
            )
        }
        plan = json.loads(conn.execute(
            "SELECT plan_json FROM task_action_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()[0])
        child = plan["children"][0]
        child["schema_version"] = 1
        child.pop("identity_version", None)
        child.pop("conversation_ref", None)
        canonical = json.dumps(
            Store._phase2b_canonical_child(child), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        for name in trigger_sql:
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(
            "UPDATE task_action_attempts SET plan_json=? WHERE id=?",
            (json.dumps(plan, ensure_ascii=False, sort_keys=True), attempt_id),
        )
        conn.execute(
            "UPDATE task_action_items SET canonical_json=? WHERE id=?",
            (canonical, item_id),
        )
        for sql in trigger_sql.values():
            conn.execute(sql)
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="downgrade"):
        Store(str(db))


def test_boot_rejects_terminal_attempt_with_deleted_projection(tmp_path):
    """终态 task attempt 不得在重启时失去全部逻辑/物理投影。"""
    db = tmp_path / "phase2a-projection-deleted.db"
    store = Store(str(db))
    task_id, attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        item_id = conn.execute(
            "SELECT id FROM task_action_items WHERE task_id=? AND ordinal=0",
            (task_id,),
        ).fetchone()[0]
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?,?)",
                ("trg_task_action_child_immutable_delete",
                 "trg_task_action_item_immutable_delete",
                 "trg_task_action_outbox_delete_requires_confirmation"),
            )
        }
        for name in trigger_sql:
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(
            "DELETE FROM task_action_children WHERE attempt_id=?", (attempt_id,)
        )
        conn.execute("DELETE FROM task_action_items WHERE id=?", (item_id,))
        conn.execute("DELETE FROM send_outbox WHERE action_id=?", (outbox_id,))
        for sql in trigger_sql.values():
            conn.execute(sql)
        conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="coverage is incomplete"):
        Store(str(db))


def test_phase2b_migration_failure_rolls_back_all_objects_and_marker(tmp_path, monkeypatch):
    db = tmp_path / "phase2b-migration-rollback.db"
    store = Store(str(db))
    _task_id, _attempt_id = _failed_generation_zero(store)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        _remove_phase2b_objects(conn)
        _restore_phase0_state_guards(conn)
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (Store._task_action_phase2a_checksum(conn), PHASE2A_VERSION),
        )
        before_schema = _task_action_schema_snapshot(conn)
        before_data = _task_action_data_snapshot(conn)
        conn.commit()

    original = Store._backfill_task_action_phase2b

    def fail_after_install(self, conn):
        original(self, conn)
        raise RuntimeError("INJECTED_PHASE2B_DDL_FAILURE")

    monkeypatch.setattr(Store, "_backfill_task_action_phase2b", fail_after_install)
    with pytest.raises(RuntimeError, match="INJECTED_PHASE2B_DDL_FAILURE"):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        assert _task_action_schema_snapshot(conn) == before_schema
        assert _task_action_data_snapshot(conn) == before_data
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (PHASE2B_VERSION,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN (?,?,?)",
            PHASE2B_OBJECTS,
        ).fetchone()[0] == 0

    monkeypatch.undo()
    Store(str(db))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (PHASE2B_VERSION,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_children"
        ).fetchone()[0] >= 1


def test_phase2b_floor_migration_failure_rolls_back_all_objects_and_marker(
        tmp_path, monkeypatch):
    """schema floor 安装失败不能留下半套 anchor 或 marker。"""
    db = tmp_path / "phase2b-floor-migration-rollback.db"
    store = Store(str(db))
    with sqlite3.connect(db) as conn:
        for name in (
            "trg_task_action_projection_anchor_immutable_update",
            "trg_task_action_projection_anchor_immutable_delete",
            "trg_task_action_projection_anchor_insert_guard",
        ):
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(
            "DROP INDEX idx_task_action_projection_anchors_attempt"
        )
        conn.execute("DROP TABLE task_action_projection_anchors")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            ("20260829_task_action_phase2b_floor_v1",),
        )
        conn.commit()

    original = Store._validate_task_action_phase2b_floor_schema

    def fail_after_install(self, conn):
        original(self, conn)
        raise RuntimeError("INJECTED_PHASE2B_FLOOR_FAILURE")

    monkeypatch.setattr(Store, "_validate_task_action_phase2b_floor_schema",
                        fail_after_install)
    with pytest.raises(RuntimeError, match="INJECTED_PHASE2B_FLOOR_FAILURE"):
        Store(str(db))
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_action_projection_anchors'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            ("20260829_task_action_phase2b_floor_v1",),
        ).fetchone() is None

    monkeypatch.undo()
    Store(str(db))


def test_receipt_migration_failure_rolls_back_triggers_and_marker(tmp_path, monkeypatch):
    db = tmp_path / "receipt-migration-rollback.db"
    store = Store(str(db))
    with store._connect() as conn:
        for name in PHASE2A_RECEIPT_TRIGGERS:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        conn.execute(
            "DELETE FROM schema_migrations WHERE version IN (?,?)",
            (PHASE2A_RECEIPT_V1_VERSION, PHASE2A_RECEIPT_V2_VERSION),
        )
        before = _task_action_schema_snapshot(conn)
        conn.commit()

    original = Store._install_task_action_phase2a_receipt_schema

    def fail_after_install(self, conn):
        original(self, conn)
        raise RuntimeError("INJECTED_RECEIPT_DDL_FAILURE")

    monkeypatch.setattr(
        Store, "_install_task_action_phase2a_receipt_schema", fail_after_install,
    )
    with pytest.raises(RuntimeError, match="INJECTED_RECEIPT_DDL_FAILURE"):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        assert _task_action_schema_snapshot(conn) == before
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V2_VERSION,),
        ).fetchone() is None
        for name in PHASE2A_RECEIPT_TRIGGERS:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone() is None

    monkeypatch.undo()
    Store(str(db))


def test_receipt_v1_to_v2_failure_restores_legacy_contract_and_retries(
        tmp_path, monkeypatch):
    """v1→v2 的 DROP/CREATE 失败时必须恢复 v1，随后可再次升级。"""
    db = tmp_path / "receipt-v1-upgrade-rollback.db"
    Store(str(db))
    with sqlite3.connect(db) as conn:
        trigger_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?) ORDER BY name",
                PHASE2A_RECEIPT_TRIGGERS,
            )
        }
        for name in PHASE2A_RECEIPT_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        for sql in trigger_sql.values():
            conn.execute(sql.replace("NEW.ordinal", "0")
                         .replace("linked.ordinal", "0"))
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V2_VERSION,),
        )
        v1_checksum = _receipt_checksum(conn, PHASE2A_RECEIPT_V1_VERSION)
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum,applied_at) "
            "VALUES (?,?,datetime('now')) "
            "ON CONFLICT(version) DO UPDATE SET checksum=excluded.checksum",
            (PHASE2A_RECEIPT_V1_VERSION, v1_checksum),
        )
        before = _task_action_schema_snapshot(conn)
        conn.commit()

    original = Store._install_task_action_phase2a_receipt_schema

    def fail_after_install(self, conn):
        original(self, conn)
        raise RuntimeError("INJECTED_RECEIPT_V2_DDL_FAILURE")

    monkeypatch.setattr(
        Store, "_install_task_action_phase2a_receipt_schema", fail_after_install,
    )
    with pytest.raises(RuntimeError, match="INJECTED_RECEIPT_V2_DDL_FAILURE"):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        assert _task_action_schema_snapshot(conn) == before
        assert conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V1_VERSION,),
        ).fetchone()[0] == v1_checksum
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V2_VERSION,),
        ).fetchone() is None

    monkeypatch.undo()
    Store(str(db))
    with sqlite3.connect(db) as conn:
        assert len(conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?",
            (PHASE2A_RECEIPT_V2_VERSION,),
        ).fetchone()[0]) == 64
        upgraded_sql = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name IN (?,?) ORDER BY name",
                PHASE2A_RECEIPT_TRIGGERS,
            )
        }
        assert "NEW.ordinal" in upgraded_sql[
            "trg_task_action_phase2a_receipt_outbox_guard"
        ]
        assert "linked.ordinal" in upgraded_sql[
            "trg_task_action_phase2a_receipt_request_guard"
        ]


def test_phase2a_migration_failure_preserves_phase0_data_and_leaves_no_retry_objects(
        tmp_path, monkeypatch):
    db = tmp_path / "phase2a-migration-rollback.db"
    store = Store(str(db))
    task_id = store.create_task("u1", "保留的旧任务", "2026-08-29 09:00")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET current_attempt_id=NULL")
        _downgrade_to_phase0_for_migration_fault(conn)
        conn.execute(
            "DELETE FROM schema_migrations WHERE version=?", (PHASE2A_VERSION,)
        )
        # 还原真实 Phase 0 trigger 后重算其冻结 marker，才能把故障注入
        # 推进到真正的 Phase 2a DDL，而不是提前被 manifest 拦截。
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (_current_phase0_checksum(conn), PHASE0_VERSION),
        )
        before = _task_action_schema_snapshot(conn)
        conn.commit()

    original = Store._install_task_action_phase2a_schema

    def fail_after_install(self, conn):
        original(self, conn)
        raise RuntimeError("INJECTED_PHASE2A_DDL_FAILURE")

    monkeypatch.setattr(Store, "_install_task_action_phase2a_schema", fail_after_install)
    with pytest.raises(RuntimeError, match="INJECTED_PHASE2A_DDL_FAILURE"):
        Store(str(db))

    with sqlite3.connect(db) as conn:
        assert _task_action_schema_snapshot(conn) == before
        assert conn.execute(
            "SELECT description,status FROM tasks WHERE id=?", (task_id,)
        ).fetchone() == ("保留的旧任务", "pending")
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (PHASE2A_VERSION,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='task_action_retry_requests'"
        ).fetchone() is None
        assert "retry_request_id" not in {
            row[1] for row in conn.execute(
                "PRAGMA table_info(task_action_attempts)"
            )
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ("
            + ",".join("?" for _ in PHASE2A_TRIGGERS) + ")",
            PHASE2A_TRIGGERS,
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ("
            + ",".join("?" for _ in PHASE2A_RECEIPT_TRIGGERS) + ")",
            PHASE2A_RECEIPT_TRIGGERS,
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN (?,?,?)",
            PHASE2B_OBJECTS,
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version IN (?,?,?,?)",
            (PHASE2A_RECEIPT_V1_VERSION, PHASE2A_RECEIPT_V2_VERSION,
             PHASE2B_VERSION, PHASE2A_VERSION),
        ).fetchone()[0] == 0

    # 回滚后的 Phase 0 快照必须可安全重试，而不是仅仅“没有残留对象”。
    monkeypatch.undo()
    Store(str(db))


def test_retry_request_guard_rejects_missing_actual_text(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with pytest.raises(sqlite3.IntegrityError, match="receipt"):
        with store._connect() as conn:
            _insert_retry_attempt(
                conn, task_id, expected_attempt_id, "retry-missing-actual",
                receipt_actual={"requested": "冻结正文"},
            )


def test_receipt_insert_guard_uses_linked_ordinal_schema_version(store):
    _task_id, attempt_id, plan = _mixed_version_attempt(store, (1, 2))
    _insert_mixed_outbox(store, attempt_id, plan["children"][0], actual=None)

    with pytest.raises(sqlite3.IntegrityError, match="receipt"):
        _insert_mixed_outbox(
            store, attempt_id, plan["children"][1],
            actual={"requested": "冻结正文-1"},
        )


def test_receipt_insert_guard_does_not_borrow_child_zero_version(store):
    _task_id, attempt_id, plan = _mixed_version_attempt(store, (2, 1))
    _insert_mixed_outbox(
        store, attempt_id, plan["children"][0],
        actual={"requested": "冻结正文-0", "text": "冻结正文-0"},
    )
    _insert_mixed_outbox(store, attempt_id, plan["children"][1], actual=None)
    with store._connect() as conn:
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (attempt_id,),
        )

    Store(store.db_path)


def test_boot_validator_rejects_missing_v2_child_after_legacy_projection(store):
    """混合旧/新 plan 一旦已有投影，也不能借 legacy child 绕过 v2 coverage。"""
    task_id, attempt_id, plan = _mixed_version_attempt(store, (1, 2))
    legacy = plan["children"][0]
    _insert_mixed_outbox(store, attempt_id, legacy, actual=None)
    with store._connect() as conn:
        canonical = json.dumps(
            Store._phase2b_canonical_child(legacy), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        item_id = int(conn.execute(
            "INSERT INTO task_action_items(task_id,ordinal,canonical_json,created_at) "
            "VALUES (?,?,?,'now') RETURNING id",
            (task_id, legacy["ordinal"], canonical),
        ).fetchone()[0])
        outbox_id = conn.execute(
            "SELECT action_id FROM send_outbox WHERE task_attempt_id=? AND ordinal=?",
            (attempt_id, legacy["ordinal"]),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO task_action_children(item_id,attempt_id,generation,ordinal,"
            "action_id,outbox_id,state,created_at,updated_at) "
            "VALUES (?,?,0,?,?,?,'pending','now','now')",
            (item_id, attempt_id, legacy["ordinal"], legacy["action_id"], outbox_id),
        )
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (attempt_id,),
        )

    with pytest.raises(sqlite3.OperationalError, match="coverage is incomplete"):
        Store(store.db_path)


def test_boot_receipt_validator_uses_linked_ordinal_schema_version(store):
    _task_id, attempt_id, plan = _mixed_version_attempt(store, (1, 2))
    _insert_mixed_outbox(store, attempt_id, plan["children"][0], actual=None)
    with store._connect() as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_task_action_phase2a_receipt_outbox_guard'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER trg_task_action_phase2a_receipt_outbox_guard")
    _insert_mixed_outbox(
        store, attempt_id, plan["children"][1],
        actual={"requested": "冻结正文-1"},
    )
    with store._connect() as conn:
        conn.execute(trigger_sql)
        conn.execute(
            "UPDATE task_action_attempts SET state='outbox_pending' WHERE id=?",
            (attempt_id,),
        )

    with pytest.raises(sqlite3.OperationalError, match="outbox identity"):
        Store(store.db_path)


def test_retry_attempt_missing_deferred_request_fails_at_commit(store):
    task_id, expected_attempt_id = _failed_generation_zero(store)
    with store._connect() as conn:
        plan = _retry_plan_from_root(conn, task_id, "never-written")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tasks SET status='sending' WHERE id=? AND status='failed'",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_action_attempts "
            "(task_id,generation,plan_id,plan_json,retry_request_id,state,"
            "created_at,updated_at) VALUES (?,1,?,?,'never-written','persisted',"
            "'now','now')",
            (task_id, plan["plan_id"], json.dumps(
                plan, ensure_ascii=False, sort_keys=True,
            )),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.commit()
        conn.rollback()

    with store._connect() as conn:
        assert conn.execute(
            "SELECT status,current_attempt_id FROM tasks WHERE id=?", (task_id,),
        ).fetchone() == ("failed", expected_attempt_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM task_action_attempts WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 1


def test_boot_rejects_missing_phase2_trigger(store):
    with store._connect() as conn:
        conn.execute("DROP TRIGGER trg_task_action_retry_request_insert_guard")

    with pytest.raises(sqlite3.OperationalError, match="trigger manifest"):
        Store(store.db_path)


def test_boot_rejects_tampered_phase2_checksum(store):
    with store._connect() as conn:
        conn.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=?",
            (PHASE2A_VERSION,),
        )

    with pytest.raises(sqlite3.OperationalError, match="checksum differs"):
        Store(store.db_path)
