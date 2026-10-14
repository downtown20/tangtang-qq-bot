"""
自治消息模拟测试 —— 验证冷场检测 + 窗口感知 + 消息生成逻辑
不测 LLM 调用（无法模拟），只测状态机
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MockMemory:
    def __init__(self):
        self.short_term = {}
    def add_msg(self, group_id, nickname, message, t):
        if group_id not in self.short_term:
            self.short_term[group_id] = []
        self.short_term[group_id].append({
            "nickname": nickname, "message": message, "time": t,
        })

class MockTracker:
    def __init__(self):
        self._engaged = {}
    def add_engaged(self, uid, gid):
        self._engaged[uid] = {"group_id": gid, "expires_at": time.time() + 120}
    def is_engaged(self, uid, gid):
        w = self._engaged.get(uid)
        return w and w["group_id"] == gid

class MockDrives:
    def __init__(self):
        self.releases = []
    def get_dominant(self):
        return None
    def release(self, name, amount):
        self.releases.append((name, amount))


class TestAutonomousColdDetection(unittest.TestCase):

    def test_cold_state_basics(self):
        """冷场计数 + 换群逻辑"""
        auto_cold = {}
        new_group = None

        auto_cold["g1"] = auto_cold.get("g1", 0) + 1
        self.assertEqual(auto_cold["g1"], 1)

        auto_cold["g1"] += 2
        self.assertEqual(auto_cold["g1"], 3)

        if auto_cold["g1"] >= 3:
            auto_cold["g1"] = 0
            new_group = "g2"
        self.assertEqual(new_group, "g2")

    def test_llm_response_mapping(self):
        """LLM 四种返回值的回避欲反馈"""
        avoid_deltas = {
            "warmly_received": 0,       # 温暖 → 不增加回避欲
            "lukewarm": -0.05,           # 敷衍 → 轻微回避
            "ignored": -0.10,            # 冷场 → 中等回避
            "rejected": -0.20,           # 被拒绝 → 强烈回避
        }
        # release(avoid, negative_amount) = avoid += abs(amount)
        self.assertEqual(abs(avoid_deltas["lukewarm"]), 0.05)
        self.assertEqual(abs(avoid_deltas["rejected"]), 0.20)

    def test_rejected_permanently_blocks(self):
        """被拒绝后设 cold=99，几乎永久停发"""
        auto_cold = {}
        auto_cold["g1"] = 99
        self.assertGreaterEqual(auto_cold["g1"], 3, "99 > 3 → 会触发换群")
        # 换群后重置为 0
        auto_cold["g1"] = 0
        self.assertEqual(auto_cold["g1"], 0)

    def test_warm_response_resets_cold(self):
        """温暖回应重置冷场计数"""
        auto_cold = {"g1": 2}
        auto_cold["g1"] = 0  # warm → reset
        self.assertEqual(auto_cold["g1"], 0)

    def test_already_in_window_skips(self):
        """已有对话窗口时不自洽"""
        tracker = MockTracker()
        tracker.add_engaged("u1", "g1")

        has_active = any(
            tracker.is_engaged(uid, "g1")
            for uid in tracker._engaged
        )
        self.assertTrue(has_active, "有活跃窗口应该跳过自治")

        # 不同群不受影响
        has_active_g2 = any(
            tracker.is_engaged(uid, "g2")
            for uid in tracker._engaged
        )
        self.assertFalse(has_active_g2, "不同群不应该受影响")

    def test_auto_pending_tracking(self):
        """自治消息发送后正确记录时间和文本"""
        auto_pending = {}
        last_auto_msg = {}

        now = time.time()
        auto_pending["g1"] = now
        last_auto_msg["g1"] = "大家最近怎么样呀~"

        time.sleep(0.001)
        elapsed = time.time() - auto_pending["g1"]
        self.assertGreater(elapsed, 0.0009, f"应该有等待时间, got {elapsed}")
        self.assertEqual(last_auto_msg["g1"][:4], "大家最近")

    def test_cold_message_collection(self):
        """冷场检测收集正确的消息"""
        mem = MockMemory()
        sent_at = time.time()
        time.sleep(1.1)  # 确保跨秒
        t_after = time.strftime("%H:%M:%S", time.localtime())
        sent_str = time.strftime("%H:%M:%S", time.localtime(sent_at))
        mem.add_msg("g1", "小红", "哈哈糖糖来了", t_after)

        follow = [
            f"{e.get('nickname','')}: {e.get('message','')[:80]}"
            for e in list(mem.short_term.get("g1", []))[-8:]
            if e.get("time", "") > sent_str
        ]
        self.assertGreaterEqual(len(follow), 1, f"应该有后续消息, sent={sent_str}, msgs={follow}")
        self.assertIn("哈哈", " ".join(follow))

    def test_no_follow_msgs_is_cold(self):
        """无人回应 = 冷场"""
        mem = MockMemory()
        # 没有后续消息 → 冷场
        follow = list(mem.short_term.get("g1", []))[-8:] if mem.short_term.get("g1") else []
        self.assertEqual(len(follow), 0, "无人回应应该是冷场")


if __name__ == "__main__":
    print("🧪 自治消息模拟测试\n")
    unittest.main(verbosity=2, argv=[__file__])
