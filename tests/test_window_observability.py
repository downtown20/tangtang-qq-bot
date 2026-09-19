"""任务 E1：窗口自治与可观测性（2026-08-28 协作任务包）

验收点：
  1. 私聊窗口 last_active 持久化/恢复——重启后 is_private_expired 行为确定
  2. 旧状态缺 last_active 字段 → 按安全过期策略处理（不假装活跃），
     上下文链路不死（get_private_context 仍返回）
  3. 窗口决策结构化统计：candidate/entry_direct/continuation/routed/reply/
     skip/fade 可写入可读取（系统只计数，不替 LLM 决定语义）
  4. set_reminder/group_say_later 归入 behavior 分类（不再强制额外收尾轮）
"""
import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from agent.conversation_tracker import ConversationTracker
from agent.metrics import MemoryMetrics


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(ConversationTracker, "STATE_FILE",
                        str(tmp_path / "conversation_state.json"))
    h = type("H", (), {})()
    return ConversationTracker(h)


# ═══════════════════════════════════════════════════════
# 1. 私聊窗口 last_active 持久化 + 恢复
# ═══════════════════════════════════════════════════════

def test_private_window_last_active_persisted_and_restored(tracker):
    tracker.init_private_windows()
    tracker.on_private_reply("1001", "好的呀", user_msg="在吗")

    # 模拟重启：新 tracker 从同一 STATE_FILE 恢复
    tracker2 = ConversationTracker(tracker.h)
    tracker2.init_private_windows()

    w = tracker2._private_windows["1001"]
    assert w.get("last_active", 0) > 0          # last_active 已恢复
    assert tracker2.is_private_expired("1001") is False  # 未过期
    ctx = tracker2.get_private_context("1001")
    assert "好的呀" in ctx and "在吗" in ctx     # 上下文链路不死


def test_private_window_old_state_missing_last_active_is_safely_expired(tracker):
    """旧状态缺 last_active 字段：按安全过期策略处理（不假装活跃），
    上下文仍可读取（链路不死）"""
    import pathlib
    path = pathlib.Path(tracker.STATE_FILE)
    path.write_text(json.dumps({
        "1001": {
            "summary": "之前聊过咖啡",
            "their_msgs": ["上次说的那家店"],
            "my_replies": ["下次一起去"],
        },
    }, ensure_ascii=False), encoding="utf-8")

    tracker.init_private_windows()
    assert tracker.is_private_expired("1001") is True   # 缺字段 → 安全过期
    ctx = tracker.get_private_context("1001")
    assert "之前聊过咖啡" in ctx                        # 上下文不丢


# ═══════════════════════════════════════════════════════
# 2. 窗口决策统计：写入 + 读取
# ═══════════════════════════════════════════════════════

def test_window_decision_metrics_writable_and_readable(store):
    from agent.interaction_contract import DecisionRun

    m = MemoryMetrics(store)
    reply = DecisionRun.start(
        run_id="window-reply", event_key="event-reply", scope_id="group:g1",
        correlation_id="cid-reply",
    ).finish(decision="reply")
    skip = DecisionRun.start(
        run_id="window-skip", event_key="event-skip", scope_id="group:g1",
        correlation_id="cid-skip",
    ).finish(decision="skip", tool_calls=("skip_response",))

    assert m.record_window_decision(reply) == "reply"
    assert m.record_window_decision(skip) == "skip"
    assert m.record_window_decision(skip, faded=True) == "skip"

    # 内存态 + 落盘态合计可读（无需 flush 断言）
    assert m.get_current("window_decisions_total") == 3
    assert m.get_current("window_decisions_reply") == 1
    assert m.get_current("window_decisions_skip") == 2
    assert m.get_current("window_fade_after_silence") == 1


def test_window_decision_metrics_in_handler_sources():
    """窗口决策统计必须存在于 handler 实际路径（源码契约）：
    candidate/entry_direct/continuation 在窗口判定处，routed 在插话命中处；
    reply/skip/fade 只接受已完成 DecisionRun 的统一记录入口。"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    for name in ("window_candidate", "window_entry_direct", "window_continuation",
                 "window_routed"):
        assert f'incr("{name}")' in src, name
    assert src.count("record_window_decision(") == 1
    assert 'if window_outcome == "skip":' in src
    assert 'if window_outcome == "reply":' in src


# ═══════════════════════════════════════════════════════
# 3. behavior 分类：set_reminder / group_say_later
# ═══════════════════════════════════════════════════════

def test_reminder_tools_are_behavior_classified():
    """Important 18：set_reminder/group_say_later 必须归入 behavior——
    否则默认 agent 分类强制额外 LLM 收尾轮"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    assert '"set_reminder": "behavior"' in src
    assert '"group_say_later": "behavior"' in src


# ═══════════════════════════════════════════════════════
# 4. E1 收尾：窗口新指标接入 /状态 摘要
# ═══════════════════════════════════════════════════════

def test_metrics_summary_exposes_window_breakdown(store):
    """candidate/entry_direct/continuation/routed 必须在 get_metrics_summary
    （/状态）中可见——不能只计数不展示"""
    from agent.handler import MessageHandler

    m = MemoryMetrics(store)
    for name, n in (("window_candidate", 7), ("window_entry_direct", 2),
                    ("window_continuation", 5), ("window_routed", 1),
                    ("window_decisions_total", 7), ("window_decisions_reply", 5),
                    ("window_decisions_skip", 2)):
        m.incr(name, n)
    h = object.__new__(MessageHandler)
    h.metrics = m
    h.memory = types.SimpleNamespace(store=store)
    text = h.get_metrics_summary()
    assert "窗口决策" in text                       # 既有决策行保留
    assert "候选7" in text and "直达2" in text
    assert "延续5" in text and "插话路由1" in text  # 新样本行可见


def test_window_skip_with_media_is_not_recorded_as_reply():
    """skip_response 继续执行媒体动作时，窗口仍应保持 skip 语义。"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    assert 'window_decision.decision == "skip"' in src
    assert "record_window_decision(" in src


def test_metrics_summary_exposes_light_llm_failure_reasons(store):
    """轻量 LLM 空结果的原因必须能在 /状态 中区分。"""
    from agent.handler import MessageHandler

    m = MemoryMetrics(store)
    m.incr("light_budget_exhausted", 2)
    m.incr("light_transport_error", 1)
    m.incr("light_empty_response", 3)
    h = object.__new__(MessageHandler)
    h.metrics = m
    h.memory = types.SimpleNamespace(store=store)
    text = h.get_metrics_summary()
    assert "预算耗尽2" in text
    assert "传输失败1" in text
    assert "空内容3" in text
