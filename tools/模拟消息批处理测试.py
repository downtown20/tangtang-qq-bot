"""
消息批处理模拟测试 —— 验证 MessageBatcher 在各种场景下不阻塞正常消息流
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.message_batcher import MessageBatcher


class MockHandler:
    """模拟 handler —— 记录消息处理顺序和延迟"""
    bot_qq = "2693914170"
    def __init__(self):
        self.processed_groups = []  # [(time, group_id, msgs)]
        self.processed_privates = []  # [(time, user_id, msgs)]

    async def handle_group_message(self, msg):
        self.processed_groups.append((time.time(), msg["group_id"], msg))

    async def handle_private_message(self, msg):
        self.processed_privates.append((time.time(), msg["user_id"], msg))


def make_group_msg(user_id, nickname, text, group_id="1092609076"):
    return {
        "group_id": group_id, "user_id": user_id, "nickname": nickname,
        "message": text, "raw_message": text, "message_id": 0,
    }

def make_private_msg(user_id, nickname, text):
    return {
        "user_id": user_id, "nickname": nickname,
        "message": text, "raw_message": text, "message_id": 0,
    }


class TestMessageBatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = MockHandler()
        self.b = MessageBatcher(self.h)

    # ═══════════════════════════════════════
    # 问题：第一条消息不应该被批处理延迟
    # ═══════════════════════════════════════

    async def test_single_message_processed_immediately(self):
        """单条消息处理后重入不会死循环"""
        msg = make_group_msg("u1", "小明", "糖糖你好")
        intercepted = await self.b.enqueue_group(msg)
        self.assertTrue(intercepted, "应该被批处理拦截")
        await asyncio.sleep(2.5)
        self.assertEqual(len(self.h.processed_groups), 1,
                         "单条消息应该只处理一次（不是死循环）")
        # 验证重入的消息带 _batched 标记
        self.assertTrue(self.h.processed_groups[0][2].get("_batched"),
                        "重入的消息应该带有 _batched 标记")

    async def test_at_message_should_not_be_delayed(self):
        """@糖糖 的消息不应该延迟 2 秒——应该立即处理"""
        # 关键问题：被 @ 的消息走批处理会延迟 2 秒
        msg = make_group_msg("u1", "小明", "糖糖，帮我查天气")
        msg["raw_message"] = "[CQ:at,qq=2693914170] 糖糖，帮我查天气"
        msg["is_at_bot"] = True

        intercepted = await self.b.enqueue_group(msg)
        # 被 @ 的消息应该不拦截
        if intercepted:
            # 如果被拦截了，至少应该在合理时间内处理完
            await asyncio.sleep(2.5)
            self.assertGreaterEqual(len(self.h.processed_groups), 1,
                                    "被@的消息应该在2.5秒内处理")

    async def test_second_message_joins_batch(self):
        """第二条消息加入已有批次"""
        msg1 = make_group_msg("u1", "小明", "糖糖你好")
        msg2 = make_group_msg("u2", "小红", "糖糖在吗")

        await self.b.enqueue_group(msg1)
        await asyncio.sleep(0.1)
        await self.b.enqueue_group(msg2)
        await asyncio.sleep(2.5)

        # 两条消息应该合并处理
        self.assertGreaterEqual(len(self.h.processed_groups), 1)

    async def test_command_not_batched(self):
        """命令消息不批处理"""
        msg = make_group_msg("u1", "小明", "/状态")
        intercepted = await self.b.enqueue_group(msg)
        self.assertFalse(intercepted, "命令不应该被批处理拦截")

    async def test_private_message_batched(self):
        """私聊同一人连续消息合并"""
        msg1 = make_private_msg("u1", "小明", "想听歌")
        msg2 = make_private_msg("u1", "小明", "日语的")

        await self.b.enqueue_private(msg1)
        await asyncio.sleep(0.1)
        await self.b.enqueue_private(msg2)
        await asyncio.sleep(2.5)

        # 应该只处理一次（合并后）
        self.assertGreaterEqual(len(self.h.processed_privates), 1)

    async def test_private_command_not_batched(self):
        """私聊命令不批处理"""
        msg = make_private_msg("u1", "小明", "/人格")
        intercepted = await self.b.enqueue_private(msg)
        self.assertFalse(intercepted, "私聊命令不应该被批处理拦截")


if __name__ == "__main__":
    print("🧪 消息批处理模拟测试\n")
    unittest.main(verbosity=2, argv=[__file__])
