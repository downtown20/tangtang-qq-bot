"""自治状态落盘失败的可见性（2026-10-23 台账 T66）。

病根：`_save_state_kv` 的 docstring 明写「调用方需要区分『内存已切换、落盘失败』，
不再静默吞」，而实测 **9 个调用点全部丢弃返回值**（赋值 0 处、if 判断 0 处），
且三处 `except Exception: return False` 把异常吞成布尔——无日志、无指标。

这 9 个 key 是 `state:auto_uncertain` / `state:auto_cold` / `state:auto_pending` /
`state:seek_uncertain` / `state:care_due`，全部是**主动开口与主动私聊的冷却**。
丢了它们 = CLAUDE.md 教训 #15 的事故原形（2026-08-16「重启 7 次骚扰同一人 7 次，
对方零回复」）。

三条契约：
1. 落盘异常 → 记 error 日志（不是 debug、不是静默）；
2. 落盘返回失败 → 也记 error 日志（「Store 说不行」和「抛异常」都是失败）；
3. 「没有落盘能力」（测试替身）**不算失败**——记 error 会把合法降级渲染成故障，
   而假警报会训练人忽略警报（T29 的教训）。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent.handler_autonomy import _persist_state_async


class _Metrics:
    def __init__(self):
        self.counters: dict[str, int] = {}

    def incr(self, name: str, *args, **kwargs):
        self.counters[name] = self.counters.get(name, 0) + 1


def _owner(**kwargs):
    base = {"metrics": _Metrics()}
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_exception_is_logged_and_counted(caplog):
    """路径 A：async saver 抛异常。"""
    async def boom(key, value):
        raise OSError("disk full")

    owner = _owner(_save_state_kv_async=boom)
    with caplog.at_level(logging.ERROR):
        ok = await _persist_state_async(owner, "state:seek_uncertain", {"a": 1})

    assert ok is False, "落盘失败必须如实返回 False"
    assert any("自治状态落盘异常" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR), (
        "落盘异常没有留下 error 日志——又回到静默失败")
    assert owner.metrics.counters.get("autonomy_state_persist_failed") == 1


@pytest.mark.asyncio
async def test_store_says_no_is_also_logged(caplog):
    """路径 B：不抛异常，但 Store 返回 False——同样必须可见。

    这条容易漏：只看 except 的话，「store 说不行」会被当成正常返回。
    """
    async def refuse(key, value):
        return False

    owner = _owner(_save_state_kv_async=refuse)
    with caplog.at_level(logging.ERROR):
        ok = await _persist_state_async(owner, "state:auto_cold", {"a": 1})

    assert ok is False
    assert any("自治状态落盘失败" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR), (
        "Store 返回失败却没有日志——这条冷却丢了也没人知道")
    assert owner.metrics.counters.get("autonomy_state_persist_failed") == 1


@pytest.mark.asyncio
async def test_success_is_silent(caplog):
    """正向对照：成功时**不许**产生 error（否则日志会被正常流量淹没）。"""
    async def ok_saver(key, value):
        return True

    owner = _owner(_save_state_kv_async=ok_saver)
    with caplog.at_level(logging.ERROR):
        ok = await _persist_state_async(owner, "state:auto_cold", {"a": 1})

    assert ok is True
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "成功路径打了 error 日志——日志会被正常流量淹没")
    assert owner.metrics.counters.get("autonomy_state_persist_failed") is None


@pytest.mark.asyncio
async def test_no_saver_capability_is_not_an_error(caplog):
    """「没有落盘能力」≠「落盘失败」：测试替身走这条路，不许报故障。

    这条防的是把闸门修瞎——如果这里也记 error，那么每次跑测试都刷 error，
    真故障那天长得一模一样（T29：假警报会训练人忽略警报）。
    """
    owner = _owner()  # 既没有 _save_state_kv_async 也没有 _save_state_kv
    with caplog.at_level(logging.ERROR):
        ok = await _persist_state_async(owner, "state:auto_cold", {"a": 1})

    assert ok is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "没有落盘能力被当成了故障——测试替身会被刷成一片红")


@pytest.mark.asyncio
async def test_sync_saver_failure_is_logged(caplog):
    """路径 C：走同步 _save_state_kv（无 _run_store_io 时）。"""
    def boom(key, value):
        raise RuntimeError("sqlite locked")

    owner = _owner(_save_state_kv=boom)
    with caplog.at_level(logging.ERROR):
        ok = await _persist_state_async(owner, "state:auto_pending", {"a": 1})

    assert ok is False
    assert any("自治状态落盘异常" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)


@pytest.mark.asyncio
async def test_metrics_failure_does_not_crash_the_path():
    """指标写不进去也不许把主流程带崩（与 T9 同款兜底）。"""
    class _BrokenMetrics:
        def incr(self, name, *args, **kwargs):
            raise RuntimeError("metrics backend down")

    async def boom(key, value):
        raise OSError("disk full")

    owner = SimpleNamespace(metrics=_BrokenMetrics(), _save_state_kv_async=boom)
    ok = await _persist_state_async(owner, "state:auto_cold", {"a": 1})
    assert ok is False, "指标失败不该让落盘路径抛出"
