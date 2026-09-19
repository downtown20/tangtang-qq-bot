"""ADR-002 B1：持久动作回执邮箱的数据层状态机。"""

from datetime import datetime, timedelta

import pytest

from agent.action_contract import ActionReceipt


def _receipt(action_id: str, scope_id: str, ordinal: int = 0,
             status: str = "confirmed") -> dict:
    return ActionReceipt(
        action_id=action_id, kind="sticker", channel="group", target="g1",
        status=status, message_ids=(ordinal + 1,),
        actual={"delivery_kind": "sticker", "files": [f"{ordinal}.png"]},
        source_id="msg-501", scope_id=scope_id, ordinal=ordinal,
    ).to_dict()


def test_receipt_mailbox_insert_is_idempotent_but_rejects_conflicting_fact(store):
    receipt = _receipt("act-1", "group:g1")

    assert store.enqueue_action_receipt(receipt) is True
    assert store.enqueue_action_receipt(receipt) is False

    conflicting = dict(receipt)
    conflicting["status"] = "failed"
    with pytest.raises(ValueError, match="conflicting action receipt"):
        store.enqueue_action_receipt(conflicting)


def test_receipt_mailbox_leases_fifo_with_strict_scope_and_ack(store):
    store.enqueue_action_receipt(_receipt("act-1", "group:g1", 0))
    store.enqueue_action_receipt(_receipt("act-2", "group:g1", 1))
    store.enqueue_action_receipt(_receipt("act-3", "private:u1", 0))

    lease = store.lease_action_receipts("group:g1", limit=5)

    assert [item["action_id"] for item in lease["receipts"]] == ["act-1", "act-2"]
    assert all(item["scope_id"] == "group:g1" for item in lease["receipts"])
    assert store.ack_action_receipts("group:g1", lease["lease_token"]) == 2
    assert store.lease_action_receipts("group:g1")["receipts"] == []
    assert [item["action_id"] for item in
            store.lease_action_receipts("private:u1")["receipts"]] == ["act-3"]


def test_get_action_receipts_reads_mailbox_without_leasing(store):
    receipt = _receipt("act-read", "group:g1")
    assert store.enqueue_action_receipt(receipt) is True

    found = store.get_action_receipts("group:g1", ["act-read"])

    assert found["act-read"] == receipt
    assert store.lease_action_receipts("group:g1")["receipts"] == [receipt]


def test_receipt_mailbox_release_and_expired_lease_redeliver_without_replay(store):
    store.enqueue_action_receipt(_receipt("act-1", "group:g1"))
    first = store.lease_action_receipts("group:g1")

    assert store.release_action_receipts("group:g1", first["lease_token"]) == 1
    second = store.lease_action_receipts("group:g1")
    assert [item["action_id"] for item in second["receipts"]] == ["act-1"]

    past = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    with store._connect() as conn:
        conn.execute(
            "UPDATE action_receipt_mailbox SET lease_until=? WHERE action_id='act-1'",
            (past,),
        )
    third = store.lease_action_receipts("group:g1")
    assert [item["action_id"] for item in third["receipts"]] == ["act-1"]
    assert third["lease_token"] != second["lease_token"]


def test_receipt_mailbox_ttl_and_scope_capacity_expire_context_only(store):
    for ordinal in range(3):
        store.enqueue_action_receipt(
            _receipt(f"act-{ordinal}", "group:g1", ordinal),
            per_scope_limit=2,
        )

    health = store.get_action_receipt_mailbox_health()
    assert health["deliverable"] == 2
    assert health["expired"] == 1
    assert health["expired_unconsumed"] == 1

    old = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    with store._connect() as conn:
        conn.execute(
            "UPDATE action_receipt_mailbox SET created_at=? WHERE state='deliverable'",
            (old,),
        )
    assert store.expire_action_receipts(ttl_seconds=86400) == 2
    assert store.lease_action_receipts("group:g1")["receipts"] == []


def test_receipt_mailbox_global_limit_prunes_only_closed_context(store):
    for ordinal in range(4):
        store.enqueue_action_receipt(
            _receipt(f"act-{ordinal}", f"group:g{ordinal}", ordinal),
            global_limit=3,
        )

    health = store.get_action_receipt_mailbox_health()
    assert health["total"] == 3
    assert health["deliverable"] == 3


@pytest.mark.parametrize("status", ["draft", "accepted"])
def test_receipt_mailbox_rejects_non_terminal_status(store, status):
    receipt = _receipt("act-invalid", "group:g1")
    receipt["status"] = status

    with pytest.raises(ValueError):
        store.enqueue_action_receipt(receipt)
