"""
对话连续性模拟测试 —— 验证 ConversationTracker 在各种场景下的决策

不需要 QQ 环境，直接测试状态机逻辑。
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.conversation_tracker import ConversationTracker


class MockHandler:
    """模拟 handler——tracker 只需要访问属性"""
    bot_qq = "2693914170"


class TestConversationTracker(unittest.TestCase):
    def setUp(self):
        self.t = ConversationTracker(MockHandler())
        self.group = "1092609076"
        self.user_a = "2524950083"
        self.user_b = "2377255095"

    # ═══════════════════════════════════════
    # 场景 1: @糖糖 → 进入对话中 → 后续消息自然回复
    # ═══════════════════════════════════════

    def test_at_bot_enters_engaged(self):
        """被 @ 后进入对话中"""
        self.t.force_engage(self.user_a, self.group)
        self.assertTrue(self.t.is_engaged(self.user_a, self.group),
                        "被@后应该进入对话中")

    def test_engaged_continues_without_at(self):
        """对话中再发消息不需要 @"""
        self.t.force_engage(self.user_a, self.group)
        self.t.on_reply_sent(self.user_a, self.group)
        self.assertTrue(self.t.is_engaged(self.user_a, self.group),
                        "糖糖回复后应该保持对话中")

    # ═══════════════════════════════════════
    # 场景 2: B 插话 → 糖糖退让
    # ═══════════════════════════════════════

    def test_other_user_not_in_window(self):
        """A 在对话中，B 发消息不能享受窗口"""
        self.t.force_engage(self.user_a, self.group)
        self.assertFalse(self.t.is_engaged(self.user_b, self.group),
                         "B 不在对话中，不能享受窗口")

    def test_user_at_other_not_in_window(self):
        """B @了别人，不是跟糖糖说话，不进入对话中"""
        # B @了别人但从未 @糖糖 → 不在对话中
        self.assertFalse(self.t.is_engaged(self.user_b, self.group),
                         "B 从未被 @糖糖，不在对话中")

    # ═══════════════════════════════════════
    # 场景 3: 连续回复 → 5 条后自动退让
    # ═══════════════════════════════════════

    def test_yield_after_max_consecutive(self):
        """连续回复 5 条后退让"""
        self.t.force_engage(self.user_a, self.group)
        for _ in range(5):
            self.t.on_reply_sent(self.user_a, self.group)
        self.assertTrue(self.t.should_yield(self.user_a, self.group),
                        "连续5条后应该退让")

    def test_no_yield_before_max(self):
        """不到 5 条不退让"""
        self.t.force_engage(self.user_a, self.group)
        for _ in range(4):
            self.t.on_reply_sent(self.user_a, self.group)
        self.assertFalse(self.t.should_yield(self.user_a, self.group),
                         "不到4条不应该退让")

    def test_yield_clears_engagement(self):
        """退让后不再处于对话中"""
        self.t.force_engage(self.user_a, self.group)
        for _ in range(5):
            self.t.on_reply_sent(self.user_a, self.group)
        self.t.should_yield(self.user_a, self.group)
        self.assertFalse(self.t.is_engaged(self.user_a, self.group),
                         "退让后应该清除对话状态")

    # ═══════════════════════════════════════
    # 场景 4: 对话窗口过期
    # ═══════════════════════════════════════

    def test_window_expires(self):
        """窗口过期后自动退出"""
        self.t.force_engage(self.user_a, self.group)
        self.t._engaged[self.user_a]["expires_at"] = time.time() - 15
        self.assertFalse(self.t.is_engaged(self.user_a, self.group),
                         "窗口过期超过容差后应该退出")

    def test_window_grace_period(self):
        """5秒容差内窗口仍然有效"""
        self.t.force_engage(self.user_a, self.group)
        self.t._engaged[self.user_a]["expires_at"] = time.time() - 3
        self.assertTrue(self.t.is_engaged(self.user_a, self.group),
                        "过期3秒内（容差期内）窗口应该仍然有效")

    def test_reply_resets_expiry(self):
        """回复后续期"""
        self.t.force_engage(self.user_a, self.group)
        original = self.t._engaged[self.user_a]["expires_at"]
        time.sleep(0.001)  # 确保时间戳不同
        self.t.on_reply_sent(self.user_a, self.group)
        self.assertGreaterEqual(self.t._engaged[self.user_a]["expires_at"], original,
                                "回复后过期时间应该续期或不变")

    # ═══════════════════════════════════════
    # 场景 5: 多人交叉
    # ═══════════════════════════════════════

    def test_multiple_users_independent(self):
        """多个用户各自独立"""
        self.t.force_engage(self.user_a, self.group)
        self.t.force_engage(self.user_b, self.group)
        self.assertTrue(self.t.is_engaged(self.user_a, self.group))
        self.assertTrue(self.t.is_engaged(self.user_b, self.group))

    def test_max_3_engaged(self):
        """最多 3 个对话窗口"""
        for i in range(5):
            uid = f"user_{i}"
            self.t.force_engage(uid, self.group)
        self.assertLessEqual(len(self.t._engaged), 3,
                             "对话窗口不应超过 3 个")

    # ═══════════════════════════════════════
    # 场景 6: 新消息到达 → 窗口内正常回复
    # ═══════════════════════════════════════

    def test_engaged_reply_sequence(self):
        """完整对话序列"""
        # A @糖糖
        self.t.force_engage(self.user_a, self.group)
        self.assertTrue(self.t.is_engaged(self.user_a, self.group))

        # 糖糖回复 A
        self.t.on_reply_sent(self.user_a, self.group)
        self.assertTrue(self.t.is_engaged(self.user_a, self.group))

        # A 再发消息（不 @）→ 仍在对话中
        self.assertTrue(self.t.is_engaged(self.user_a, self.group))

        # 糖糖再回
        self.t.on_reply_sent(self.user_a, self.group)
        self.assertEqual(self.t._engaged[self.user_a]["count"], 2)


if __name__ == "__main__":
    print("🧪 对话连续性模拟测试\n")
    unittest.main(verbosity=2, argv=[__file__])
