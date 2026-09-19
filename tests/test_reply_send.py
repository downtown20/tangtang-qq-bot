"""
私聊不拆句（2026-08-15）

一条回复拆成两条气泡，主人会觉得第二条是与对话无关的主动消息。
修复：只有群聊才分句发送，私聊整条发送。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline

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
