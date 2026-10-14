# -*- coding: utf-8 -*-
"""T9 回归测试：self_state 落盘失败必须留下可观测事实。

2026-10-14 台账 T9。修复前 self_state.save() 的 7 个调用点失败处理三种不一致
（5 处 except Exception: pass、1 处无 try、1 处正确），导致糖糖的冷热场学习、
per-user 主动私聊冷却、24h 冷场结算与优雅退出持久化在落盘失败时全部静默丢失。

本测试是这次修复自己的阳性对照（CLAUDE.md 教训 #30）：
  - 阳性：save() 抛异常 -> 不抛给上层（fail-open）+ logger.error 被调用 + 指标 +1
  - 阴性：save() 成功 -> 不记 error、不记指标
"""
import logging
import types

import pytest

from agent.handler_autonomy import AutonomyMixin


class _FakeMetrics:
    def __init__(self):
        self.counts = {}

    def incr(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n


def _make_owner(save_side_effect=None):
    """最小替身：只带 _persist_self_state 用到的 self.self_state / self.metrics。"""
    owner = types.SimpleNamespace()
    owner.metrics = _FakeMetrics()
    saved = {"n": 0}

    def _save():
        saved["n"] += 1
        if save_side_effect is not None:
            raise save_side_effect

    owner.self_state = types.SimpleNamespace(save=_save)
    owner._saved = saved
    return owner


class TestPersistSelfState:
    """_persist_self_state：fail-open，但失败必须可观测。"""

    def test_success_no_error_no_metric(self):
        """阴性对照：落盘成功 -> 不记 error、不记指标，返回 True。"""
        owner = _make_owner()
        ok = AutonomyMixin._persist_self_state(owner, "测试目的")
        assert ok is True
        assert owner._saved["n"] == 1
        assert owner.metrics.counts == {}

    def test_failure_is_fail_open(self):
        """阳性对照①：save() 抛异常 -> 不抛给上层（fail-open），返回 False。"""
        owner = _make_owner(OSError("disk full"))
        ok = AutonomyMixin._persist_self_state(owner, "测试目的")
        assert ok is False
        assert owner._saved["n"] == 1

    def test_failure_logs_error(self, caplog):
        """阳性对照②：失败必须记 error 级日志，且带上目的与异常类型。"""
        owner = _make_owner(OSError("disk full"))
        with caplog.at_level(logging.ERROR, logger="糖糖.Autonomy"):
            AutonomyMixin._persist_self_state(owner, "主动私聊意愿分调整")
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "落盘失败没有产生 error 级日志——失败会再次静默"
        msg = errors[0].getMessage()
        assert "主动私聊意愿分调整" in msg, "日志没带上落盘目的，无法定位是哪条路径丢的"
        assert "OSError" in msg
        assert "disk full" in msg

    def test_failure_increments_metric(self):
        """阳性对照③：失败必须计数，让失败率可被观测。"""
        owner = _make_owner(OSError("disk full"))
        AutonomyMixin._persist_self_state(owner, "测试目的")
        assert owner.metrics.counts.get("self_state_save_failed") == 1

    def test_metric_failure_does_not_break_persist(self):
        """指标自身失败也不能把 persist 搞崩（防御）。"""
        owner = _make_owner(OSError("disk full"))

        def _boom(*a, **k):
            raise RuntimeError("metrics down")

        owner.metrics.incr = _boom
        ok = AutonomyMixin._persist_self_state(owner, "测试目的")
        assert ok is False

    @pytest.mark.parametrize(
        "exc",
        [OSError("disk full"), ValueError("bad json"), TypeError("x"),
         RuntimeError("boom"), MemoryError("oom")],
        ids=["OSError", "ValueError", "TypeError", "RuntimeError", "MemoryError"],
    )
    def test_various_exception_types_are_caught(self, exc):
        """宽泛异常类型都要被兜住：持久化失败不该有漏网形态。"""
        owner = _make_owner(exc)
        ok = AutonomyMixin._persist_self_state(owner, "测试目的")
        assert ok is False
        assert owner.metrics.counts.get("self_state_save_failed") == 1


class TestNoBareSaveInHotPaths:
    """结构闸门：主动私聊热点不许再出现裸 self_state.save()。

    防回归——2026-10-14 修的正是这些处；谁再顺手加回裸调用，这条红。
    """

    def test_autonomy_has_no_bare_save_outside_helper(self):
        import io

        src = io.open(
            "agent/handler_autonomy.py", encoding="utf-8", errors="ignore"
        ).read()
        lines = src.splitlines()
        helper_start = None
        for i, line in enumerate(lines):
            if "def _persist_self_state" in line:
                helper_start = i
                break
        assert helper_start is not None, "_persist_self_state 不见了？"
        helper_end = len(lines)
        for i in range(helper_start + 1, len(lines)):
            if lines[i].startswith("    def "):
                helper_end = i
                break
        outside = [
            (i + 1, line.strip())
            for i, line in enumerate(lines)
            if "self.self_state.save()" in line
            and not (helper_start <= i < helper_end)
            and not line.startswith("#")
        ]
        assert not outside, (
            "主动私聊热点里又出现了裸 self_state.save()（T9 回归）："
            + str(outside)
        )
