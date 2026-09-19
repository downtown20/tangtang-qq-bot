"""ADR-003：durable window events 是真值，Tracker 只做可重建缓存。"""

from datetime import datetime, timedelta

from agent.action_contract import (
    ActionEnvelope,
    ConversationRef,
    build_action_receipt_template,
    derive_action_id,
)
from agent.conversation_tracker import ConversationTracker


def _project(store, *, source_suffix, channel="group", target="g1",
             user_id="u1", group_id="g1", text="收到啦"):
    scope = group_id if channel == "group" else f"_private_{user_id}"
    payload = {
        "text": text, "emotion": "温柔", "speed": 1.0, "pause": "自然",
    }
    action_id = derive_action_id(
        source_id=f"{scope}:{source_suffix}", scope_id=scope,
        kind="voice", channel=channel, target=target, payload=payload,
        schema_version=2, identity_version=1,
    )
    envelope = ActionEnvelope(
        action_id=action_id, kind="voice", channel=channel, target=target,
        payload=payload, source_id=f"{scope}:{source_suffix}", scope_id=scope,
        schema_version=2, identity_version=1,
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply",
            conversation_user_id=user_id,
            group_id=group_id if channel == "group" else "",
        ),
    )
    template = build_action_receipt_template(envelope, {
        "delivery_kind": "voice", "voice_generated": True,
        "fallback_used": False, "text": text,
    })
    outbox_id = store.enqueue_send_outbox(
        channel, target, "voice", receipt_template=template,
    )
    assert store.claim_send_outbox(outbox_id)
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(int(source_suffix),),
    ) == "confirmed"
    return action_id


def _tracker(store, tmp_path, monkeypatch):
    tracker = ConversationTracker(None)
    monkeypatch.setattr(tracker, "STATE_FILE", str(tmp_path / "conversation.json"))
    tracker.init_private_windows()
    tracker.bind_durable_store(store)
    return tracker


def test_store_window_reader_is_ordered_paged_and_has_frozen_snapshot_cursor(store):
    _project(store, source_suffix="1", user_id="u1", text="一")
    _project(store, source_suffix="2", user_id="u1", text="二")
    _project(
        store, source_suffix="3", channel="private", target="u2",
        user_id="u2", group_id="", text="私聊",
    )

    first = store.list_conversation_window_events_after(0, limit=2)
    second = store.list_conversation_window_events_after(first[-1]["id"], limit=2)
    snapshot = store.get_conversation_window_rebuild_snapshot(
        "2000-01-01 00:00:00", private_per_user=1,
    )

    assert [row["id"] for row in first + second] == [1, 2, 3]
    assert snapshot["cursor"] == 3
    assert [row["channel"] for row in snapshot["events"]] == [
        "group", "group", "private",
    ]


def test_window_summary_keeps_sentence_alignment_when_embedding_is_missing():
    """某条消息向量缺失时，摘要不能把后续向量错配到被丢弃的句子。"""
    import numpy as np
    import types

    class Embed:
        ready = True

        @staticmethod
        def encode(text):
            if text == "drop":
                return None
            return np.array([1.0, 0.0]) if text == "first" else np.array([0.0, 1.0])

    relation = types.SimpleNamespace(last_conversation={})
    handler = types.SimpleNamespace(
        embed_engine=Embed(),
        self_state=types.SimpleNamespace(relationships={"u1": relation}),
    )
    tracker = ConversationTracker(handler)
    tracker.force_engage("u1", "g1")
    window = tracker.get_window_state("u1", "g1")
    window["their_msgs"] = ["first", "drop", "second"]
    tracker._extract_window_summary("u1", "g1")

    assert relation.last_conversation["summary"] == ["first", "second"]


def test_group_durable_event_read_through_is_idempotent_and_scope_safe(
        store, tmp_path, monkeypatch):
    tracker = _tracker(store, tmp_path, monkeypatch)
    _project(store, source_suffix="11", user_id="u1", group_id="g1")

    assert tracker.is_engaged("u1", "g1") is True
    assert tracker.is_engaged("u1", "g2") is False
    assert tracker.is_engaged("u2", "g1") is False
    assert tracker.get_window_state("u1", "g1")["count"] == 1

    tracker._sync_durable_events()
    tracker._sync_durable_events()
    assert tracker.get_window_state("u1", "g1")["count"] == 1

    _project(store, source_suffix="12", user_id="u1", group_id="g1")
    assert tracker.has_active_window("g1") is True
    assert tracker.get_window_state("u1", "g1")["count"] == 2


def test_private_durable_event_restores_context_without_opening_group_window(
        store, tmp_path, monkeypatch):
    _project(
        store, source_suffix="21", channel="private", target="u1",
        user_id="u1", group_id="", text="私聊已送达",
    )
    tracker = _tracker(store, tmp_path, monkeypatch)

    assert "私聊已送达" in tracker.get_private_context("u1")
    assert tracker.is_private_expired("u1") is False
    assert tracker.has_active_window("g1") is False

    tracker2 = _tracker(store, tmp_path, monkeypatch)
    context = tracker2.get_private_context("u1")
    assert context.count("私聊已送达") == 1


def test_expired_group_durable_event_does_not_reopen_window(
        store, tmp_path, monkeypatch):
    action_id = _project(store, source_suffix="31", user_id="u1", group_id="g1")
    old = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with store._connect() as conn:
        conn.execute(
            "UPDATE conversation_window_events SET occurred_at=? "
            "WHERE domain_action_id=?", (old, action_id),
        )
    tracker = _tracker(store, tmp_path, monkeypatch)
    assert tracker.is_engaged("u1", "g1") is False


def test_invalid_or_future_event_isolated_without_blocking_later_events(
        store, tmp_path, monkeypatch):
    first = _project(store, source_suffix="41", user_id="u1", group_id="g1")
    _project(store, source_suffix="42", user_id="u1", group_id="g1")
    with store._connect() as conn:
        conn.execute(
            "UPDATE conversation_window_events SET scope_id='wrong' "
            "WHERE domain_action_id=?", (first,),
        )
    tracker = _tracker(store, tmp_path, monkeypatch)
    assert tracker._durable_invalid_events == 1
    assert tracker.get_window_state("u1", "g1")["count"] == 1


def test_malformed_reader_id_does_not_block_following_event(
        store, tmp_path, monkeypatch):
    tracker = _tracker(store, tmp_path, monkeypatch)
    valid = {
        "id": 2, "event_key": "e2", "domain_action_id": "a2",
        "scope_id": "g1", "channel": "group", "actor_kind": "bot",
        "conversation_user_id": "u1", "group_id": "g1", "chat_log_id": 2,
        "reply_text": "ok", "occurred_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    bad = {**valid, "id": "bad", "domain_action_id": "bad"}
    events = [bad, valid]
    monkeypatch.setattr(
        store, "list_conversation_window_events_after",
        lambda _cursor, limit=200: events,
    )
    tracker._sync_durable_events()
    assert tracker._durable_invalid_events == 1
    assert tracker._durable_cursor == 2

    future = _project(store, source_suffix="43", user_id="u1", group_id="g1")
    with store._connect() as conn:
        conn.execute(
            "UPDATE conversation_window_events SET occurred_at='2099-01-01 00:00:00' "
            "WHERE domain_action_id=?", (future,),
        )
    tracker._sync_durable_events()
    assert tracker._durable_invalid_events == 2
    assert tracker.get_window_state("u1", "g1")["count"] == 1


def test_private_rebuild_batches_state_persistence(store, tmp_path, monkeypatch):
    for suffix in ("51", "52", "53"):
        _project(
            store, source_suffix=suffix, channel="private", target="u1",
            user_id="u1", group_id="", text=suffix,
        )
    tracker = ConversationTracker(None)
    monkeypatch.setattr(tracker, "STATE_FILE", str(tmp_path / "conversation.json"))
    tracker.init_private_windows()
    saves = []
    monkeypatch.setattr(tracker, "_save_state", lambda: saves.append(True))
    tracker.bind_durable_store(store)
    assert len(saves) == 1


def test_store_sync_failure_keeps_existing_direct_window(store, tmp_path, monkeypatch):
    tracker = _tracker(store, tmp_path, monkeypatch)
    tracker.force_engage("u1", "g1")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "list_conversation_window_events_after", unavailable)
    assert tracker.is_engaged("u1", "g1") is True


def test_repeated_force_engage_renews_active_window_without_erasing_context(
        store, tmp_path, monkeypatch):
    tracker = _tracker(store, tmp_path, monkeypatch)
    tracker.force_engage("u1", "g1")
    tracker.on_reply_sent("u1", "g1", "reply1")
    tracker.record_user_msg("u1", "g1", "followup")
    before = dict(tracker.get_window_state("u1", "g1"))

    tracker.force_engage("u1", "g1")
    after = tracker.get_window_state("u1", "g1")

    assert after["count"] == before["count"]
    assert after["my_replies"] == before["my_replies"]
    assert after["their_msgs"] == before["their_msgs"]
    assert after["expires_at"] >= before["expires_at"]


def test_durable_health_exposes_cursor_and_invalid_event_count(
        store, tmp_path, monkeypatch):
    tracker = _tracker(store, tmp_path, monkeypatch)
    health = tracker.get_durable_health()
    assert health["bound"] is True
    assert health["cursor"] == 0
    assert health["invalid_events"] == 0


def test_async_window_bonus_offloads_expired_summary_embedding():
    """窗口过期摘要的 BGE 编码不能阻塞消息事件循环。"""
    import asyncio
    import time
    import types

    class SlowEmbed:
        ready = True

        def encode(self, _text):
            time.sleep(0.02)
            return [1.0, 0.0]

    relation = types.SimpleNamespace(last_conversation={})
    handler = types.SimpleNamespace(
        embed_engine=SlowEmbed(),
        self_state=types.SimpleNamespace(relationships={"u1": relation}),
    )
    tracker = ConversationTracker(handler)
    tracker.force_engage("u1", "g1")
    window = tracker.get_window_state("u1", "g1")
    window["expires_at"] = time.time() - 400
    window["their_msgs"] = [f"their-{i}" for i in range(10)]
    window["my_replies"] = [f"reply-{i}" for i in range(10)]

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await tracker.get_window_bonus_async("u1", "g1")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert ticks >= 10
    assert result == (False, 80)
    assert tracker.get_window_state("u1", "g1") == {}


def test_force_engage_cleanup_does_not_block_event_loop():
    """新消息触发清理多个过期窗口时，摘要任务应留在后台。"""
    import asyncio
    import time
    import types

    class SlowEmbed:
        ready = True

        def encode(self, _text):
            time.sleep(0.01)
            return [1.0, 0.0]

    handler = types.SimpleNamespace(
        embed_engine=SlowEmbed(),
        self_state=types.SimpleNamespace(relationships={}),
    )
    tracker = ConversationTracker(handler)
    for i in range(4):
        tracker.force_engage(f"old-{i}", "g1")
        window = tracker.get_window_state(f"old-{i}", "g1")
        window["expires_at"] = time.time() - 400
        window["their_msgs"] = [f"their-{j}" for j in range(10)]
        window["my_replies"] = [f"reply-{j}" for j in range(10)]

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            tracker.force_engage("new", "g1")
            await asyncio.sleep(0.3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks >= 20
    assert tracker.get_window_state("new", "g1")


def test_flush_pending_summaries_waits_for_graceful_shutdown():
    """优雅退出时，后台摘要队列必须收口后再保存关系状态。"""
    import asyncio
    import time
    import types

    class SlowEmbed:
        ready = True

        def encode(self, _text):
            time.sleep(0.005)
            return [1.0, 0.0]

    relation = types.SimpleNamespace(last_conversation={})
    handler = types.SimpleNamespace(
        embed_engine=SlowEmbed(),
        self_state=types.SimpleNamespace(relationships={"u1": relation}),
    )
    tracker = ConversationTracker(handler)
    tracker.force_engage("u1", "g1")
    window = tracker.get_window_state("u1", "g1")
    window["expires_at"] = time.time() - 400
    window["their_msgs"] = [f"their-{i}" for i in range(3)]
    window["my_replies"] = [f"reply-{i}" for i in range(3)]

    async def scenario():
        tracker.force_engage("new", "g1")
        await tracker.flush_pending_summaries()

    asyncio.run(scenario())
    assert not tracker._pending_window_summaries
    assert relation.last_conversation["summary"]


def test_async_window_bonus_offloads_durable_reader():
    """窗口异步门槛的 durable 增量读取不能冻结事件循环。"""
    import asyncio
    import time
    import types

    class SlowStore:
        def list_conversation_window_events_after(self, _cursor, limit=200):
            time.sleep(0.08)
            return []

    tracker = ConversationTracker(types.SimpleNamespace())
    tracker._durable_store = SlowStore()
    tracker.force_engage("u1", "g1")

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await tracker.get_window_bonus_async("u1", "g1")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert ticks >= 5
    assert result == (True, 50)


def test_async_window_bonus_reuses_durable_sync_within_turn():
    """同一消息回合的窗口读取只应触发一次 durable 增量查询。"""
    import asyncio
    import types

    class CountingStore:
        def __init__(self):
            self.calls = 0

        def list_conversation_window_events_after(self, _cursor, limit=200):
            self.calls += 1
            return []

    store = CountingStore()
    tracker = ConversationTracker(types.SimpleNamespace())
    tracker._durable_store = store
    tracker.force_engage("u1", "g1")
    tracker._engaged[tracker._key("g1", "u1")]["their_msgs"] = ["hello"]

    async def scenario():
        assert await tracker.get_window_bonus_async("u1", "g1") == (True, 50)
        assert tracker.is_engaged("u1", "g1") is True
        assert tracker.get_window_context("u1", "g1")
        assert tracker.get_window_state("u1", "g1")

    asyncio.run(scenario())
    assert store.calls == 1
