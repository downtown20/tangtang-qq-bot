"""
昵称匹配三层防误触发测试（2026-08-10 修复"谁发消息都回"）

1. jieba 完整词匹配——"小糖果"不命中"小糖"
2. 弱昵称（女仆/猫娘）需要称谓性信号——"女仆装""猫娘图"不触发
3. 否定语境——"不要叫你糖糖了"不算叫名字
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler

NICKNAMES = ["女仆", "猫娘", "糖啥子", "糖傻子", "糖糖", "小糖", "糖姐", "小糖糖"]


@pytest.fixture
def matcher():
    fake = types.SimpleNamespace(config={"bot": {"nicknames": NICKNAMES}})
    return lambda text: MessageHandler._has_bot_nickname(fake, text)


class TestNicknameSubstringNotTriggered:
    """群聊高频词/复合词不应被当作叫名字"""

    @pytest.mark.parametrize("text", [
        "女仆装真好看",
        "发个猫娘图",
        "这个女仆好可爱",
        "猫娘是最可爱的",
        "小糖果好好吃",
        "女仆装",
        "猫娘图",
    ])
    def test_compound_words_not_trigger(self, matcher, text):
        assert matcher(text) is False


class TestNicknameNegation:
    """否定语境不算叫名字"""

    @pytest.mark.parametrize("text", [
        "不要叫你糖糖了",
        "别喊糖糖了",
        "不准叫糖糖",
    ])
    def test_negation_not_trigger(self, matcher, text):
        assert matcher(text) is False


class TestNicknameAddressStyle:
    """真正的叫名字（称谓性使用）必须触发"""

    @pytest.mark.parametrize("text", [
        "糖糖早安",
        "小糖糖你在吗",
        "女仆，来陪我玩",
        "猫娘~",
        "糖姐今天心情好",
        "糖糖，该起床啦",
        "糖糖在吗",
        "糖糖",
    ])
    def test_address_style_triggers(self, matcher, text):
        assert matcher(text) is True


class TestCrossGroupWindow:
    """2026-08-16 跨群窗口键修复——同人在 B 群互动不再顶掉 A 群的窗口
    （现场：主人 21:36 在群 A 被@进窗口 → 21:37 在群 B 发了条 @ → A 群窗口
    被顶掉 → 30 秒前的追问被当插话 92/93 跳过）"""

    @pytest.fixture
    def tracker(self):
        from agent.conversation_tracker import ConversationTracker
        t = ConversationTracker(None)
        t._engaged = {}
        return t

    def test_engagement_in_other_group_does_not_kill_window(self, tracker):
        tracker.force_engage("123", "群A")
        assert tracker.get_window_bonus("123", "群A")[0] is True
        # 同一人在 B 群被@/被回复——A 群窗口必须不受影响
        tracker.force_engage("123", "群B")
        tracker.on_reply_sent("123", "群B", "你好呀")
        in_a, threshold = tracker.get_window_bonus("123", "群A")
        assert in_a is True and threshold == 50
        in_b, _ = tracker.get_window_bonus("123", "群B")
        assert in_b is True

    def test_other_users_still_isolated(self, tracker):
        tracker.force_engage("111", "群A")
        tracker.force_engage("222", "群A")
        tracker.on_reply_sent("222", "群A", "回复")
        assert tracker.get_window_bonus("111", "群A")[0] is True
        assert tracker.get_window_bonus("111", "群C")[0] is False
