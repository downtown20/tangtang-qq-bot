# -*- coding: utf-8 -*-
"""@ 消息防抖必须真的「取消前一条」——回归测试（2026-10-15 修复）。

## 修复前的缺陷

`MessageBatcher.debounce_at` 把 `loop.create_future()` 存进 `_at_timers`
当「取消令牌」，而真正计时的 `asyncio.create_task(_wait_then_process())`
**没有任何字段持有**（返回值被丢弃）。于是：

    prev = self._at_timers.pop(group_id, None)   # 是个 Future
    prev.cancel()                                # 取消了个寂寞
    ...
    asyncio.create_task(_wait_then_process())    # 这个 Task 无人持有，照跑

结果：N 个人在 1.5 秒内同时 @ 糖糖 → N 次 handle_group_message → N 次 LLM 回合。
docstring 承诺的「多人同时@ → 取消前一次等待，只处理最后一条」从未成立。

修法：把 create_task 的返回值本身存进字段——取消令牌与执行体是同一个东西。
（字段原本的类型声明就是 `dict[str, asyncio.Task]`，写错的是赋值那行。）

## 本文件守什么

1. 同一群连续 @ N 次 → 只处理最后一次；
2. 不同群各自独立（一个群的 @ 不能取消另一个群的等待）；
3. 到点后 `_at_timers` 不留残渣（否则取消句柄会泄漏、群数一多就是内存增长）。
"""

import asyncio

from agent.message_batcher import AT_DEBOUNCE, MessageBatcher


class _FakeHandler:
    bot_qq = "10000"

    def __init__(self):
        self.calls = []

    async def handle_group_message(self, msg):
        self.calls.append(msg["message_id"])


def _msg(mid: int) -> dict:
    return {
        "type": "group", "group_id": "g1", "user_id": "1001",
        "nickname": "小明", "message": f"糖糖在吗 {mid}",
        "raw_message": f"糖糖在吗 {mid}", "message_id": mid,
    }


def test_simultaneous_at_messages_collapse_to_the_last_one():
    """多人/多次同时 @ 只应产生一次 handle_group_message。"""
    h = _FakeHandler()
    b = MessageBatcher(h)

    async def scenario():
        for mid in (1, 2, 3, 4):
            # 间隔远小于防抖窗口 → 全部落在同一个防抖窗内
            await b.debounce_at("g1", _msg(mid))
            await asyncio.sleep(0.05)
        # 等到防抖窗口过去（+余量）
        await asyncio.sleep(AT_DEBOUNCE + 0.5)

    asyncio.run(scenario())

    assert h.calls == [4], (
        f"防抖失效：期望只处理最后一条(4)，实际处理了 {h.calls}——"
        "「取消前一条」没有作用到真正计时的 Task 上"
    )
    assert b._at_timers == {}, f"防抖表没清干净：{b._at_timers}"


def test_old_cancel_token_would_have_really_failed():
    """阳性对照：把「旧的」取消令牌写法照抄一遍，证明上面那条断言**会红**。

    教训 #30——只测「跑起来没报错」等于没测。这条测试证明防抖门真的在拦东西，
    而不是因为「反正只跑一条」而恒真。
    """
    h = _FakeHandler()
    timers: dict = {}

    async def old_style_debounce(mid: int):
        prev = timers.pop("g1", None)
        if prev:
            prev.cancel()  # 取消的是一个从未被 await 的 Future
        loop = asyncio.get_event_loop()
        timers["g1"] = loop.create_future()  # ← 旧代码存的就是这个

        async def _wait_then_process():
            await asyncio.sleep(AT_DEBOUNCE)
            h.calls.append(mid)

        asyncio.create_task(_wait_then_process())  # ← 句柄被丢弃，取消不到

    async def scenario():
        for mid in (1, 2, 3, 4):
            await old_style_debounce(mid)
            await asyncio.sleep(0.05)
        await asyncio.sleep(AT_DEBOUNCE + 0.5)

    asyncio.run(scenario())

    assert h.calls == [1, 2, 3, 4], (
        "旧的取消令牌写法居然也合并了？那前面那条防抖断言就是假绿——"
        f"实际 {h.calls}"
    )


def test_debounce_is_per_group_and_does_not_leak_handles():
    """一个群的 @ 不能取消另一个群；到点后登记表必须清空。"""
    h = _FakeHandler()
    b = MessageBatcher(h)

    async def scenario():
        m_a = _msg(11); m_a["group_id"] = "gA"
        m_b = _msg(22); m_b["group_id"] = "gB"
        await b.debounce_at("gA", m_a)
        await asyncio.sleep(0.05)
        # gB 的 @ 不该取消 gA 的等待
        await b.debounce_at("gB", m_b)
        assert set(b._at_timers) == {"gA", "gB"}, b._at_timers
        await asyncio.sleep(AT_DEBOUNCE + 0.5)

    asyncio.run(scenario())

    assert sorted(h.calls) == [11, 22], f"两个群各自应该各处理一次，实际 {h.calls}"
    assert b._at_timers == {}, f"防抖表没清干净：{b._at_timers}"
