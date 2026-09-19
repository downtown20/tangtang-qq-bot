"""第二/三阶段回归：自忆证据链与窗口自主沉默。"""

import asyncio
import json
import types
from unittest.mock import AsyncMock

from agent import protocols
from agent.conversation_tracker import ConversationTracker
from agent.handler import MessageHandler, THINKING_OFF
from agent.memory import MemorySystem
from agent.metrics import MemoryMetrics
from agent.store import Store


def test_legacy_unscoped_self_memories_are_quarantined(store):
    store.insert_memory("bot", "promise", "旧版全局承诺", origin="self")

    # 再次初始化模拟升级/重启，迁移必须幂等且保留审计记录。
    Store(store.db_path)

    assert store.query_memories("bot") == []
    audit = store.query_memories("bot", include_retracted=True)
    assert len(audit) == 1
    assert audit[0]["status"] == "retracted"


def test_self_memory_binds_target_original_message_and_provenance(store):
    memory = MemorySystem(store=store)
    assert memory.remember_self("bot", "无来源承诺", target_qq="甲") == 0
    source_a = store.insert_chat(
        "甲", "明天把结果告诉甲", group_id="g1", is_bot=True,
        timestamp="2026-08-26 10:00:00",
    )
    source_b = store.insert_chat(
        "乙", "已经把语音发给乙", group_id="g2", is_bot=True,
        timestamp="2026-08-26 10:01:00",
    )
    memory.remember_self(
        "bot", "明天把结果告诉甲", key="promise", target_qq="甲",
        source_group_id="g1", evidence_ids=str(source_a),
    )
    memory.remember_self(
        "bot", "已经把语音发给乙", key="action_completed", target_qq="乙",
        source_group_id="g2", evidence_ids=str(source_b),
    )

    rows = memory.recall("bot", target_qq="甲", grounded_only=True)
    assert [m.value for m in rows] == ["明天把结果告诉甲"]
    assert rows[0].evidence_ids == str(source_a)
    formatted = memory._format_self_memories(rows)
    assert "承诺过" in formatted
    assert "置信度" in formatted
    assert f"来源 chat_log#{source_a} 群g1" in formatted


def test_action_completed_requires_confirmed_action_receipt(store):
    memory = MemorySystem(store=store)
    source = store.insert_chat(
        "甲", "已经把事情办好了", group_id="g1", is_bot=True,
    )

    assert memory.remember_self(
        "bot", "已经把事情办好了", key="action_completed", target_qq="甲",
        group_id="g1", evidence_ids=str(source),
    ) == 0
    assert store.query_memories("bot", target_qq="甲") == []


def test_legacy_action_completed_is_quarantined_on_restart(store):
    source = store.insert_chat(
        "甲", "旧版声称动作完成", group_id="g1", is_bot=True,
    )
    memory_id = store.insert_memory(
        "bot", "action_completed", "旧版声称动作完成", origin="self",
        target_qq="甲", source_group_id="g1", evidence_ids=str(source),
    )
    with store._connect() as conn:
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE version='20260831_self_action_receipt_v1'"
        )

    Store(store.db_path)
    row = next(item for item in store.query_memories(
        "bot", target_qq="甲", include_retracted=True,
    ) if item["id"] == memory_id)
    assert row["status"] == "active"
    assert row["trust_level"] == "legacy_unverified"
    assert MemorySystem(store=store).recall(
        "bot", target_qq="甲", grounded_only=True,
    ) == []


def test_self_memory_llm_types_are_bound_to_selected_source(store):
    memory = MemorySystem(store=store)
    source_ids = [
        store.insert_chat("甲", "我觉得A方案更自然", group_id="g1", is_bot=True),
        store.insert_chat("乙", "我明天继续核查", group_id="g2", is_bot=True),
        store.insert_chat("甲", "语音已经发给你了", group_id="g1", is_bot=True),
    ]
    metrics = MemoryMetrics(store)
    payload = [
        {"source_index": 1, "type": "said", "value": "觉得A方案更自然"},
        {"source_index": 2, "type": "promise", "value": "明天继续核查"},
        {"source_index": 3, "type": "action_completed", "value": "已经发送语音"},
        {"source_index": 99, "type": "promise", "value": "越界来源不能入库"},
    ]
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=metrics, embed_engine=None,
        _call_llm_light=AsyncMock(return_value=json.dumps(payload, ensure_ascii=False)),
    )
    replies = [
        {"reply": "我觉得A方案更自然", "target_qq": "甲", "group_id": "g1", "source_message_id": source_ids[0]},
        {"reply": "我明天继续核查", "target_qq": "乙", "group_id": "g2", "source_message_id": source_ids[1]},
        {"reply": "语音已经发给你了", "target_qq": "甲", "group_id": "g1", "source_message_id": source_ids[2]},
    ]

    asyncio.run(MessageHandler._extract_self_memories_llm(fake, replies))

    all_rows = store.query_memories("bot")
    assert {(r["key"], r["target_qq"], r["evidence_ids"]) for r in all_rows} == {
        ("said", "甲", str(source_ids[0])),
        ("promise", "乙", str(source_ids[1])),
    }


def test_action_completed_can_settle_only_explicit_same_scope_promise(store):
    memory = MemorySystem(store=store)
    promise_source = store.insert_chat(
        "甲", "我答应给你唱告白气球", group_id="g1", is_bot=True,
    )
    promise_id = memory.remember_self(
        "bot", "答应给甲唱《告白气球》", key="promise", target_qq="甲",
        group_id="g1", evidence_ids=str(promise_source),
    )
    completion_source = store.insert_chat(
        "甲", "已经唱完《告白气球》", group_id="g1", is_bot=True,
    )
    # 只有 confirmed action receipt 才能作为“动作已完成”的第二锚点；
    # 这里用最小的存储层替身表示发送链已确认该动作，真实投影由
    # test_confirmed_action_projection 覆盖。
    store.validate_self_memory_action_anchor = lambda *_args: True
    payload = [{
        "source_index": 1,
        "type": "action_completed",
        "value": "已经唱完《告白气球》",
        "fulfills_memory_ids": [promise_id],
    }]
    released = []
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(
            drives=types.SimpleNamespace(
                release=lambda name, amount: released.append((name, amount)),
            ),
        ),
        _call_llm_light=AsyncMock(
            return_value=json.dumps(payload, ensure_ascii=False),
        ),
    )

    asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "已经唱完《告白气球》", "target_qq": "甲", "group_id": "g1",
        "source_message_id": completion_source, "confirmed_action_id": "act-1",
    }]))

    active = store.query_memories("bot", target_qq="甲")
    assert all(row["id"] != promise_id for row in active)
    audit = {
        row["id"]: row for row in store.query_memories(
            "bot", target_qq="甲", include_retracted=True,
        )
    }
    assert audit[promise_id]["status"] == "fulfilled"
    completion = next(row for row in active if row["key"] == "action_completed")
    assert audit[promise_id]["superseded_by"] == completion["id"]

    # 重启后仍保持终态：不能把已履行承诺重新注入上下文。
    Store(store.db_path)
    restarted_active = store.query_memories("bot", target_qq="甲")
    assert all(row["id"] != promise_id for row in restarted_active)
    restarted_audit = {
        row["id"]: row for row in store.query_memories(
            "bot", target_qq="甲", include_retracted=True,
        )
    }
    assert restarted_audit[promise_id]["status"] == "fulfilled"
    assert released == [("commitment", 0.2)]

    llm_call = fake._call_llm_light.await_args
    assert llm_call.kwargs["extra_body"] == THINKING_OFF
    prompt = llm_call.args[1]
    assert "示例（编号仅示意）" in prompt
    assert '"fulfills_memory_ids":[42]' in prompt


def test_failed_promise_settlement_does_not_release_commitment(store):
    memory = MemorySystem(store=store)
    promise_source = store.insert_chat(
        "甲", "我答应给你查资料", group_id="g1", is_bot=True,
    )
    promise_id = memory.remember_self(
        "bot", "答应给甲查资料", key="promise", target_qq="甲",
        group_id="g1", evidence_ids=str(promise_source),
    )
    completion_source = store.insert_chat(
        "甲", "资料已经查完了", group_id="g1", is_bot=True,
    )
    store.validate_self_memory_action_anchor = lambda *_args: True
    settle_attempts = []

    def reject_settlement(candidate_id, completion_id):
        settle_attempts.append((candidate_id, completion_id))
        return False

    memory_proxy = types.SimpleNamespace(
        store=store,
        remember_self=memory.remember_self,
        fulfill_self_promise=reject_settlement,
    )
    released = []
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory_proxy, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(
            drives=types.SimpleNamespace(
                release=lambda name, amount: released.append((name, amount)),
            ),
        ),
        _call_llm_light=AsyncMock(return_value=json.dumps([{
            "source_index": 1,
            "type": "action_completed",
            "value": "资料已经查完了",
            "fulfills_memory_ids": [promise_id],
        }], ensure_ascii=False)),
    )

    asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "资料已经查完了", "target_qq": "甲", "group_id": "g1",
        "source_message_id": completion_source, "confirmed_action_id": "act-2",
    }]))

    assert len(settle_attempts) == 1
    assert released == []
    assert any(
        row["id"] == promise_id
        for row in store.query_memories("bot", target_qq="甲")
    )


def test_self_promise_settlement_rejects_cross_user_completion(store):
    memory = MemorySystem(store=store)
    promise_source = store.insert_chat("甲", "答应给甲唱歌", group_id="g1", is_bot=True)
    promise_id = memory.remember_self(
        "bot", "答应给甲唱歌", key="promise", target_qq="甲", group_id="g1",
        evidence_ids=str(promise_source),
    )
    completion_source = store.insert_chat("乙", "已经给乙唱歌", group_id="g1", is_bot=True)
    completion_id = memory.remember_self(
        "bot", "已经给乙唱歌", key="action_completed", target_qq="乙", group_id="g1",
        evidence_ids=str(completion_source),
    )

    assert memory.fulfill_self_promise(promise_id, completion_id) is False
    assert any(row["id"] == promise_id for row in store.query_memories("bot", target_qq="甲"))


def test_evidence_and_window_protocols_are_explicit():
    assert "原始聊天记录 > 带来源的自忆 > 摘要和画像" in protocols.MEMORY_EVIDENCE_CONTRACT
    assert "skip_response" in protocols.WINDOW_REPLY_PROTOCOL
    assert "不要为了维持窗口而凑一句回复" in protocols.WINDOW_REPLY_PROTOCOL
    assert "不要发送解释沉默的过程文字" in protocols.WINDOW_REPLY_PROTOCOL


def test_consecutive_llm_silence_moves_window_to_fade_and_reply_resets():
    tracker = ConversationTracker(None)
    tracker.force_engage("u1", "g1")
    assert tracker.get_window_bonus("u1", "g1") == (True, 50)

    assert tracker.on_llm_silence("u1", "g1") is False
    tracker.on_llm_reply_decision("u1", "g1")
    assert tracker.on_llm_silence("u1", "g1") is False
    assert tracker.on_llm_silence("u1", "g1") is True
    assert tracker.get_window_bonus("u1", "g1") == (True, 65)


def test_window_metrics_include_unflushed_decisions(store):
    metrics = MemoryMetrics(store)
    metrics.incr("window_decisions_total")
    metrics.incr("window_decisions_skip")

    assert metrics.get_current("window_decisions_total") == 1
    assert metrics.get_current("window_decisions_skip") == 1
