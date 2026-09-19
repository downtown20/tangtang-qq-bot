"""ADR-003：QQ confirmed 与本地 settle 必须属于不同异常域。"""

import asyncio
from unittest.mock import AsyncMock, Mock

from napcat.ws_client import NapCatClient


def test_local_settle_exception_after_confirmed_never_becomes_uncertain_or_resends(
        store, monkeypatch):
    outbox_id = store.enqueue_send_outbox("group", "100", "hello")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 88},
    })
    settle_calls = []

    def broken_settle(*args, **kwargs):
        settle_calls.append((args, kwargs))
        raise RuntimeError("sqlite write failed")

    monkeypatch.setattr(store, "settle_send_outbox", broken_settle)

    assert asyncio.run(client.process_send_outbox()) == 1

    assert client._call_api.await_count == 1
    assert len(settle_calls) == 1
    assert settle_calls[0][0][1] == "confirmed"
    job = store.get_send_outbox(outbox_id)
    assert job["status"] == "confirmed_unaccounted"
    assert job["confirmed_message_ids"] == "[88]"
    assert store.list_due_send_outbox() == []

    assert asyncio.run(client.process_send_outbox()) == 0
    assert client._call_api.await_count == 1


def test_outbox_loop_survives_one_processing_failure(monkeypatch):
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._running = True
        client._closed = False
        attempts = 0

        async def process(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary sqlite failure")
            client._running = False
            return 0

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr(client, "process_send_outbox", process)
        monkeypatch.setattr("napcat.ws_client.asyncio.sleep", no_wait)
        monkeypatch.setattr(
            type(client), "ready_to_send", property(lambda _self: True),
        )
        await client._outbox_loop()
        return attempts

    assert asyncio.run(scenario()) == 2


def test_confirmed_projection_repair_is_db_only_and_runs_while_offline(
        store, monkeypatch):
    """known-confirmed 修复不能因 QQ 离线而等待，也绝不触发外部发送。"""
    outbox_id = store.enqueue_send_outbox("group", "100", "hello")
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_unaccounted',"
            "confirmed_message_ids='[88]',confirmed_at='2026-08-28 12:00:00',"
            "projection_error='PROJECTION_LOCAL_ERROR' WHERE action_id=?",
            (outbox_id,),
        )

    client = NapCatClient(testing_mode=True)
    client._qq_online = False
    client.bind_outbox_store(store)
    send = AsyncMock()
    monkeypatch.setattr(client, "send_group_message", send)

    # 该 legacy outbox 没有 receipt_template，无法完成永久投影；关键是修复
    # 仍在离线时被尝试，且不会退回 QQ 发送或 pending。
    assert client.process_confirmed_projection_repairs() == 1
    assert store.get_send_outbox(outbox_id)["status"] == "confirmed_unaccounted"
    send.assert_not_awaited()


def test_confirmed_conflict_is_not_auto_retried_by_db_only_repair(store, monkeypatch):
    """冲突态保留人工审查入口，不在后台循环中反复投影。"""
    outbox_id = store.enqueue_send_outbox("group", "100", "hello")
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_conflict',"
            "confirmed_message_ids='[89]',confirmed_at='2026-08-28 12:00:00',"
            "projection_error='PROJECTION_CONFLICT' WHERE action_id=?",
            (outbox_id,),
        )

    client = NapCatClient(testing_mode=True)
    client._outbox_store = store
    repair = Mock(wraps=store.repair_confirmed_projection)
    monkeypatch.setattr(store, "repair_confirmed_projection", repair)

    assert client.process_confirmed_projection_repairs() == 0
    repair.assert_not_called()
    assert store.get_send_outbox(outbox_id)["status"] == "confirmed_conflict"


def test_outbox_loop_survives_repair_round_failure(monkeypatch):
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._running = True
        client._closed = False
        repair_rounds = 0
        process_rounds = 0

        def broken_repair(*_args, **_kwargs):
            nonlocal repair_rounds
            repair_rounds += 1
            if repair_rounds == 1:
                raise RuntimeError("temporary repair failure")

        async def process(*_args, **_kwargs):
            nonlocal process_rounds
            process_rounds += 1
            if process_rounds == 2:
                client._running = False
            return 0

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr(client, "process_confirmed_projection_repairs", broken_repair)
        monkeypatch.setattr(client, "process_send_outbox", process)
        monkeypatch.setattr("napcat.ws_client.asyncio.sleep", no_wait)
        monkeypatch.setattr(
            type(client), "ready_to_send", property(lambda _self: True),
        )
        await client._outbox_loop()
        return repair_rounds, process_rounds

    assert asyncio.run(scenario()) == (2, 2)
