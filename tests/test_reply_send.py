"""
私聊不拆句（2026-08-15）——**系统切分**的边界

一条回复拆成两条气泡，主人会觉得第二条是与对话无关的主动消息。
修复：只有群聊才分句发送，私聊整条发送。

⚠ 2026-09-22 补充：上面禁掉的是「**系统按长度/随机**切分」，不是「分条」本身。
主人 2026-09-22 01:50–01:55 连问三次「为什么不多发几条」「你一次只能发一条消息
吗」之后，切分权交给了 LLM（[分段] 标记 → protocols.parse_split_markers）。
下面 test_private_not_split 用**不传 presplit_parts** 的方式调用，因此它继续
成立——**它是「不给 LLM 拆分权时行为零变化」的机器证据**，不要删。
带 presplit 的用例在同文件下方。
"""

import asyncio
import sys
from pathlib import Path

from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline
from onebot.ws_client import SendResult

LONG_PRIVATE = "喵～睡饱了没呀？这个点才醒，是熬到天亮了还是补了个长觉？\n\n肚子饿不饿，先去弄口吃的吧，糖糖在这儿等你呢。"

# >80 字，群聊路径必然触发分句（maybe_split_reply 对 40-80 字是 50% 随机，>80 必拆）
LONG_GROUP = (
    "喵～睡饱了没呀？这个点才醒，是熬到天亮了还是补了个长觉？哈哈哈哈哈哈。"
    "\n\n肚子饿不饿，先去弄口吃的吧，糖糖在这儿等你呢，快去快去喵～"
    "等你回来我们还能再聊一百块的。"
)


class FakeNapcat:
    def __init__(self):
        self.private_calls = []
        self.group_calls = []

    async def send_private_message(self, target, content, group_id=""):
        self.private_calls.append(content)
        return True

    async def send_group_message(self, target, content):
        self.group_calls.append(content)
        return True


def _pipeline(napcat):
    p = object.__new__(ReplyPipeline)
    p.napcat = napcat
    return p


def test_private_not_split():
    nc = FakeNapcat()
    ok = asyncio.run(_pipeline(nc).send("private", "123", LONG_PRIVATE))
    assert ok
    assert len(nc.private_calls) == 1
    assert nc.private_calls[0] == LONG_PRIVATE


def test_group_still_splits():
    nc = FakeNapcat()
    ok = asyncio.run(_pipeline(nc).send("group", "456", LONG_GROUP))
    assert ok
    assert len(nc.group_calls) >= 2
    # 分句在断点处 strip 空白——断言内容分段而非整串拼接相等
    assert "睡饱了" in nc.group_calls[0]
    assert "肚子饿不饿" in nc.group_calls[1]


# ═══════════════════════════════════════════════════════════════════
# presplit_parts —— LLM 自己切好的段（2026-09-22）
#
# 上面 test_private_not_split 证明「不给 presplit 时私聊仍是一条」；
# 下面证明「给了 presplit 才分条」。两条合起来才是完整的契约。
# ═══════════════════════════════════════════════════════════════════

class ScriptedNapcat:
    """按脚本返回发送结果的替身——用来演「第一段成功、第二段未确认」。"""

    def __init__(self, results):
        self._results = list(results)
        self.private_calls = []
        self.group_calls = []

    def _next(self):
        return self._results.pop(0) if self._results else True

    async def send_private_message(self, target, content, group_id=""):
        self.private_calls.append(content)
        return self._next()

    async def send_group_message(self, target, content):
        self.group_calls.append(content)
        return self._next()


def test_presplit_private_sends_each_part(monkeypatch):
    """LLM 切了几段就发几条——私聊不再恒一条。"""
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = ScriptedNapcat([])

    ok = asyncio.run(_pipeline(nc).send(
        "private", "123", "第一段第二段",
        presplit_parts=["第一段", "第二段"],
    ))

    assert ok
    assert nc.private_calls == ["第一段", "第二段"]


def test_presplit_bypasses_system_splitter(monkeypatch):
    """LLM 决定在哪切之后，系统不许再按长度/随机二次切分（教训 #9）。

    阳性对照性质：把 maybe_split_reply 换成会抛异常的桩，presplit 路径必须照样跑完。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = ScriptedNapcat([])
    pipeline = _pipeline(nc)

    def _must_not_be_called(_reply):
        raise AssertionError("给了 presplit 还在调系统切分——这是第二个判定点")

    pipeline.maybe_split_reply = _must_not_be_called

    ok = asyncio.run(pipeline.send(
        "private", "123", "原始整条",
        presplit_parts=["第一段", "第二段"],
    ))

    assert ok
    assert nc.private_calls == ["第一段", "第二段"]


@pytest.mark.parametrize("parts", [None, [], ["只有一段"]])
def test_degenerate_presplit_falls_back_to_old_private_path(monkeypatch, parts):
    """空列表/单元素等于没给——fail-safe：绝不发空，也绝不改旧行为。

    这同时是「不给拆分权 ⇒ 行为零变化」的设备：旧私聊路径连
    maybe_split_reply 都不该调。
    """
    nc = ScriptedNapcat([])
    pipeline = _pipeline(nc)

    def _must_not_be_called(_reply):
        raise AssertionError("私聊旧路径不该调用系统切分")

    pipeline.maybe_split_reply = _must_not_be_called

    ok = asyncio.run(pipeline.send(
        "private", "123", LONG_PRIVATE, presplit_parts=parts,
    ))

    assert ok
    assert nc.private_calls == [LONG_PRIVATE]


def test_presplit_stops_after_first_unconfirmed(monkeypatch):
    """第一段未确认 → 后面的段不再发（本仓既有语义，被分段放大）。

    **这是有意与业界相反的地方**：Koishi issue #267 把「前段失败后面就不发」
    当作要改掉的反面教材。本仓的理由是「分段回复有顺序依赖：前段未确认或明确
    失败后继续发后段，会把一条回复撕成无法核验的半截对话」。这条语义由本文件
    与 test_send_commit_boundary.py 共同钉住——要改它是一次显式决定。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = ScriptedNapcat([SendResult(False, False, error="BOT_ERROR", retryable=False)])
    pipeline = _pipeline(nc)

    ok = asyncio.run(pipeline.send(
        "private", "123", "整条", presplit_parts=["一", "二", "三"],
    ))

    assert ok is False
    assert nc.private_calls == ["一"]  # 二、三不再发


def test_presplit_partial_delivery_is_marked_partial(monkeypatch):
    """首段送达、次段未确认 → 聚合结果必须标 PARTIAL_DELIVERY，
    上层据此不落库（既有语义，未改写循环即继承）。"""
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = ScriptedNapcat([
        SendResult(True, True, message_id=1),
        SendResult(False, False, error="NETWORK_UNCERTAIN", uncertain=True),
    ])
    pipeline = _pipeline(nc)

    ok = asyncio.run(pipeline.send(
        "private", "123", "整条", presplit_parts=["第一段", "第二段"],
    ))

    assert ok is False
    assert nc.private_calls == ["第一段", "第二段"]
    assert "PARTIAL_DELIVERY" in (pipeline.last_send_result.error or "")
