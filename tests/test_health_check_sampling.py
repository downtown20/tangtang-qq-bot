# -*- coding: utf-8 -*-
"""T29 回归：提取成功率检查不许拿热身样本下结论（2026-10-14）。

现场：bot 每次重启后，「启动健康检查」都会印一条
「协议成功率 7%，JSON无效率 93%」的 WARNING，而**同一时刻**
`extraction_jobs` 显示 10210 done / 0 failed —— 管道是健康的。

两个原因：
  1. `_check_extract_success` **没有**调 `_in_warmup()`，
     而同文件的 `_check_extract_running` 调了 —— 两个检查对「刚启动」判断不一致；
  2. 样本下限只有 5，而计数器是进程内累计、重启清零。

假警报会训练人忽略警报，所以这里把它钉住。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import health_check as hc


class _Metrics:
    def __init__(self, **kw):
        self._d = kw

    def get_current(self, key):
        return self._d.get(key, 0)


class _Handler:
    def __init__(self, **kw):
        self.metrics = _Metrics(**kw)


def _run(monkeypatch, warmup, **kw):
    monkeypatch.setattr(hc, "_in_warmup", lambda: warmup)
    import asyncio
    return asyncio.run(hc._check_extract_success(_Handler(**kw)))


def test_warmup_does_not_judge(monkeypatch):
    """保温期内：即使样本看起来很差，也只报「积累中」，不报 warn。"""
    r = _run(monkeypatch, warmup=True,
             extract_outcomes_total=14,
             extract_protocol_success=1,
             extract_invalid_json=13)
    assert r["status"] == "ok", r
    assert "保温期" in r["message"], r


def test_small_sample_does_not_judge(monkeypatch):
    """非保温期但样本仍不足：报「样本不足」，不报 warn。"""
    r = _run(monkeypatch, warmup=False,
             extract_outcomes_total=14,
             extract_protocol_success=1,
             extract_invalid_json=13)
    assert r["status"] == "ok", r
    assert "样本不足" in r["message"], r
    assert "14/" in r["message"], r


def test_enough_samples_still_warns(monkeypatch):
    """样本足够且协议成功率真的低 —— **必须仍然报 warn**（别把闸门修瞎）。"""
    r = _run(monkeypatch, warmup=False,
             extract_outcomes_total=100,
             extract_protocol_success=30,
             extract_invalid_json=70)
    assert r["status"] == "warn", r
    assert "协议成功率 30%" in r["message"], r


def test_enough_samples_and_healthy_is_ok(monkeypatch):
    r = _run(monkeypatch, warmup=False,
             extract_outcomes_total=100,
             extract_protocol_success=98,
             extract_with_items=40,
             extract_invalid_json=2)
    assert r["status"] == "ok", r


def test_threshold_is_at_least_30():
    """结构闸门：下限不许被改回 5 —— 那正是本任务修掉的东西。"""
    assert hc.MIN_EXTRACT_OUTCOMES >= 30, hc.MIN_EXTRACT_OUTCOMES


def test_check_honours_warmup_like_its_sibling():
    """同一文件里两个提取检查必须**同口径**看待「刚启动」。"""
    import inspect
    src = inspect.getsource(hc._check_extract_success)
    assert "_in_warmup()" in src, ("`_check_extract_success` 又不看保温期了——"
                                  "与 `_check_extract_running` 口径不一致")
