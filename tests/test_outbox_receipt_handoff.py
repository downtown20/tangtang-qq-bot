"""ADR-002 B1：outbox 保持唯一重试所有权，并原子移交 terminal receipt。"""

import hashlib
import json
import sqlite3

from agent.action_contract import (
    ActionEnvelope,
    build_action_receipt_template,
    derive_action_id,
    finalize_action_receipt_template,
)
from agent.store import Store
from napcat.ws_client import NapCatClient


def _template(scope_id: str, action_id: str = "act-voice-outbox") -> dict:
    channel = "group" if not scope_id.startswith("_private_") else "private"
    target = scope_id if channel == "group" else scope_id[len("_private_"):]
    envelope = ActionEnvelope(
        action_id=action_id,
        kind="voice",
        channel=channel,
        target=target,
        payload={
            "text": "你好呀", "emotion": "温柔", "speed": 1.0, "pause": "自然",
        },
        source_id=f"{scope_id}:501",
        scope_id=scope_id,
        ordinal=0,
    )
    return build_action_receipt_template(envelope, {
        "delivery_kind": "voice",
        "voice_generated": True,
        "fallback_used": False,
        "emotion": "温柔",
        "speed": 1.0,
        "pause": "自然",
    })


def test_receipt_template_finalizer_preserves_static_fact_contract():
    template = _template("g1")

    receipt = finalize_action_receipt_template(
        template,
        status="confirmed",
        message_ids=(-7,),
    )

    assert receipt["action_id"] == "act-voice-outbox"
    assert receipt["scope_id"] == "g1"
    assert receipt["status"] == "confirmed"
    assert receipt["message_ids"] == [-7]
    assert receipt["actual"]["delivery_kind"] == "voice"


def test_sticker_outbox_replay_requires_original_file_hash(tmp_path):
    asset = tmp_path / "happy.png"
    asset.write_bytes(b"original sticker")
    payload = {
        "asset_ref": "happy.png",
        "asset_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        "asset_valid": True,
        "emotion": "开心",
        "count": 1,
        "role_id": "default",
        "library_id": "stickers-v1",
    }
    envelope = ActionEnvelope(
        action_id=derive_action_id(
            source_id="g1:501", scope_id="g1", kind="sticker",
            channel="group", target="g1", payload=payload,
            schema_version=2,
        ),
        kind="sticker", channel="group", target="g1", payload=payload,
        source_id="g1:501", scope_id="g1", schema_version=2,
    )
    template = build_action_receipt_template(envelope, {
        "delivery_kind": "sticker", "asset_ref": "happy.png",
    })
    job = {
        "receipt_template": json.dumps(template, ensure_ascii=False),
        "message": f"[CQ:image,file=file:///{asset.as_posix()}]",
    }
    assert NapCatClient._validate_sticker_outbox_asset(job) == ""
    asset.write_bytes(b"replaced sticker")
    assert NapCatClient._validate_sticker_outbox_asset(job) == \
        "STICKER_ASSET_HASH_MISMATCH"


def test_confirmed_outbox_atomically_moves_terminal_receipt_to_mailbox(store):
    outbox_id = store.enqueue_send_outbox(
        "group", "g1", "[CQ:record,file=test.wav]",
        receipt_template=_template("g1"),
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(-9,),
    ) == "confirmed"

    assert store.get_send_outbox(outbox_id) is None
    receipts = store.lease_action_receipts("g1")["receipts"]
    assert [(item["action_id"], item["status"], item["message_ids"])
            for item in receipts] == [("act-voice-outbox", "confirmed", [-9])]


def test_uncertain_outbox_is_terminal_and_never_replayed(store):
    outbox_id = store.enqueue_send_outbox(
        "private", "u1", "[CQ:record,file=test.wav]",
        receipt_template=_template("_private_u1"),
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "uncertain", error_code="MESSAGE_ID_UNCONFIRMED",
    ) == "uncertain"

    assert store.get_send_outbox(outbox_id)["status"] == "uncertain"
    assert store.list_due_send_outbox() == []
    receipt = store.lease_action_receipts("_private_u1")["receipts"][0]
    assert receipt["status"] == "uncertain"
    assert receipt["error_code"] == "MESSAGE_ID_UNCONFIRMED"


def test_failed_outbox_writes_receipt_only_after_bounded_retries_are_dead(store):
    outbox_id = store.enqueue_send_outbox(
        "group", "g2", "[CQ:record,file=test.wav]",
        receipt_template=_template("g2", action_id="act-dead"),
    )

    for expected in ("pending", "pending", "dead"):
        assert store.claim_send_outbox(outbox_id)
        assert store.settle_send_outbox(
            outbox_id,
            "failed",
            error_code="NETWORK",
            max_attempts=3,
            retry_delay_seconds=0,
        ) == expected
        if expected != "dead":
            assert store.lease_action_receipts("g2")["receipts"] == []

    receipt = store.lease_action_receipts("g2")["receipts"][0]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "NETWORK"


def test_restart_recovers_sending_outbox_as_uncertain_receipt(store):
    outbox_id = store.enqueue_send_outbox(
        "group", "g3", "[CQ:record,file=test.wav]",
        receipt_template=_template("g3", action_id="act-restart"),
    )
    legacy_id = store.enqueue_send_outbox("group", "g4", "legacy")
    assert store.claim_send_outbox(outbox_id)
    assert store.claim_send_outbox(legacy_id)

    assert store.recover_send_outbox_after_restart() == 2

    receipt = store.lease_action_receipts("g3")["receipts"][0]
    assert receipt["status"] == "uncertain"
    assert receipt["error_code"] == "PROCESS_RESTART_UNCERTAIN"
    assert store.lease_action_receipts("g4")["receipts"] == []


def test_existing_database_migrates_receipt_template_column(tmp_path):
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
    assert "receipt_template" in columns


def test_corrupt_template_is_quarantined_without_deleting_confirmed_outbox(store):
    outbox_id = store.enqueue_send_outbox("group", "g5", "voice")
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET receipt_template='{' WHERE action_id=?",
            (outbox_id,),
        )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(5,),
    ) == "confirmed_unaccounted"

    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "confirmed_unaccounted"
    assert job["projection_error"] == "RECEIPT_TEMPLATE_INVALID"
    assert job["confirmed_message_ids"] == "[5]"
    assert store.lease_action_receipts("g5")["receipts"] == []


def test_restart_quarantines_corrupt_template_without_blocking_startup(store):
    outbox_id = store.enqueue_send_outbox("group", "g6", "voice")
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET receipt_template='{' WHERE action_id=?",
            (outbox_id,),
        )
    assert store.claim_send_outbox(outbox_id)

    assert store.recover_send_outbox_after_restart() == 1

    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "uncertain"
    assert job["last_error"] == "RECEIPT_TEMPLATE_INVALID"


def test_outbox_keeps_error_detail_but_receipt_uses_machine_error_code(store):
    outbox_id = store.enqueue_send_outbox(
        "group", "g7", "voice", receipt_template=_template("g7"),
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id,
        "uncertain",
        error_code="SEND_RESULT_LOST",
        error_detail="response lost after POST",
    ) == "uncertain"

    assert store.get_send_outbox(outbox_id)["last_error"] == "response lost after POST"
    leased = store.lease_action_receipts("g7")
    assert leased["receipts"][0]["error_code"] == "SEND_RESULT_LOST"


def test_conflicting_existing_receipt_quarantines_outbox_instead_of_sticking_sending(
        store):
    template = _template("g8")
    store.enqueue_action_receipt({
        **template,
        "status": "failed",
        "message_ids": [],
        "error_code": "OLD_FACT",
    })
    outbox_id = store.enqueue_send_outbox(
        "group", "g8", "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)

    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(88,),
    ) == "confirmed_conflict"

    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "confirmed_conflict"
    assert job["projection_error"] == "ACTION_RECEIPT_CONFLICT"
