# -*- coding: utf-8 -*-
"""工具执行器内同步调 `_call_llm_light` 必须能重入——回归测试（2026-10-15 修复）。

## 缺陷

`_call_llm` 早就处理了「同一 Task 重入自己持有的锁」这件事（handler.py 里
`_llm_owner_task` 判据，注释写着「asyncio.Lock 不可重入，旧实现会让同一 Task
永久等待自己持有的锁」）。但 `_call_llm_light` **没有同款判据**。

而工具执行器恰恰跑在 `_call_llm` 的持锁区间内：工具循环是 `_call_llm_with_skills`
里的一环，`send_message` 工具在 `_execute_tool` 里同步调用 `_call_llm_light`
来重写正文。于是：

    同一 Task → 再 acquire 非重入 Lock → 等到 15 秒超时 → 返回 ""
    → send_actions 的 `actual = await _natural_rewrite(...) or requested` 静默回退成原文

表现是「工具说消息发出去了，对方收到的却是原话」——而且只在日志里留下一行
`轻量LLM调用跳过 | reason=lock_timeout`。

## 本文件守什么

1. 锁拥有者 Task 调 `_call_llm_light` → 直接重入，不等待、不超时；
2. 重入不能把外层的锁放掉（释放权仍在 `_call_llm` 的 finally），
   也不能把外层的 `_llm_busy` 抹掉。
"""

import asyncio

import pytest

import agent.handler as handler_module
from agent.handler import MessageHandler


class _Metrics:
    def __init__(self):
        self.names = []

    def incr(self, name, *a, **k):
        self.names.append(name)


def _bare_handler(monkeypatch):
    """只装 `_call_llm_light` 真正用到的那几件东西，绕开 MessageHandler.__init__。"""
    h = MessageHandler.__new__(MessageHandler)
    h._llm_lock = asyncio.Lock()
    h._llm_owner_task = None
    h._llm_busy = False
    h._last_llm_diagnostic = {}
    h.llm_config = {"provider": "deepseek"}
    h.metrics = _Metrics()
    monkeypatch.setattr(
        handler_module, "_light_thinking_extra", lambda cfg, extra: None
    )
    return h


def test_light_call_reenters_when_the_same_task_holds_the_lock(monkeypatch):
    """锁拥有者 Task 再调轻量 LLM —— 必须直接重入，不能等 15 秒超时。"""
    h = _bare_handler(monkeypatch)
    seen = []

    async def fake_deepseek(system_prompt, user_message, **kwargs):
        seen.append((system_prompt, user_message))
        return "重写后的正文"

    h._call_deepseek = fake_deepseek

    async def scenario():
        await h._llm_lock.acquire()          # 模拟主回复回合持锁
        h._llm_owner_task = asyncio.current_task()
        h._llm_busy = True
        try:
            # ⚠ 必须**直接 await**：asyncio.wait_for(coro) 会把 coro 包进一个
            # 新的 Task，current_task() 就不再是持锁的那个 Task 了——那测的
            # 是「另一个 Task 能不能拿到锁」，与本函数要守的语义相反。
            # （生产里 _execute_tool 也是直接 await 的。）
            return await h._call_llm_light("sys", "user")
        finally:
            h._llm_owner_task = None
            h._llm_lock.release()

    async def main():
        # 超时护栏加在**外层**：只要还在等那把锁，6 秒内必被抓住
        return await asyncio.wait_for(scenario(), timeout=6)

    try:
        out = asyncio.run(main())
    except asyncio.TimeoutError:
        pytest.fail(
            "轻量 LLM 调用在持锁 Task 内超时——缺 _call_llm 那套 _llm_owner_task 重入判据"
        )

    assert out == "重写后的正文"
    assert seen == [("sys", "user")]
    assert "light_lock_timeout" not in h.metrics.names


def test_reentrant_call_does_not_release_or_clear_the_outer_lock(monkeypatch):
    """重入结束后，锁仍属于外层主回合——不能被内层放开、也不能抹掉 _llm_busy。"""
    h = _bare_handler(monkeypatch)

    async def fake_deepseek(system_prompt, user_message, **kwargs):
        return "正文"

    h._call_deepseek = fake_deepseek

    async def scenario():
        await h._llm_lock.acquire()
        h._llm_owner_task = asyncio.current_task()
        h._llm_busy = True
        try:
            await h._call_llm_light("sys", "user")
            still_locked = h._llm_lock.locked()
            busy = h._llm_busy
            owner = h._llm_owner_task
        finally:
            h._llm_owner_task = None
            h._llm_lock.release()
        return still_locked, busy, owner

    still_locked, busy, owner = asyncio.run(scenario())
    assert still_locked, "轻量调用把外层的锁放掉了——主回合的串行契约被破坏"
    assert busy, "轻量调用抹掉了外层 _llm_busy"
    assert owner is not None, "轻量调用清掉了外层 _llm_owner_task"
