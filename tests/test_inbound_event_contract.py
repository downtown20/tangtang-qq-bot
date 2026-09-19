"""入站事件持久化契约：原文、typed segments 与跨重启幂等。"""

import asyncio
import json


class _Memory:
    def __init__(self, store):
        self.store = store

    def log_chat(self, *args, **kwargs):
        return self.store.insert_chat(*args, **kwargs)


def test_claim_and_mark_executing_is_single_attempt_transition(store):
    key = "v2:group:g1:message:8801"
    assert store.register_inbound_event(key, "group") == "received"

    assert store.claim_and_mark_inbound_event_executing(key) is True
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, attempts FROM inbound_events WHERE event_key=?",
            (key,),
        ).fetchone()
    assert tuple(row) == ("executing", 1)

    # 原子路径不可被重复领取；重放仍由持久 inbox 拦截。
    assert store.claim_and_mark_inbound_event_executing(key) is False


def test_same_event_replay_is_idempotent_and_preserves_evidence(store):
    from agent.inbound_event import persist_inbound_message

    memory = _Memory(store)
    msg = {
        "type": "group",
        "group_id": "g1",
        "user_id": "u1",
        "message_id": 321,
        "time": 1787875200,
        "raw_message": "看看[CQ:image,file=a.jpg]",
        "message": "看看[图片]",
    }

    first = persist_inbound_message(memory, "group", msg, "看看[图片]")
    replay = persist_inbound_message(memory, "group", dict(msg), "看看[图片]")

    assert first.chat_id is not None and first.duplicate is False
    assert replay.chat_id is None and replay.duplicate is True
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT event_key,raw_message,segments FROM chat_log"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0].endswith(":321")
    assert rows[0][1] == msg["raw_message"]
    assert [part["type"] for part in json.loads(rows[0][2])] == ["text", "cq"]
    history = store.query_chat_history(
        chat_type="group", chat_id="g1", limit=1,
    )
    assert history[0]["message_id"] == 321


def test_same_platform_message_id_on_different_event_time_is_not_swallowed(store):
    """OneBot 只给 int32 消息 ID；持久键不能把跨时段复用静默吞掉。"""
    from agent.inbound_event import persist_inbound_message

    memory = _Memory(store)
    base = {
        "type": "private", "user_id": "u1", "message_id": 99,
        "time": 1787875200, "raw_message": "第一条", "message": "第一条",
    }
    later = dict(
        base, time=1787961600, raw_message="第二天的新消息", message="第二天的新消息",
    )

    first = persist_inbound_message(memory, "private", base, base["message"])
    second = persist_inbound_message(memory, "private", later, later["message"])

    assert first.duplicate is False and second.duplicate is False
    with store._connect() as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT event_key FROM chat_log ORDER BY id"
        )]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_missing_message_id_is_not_forced_into_false_dedup(store):
    from agent.inbound_event import persist_inbound_message

    memory = _Memory(store)
    msg = {
        "type": "private", "user_id": "u1", "message_id": 0,
        "time": 1787875200, "raw_message": "x", "message": "x",
    }

    one = persist_inbound_message(memory, "private", msg, "x")
    two = persist_inbound_message(memory, "private", msg, "x")

    assert one.duplicate is False and two.duplicate is False
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 2


def test_persist_inbound_message_crosses_inbound_event_boundary(store, monkeypatch):
    """持久化入口必须先形成统一 InboundEvent，再写 Store。"""
    import agent.inbound_event as module

    seen = []
    original = module.normalize_inbound_event

    def spy(message_type, msg):
        event = original(message_type, msg)
        seen.append(event)
        return event

    monkeypatch.setattr(module, "normalize_inbound_event", spy)
    memory = _Memory(store)
    module.persist_inbound_message(memory, "private", {
        "type": "private", "user_id": "u1", "message_id": 7,
        "time": 1787875200, "raw_message": "hi", "message": "hi",
    }, "hi")
    assert len(seen) == 1
    assert seen[0].scope_id == "private:u1"


def test_replayed_event_exits_before_handler_side_effects(store):
    """inbox 降级窗口中的旧重投，也不能先污染反馈/关系/buffer。"""
    from agent.handler import MessageHandler
    from agent.inbound_event import persist_inbound_message

    memory = _Memory(store)

    class ExplodingState:
        def tick(self):
            raise AssertionError("replay reached handler side effects")

    group_msg = {
        "type": "group", "group_id": "g1", "user_id": "u1",
        "nickname": "n1", "message_id": 501, "time": 1787875200,
        "raw_message": "hello", "message": "hello", "role": "member",
    }
    persist_inbound_message(memory, "group", group_msg, "hello")
    group_handler = object.__new__(MessageHandler)
    group_handler.bot_qq = "bot"
    group_handler._group_blacklist = set()
    group_handler.memory = memory
    group_handler.self_state = ExplodingState()
    asyncio.run(MessageHandler.handle_group_message.__wrapped__(
        group_handler, dict(group_msg),
    ))

    private_msg = {
        "type": "private", "user_id": "u2", "nickname": "n2",
        "message_id": 502, "time": 1787875201,
        "raw_message": "hi", "message": "hi",
    }
    persist_inbound_message(memory, "private", private_msg, "hi")
    private_handler = object.__new__(MessageHandler)
    private_handler.bot_qq = "bot"
    private_handler.memory = memory
    private_handler.self_state = ExplodingState()
    asyncio.run(MessageHandler.handle_private_message.__wrapped__(
        private_handler, dict(private_msg),
    ))
