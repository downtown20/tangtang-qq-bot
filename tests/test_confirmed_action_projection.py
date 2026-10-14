"""ADR-003：known-confirmed 的永久事实、聊天与窗口原子归账。"""

import json
import sqlite3
from datetime import datetime

import pytest

import agent.action_contract as action_contract
from agent.action_contract import (
    ActionEnvelope,
    ConversationRef,
    build_action_receipt_template,
    derive_action_id,
)
from agent.store import ConfirmedProjectionConflict


VOICE = {
    "text": "哥哥，听到了吗？",
    "emotion": "温柔",
    "speed": 1.0,
    "pause": "自然",
}


def _template(*, channel="group", target="g1", user_id="u1",
              group_id="g1", source_chat_id=None, projection_kind="conversation_reply",
              delivered_text="哥哥，听到了吗？", self_memory_eligible=False,
              source_suffix="501"):
    scope_id = group_id if channel == "group" else f"_private_{user_id}"
    ref = ConversationRef() if projection_kind == "none" else ConversationRef(
        projection_kind="conversation_reply",
        conversation_user_id=user_id,
        group_id=group_id if channel == "group" else "",
        source_chat_id=source_chat_id,
        self_memory_eligible=self_memory_eligible,
    )
    action_id = derive_action_id(
        source_id=f"{scope_id}:{source_suffix}", scope_id=scope_id,
        kind="voice", channel=channel, target=target, payload=VOICE,
        schema_version=2, identity_version=1,
    )
    envelope = ActionEnvelope(
        action_id=action_id, kind="voice", channel=channel, target=target,
        payload=VOICE, source_id=f"{scope_id}:{source_suffix}", scope_id=scope_id,
        schema_version=2, identity_version=1, conversation_ref=ref,
    )
    return build_action_receipt_template(envelope, {
        "delivery_kind": "voice",
        "voice_generated": True,
        "fallback_used": False,
        "emotion": "温柔",
        "speed": 1.0,
        "pause": "自然",
        "text": delivered_text,
    })


def _rows(store, sql, params=()):
    with store._connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def test_legacy_outbox_database_additively_migrates_projection_schema(tmp_path):
    from agent.store import Store

    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE send_outbox (
                action_id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    Store(str(db))

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(send_outbox)")}
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "receipt_template", "domain_action_id", "confirmed_message_ids",
        "confirmed_at", "projection_error",
    } <= columns
    assert {
        "confirmed_action_facts", "conversation_window_events",
        "action_projection_conflicts",
    } <= tables


@pytest.mark.parametrize("channel,target,user_id,group_id", [
    ("group", "g1", "u1", "g1"),
    ("private", "u1", "u1", ""),
])
def test_confirmed_v2_atomically_projects_fact_chat_window_and_mailbox(
        store, channel, target, user_id, group_id):
    source_id = store.insert_chat(user_id, "还想听", group_id)
    template = _template(
        channel=channel, target=target, user_id=user_id,
        group_id=group_id, source_chat_id=source_id,
    )
    outbox_id = store.enqueue_send_outbox(
        channel, target, "[CQ:record,file=test.wav]", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(-901,),
    ) == "confirmed"

    assert store.get_send_outbox(outbox_id) is None
    facts = _rows(store, "SELECT * FROM confirmed_action_facts")
    chats = _rows(
        store, "SELECT * FROM chat_log WHERE event_key LIKE 'action:%:chat'",
    )
    windows = _rows(store, "SELECT * FROM conversation_window_events")
    receipts = store.lease_action_receipts(template["scope_id"])["receipts"]
    assert len(facts) == len(chats) == len(windows) == len(receipts) == 1
    assert facts[0]["domain_action_id"] == template["action_id"]
    assert facts[0]["outbox_id"] == outbox_id
    assert facts[0]["self_memory_eligible"] == 0
    assert chats[0]["qq_id"] == user_id
    assert chats[0]["group_id"] == group_id
    assert chats[0]["is_bot_reply"] == 1
    assert chats[0]["message"] == "哥哥，听到了吗？"
    assert windows[0]["actor_kind"] == "bot"
    assert windows[0]["conversation_user_id"] == user_id
    assert windows[0]["group_id"] == group_id


def test_projection_kind_none_keeps_transport_fact_without_chat_or_window(store):
    template = _template(projection_kind="none")
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(11,),
    ) == "confirmed"

    assert len(_rows(store, "SELECT * FROM confirmed_action_facts")) == 1
    assert _rows(store, "SELECT * FROM conversation_window_events") == []
    assert _rows(
        store, "SELECT * FROM chat_log WHERE event_key LIKE 'action:%'",
    ) == []


def test_mailbox_rejects_forged_v2_receipt_without_identity_payload(store):
    forged = {
        "action_id": "act-forged", "kind": "voice", "channel": "group",
        "target": "g1", "status": "confirmed", "schema_version": 2,
        "source_id": "msg-1", "scope_id": "g1", "ordinal": 0,
        "actual": {"text": "伪造"}, "message_ids": [1],
        "conversation_ref": ConversationRef().to_dict(),
    }
    with pytest.raises(ValueError, match="invalid v2 action receipt"):
        store.enqueue_action_receipt(forged)


def test_outbox_rejects_transport_target_mismatch_before_claim(store):
    template = _template(target="g1")
    with pytest.raises(ValueError, match="transport target"):
        store.enqueue_send_outbox(
            "group", "g2", "voice", receipt_template=template,
        )


def test_source_chat_mismatch_is_confirmed_unaccounted_without_partial_projection(store):
    wrong_source_id = store.insert_chat("other-user", "不是这位用户", "g1")
    template = _template(source_chat_id=wrong_source_id)
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(12,),
    ) == "confirmed_unaccounted"

    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "confirmed_unaccounted"
    assert job["projection_error"] == "PROJECTION_INVALID_SOURCE"
    assert json.loads(job["confirmed_message_ids"]) == [12]
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []
    assert _rows(store, "SELECT * FROM conversation_window_events") == []
    assert store.lease_action_receipts("g1")["receipts"] == []


@pytest.mark.parametrize("column,value", [
    ("is_synthetic", 1),
    ("quarantined_at", "2026-08-28 09:00:00"),
])
def test_synthetic_or_quarantined_source_is_not_projection_evidence(
        store, column, value):
    source_id = store.insert_chat("u1", "真实入站", "g1")
    with store._connect() as conn:
        conn.execute(
            f"UPDATE chat_log SET {column}=? WHERE id=?", (value, source_id),
        )
    template = _template(source_chat_id=source_id)
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(120,),
    ) == "confirmed_unaccounted"
    assert store.get_send_outbox(outbox_id)["projection_error"] == \
        "PROJECTION_INVALID_SOURCE"
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []


def test_tampered_outbox_template_cannot_switch_domain_identity(store):
    original = _template(projection_kind="none")
    replacement = _template(
        projection_kind="none", delivered_text="另一动作", source_suffix="502",
    )
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=original,
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET receipt_template=? WHERE action_id=?",
            (json.dumps(replacement, ensure_ascii=False), outbox_id),
        )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(121,),
    ) == "confirmed_conflict"
    assert store.get_send_outbox(outbox_id)["status"] == "confirmed_conflict"
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET receipt_template=? WHERE action_id=?",
            (json.dumps(original, ensure_ascii=False), outbox_id),
        )
    assert store.repair_confirmed_projection(outbox_id) == "confirmed"


def test_local_projection_error_never_returns_to_send_and_db_only_repair_succeeds(
        store, monkeypatch):
    source_id = store.insert_chat("u1", "还想听", "g1")
    template = _template(source_chat_id=source_id)
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    original = store._commit_confirmed_action_conn

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("injected local failure")

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", fail_projection)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(13,),
    ) == "confirmed_unaccounted"
    assert store.list_due_send_outbox() == []

    monkeypatch.setattr(store, "_commit_confirmed_action_conn", original)
    assert store.repair_confirmed_projection(outbox_id) == "confirmed"
    assert store.get_send_outbox(outbox_id) is None
    assert len(_rows(store, "SELECT * FROM confirmed_action_facts")) == 1


def test_confirmed_fact_blocks_mailbox_reinjection_after_consumption(store):
    template = _template(projection_kind="none")
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(14,),
    ) == "confirmed"
    lease = store.lease_action_receipts("g1")
    assert store.ack_action_receipts("g1", lease["lease_token"]) == 1

    with pytest.raises(ValueError, match="already confirmed"):
        store.enqueue_send_outbox(
            "group", "g1", "voice", receipt_template=template,
        )
    assert store.lease_action_receipts("g1")["receipts"] == []


def test_same_pending_domain_action_is_idempotent_but_payload_conflict_rejected(store):
    template = _template(projection_kind="none")
    first = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    second = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert second == first

    changed = {**template, "actual": {**template["actual"], "text": "另一句话"}}
    with pytest.raises(ValueError, match="domain action conflict"):
        store.enqueue_send_outbox(
            "group", "g1", "other", receipt_template=changed,
        )


def test_conflicting_duplicate_confirmed_is_audited_and_never_overwrites_fact(store):
    source_id = store.insert_chat("u1", "还想听", "g1")
    template = _template(source_chat_id=source_id)
    first = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(first)
    assert store.settle_send_outbox(first, "confirmed", message_ids=(15,)) == "confirmed"

    changed = {**template, "actual": {**template["actual"], "text": "被篡改"}}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outbox_id = "manual-conflict"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,status,"
            "attempts,next_retry_at,last_error,created_at,updated_at,domain_action_id) "
            "VALUES (?,?,?,?,?,?,'pending',0,'','',?,?,?)",
            (outbox_id, "group", "g1", "", "voice",
             json.dumps(changed, ensure_ascii=False), now, now, template["action_id"]),
        )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(15,),
    ) == "confirmed_conflict"
    assert store.get_send_outbox(outbox_id)["status"] == "confirmed_conflict"
    assert len(_rows(store, "SELECT * FROM action_projection_conflicts")) == 1
    assert len(_rows(store, "SELECT * FROM confirmed_action_facts")) == 1
    projected = _rows(
        store, "SELECT message FROM chat_log WHERE event_key LIKE 'action:%:chat'",
    )
    assert projected == [{"message": "哥哥，听到了吗？"}]


def test_exact_duplicate_confirmed_does_not_reinject_consumed_mailbox(store):
    template = _template(projection_kind="none")
    first = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(first)
    assert store.settle_send_outbox(first, "confirmed", message_ids=(16,)) == "confirmed"
    lease = store.lease_action_receipts("g1")
    assert store.ack_action_receipts("g1", lease["lease_token"]) == 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    duplicate_outbox = "manual-exact-duplicate"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO send_outbox "
            "(action_id,target_type,target_id,group_id,message,receipt_template,status,"
            "attempts,next_retry_at,last_error,created_at,updated_at,domain_action_id) "
            "VALUES (?,?,?,?,?,?,'pending',0,'','',?,?,?)",
            (duplicate_outbox, "group", "g1", "", "voice",
             json.dumps(template, ensure_ascii=False), now, now,
             template["action_id"]),
        )
    assert store.claim_send_outbox(duplicate_outbox)
    assert store.settle_send_outbox(
        duplicate_outbox, "confirmed", message_ids=(16,),
    ) == "confirmed_duplicate"
    assert store.get_send_outbox(duplicate_outbox) is None
    assert store.lease_action_receipts("g1")["receipts"] == []
    assert len(_rows(store, "SELECT * FROM confirmed_action_facts")) == 1


def test_failure_after_chat_fact_window_rolls_back_all_projection_rows(
        store, monkeypatch):
    source_id = store.insert_chat("u1", "还想听", "g1")
    template = _template(source_chat_id=source_id)
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    original = store._enqueue_action_receipt_conn

    def fail_mailbox(*_args, **_kwargs):
        raise RuntimeError("mailbox unavailable")

    monkeypatch.setattr(store, "_enqueue_action_receipt_conn", fail_mailbox)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(17,),
    ) == "confirmed_unaccounted"
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []
    assert _rows(store, "SELECT * FROM conversation_window_events") == []
    assert _rows(
        store, "SELECT * FROM chat_log WHERE event_key LIKE 'action:%'",
    ) == []
    assert store.lease_action_receipts("g1")["receipts"] == []

    monkeypatch.setattr(store, "_enqueue_action_receipt_conn", original)
    assert store.repair_confirmed_projection(outbox_id) == "confirmed"


def test_failure_deleting_outbox_rolls_back_projection_and_repairs_db_only(store):
    template = _template(projection_kind="none")
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_confirmed_delete BEFORE DELETE ON send_outbox "
            f"WHEN OLD.action_id='{outbox_id}' BEGIN "
            "SELECT RAISE(ABORT,'injected delete failure'); END"
        )

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(18,),
    ) == "confirmed_unaccounted"
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []
    assert store.lease_action_receipts("g1")["receipts"] == []

    with store._connect() as conn:
        conn.execute("DROP TRIGGER fail_confirmed_delete")
    assert store.repair_confirmed_projection(outbox_id) == "confirmed"
    assert store.get_send_outbox(outbox_id) is None


def test_repair_conflict_before_receipt_assignment_is_quarantined_not_crash(
        store, monkeypatch):
    """finalizer 提前抛出冲突时不能触发 UnboundLocalError 或改成可发送状态。"""
    template = _template(projection_kind="none")
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_unaccounted',"
            "confirmed_message_ids='[181]',confirmed_at='2026-08-28 12:00:00' "
            "WHERE action_id=?", (outbox_id,),
        )

    def fail_before_receipt(*_args, **_kwargs):
        raise ConfirmedProjectionConflict("old", "new")

    monkeypatch.setattr(
        action_contract, "finalize_action_receipt_template", fail_before_receipt,
    )
    assert store.repair_confirmed_projection(outbox_id) == "confirmed_unaccounted"
    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "confirmed_unaccounted"
    assert job["projection_error"] == "PROJECTION_CONFLICT_BEFORE_RECEIPT"


def test_self_memory_eligibility_is_dormant_fact_only(store):
    template = _template(
        projection_kind="conversation_reply", self_memory_eligible=True,
    )
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(19,),
    ) == "confirmed"

    assert _rows(store, "SELECT self_memory_eligible FROM confirmed_action_facts") == [
        {"self_memory_eligible": 1},
    ]
    assert _rows(store, "SELECT * FROM memories") == []


def test_self_memory_action_anchor_requires_confirmed_same_scope_fact(store):
    source_id = store.insert_chat("u1", "唱一首歌", group_id="g1")
    template = _template(
        channel="group", target="g1", user_id="u1", group_id="g1",
        source_chat_id=source_id, self_memory_eligible=True,
    )
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "[CQ:record,file=test.wav]", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(outbox_id, "confirmed", message_ids=(21,)) == "confirmed"

    assert store.validate_self_memory_action_anchor(
        template["action_id"], "u1", "g1",
    ) is True
    assert store.validate_self_memory_action_anchor(
        template["action_id"], "u2", "g1",
    ) is False
    assert store.validate_self_memory_action_anchor(
        template["action_id"], "u1", "g2",
    ) is False


@pytest.mark.parametrize("terminal_state", ["uncertain", "failed"])
def test_non_confirmed_terminal_states_never_project(store, terminal_state):
    template = _template(projection_kind="none")
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    store.settle_send_outbox(
        outbox_id, terminal_state, error_code="NETWORK", max_attempts=1,
    )
    assert _rows(store, "SELECT * FROM confirmed_action_facts") == []
    assert _rows(store, "SELECT * FROM conversation_window_events") == []
