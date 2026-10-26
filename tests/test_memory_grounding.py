"""第二/三阶段回归：自忆证据链与窗口自主沉默。"""

import asyncio
import json
import types
from unittest.mock import AsyncMock

import pytest

from agent import protocols
from agent.conversation_tracker import ConversationTracker
from agent.drives import DriveSystem
from agent.handler import MessageHandler, THINKING_OFF
from agent.memory import MemorySystem
from agent.metrics import MemoryMetrics
from agent.store import Store


def _real_drives(**values) -> DriveSystem:
    """挂一个**真实** DriveSystem 作替身（2026-10-13 P1）。

    为什么不用 lambda 假替身：审计抓的正是「命名动作零调用」——用假替身
    `release_by_action=lambda ...` 就等于在测试里**重写一遍** mapping，
    表改了测试照样绿（教训 #30 的假绿同族）。用真实系统才能钉住
    「数值只有一个来源」。
    """
    drives = DriveSystem()
    for name, value in values.items():
        drives.drives[name].value = value
    return drives


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
    # 2026-10-25 第十轮：provenance 不再**逐条**打印 chat_log 编号与置信度。
    # 原因（实测）：那 50 字符前缀 ≈33t/条，而 promise 正文只有 9~20 字，
    # 5 条共 242t、其中 165t（68%）是元数据，150t 预算下只装得下 2 条——
    # 代码检索的却是 5 条（`candidates[:5]`），等于每次白跑 3 条。
    # 真正的 provenance 由三处保证，都比「逐条打印编号」强：
    #   ① 写入侧 validate_self_memory_source（本测试上半部分断言的绑定）；
    #   ② 块首「有原始消息支持的自忆」是全块声明，不是逐条重复；
    #   ③ 读侧 grounded_only=True 只取可信档。
    assert "有原始消息支持" in formatted   # ② 全块 provenance 声明仍在
    # ③ 日期仍在（时间感是第 9 轮的修复项）。断「MM-DD 格式」而不断言具体日期：
    #   这里 remember_self 没传时间戳，timestamp 取建档时刻（now），
    #   断言字面值会变成「测试跑在哪天就要求哪天」。
    import re as _re
    assert _re.search(r"\b\d{2}-\d{2}\b", formatted)
    assert "群g1" in formatted             # 作用域仍在（哪个群说的）
    assert "置信度" not in formatted       # 噪音字段不得回归（全库同一值 0.70）
    assert "chat_log#" not in formatted    # 噪音字段不得回归（无工具可按 id 取回）


def test_action_completed_accepts_confirmed_text_reply_and_rejects_bogus_receipt(store):
    """2026-10-12 主人拍板（方案 B）：回执锚点口径收窄成「传输层确认」。

    改造前：action_completed 必须有**冻结动作回执**才准写入，而回执只有语音路径
    产出（_build_voice_action_envelope 只服务语音；查库全库 5 条 conversation_reply
    事实的 self_memory_eligible 全是 0）→ 文字回复一律被挡在**写入之前**。
    取证：action_completed 自忆最后一次写入停在 2026-08-30，此后一条都没有——
    这正是 `fulfilled` 恒为 0 的真凶。

    改造后**保留**的防线：
      · 证据仍必须满足 validate_self_memory_source：同一对象、同一作用域、
        is_bot_reply=1 的 chat_log 行——而 bot 行只在**确认送达**后才写；
      · **提供了**动作回执就必须真实有效，无效一律拒，不许降级蒙混；
      · 结算仍需 LLM 显式点名同作用域的候选 promise。
    """
    memory = MemorySystem(store=store)
    source = store.insert_chat(
        "甲", "已经把事情办好了", group_id="g1", is_bot=True,
    )

    # ① 文字回复（无动作回执）：确认送达即可写入
    assert memory.remember_self(
        "bot", "已经把事情办好了", key="action_completed", target_qq="甲",
        group_id="g1", evidence_ids=str(source),
    ) > 0

    # ② 提供了动作回执但校验不通过：拒收（fail-closed，不许降级成文字锚点）
    store.validate_self_memory_action_anchor = lambda *_args: False
    source2 = store.insert_chat("甲", "另一个声称", group_id="g1", is_bot=True)
    assert memory.remember_self(
        "bot", "另一个声称", key="action_completed", target_qq="甲",
        group_id="g1", evidence_ids=str(source2), confirmed_action_id="act-bogus",
    ) == 0


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
    # 2026-10-12 主人拍板（方案 B）：第 3 条（"语音已经发给你了"，同作用域的确认送达
    # 文字回复）现在**可以**写入 action_completed——它不再需要冻结动作回执。
    # 本用例真正钉的东西不变：类型必须绑定到被选中的来源，且越界 source_index 拒收。
    assert {(r["key"], r["target_qq"], r["evidence_ids"]) for r in all_rows} == {
        ("said", "甲", str(source_ids[0])),
        ("promise", "乙", str(source_ids[1])),
        ("action_completed", "甲", str(source_ids[2])),
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
    # 2026-10-13 P1（审计 P0-1「reply_commitment 零调用」）：结算不再写死
    # `release("commitment", 0.2)`——数值由 drives.release_by_action 的 mapping
    # **唯一**决定。这里挂**真实 DriveSystem**，让整条链（结算 → 命名动作 → 表
    # → 数值）都被测到，而不是用替身假装数值。
    drives = _real_drives(commitment=0.9)
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(drives=drives),
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
    # 数值来自 drives.release_by_action 的 mapping（reply_commitment = 0.30），
    # 净释放 ×1.1 过冲 = 0.33；0.9 → 0.57。**不是**写死的 (commitment, 0.2)。
    assert drives.drives["commitment"].value == pytest.approx(0.9 - 0.3 * 1.1, abs=1e-6)

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

def test_text_reply_settles_promise_without_action_envelope(store):
    """2026-10-12 主人拍板（方案 B）：文字回复也能结算承诺。

    取证：`action_completed` 自忆最后一次写入是 2026-08-30——因为写入**之前**的
    闸门只认 action envelope（只有语音路径会产出），文字回复一律被挡在写入之前，
    于是 `fulfilled` 恒为 0。现在把「确认送达并落进 chat_log 的 bot 回复」认作
    同等级别的**传输层锚点**（validate_self_memory_source 复用的就是
    _validate_memory_evidence 对 origin='self' 的校验：is_bot_reply=1 + 同一对象 + 同一群）。
    """
    memory = MemorySystem(store=store)
    promise_source = store.insert_chat("甲", "我答应明天告诉你", group_id="g1", is_bot=True)
    promise_id = memory.remember_self(
        "bot", "答应明天告诉甲", key="promise", target_qq="甲",
        group_id="g1", evidence_ids=str(promise_source),
    )
    completion_source = store.insert_chat("甲", "已经告诉你了", group_id="g1", is_bot=True)

    # 2026-10-13 P1：结算走命名动作，数值来自真实 DriveSystem 的表。
    drives = _real_drives(commitment=0.9)
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(drives=drives),
        _call_llm_light=AsyncMock(return_value=json.dumps([{
            "source_index": 1,
            "type": "action_completed",
            "value": "已经告诉你了",
            "fulfills_memory_ids": [promise_id],
        }], ensure_ascii=False)),
    )

    asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "已经告诉你了", "target_qq": "甲", "group_id": "g1",
        "source_message_id": completion_source, "confirmed_action_id": "",
    }]))

    audit = {
        row["id"]: row for row in store.query_memories(
            "bot", target_qq="甲", include_retracted=True,
        )
    }
    assert audit[promise_id]["status"] == "fulfilled", "文字回复没能结算承诺"
    assert drives.drives["commitment"].value == pytest.approx(0.9 - 0.3 * 1.1, abs=1e-6)


def test_new_promise_raises_commitment_pressure(store):
    """2026-10-13 P1（审计 P0-1「补 commitment 消费点」）：新承诺入账 → 承诺压力升。

    **取证**（改造前实际状态）：`self_state.update_relationship(unfinished_add=…)`
    里的 `accumulate_commitment()` 在生产代码零调用——`unfinished_add` /
    `unfinished_remove` 两个参数都只有定义、没有调用方。而自忆 `promise` 才是今天
    真正的承诺来源（查库：175 条 active promise）。于是承诺压力只靠 tick 的固定
    alpha 累积，**与她实际攒了多少承诺毫无关系**——实测 175 条在账，
    commitment 长期 0.976 贴顶。

    这里钉住的是「承诺账本的写入端真的接上了」：走真实 DriveSystem 取数值，
    不写死增量（表改了测试跟着走，不假绿）。
    """
    memory = MemorySystem(store=store)
    source = store.insert_chat("甲", "我明天告诉你结果", group_id="g1", is_bot=True)
    drives = _real_drives(commitment=0.5)
    before = drives.drives["commitment"].value

    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(drives=drives),
        _call_llm_light=AsyncMock(return_value=json.dumps([{
            "source_index": 1, "type": "promise", "value": "明天告诉甲结果",
        }], ensure_ascii=False)),
    )
    asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "明天告诉你结果", "target_qq": "甲", "group_id": "g1",
        "source_message_id": source, "confirmed_action_id": "",
    }]))

    assert any(
        row["key"] == "promise"
        for row in store.query_memories("bot", target_qq="甲")
    ), "承诺自忆没写进去——本用例的前提没了"
    assert drives.drives["commitment"].value > before, (
        "新承诺入账却没让承诺压力升——承诺压力与她的承诺账本仍然脱钩")


def test_accumulate_commitment_missing_on_driver_is_not_fatal(store):
    """阴性对照：替身/离线 harness 没有 accumulate_commitment 时不许炸主链路。

    （自忆提取跑在后台任务里，一次 AttributeError 会让整批回滚重试——
    承诺记忆会因此永远写不进去。可选依赖必须按 getattr 取。）
    """
    memory = MemorySystem(store=store)
    source = store.insert_chat("甲", "我明天告诉你结果", group_id="g1", is_bot=True)
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        # 只有空的 drives：既没有 accumulate_commitment 也没有 release_by_action
        self_state=types.SimpleNamespace(drives=types.SimpleNamespace()),
        _call_llm_light=AsyncMock(return_value=json.dumps([{
            "source_index": 1, "type": "promise", "value": "明天告诉甲结果",
        }], ensure_ascii=False)),
    )
    assert asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "明天告诉你结果", "target_qq": "甲", "group_id": "g1",
        "source_message_id": source, "confirmed_action_id": "",
    }])) is True
    assert any(
        row["key"] == "promise"
        for row in store.query_memories("bot", target_qq="甲")
    )


def test_text_anchor_is_fail_closed(store):
    """阴性对照：锚点校验本身必须是 fail-closed（换个人/换群都不认）。"""
    memory = MemorySystem(store=store)
    mine = store.insert_chat("甲", "已经告诉你了", group_id="g1", is_bot=True)
    other_person = store.insert_chat("乙", "已经告诉你了", group_id="g1", is_bot=True)
    other_group = store.insert_chat("甲", "已经告诉你了", group_id="g2", is_bot=True)
    user_row = store.insert_chat("甲", "不是她说的", group_id="g1", is_bot=False)

    assert store.validate_self_memory_source("甲", "g1", str(mine)) is True
    assert store.validate_self_memory_source("甲", "g1", str(other_person)) is False
    assert store.validate_self_memory_source("甲", "g1", str(other_group)) is False
    assert store.validate_self_memory_source("甲", "g1", str(user_row)) is False

    # 端到端：跨对象证据的完成自忆不该写进去，更不该结算
    promise_source = store.insert_chat("甲", "我答应明天告诉你", group_id="g1", is_bot=True)
    promise_id = memory.remember_self(
        "bot", "答应明天告诉甲", key="promise", target_qq="甲",
        group_id="g1", evidence_ids=str(promise_source),
    )
    fake = types.SimpleNamespace(
        bot_qq="bot", memory=memory, metrics=MemoryMetrics(store),
        embed_engine=None,
        self_state=types.SimpleNamespace(
            drives=types.SimpleNamespace(release=lambda name, amount: None),
        ),
        _call_llm_light=AsyncMock(return_value=json.dumps([{
            "source_index": 1, "type": "action_completed",
            "value": "已经告诉你了", "fulfills_memory_ids": [promise_id],
        }], ensure_ascii=False)),
    )
    asyncio.run(MessageHandler._extract_self_memories_llm(fake, [{
        "reply": "已经告诉你了", "target_qq": "甲", "group_id": "g1",
        "source_message_id": other_person, "confirmed_action_id": "",
    }]))
    audit = {
        row["id"]: row for row in store.query_memories(
            "bot", target_qq="甲", include_retracted=True,
        )
    }
    assert audit[promise_id]["status"] == "active", "跨对象证据被当成了锚点"


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
