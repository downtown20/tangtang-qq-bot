"""NapCat 入站事件的顺序、去重、并发和停机契约。"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from onebot.ws_client import NapCatClient


def _run(coro):
    return asyncio.run(coro)


def _group_event(message_id: int, group_id: str = "100") -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": message_id,
        "group_id": int(group_id),
        "user_id": 1,
        "sender": {"user_id": 1, "nickname": "tester"},
        "message": f"message-{message_id}",
        "raw_message": f"message-{message_id}",
        "time": 1,
    }


def test_inbox_store_registration_is_bounded_and_nonblocking():
    """持久 inbox 登记不能因慢 SQLite 把网关事件循环卡住。"""
    class SlowStore:
        def register_inbound_event(self, key, event_type):
            time.sleep(0.08)
            return "received"

        def claim_inbound_event(self, key):
            return True

        def mark_inbound_event_executing(self, key):
            return True

        def complete_inbound_event(self, key):
            return True

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._outbox_store = SlowStore()
        client.on_group_message = lambda _message: None
        ticks = []

        async def heartbeat():
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks.append(time.perf_counter())

        await asyncio.gather(client._dispatch(_group_event(999)), heartbeat())
        await client.close()
        return len(ticks)

    assert asyncio.run(scenario()) == 5


def test_combined_inbox_claim_records_latency_bucket():
    """合并领取路径应采集低开销延迟桶，供线上 p50/p95 验收。"""
    class Store:
        def claim_and_mark_inbound_event_executing(self, _key):
            return True

        def complete_inbound_event(self, _key):
            return True

    class Metrics:
        def __init__(self):
            self.names = []

        def incr(self, name, delta=1):
            self.names.extend([name] * delta)

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._outbox_store = Store()
        metrics = Metrics()
        client.bind_metrics(metrics)
        done = asyncio.Event()

        async def callback(_message):
            done.set()

        client.on_group_message = callback
        await client._dispatch(_group_event(1001))
        await asyncio.wait_for(done.wait(), timeout=0.5)
        await client.close()
        return metrics.names

    names = _run(scenario())
    assert "gateway_inbox_claim_latency_samples" in names
    assert any(name.startswith("gateway_inbox_claim_latency_")
               and name != "gateway_inbox_claim_latency_samples"
               for name in names)


def test_same_scope_messages_are_processed_in_fifo_order():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        both_done = asyncio.Event()
        started = []
        completed = []

        async def callback(message):
            message_id = message["message_id"]
            started.append(message_id)
            if message_id == 1:
                first_started.set()
                await release_first.wait()
            completed.append(message_id)
            if len(completed) == 2:
                both_done.set()

        client.on_group_message = callback
        await client._dispatch(_group_event(1))
        await client._dispatch(_group_event(2))
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        await asyncio.sleep(0)

        assert started == [1]
        release_first.set()
        await asyncio.wait_for(both_done.wait(), timeout=0.2)
        assert completed == [1, 2]
        await client.close()

    _run(scenario())


def test_duplicate_message_id_is_processed_once():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        handled = []
        done = asyncio.Event()

        async def callback(message):
            handled.append(message["message_id"])
            done.set()

        client.on_group_message = callback
        event = _group_event(7)
        await client._dispatch(event)
        await client._dispatch(dict(event))
        await asyncio.wait_for(done.wait(), timeout=0.2)
        await asyncio.sleep(0.02)

        assert handled == [7]
        await client.close()

    _run(scenario())


def test_message_callbacks_receive_normalized_inbound_event_contract():
    """网关边界先生成不可变入站事实，旧 dict 字段仍保持兼容。"""
    from agent.interaction_contract import InboundEvent

    async def scenario():
        client = NapCatClient(testing_mode=True, self_id="999")
        captured = []

        async def group_callback(message):
            captured.append(message)

        client.on_group_message = group_callback
        await client._handle_group(_group_event(42, "100"))

        assert len(captured) == 1
        message = captured[0]
        event = message["_inbound_event"]
        assert isinstance(event, InboundEvent)
        assert event.channel == "group"
        assert event.scope_id == "group:100"
        assert event.actor_id == "1"
        assert event.message_id == 42
        assert event.raw_message == "message-42"
        assert message["message"] == "message-42"

        client.on_private_message = group_callback
        await client._handle_private({
            "message_type": "private",
            "message_id": 43,
            "user_id": 2,
            "sender": {"user_id": 2, "nickname": "private"},
            "message": "private-43",
            "raw_message": "private-43",
            "time": 1,
        })
        private_event = captured[1]["_inbound_event"]
        assert isinstance(private_event, InboundEvent)
        assert private_event.channel == "private"
        assert private_event.scope_id == "private:2"
        assert private_event.actor_id == "2"
        assert private_event.message_id == 43
        assert captured[1]["message"] == "private-43"

    _run(scenario())


def test_same_message_id_in_different_groups_is_not_a_duplicate():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        handled = []
        done = asyncio.Event()

        async def callback(message):
            handled.append((message["group_id"], message["message_id"]))
            if len(handled) == 2:
                done.set()

        client.on_group_message = callback
        await client._dispatch(_group_event(7, "100"))
        await client._dispatch(_group_event(7, "200"))
        await asyncio.wait_for(done.wait(), timeout=0.2)

        assert sorted(handled) == [("100", 7), ("200", 7)]
        await client.close()

    _run(scenario())


def test_same_message_id_at_different_event_time_is_not_a_duplicate():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        handled = []
        done = asyncio.Event()

        async def callback(message):
            handled.append((message["message_id"], message["time"]))
            if len(handled) == 2:
                done.set()

        first = _group_event(8, "100")
        second = dict(first, time=2, message="new", raw_message="new")
        client.on_group_message = callback
        await client._dispatch(first)
        await client._dispatch(second)
        await asyncio.wait_for(done.wait(), timeout=0.5)

        assert handled == [(8, 1), (8, 2)]
        await client.close()

    _run(scenario())


def test_processed_event_is_not_replayed_after_process_restart(store):
    """内存 TTL 失效/进程重启后，持久 inbox 仍必须阻止重复副作用。"""
    async def scenario():
        first = NapCatClient(testing_mode=True)
        first.bind_outbox_store(store)
        handled = []

        async def callback(message):
            handled.append(message["message_id"])

        event = _group_event(701, "900")
        first.on_group_message = callback
        await first._dispatch(event)
        for _ in range(50):
            if store.get_inbound_event_health()["processed"] == 1:
                break
            await asyncio.sleep(0.01)
        assert handled == [701]
        assert store.get_inbound_event_health()["processed"] == 1
        await first.close()

        restarted = NapCatClient(testing_mode=True)
        restarted.bind_outbox_store(store)
        restarted.on_group_message = callback
        await restarted._dispatch(dict(event))
        await asyncio.sleep(0.05)

        assert handled == [701]
        assert restarted._runtime_counters.get("events_deduped_persistent", 0) == 1
        await restarted.close()

    _run(scenario())


def test_inflight_event_is_recovered_and_retried_after_restart(store):
    """回调尚未开始的 claimed 事件可安全退回 received 并重试。"""
    from agent.inbound_event import build_platform_event_key

    event = _group_event(702, "901")
    key = build_platform_event_key("group", event)
    assert store.register_inbound_event(key, "group") == "received"
    assert store.claim_inbound_event(key) is True
    assert store.get_inbound_event_health()["claimed"] == 1

    async def scenario():
        restarted = NapCatClient(testing_mode=True)
        restarted.bind_outbox_store(store)
        handled = asyncio.Event()

        async def callback(_message):
            handled.set()

        restarted.on_group_message = callback
        await restarted._dispatch(event)
        await asyncio.wait_for(handled.wait(), timeout=0.5)
        for _ in range(50):
            if store.get_inbound_event_health()["processed"] == 1:
                break
            await asyncio.sleep(0.01)
        assert store.get_inbound_event_health()["processed"] == 1
        await restarted.close()

    _run(scenario())


def test_callback_failure_is_not_acknowledged_or_blindly_replayed(store):
    """回调可能已有部分副作用；失败必须待审，不把平台重投当安全重试。"""
    event = _group_event(703, "902")

    async def scenario():
        failed = NapCatClient(testing_mode=True)
        failed.bind_outbox_store(store)

        async def boom(_message):
            raise RuntimeError("boom")

        failed.on_group_message = boom
        await failed._dispatch(event)
        for _ in range(50):
            if store.get_inbound_event_health()["failed"] == 1:
                break
            await asyncio.sleep(0.01)
        health = store.get_inbound_event_health()
        assert health["failed"] == 1 and health["processed"] == 0
        await failed.close()

        retry = NapCatClient(testing_mode=True)
        retry.bind_outbox_store(store)
        done = asyncio.Event()

        async def ok(_message):
            done.set()

        retry.on_group_message = ok
        await retry._dispatch(dict(event))
        await asyncio.sleep(0.05)
        assert not done.is_set()
        assert store.get_inbound_event_health()["failed"] == 1
        assert retry._runtime_counters.get("events_deduped_persistent", 0) == 1
        await retry.close()

    _run(scenario())


def test_executing_event_becomes_uncertain_after_restart(store):
    """回调开始后的崩溃边界不可证明未执行，必须冻结而不是重放。"""
    from agent.inbound_event import build_platform_event_key

    event = _group_event(704, "903")
    key = build_platform_event_key("group", event)
    assert store.register_inbound_event(key, "group") == "received"
    assert store.claim_inbound_event(key) is True
    assert store.mark_inbound_event_executing(key) is True

    client = NapCatClient(testing_mode=True)
    client.bind_outbox_store(store)
    health = store.get_inbound_event_health()

    assert health["executing"] == 0
    assert health["uncertain"] == 1


def test_global_limit_rejection_does_not_create_idle_worker(monkeypatch):
    async def scenario():
        client = NapCatClient(testing_mode=True)
        monkeypatch.setattr("onebot.ws_client.EVENT_TOTAL_INFLIGHT_MAX", 1)
        client._event_inflight = 1

        await client._dispatch(_group_event(1, "300"))

        assert not client._event_workers
        assert not client._event_queues
        await client.close()

    _run(scenario())


def test_active_scope_count_is_bounded(monkeypatch):
    async def scenario():
        client = NapCatClient(testing_mode=True)
        monkeypatch.setattr("onebot.ws_client.EVENT_ACTIVE_SCOPE_MAX", 2)
        release = asyncio.Event()

        async def callback(_message):
            await release.wait()

        client.on_group_message = callback
        await client._dispatch(_group_event(1, "100"))
        await client._dispatch(_group_event(2, "200"))
        await client._dispatch(_group_event(3, "300"))
        await asyncio.sleep(0)

        assert len(client._event_workers) == 2
        assert "group:300" not in client._event_queues
        release.set()
        await client.close()

    _run(scenario())


def test_dedup_cache_has_hard_bound(monkeypatch):
    async def scenario():
        client = NapCatClient(testing_mode=True)
        monkeypatch.setattr("onebot.ws_client.EVENT_DEDUP_MAX", 3)
        done = asyncio.Event()
        count = 0

        async def callback(_message):
            nonlocal count
            count += 1
            if count == 5:
                done.set()

        client.on_group_message = callback
        for message_id in range(1, 6):
            await client._dispatch(_group_event(message_id))
        await asyncio.wait_for(done.wait(), timeout=0.2)

        assert len(client._seen_event_ids) <= 3
        await client.close()

    _run(scenario())


def test_close_awaits_background_task_cancellation_before_http_close():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        cancelled = set()

        async def background(name):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cancelled.add(name)
                raise

        class Http:
            async def aclose(self):
                assert cancelled == {"poll", "heartbeat"}

        client._poll_task = asyncio.create_task(background("poll"))
        client._heartbeat_task = asyncio.create_task(background("heartbeat"))
        client._http = Http()
        await asyncio.sleep(0)

        await client.close()

        assert cancelled == {"poll", "heartbeat"}

    _run(scenario())


def test_different_scopes_can_run_concurrently():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        both_started = asyncio.Event()
        release = asyncio.Event()
        active = 0

        async def callback(_message):
            nonlocal active
            active += 1
            if active == 2:
                both_started.set()
            await release.wait()

        client.on_group_message = callback
        await client._dispatch(_group_event(1, "100"))
        await client._dispatch(_group_event(2, "200"))
        await asyncio.wait_for(both_started.wait(), timeout=0.2)
        release.set()
        await client.close()

    _run(scenario())


def test_close_cancels_inflight_event_workers():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def callback(_message):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        client.on_group_message = callback
        await client._dispatch(_group_event(1))
        await asyncio.wait_for(started.wait(), timeout=0.2)
        await client.close()

        assert cancelled.is_set()
        assert not client._event_tasks
        assert not client._event_queues

    _run(scenario())


def test_one_thousand_scopes_are_accepted_without_cross_scope_loss():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        handled = 0
        done = asyncio.Event()

        async def callback(_message):
            nonlocal handled
            handled += 1
            if handled == 1000:
                done.set()

        client.on_group_message = callback
        for index in range(1000):
            await client._dispatch(_group_event(index + 1, str(index + 1)))

        await asyncio.wait_for(done.wait(), timeout=5)
        assert handled == 1000
        assert client._runtime_counters.get("events_dropped_global", 0) == 0
        assert client._runtime_counters.get("events_dropped_scopes", 0) == 0
        await client.close()

    _run(scenario())
