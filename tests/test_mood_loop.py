"""
测试实时情绪闭环（2026-08-14）：
- _mood_snapshot_context：分析当前消息 → 入库 → 一行状态事实（同回合给 LLM）
- 分数映射：负面/中性/正面；CQ 码不进模型；空消息跳过
- 告警：进 _care_due + 通知主人
- 失败安全：模型未就绪/分析异常 → 空串，绝不阻断回复链路
"""

import asyncio
from unittest.mock import AsyncMock

from agent.handler import MessageHandler


def _make_handler(tracker, napcat=None):
    """只装 _mood_snapshot_context 用到的属性（不跑 MessageHandler.__init__）"""
    h = object.__new__(MessageHandler)
    h.mood_tracker = tracker
    h._care_due = {}
    h.owner_qq = "10001"
    h.napcat = napcat
    return h


class FakeMoodTracker:
    """记录 analyze 收到的文本 + 返回预设分数"""

    def __init__(self, ready=True, scores=None):
        self.ready = ready
        self._scores = list(scores or [])
        self.seen = []
        self.recorded = []
        self.alert = None

    def analyze(self, text):
        self.seen.append(text)
        return self._scores.pop(0) if self._scores else 0.2

    def record(self, qq_id, score):
        self.recorded.append((qq_id, score))

    def check_alert(self, qq_id):
        return self.alert


class TestSnapshot:
    def test_negative_recorded_and_injected(self):
        t = FakeMoodTracker(scores=[0.13])
        h = _make_handler(t)
        out = h._mood_snapshot_context("u1", "小明", "我好难受，睡不着")
        # 2026-08-15 整体审查：数值不进提示词（与「不要提心情指数」禁令冲突）——只给定性
        assert "负面" in out
        assert "0.13" not in out
        assert t.recorded == [("u1", 0.13)]

    def test_score_mapping(self):
        t = FakeMoodTracker(scores=[0.66, 0.40])
        h = _make_handler(t)
        assert "正面" in h._mood_snapshot_context("u1", "小明", "今天超开心")
        assert "中性" in h._mood_snapshot_context("u1", "小明", "随便说说")

    def test_cq_codes_stripped_before_analyze(self):
        t = FakeMoodTracker(scores=[0.1])
        h = _make_handler(t)
        h._mood_snapshot_context("u1", "小明", "[CQ:image,file=x][CQ:face,id=178]今天真难过")
        assert t.seen == ["今天真难过"]

    def test_empty_text_skips_analyze(self):
        t = FakeMoodTracker(scores=[0.1])
        h = _make_handler(t)
        assert h._mood_snapshot_context("u1", "小明", "  [CQ:image,file=x]  ") == ""
        assert t.recorded == []

    def test_not_ready_noop(self):
        t = FakeMoodTracker(ready=False)
        h = _make_handler(t)
        assert h._mood_snapshot_context("u1", "小明", "hi") == ""
        assert t.recorded == []

    def test_analyze_none_noop(self):
        t = FakeMoodTracker(scores=[None])
        h = _make_handler(t)
        assert h._mood_snapshot_context("u1", "小明", "hi") == ""
        assert t.recorded == []

    def test_analyze_crash_safe(self):
        class BoomTracker(FakeMoodTracker):
            def analyze(self, text):
                raise RuntimeError("boom")
        h = _make_handler(BoomTracker())
        assert h._mood_snapshot_context("u1", "小明", "hi") == ""

    def test_alert_queued_and_owner_notified(self):
        t = FakeMoodTracker(scores=[0.1])
        t.alert = "今天心情骤降，需要关注"
        napcat = AsyncMock()
        h = _make_handler(t, napcat=napcat)

        async def go():
            return h._mood_snapshot_context("u1", "小明", "好难过")

        out = asyncio.run(go())
        assert "负面" in out
        assert h._care_due["u1"] == "今天心情骤降，需要关注"
        napcat.send_private_message.assert_called_once()
