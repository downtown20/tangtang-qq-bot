"""
测试实时情绪闭环（2026-08-14）：
- _mood_snapshot_context：分析当前消息 → 入库 → 一行状态事实（同回合给 LLM）
- 分数映射：负面/中性/正面；CQ 码不进模型；空消息跳过
- 告警：进 _care_due + 通知主人
- 失败安全：模型未就绪/分析异常 → 空串，绝不阻断回复链路
"""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock

from agent.handler import MessageHandler


@contextlib.contextmanager
def caplog_at_level(level):
    """把 root logger 上的记录收进一个列表（不依赖 pytest 的 caplog fixture）。

    存在的理由：断言「白名单外的自报不是**静默**丢弃」需要看到真实日志记录。
    用 caplog fixture 也行，但这里是普通方法，用一个 6 行的上下文管理器更直白。
    """
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level=level)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


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


class TestMoodReportBackfill:
    """回复 LLM 自报情绪回灌（2026-10-13 M3 剩余项，审计 P0-2）。

    审计实测：15 个事件里 11 个在定义之外**零引用**，其中五个正向事件
    （被夸/被感谢/被想念/亲密聊天/帮到别人）一个发射点都没有——心情只会被
    `_apply_decay` 收敛回中性 70，任何经历都推不动它。M3 当时只接了负向信号，
    并写明「正向输入留给『回复 LLM 自报标签回灌』——那才是它的正主」。
    """

    @staticmethod
    def _handler(engine):
        h = object.__new__(MessageHandler)
        h.mood = engine
        return h

    def test_whitelisted_label_updates_engine(self):
        from agent.mood import MoodEngine
        engine = MoodEngine()          # 真实引擎——数值由事件表决定，不写死
        before = engine.mood
        h = self._handler(engine)
        assert h._apply_mood_report("被夸") is True
        # 事件表里 BEING_PRAISED = (0, +5, +2)：心情必须真的涨
        assert engine.mood > before, "自报被夸却没让心情动——回灌没接上"

    def test_every_reportable_label_reaches_the_engine(self):
        """白名单里的每一个词都必须真的能被消费（穷举，不是抽样）。"""
        from agent.mood import MoodEngine, MOOD_REPORT_EVENTS
        for label in MOOD_REPORT_EVENTS:
            engine = MoodEngine()
            before = (engine.mood, engine.energy, engine.patience)
            assert self._handler(engine)._apply_mood_report(label) is True, label
            after = (engine.mood, engine.energy, engine.patience)
            assert after != before, f"{label} 回灌了但三个维度都没动"
            assert any(a > b for a, b in zip(after, before)), (
                f"{label} 是正向事件，数值却不升反降：{before} → {after}")

    def test_off_whitelist_label_is_dropped_not_crashed(self):
        """fail-closed：她自造词一律丢弃。**但不能静默**——静默丢弃正是
        审计 RC5「失败路径不产生可观测事实」那一条（caplog 钉住有日志）。"""
        import logging
        from agent.mood import MoodEngine
        engine = MoodEngine()
        before = (engine.mood, engine.energy, engine.patience)
        h = self._handler(engine)
        with caplog_at_level(logging.INFO) as records:
            assert h._apply_mood_report("自造的词") is False
        assert (engine.mood, engine.energy, engine.patience) == before
        assert any("白名单外" in r.getMessage() for r in records), (
            "白名单外的自报被静默丢弃——日志里必须留下事实")

    def test_empty_label_is_a_noop(self):
        from agent.mood import MoodEngine
        engine = MoodEngine()
        before = (engine.mood, engine.energy, engine.patience)
        h = self._handler(engine)
        assert h._apply_mood_report("") is False
        assert h._apply_mood_report("   ") is False
        assert (engine.mood, engine.energy, engine.patience) == before

    def test_empty_label_is_visible_when_protocol_was_injected(self):
        """2026-10-13 T1.2（审计 C3）：**协议注入了但她没写**必须留痕。

        修复前的形态：这一支静默 return False，于是「闸门没开」与「注入了但没遵从」
        在日志里完全同形——审计正是因此把「心情自报回灌=0」读成「LLM 没遵从协议」
        （实测真因是闸门从未打开：协议上线后 N=0 个回合满足闸门条件）。
        """
        from agent.mood import MoodEngine
        h = self._handler(MoodEngine())
        with caplog_at_level(logging.INFO) as records:
            assert h._apply_mood_report("", injected=True, source="群-10001") is False
        msgs = [r.getMessage() for r in records]
        assert any("心情自报未写" in m for m in msgs), (
            "协议已注入但她没写——这件事没进日志，遵从率无从计算")
        assert any("群-10001" in m for m in msgs), "日志里必须能看出是哪个来源"

    def test_empty_label_is_silent_when_gate_closed(self):
        """阴性对照：闸门没开时**不记**。否则每个成功回复都会多一行无法区分
        来源的日志；而「闸门没开」本来就由注入日志的**缺席**直接可判。"""
        from agent.mood import MoodEngine
        h = self._handler(MoodEngine())
        with caplog_at_level(logging.INFO) as records:
            assert h._apply_mood_report("") is False
        assert not any("心情自报未写" in r.getMessage() for r in records), (
            "闸门没开也记了日志——两种情况的区分度被冲掉了")

    def test_missing_engine_does_not_break_the_turn(self):
        """离线 harness / 引擎未初始化时不许炸主链路（与 embed_engine 同款可选依赖）。"""
        h = object.__new__(MessageHandler)   # 连 mood 属性都没有
        assert h._apply_mood_report("被夸") is False
