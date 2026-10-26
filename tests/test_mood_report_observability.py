"""T4.8 观测点：心情自报的分母必须可测量（2026-10-23）。

背景（取证见 `记录/T4.8-心情自报分母可见性.md`）：
`_mood_report_turn` 的唯一来源是 `_take_feedback_candidate(scope, user, 60)`，
它要求「她刚说完、同一个人在 **60 秒**内接着回」。实测当前两天日志（17,023 行、
100+ 次回复）里 `🌡 心情自报协议已注入` = **0 次** ⇒ 整条心情自报链路从未触发过，
T4.5 说的「分母结构性恒 0」至今仍成立。

本轮补两个**纯观测**点（不改闸门、不改语义，审计「不要动」第 11 条）：
1. 候选过期时落一条分桶日志 —— 回答「窗口放大到多少会有命中」；
2. 回灌日志带 `injected` 标记 —— 区分「注入后照做」与「没注入她自发报」。

测试只钉「观测点存在且信息够用」，不钉任何行为改变。
"""
from __future__ import annotations

import logging
import time

import pytest


def _handler_stub(replies: dict):
    """最小替身：只带 _last_bot_reply 与函数本身需要的属性。"""
    from agent.handler import MessageHandler

    obj = object.__new__(MessageHandler)
    obj._last_bot_reply = replies
    return obj


def test_expired_candidate_leaves_a_bucketed_trace(caplog):
    """过期必须留痕，且带分桶——这是「窗口该多大」的唯一数据来源。"""
    h = _handler_stub({"g1": {"reply": "早啊", "time": time.time() - 300,
                               "target_user": "u1"}})
    with caplog.at_level(logging.INFO):
        got = h._take_feedback_candidate("g1", "u1", 60)

    assert got is None, "过期候选不该被消费"
    assert "g1" not in h._last_bot_reply, "过期候选必须被清掉（否则它会一直卡着）"
    msgs = [r.getMessage() for r in caplog.records]
    hit = [m for m in msgs if "反馈候选过期" in m]
    assert hit, "过期没有留痕——「窗口够不够」又变成不可测量"
    assert "2-10min" in hit[0], f"5 分钟前应当落在 2-10min 桶里：{hit[0]}"
    assert "窗=60s" in hit[0], f"日志要写明当前窗口值：{hit[0]}"


@pytest.mark.parametrize("age_seconds,expected_bucket", [
    (90, "<2min"),
    (300, "2-10min"),
    (1800, "10-60min"),
    (7200, ">1h"),
])
def test_bucket_boundaries(age_seconds, expected_bucket, caplog):
    """四个桶边界各自可达——否则分桶是摆设（T2.1「名字不是证据」同族）。"""
    h = _handler_stub({"g1": {"reply": "x", "time": time.time() - age_seconds,
                               "target_user": "u1"}})
    with caplog.at_level(logging.INFO):
        h._take_feedback_candidate("g1", "u1", 60)
    hit = [r.getMessage() for r in caplog.records if "反馈候选过期" in r.getMessage()]
    assert hit and expected_bucket in hit[0], (
        f"{age_seconds}s 应落在 {expected_bucket}：{hit}")


def test_fresh_candidate_is_consumed_silently(caplog):
    """正向对照：窗口内的候选照旧消费，且**不许**打「过期」日志。"""
    h = _handler_stub({"g1": {"reply": "早啊", "time": time.time() - 5,
                               "target_user": "u1"}})
    with caplog.at_level(logging.INFO):
        got = h._take_feedback_candidate("g1", "u1", 60)
    assert got == ("早啊", got[1]) and got[1] < 60_000
    assert not [r for r in caplog.records if "反馈候选过期" in r.getMessage()], (
        "窗口内的候选被记成了过期——分桶统计会全错")


def test_other_user_does_not_consume(caplog):
    """旁观者不抢候选（既有契约，顺带钉住，防止本轮改动碰坏它）。"""
    h = _handler_stub({"g1": {"reply": "早啊", "time": time.time() - 5,
                               "target_user": "u1"}})
    with caplog.at_level(logging.INFO):
        got = h._take_feedback_candidate("g1", "u2", 60)
    assert got is None
    assert "g1" in h._last_bot_reply, "旁观者消息不该把候选吃掉"


def test_mood_report_log_carries_injected_flag(caplog):
    """回灌日志必须带 injected——否则「注入后照做」与「自发报」不可区分。"""
    from agent.handler import MessageHandler
    from agent.mood import MOOD_REPORT_EVENTS

    label = next(iter(MOOD_REPORT_EVENTS))

    class _Engine:
        def __init__(self):
            self.updated = []

        def update(self, event):
            self.updated.append(event)

    obj = object.__new__(MessageHandler)
    obj.mood = _Engine()

    with caplog.at_level(logging.INFO):
        ok = obj._apply_mood_report(label, injected=False, source="测试来源")

    assert ok is True
    hit = [r.getMessage() for r in caplog.records if "心情自报回灌" in r.getMessage()]
    assert hit, "回灌了却没有日志"
    assert "injected=False" in hit[0], (
        f"没标注注入状态——「她自发报心情」这条路又看不见了：{hit[0]}")
    assert "测试来源" in hit[0], "来源丢了，定位不到是哪条路径"
