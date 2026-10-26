# -*- coding: utf-8 -*-
"""背景块「取回路径」闸门（2026-10-25 第九轮）

## 这一轮修的是一条**假的取回路径**

`_BG_RECOVERY_TOOLS` 是「这块材料被淘汰后，她能用哪个工具自己取回来」的登记表。
`fit_backgrounds` 淘汰块时据此生成一行「缺口」告知：

    （这一轮有几块材料没放进来：自忆。要的话你可以自己取回来：自忆用 search_memories。）

看起来天经地义——「自忆」是记忆，「search_memories」是记忆搜索，不正好吗？

**不对，作用域错了。** `search_memories` 的执行体是
`self.memory.recall(qq_id, …)`，`qq_id` 是**当前对话对象**（主人/群友）；
而自忆存在 `bot_qq`（她自己的 QQ）名下。**用这个工具永远查不到自己的话。**

于是形成一个闭环缺陷：

    自忆块 94% 的回合被整块淘汰（15 天 390 条淘汰记录里 365 条是自忆）
    → 缺口块照实告诉她「自忆没放进来，要的话用 search_memories 取回来」
    → 她照做，查回来的是**对方的**记忆
    → 她既没有自己的过去，也不知道自己其实没有

## 修法为什么是两处，而且必须一起

删掉假登记 → 自忆变成「取不回来」的一类 → 按 `BACKGROUND_PRIORITY` 自己的定序
原则（「丢了能不能用工具取回」，关系档案 P9→P4 是先例）它必须优先保留
→ P10 改 P6。**两处是同一条推理的两半**，缺一半都不成立：

- 只删登记：她连「自忆没进来」都不会被告知，比修之前更差；
- 只提优先级：假路径还在，偶尔淘汰时仍会把她引向一个查不到东西的工具。

## 闸门怎么才算真红

`test_gap_notice_does_not_promise_self_memory_recovery` 是**端到端**的：
它逼出一次真实的淘汰，然后检查缺口文本里有没有「自忆用」这三个字。
只断言常量表里没有「自忆」是不够的——那测的是实现，不是行为。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols

# 按日志实测的块价构造（不按直觉）：关系档案 240t / 窗口 216t / 记忆 215t /
# 自忆 240t / 协议 30t。中文按 0.72t/字，所以 333 字 ≈ 240t。
_REL, _WIN, _MEM, _SELF, _PROTO, _REFL = (
    ("关系档案", "档" * 333),
    ("窗口", "窗" * 300),
    ("记忆", "记" * 300),
    ("自忆", "自" * 333),
    ("协议", "协" * 42),
    ("反思", "反" * 200),
)


class TestNoPhantomRecoveryPath:
    def test_self_memory_is_not_recoverable_by_peer_recall(self):
        """自忆不能用 search_memories 取回——作用域不同（bot_qq vs 对话对象）。

        这条钉的是一个**语义**事实，不是实现细节：`_BG_RECOVERY_TOOLS` 的每一行
        都在承诺「你照这个做就能拿回来」。承诺必须是能兑现的。
        """
        assert "自忆" not in protocols._BG_RECOVERY_TOOLS, (
            "自忆属于 bot_qq，而 search_memories 检索的是当前对话对象的 qq_id——"
            "登记它等于给她一条永远走不通的路")

    def test_self_memory_outranks_recoverable_memory(self):
        """取不回来的必须排在能取回的前面（优先级数字越小越优先）。"""
        assert (protocols.BACKGROUND_PRIORITY["自忆"]
                < protocols.BACKGROUND_PRIORITY["记忆"]), (
            "自忆取不回来、记忆能用 search_facts 取回来 → "
            "自忆的优先级必须高于记忆")

    def test_gap_notice_does_not_promise_self_memory_recovery(self):
        """端到端：真逼出一次自忆淘汰，缺口文本里不许出现「自忆用」。"""
        out = protocols.fit_backgrounds(
            [_PROTO, _REL, _WIN, _MEM, _REFL, _SELF],
            budget_tokens=400,        # 故意给很小的预算，逼出整块淘汰
        )
        notice = dict(out).get("缺口", "")
        assert notice, "构造失败：预算 400 竟然没有淘汰任何块"
        assert "自忆用" not in notice, (
            f"缺口仍在承诺一条走不通的取回路径：{notice}")
        # 同时确认它确实被淘汰了（否则这条测试测的是别的场景）
        assert "自忆" in notice, f"构造失败：自忆没被淘汰，缺口是 {notice!r}"


class TestBudgetFitsBothMemoryAndSelfMemory:
    def test_default_budget_keeps_memory_and_self_memory(self):
        """默认预算必须同时装下「记忆」和「自忆」。

        实测（2026-10-25，同一份真实块价）：
            预算  900 → 自忆 ✅ 记忆 ❌
            预算 1000 → 自忆 ✅ 记忆 ❌（省下的钱被裁到别处，白加）
            预算 1100 → 两块都在 ✅  ← 默认值取这个
            预算 1200 → 与 1100 相同（已饱和）
        这条钉住预算不被悄悄调回去——调回去会静默牺牲其中一块，
        而日志只会写「淘汰了一块」，看不出是哪一块被牺牲了。
        """
        out = protocols.fit_backgrounds([_PROTO, _REL, _WIN, _MEM, _REFL, _SELF])
        labels = [l for l, _ in out]
        assert "记忆" in labels, f"记忆块被淘汰：{labels}——她又会「每轮现查档案」"
        assert "自忆" in labels, f"自忆块被淘汰：{labels}——她不知道自己说过什么，也补不回来"
