"""P2 可重试发送 outbox：持久、有限重试、崩溃不盲重放。"""

import asyncio
import hashlib
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.action_contract import ActionEnvelope, build_action_receipt_template
from agent.store import Store
from onebot.ws_client import NapCatClient, SendResult, send_delivery_state


def test_outbox_store_reads_do_not_block_event_loop():
    """重试轮次的同步 Store 读取必须在线程池执行。"""
    class SlowStore:
        def list_due_send_outbox(self, limit=20):
            time.sleep(0.08)
            return []

    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client._outbox_store = SlowStore()

    async def scenario():
        ticks = []

        async def heartbeat():
            await asyncio.sleep(0.01)
            ticks.append(time.perf_counter())

        started = time.perf_counter()
        await asyncio.gather(client.process_send_outbox(), heartbeat())
        return ticks[0] - started

    assert asyncio.run(scenario()) < 0.06


def test_outbox_media_validation_does_not_block_event_loop(monkeypatch):
    """重放前媒体哈希校验也不能把大文件读取放回事件循环。"""
    job = {
        "action_id": "a1", "target_type": "group", "target_id": "1",
        "message": "hello", "receipt_template": "{}",
    }

    class StoreStub:
        def list_due_send_outbox(self, limit=20):
            return [job]

        def claim_send_outbox(self, action_id):
            return job

        def settle_send_outbox(self, *args, **kwargs):
            return "confirmed"

    def slow_validate(_job):
        time.sleep(0.08)
        return ""

    monkeypatch.setattr(
        NapCatClient, "_validate_sticker_outbox_asset",
        staticmethod(slow_validate),
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client._outbox_store = StoreStub()

    async def send(*args, **kwargs):
        return SendResult(True, True, message_id=1)

    client.send_group_message = send

    async def scenario():
        ticks = []

        async def heartbeat():
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks.append(time.perf_counter())

        await asyncio.gather(client.process_send_outbox(), heartbeat())
        return len(ticks)

    assert asyncio.run(scenario()) == 5


def _receipt_template(scope_id: str, channel: str, target: str) -> dict:
    envelope = ActionEnvelope(
        action_id=f"act-{channel}-outbox",
        kind="voice",
        channel=channel,
        target=target,
        payload={
            "text": "稍后发送", "emotion": "温柔", "speed": 1.0, "pause": "自然",
        },
        source_id=f"{scope_id}:501",
        scope_id=scope_id,
        ordinal=0,
    )
    return build_action_receipt_template(envelope, {
        "delivery_kind": "voice",
        "voice_generated": True,
        "fallback_used": False,
    })


def test_outbox_persists_and_sending_restarts_as_uncertain(tmp_path):
    path = tmp_path / "memory.db"
    store = Store(str(path))
    action_id = store.enqueue_send_outbox("group", "100", "hello")

    job = store.claim_send_outbox(action_id)
    assert job["status"] == "sending"

    restarted = Store(str(path))
    NapCatClient(testing_mode=True).bind_outbox_store(restarted)
    assert restarted.get_send_outbox(action_id)["status"] == "uncertain"
    assert restarted.list_due_send_outbox() == []


def test_outbox_retry_is_bounded_then_dead_letter(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    action_id = store.enqueue_send_outbox("private", "200", "hello")

    for attempt in range(3):
        assert store.claim_send_outbox(action_id)
        store.fail_send_outbox(action_id, "network", max_attempts=3)

    job = store.get_send_outbox(action_id)
    assert job["status"] == "dead"
    assert job["attempts"] == 3


def test_group_network_failure_enters_outbox_exactly_once(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })

    result = asyncio.run(client.send_group_message("100", "hello"))
    assert bool(result) is False
    assert result.error == "NETWORK"

    jobs = store.list_due_send_outbox()
    assert len(jobs) == 1
    assert jobs[0]["target_type"] == "group"
    assert jobs[0]["target_id"] == "100"
    assert jobs[0]["message"] == "hello"
    assert jobs[0]["last_error"] == "NETWORK"


def test_private_network_failure_enters_outbox_exactly_once(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })

    result = asyncio.run(client.send_private_message(
        "200", "hello privately", group_id="100",
    ))

    assert bool(result) is False
    assert result.error == "NETWORK"
    assert result.retryable is True
    jobs = store.list_due_send_outbox()
    assert len(jobs) == 1
    assert jobs[0]["target_type"] == "private"
    assert jobs[0]["target_id"] == "200"
    assert jobs[0]["group_id"] == "100"
    assert jobs[0]["message"] == "hello privately"


def test_known_offline_send_enters_outbox_without_calling_api(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = False
    client.bind_outbox_store(store)
    client._call_api = AsyncMock()

    result = asyncio.run(client.send_group_message("100", "wait for reconnect"))

    assert result.error == "OFFLINE"
    assert result.retryable is True
    client._call_api.assert_not_awaited()
    jobs = store.list_due_send_outbox()
    assert len(jobs) == 1
    assert jobs[0]["message"] == "wait for reconnect"


@pytest.mark.parametrize("channel,target,scope", [
    ("group", "100", "100"),
    ("private", "200", "_private_200"),
])
def test_retryable_send_carries_receipt_template_to_outbox_terminal(
        tmp_path, channel, target, scope):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = False
    client.bind_outbox_store(store)
    template = _receipt_template(scope, channel, target)

    if channel == "group":
        result = asyncio.run(client.send_group_message(
            target, "[CQ:record,file=test.wav]", receipt_template=template,
        ))
    else:
        result = asyncio.run(client.send_private_message(
            target, "[CQ:record,file=test.wav]", receipt_template=template,
        ))
    assert result.retryable is True
    job = store.list_due_send_outbox()[0]
    assert job["receipt_template"]

    client._qq_online = True
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": -321},
    })
    assert asyncio.run(client.process_send_outbox()) == 1

    receipt = store.lease_action_receipts(scope)["receipts"][0]
    assert receipt["action_id"] == f"act-{channel}-outbox"
    assert receipt["status"] == "confirmed"
    assert receipt["message_ids"] == [-321]


def test_frozen_voice_asset_is_verified_before_outbox_replay(tmp_path):
    audio = tmp_path / "frozen.wav"
    audio.write_bytes(b"frozen voice bytes")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    envelope = ActionEnvelope(
        action_id="act-voice-frozen",
        kind="voice", channel="group", target="100",
        payload={"text": "稍后发送", "emotion": "温柔", "speed": 1.0,
                 "pause": "自然"},
        source_id="g1:voice-frozen", scope_id="100", ordinal=0,
    )
    template = build_action_receipt_template(envelope, {
        "delivery_kind": "voice", "voice_generated": True,
        "asset_frozen": True, "asset_sha256": digest,
    })
    message = f"[CQ:record,file=file:///{audio.resolve().as_posix()}]"
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = False
    client.bind_outbox_store(store)
    result = asyncio.run(client.send_group_message(
        "100", message, receipt_template=template,
    ))
    assert result.retryable is True
    action_id = store.list_due_send_outbox()[0]["action_id"]

    audio.unlink()
    client._qq_online = True
    client._call_api = AsyncMock()
    assert asyncio.run(client.process_send_outbox()) == 1
    client._call_api.assert_not_awaited()
    job = store.get_send_outbox(action_id)
    assert job["status"] == "dead"
    assert job["last_error"] == "VOICE_ASSET_MISSING"


def test_long_group_retryable_failure_enqueues_original_message_once(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })
    message = "x" * 2001

    result = asyncio.run(client.send_group_message("100", message))

    assert result.retryable is True
    jobs = store.list_due_send_outbox()
    assert len(jobs) == 1
    assert jobs[0]["message"] == message


def test_http_4xx_and_business_rejections_do_not_enter_outbox(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)

    client._call_api = AsyncMock(return_value={
        "status": "failed", "retcode": 400,
        "_transport_error": "HTTP_4XX",
    })
    http_result = asyncio.run(client.send_group_message("100", "bad request"))

    client._call_api = AsyncMock(return_value={
        "status": "failed", "retcode": 1400, "msg": "bad params",
    })
    business_result = asyncio.run(client.send_private_message("200", "rejected"))

    assert http_result.retryable is False
    assert business_result.retryable is False
    assert store.list_due_send_outbox() == []


def test_outbox_replay_failure_does_not_enqueue_duplicate_jobs(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    group_action = store.enqueue_send_outbox("group", "100", "group retry")
    private_action = store.enqueue_send_outbox(
        "private", "200", "private retry", group_id="100",
    )
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "failed", "_transport_error": "NETWORK",
    })

    assert asyncio.run(client.process_send_outbox()) == 2

    assert store.get_send_outbox_health()["open"] == 2
    assert store.get_send_outbox(group_action)["attempts"] == 1
    assert store.get_send_outbox(private_action)["attempts"] == 1


def test_post_read_timeout_is_uncertain_and_never_auto_replayed(tmp_path):
    class StubHttp:
        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "http://127.0.0.1/send_group_msg")
            raise httpx.ReadTimeout("response lost", request=request)

    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._http = StubHttp()

    result = asyncio.run(client.send_group_message("100", "maybe delivered"))

    assert bool(result) is False
    assert send_delivery_state(result) == "uncertain"
    assert result.error == "NETWORK_UNCERTAIN"
    assert result.retryable is False
    assert store.list_due_send_outbox() == []


def test_unconfirmed_retry_result_stays_uncertain_and_is_not_replayed(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    action_id = store.enqueue_send_outbox("group", "100", "hello")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": 0},
    })

    assert asyncio.run(client.process_send_outbox()) == 1

    job = store.get_send_outbox(action_id)
    assert job["status"] == "uncertain"
    assert store.list_due_send_outbox() == []


def test_network_uncertain_with_ok_false_is_quarantined_not_dead(tmp_path):
    """网络响应丢失时 ok 可能为 False，但仍不能按确定失败重放。"""
    store = Store(str(tmp_path / "memory.db"))
    action_id = store.enqueue_send_outbox("group", "100", "maybe delivered")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client.send_group_message = AsyncMock(return_value=SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    ))

    assert asyncio.run(client.process_send_outbox()) == 1

    job = store.get_send_outbox(action_id)
    assert job["status"] == "uncertain"
    assert job["last_error"] == "NETWORK_UNCERTAIN"
    assert store.list_due_send_outbox() == []


def test_outbox_send_exception_is_quarantined_not_replayed(tmp_path):
    """未知回调异常可能发生在 POST 之后，不能回到 pending 盲重放。"""
    store = Store(str(tmp_path / "memory.db"))
    action_id = store.enqueue_send_outbox("group", "100", "maybe delivered")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client.send_group_message = AsyncMock(
        side_effect=RuntimeError("response lost after POST")
    )

    assert asyncio.run(client.process_send_outbox()) == 1

    job = store.get_send_outbox(action_id)
    assert job["status"] == "uncertain"
    assert "response lost" in job["last_error"]
    assert store.list_due_send_outbox() == []


def test_outbox_uses_current_legacy_boolean_result_not_stale_client_state(tmp_path):
    """旧适配器返回 bool 时，不能读取上一次请求的共享结果。"""
    store = Store(str(tmp_path / "memory.db"))
    action_id = store.enqueue_send_outbox("group", "100", "hello")
    client = NapCatClient(testing_mode=True)
    client._qq_online = True
    client.bind_outbox_store(store)
    client._last_send_result = SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    )
    client.send_group_message = AsyncMock(return_value=True)

    assert asyncio.run(client.process_send_outbox()) == 1
    assert store.get_send_outbox(action_id) is None


def test_outbox_loop_replays_pending_jobs_while_online():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        client._running = True
        client._closed = False
        client.process_send_outbox = AsyncMock(return_value=1)
        sleeps = 0

        async def stop_after_first(_seconds):
            nonlocal sleeps
            sleeps += 1
            client._running = False

        from unittest.mock import patch
        with patch("onebot.ws_client.asyncio.sleep", new=stop_after_first):
            await client._outbox_loop()

        assert sleeps == 1
        client.process_send_outbox.assert_awaited_once_with(limit=5)

    asyncio.run(scenario())
