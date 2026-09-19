"""主动私聊观察回路测试（2026-08-16）——冷场长记性，像人一样选择主动找人。

主人需求：糖糖主动私聊后观察对方回应——24h 无回复=冷场（意愿分 -0.35）、
敷衍/负面=半次冷场（-0.15）、热场=+0.1；连续冷场后不再主动私聊；
对方主动来聊=回弹（≥0.5）。意愿分住在关系场 seek_willingness，持久化。
"""
import asyncio

import pytest

from agent.handler_autonomy import AutonomyMixin
from agent.self_state import RelationshipField


class FakeSelfState:
    def __init__(self):
        self.relationships = {}
        self._saved = 0

    def save(self):
        self._saved += 1

    def get_or_create_relationship(self, qq_id, nickname=""):
        if qq_id not in self.relationships:
            self.relationships[qq_id] = RelationshipField(qq_id=qq_id)
        return self.relationships[qq_id]


class FakePerception:
    def __init__(self, sentiment="positive"):
        self._sentiment = sentiment

    async def _llm_evaluate(self, bot_reply, reaction):
        return {"sentiment": self._sentiment}


class FakeMemory:
    def __init__(self, last_chat=""):
        self._last_chat = last_chat

    def get_or_create_person(self, qq_id):
        return {"last_chat": self._last_chat}


class FakeNapcat:
    _no_friend_until = {}


class FakeAutonomy(AutonomyMixin):
    def __init__(self):
        self.self_state = FakeSelfState()
        self.perception = FakePerception()
        self.memory = FakeMemory()
        self.napcat = FakeNapcat()
        self.bot_qq = "10000"
        self._private_blacklist = set()
        self.reply_only_to = None


def _mk(w=0.5, pending_ts=0.0, last_seek_ts=0.0, silent_days=999.0):
    a = FakeAutonomy()
    # 2026-08-16 Codex I4：FakeMemory 的 last_chat 决定 _days_silent——
    # silent_days<1 时 cooldown=24h，冷却先于 pending 铁律放行，
    # 铁律才是唯一拦截原因（此前 last_chat="" → 999 天 → 72h 冷却拦截，假绿）
    import time as _t
    last = "" if silent_days >= 999 else _t.strftime(
        "%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - silent_days * 86400))
    a.memory = FakeMemory(last_chat=last)
    if w is not None:
        rel = RelationshipField(qq_id="20001")
        rel.seek_willingness = w
        rel.seek_pending_ts = pending_ts
        rel.last_seek_ts = last_seek_ts
        a.self_state.relationships["20001"] = rel
    return a


# ── 结算 ──

def test_cold_field_after_24h_no_reply(monkeypatch):
    a = _mk(w=0.8)
    a._mark_seek_sent("20001", now=100.0, msg="hi")
    # 时钟拨到 25 小时后
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 100.0 + 25 * 3600)
    a._settle_stale_seeks()
    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == pytest.approx(0.45)
    assert rel.seek_pending_ts == 0  # 已结算


def test_no_cold_field_before_24h(monkeypatch):
    a = _mk(w=0.8)
    a._mark_seek_sent("20001", now=100.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 100.0 + 12 * 3600)
    a._settle_stale_seeks()
    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == 0.8
    assert rel.seek_pending_ts > 0  # 未到 24h 不结算


def test_warm_reply_settles_positive(monkeypatch):
    a = _mk(w=0.4)
    a.perception = FakePerception("positive")
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 1_000_000.5)
    asyncio.run(a._settle_seek_with_reply("20001", "好呀好呀！"))
    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == 0.5
    assert rel.seek_pending_ts == 0


def test_tepid_reply_settles_negative(monkeypatch):
    a = _mk(w=0.5)
    a.perception = FakePerception("negative")
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 1_000_000.5)
    asyncio.run(a._settle_seek_with_reply("20001", "哦"))
    assert a.self_state.relationships["20001"].seek_willingness == pytest.approx(0.35)


def test_neutral_reply_no_change(monkeypatch):
    a = _mk(w=0.5)
    a.perception = FakePerception("neutral")
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 1_000_000.5)
    asyncio.run(a._settle_seek_with_reply("20001", "在忙"))
    assert a.self_state.relationships["20001"].seek_willingness == 0.5


def test_no_second_seek_while_pending(monkeypatch):
    """2026-08-16 事故回归（重启骚扰）：上次主动找还没回复 → 绝不二次打扰。
    活跃用户（沉默<1天→24h 冷却）在 25h 后冷却已放行——此时仍被拦截，
    唯一原因就是 pending 铁律（2026-08-16 Codex I4：此前 FakeMemory
    last_chat 为空 → 999 天 → 72h 冷却拦截，铁律从未被走到，假绿）。"""
    a = _mk(w=0.8, pending_ts=500.0, last_seek_ts=500.0, silent_days=0.2)
    # 模拟重启后：状态从关系场恢复，时钟在 500+25h（24h 冷却已过）
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 500.0 + 25 * 3600)
    assert a._private_gate_ok("20001", now=500.0 + 25 * 3600) is False


def test_settle_not_cold_when_spoke_elsewhere(monkeypatch):
    """2026-08-16 Codex I3：对方 24h 内在其他渠道（群里）说过话 →
    按中性结算不罚，只有真沉默才 -0.35"""
    import time as _t
    a = _mk(w=0.8, silent_days=0.1)  # 对方今天说过话（last_chat 2.4h 前）
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time",
                        lambda: 1_000_000.0 + 25 * 3600)
    a._settle_stale_seeks()
    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == 0.8  # 不罚
    assert rel.seek_pending_ts == 0


def test_settle_cold_when_truly_silent(monkeypatch):
    """真沉默（任何渠道无发言）→ 冷场 -0.35"""
    a = _mk(w=0.8, silent_days=999.0)
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time",
                        lambda: 1_000_000.0 + 25 * 3600)
    a._settle_stale_seeks()
    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == pytest.approx(0.45)


def test_async_settle_stale_seeks_preserves_neutral_semantics(monkeypatch):
    """自治循环的异步结算仍需在跨渠道发言时按中性结算。"""
    a = _mk(w=0.8, silent_days=0.1)
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr(
        "agent.handler_autonomy.time_mod.time",
        lambda: 1_000_000.0 + 25 * 3600,
    )

    asyncio.run(a._settle_stale_seeks_async())

    rel = a.self_state.relationships["20001"]
    assert rel.seek_willingness == pytest.approx(0.8)
    assert rel.seek_pending_ts == 0


def test_async_settle_stale_seeks_does_not_block_event_loop(monkeypatch):
    """慢的 people 读取必须在线程边界执行，不能卡住自治事件循环。"""
    import time as _t

    class SlowMemory(FakeMemory):
        def get_or_create_person(self, qq_id):
            _t.sleep(0.05)
            return super().get_or_create_person(qq_id)

    a = _mk(w=0.8, silent_days=999.0)
    a.memory = SlowMemory()
    a._mark_seek_sent("20001", now=1_000_000.0, msg="hi")
    monkeypatch.setattr(
        "agent.handler_autonomy.time_mod.time",
        lambda: 1_000_000.0 + 25 * 3600,
    )

    async def _run():
        ticks = []

        async def _tick():
            await asyncio.sleep(0.005)
            ticks.append(True)

        await asyncio.gather(a._settle_stale_seeks_async(), _tick())
        return ticks

    assert asyncio.run(_run())


def test_seek_cooldown_uses_relationship_ts(monkeypatch):
    """冷却读关系场 last_seek_ts——重启后 24h 冷却依然有效（事故回归）"""
    a = _mk(w=0.8, last_seek_ts=1000.0)
    # 重启后 2 小时：活跃用户 24h 冷却未过 → 拦截
    monkeypatch.setattr("agent.handler_autonomy.time_mod.time", lambda: 1000.0 + 2 * 3600)
    assert a._private_gate_ok("20001", now=1000.0 + 2 * 3600) is False


# ── 回弹 ──

def test_user_initiated_rebounds_to_floor():
    a = _mk(w=0.05)
    a._note_user_initiated("20001", "糖糖我回来啦！！")
    assert a.self_state.relationships["20001"].seek_willingness == 0.5


def test_user_initiated_long_message_small_bonus():
    a = _mk(w=0.7)
    a._note_user_initiated("20001", "糖糖糖糖，我跟你说个特别有意思的事情呀！")
    assert a.self_state.relationships["20001"].seek_willingness == 0.75


def test_user_initiated_short_message_no_bonus():
    a = _mk(w=0.7)
    a._note_user_initiated("20001", "在吗")
    assert a.self_state.relationships["20001"].seek_willingness == 0.7


# ── 门槛 ──

def test_gate_blocks_when_willingness_low():
    a = _mk(w=0.08)
    assert a._private_gate_ok("20001", now=999999.0) is False


def test_gate_passes_when_willingness_ok():
    a = _mk(w=0.5)
    assert a._private_gate_ok("20001", now=999999.0) is True


def test_care_priority_also_gated_by_willingness():
    """用户确认：care 场景同样受意愿分约束——情绪关心不是无限打扰"""
    a = _mk(w=0.0)
    assert a._private_gate_ok("20001", now=999999.0, care_priority=True) is False


def test_adjust_persists():
    a = _mk(w=0.5)
    a._adjust_willingness("20001", -0.35, "冷场")
    assert a.self_state.relationships["20001"].seek_willingness == pytest.approx(0.15)
    assert a.self_state._saved >= 1
    # 封底：不低于 0
    a._adjust_willingness("20001", -1.0, "再冷场")
    assert a.self_state.relationships["20001"].seek_willingness == 0.0
