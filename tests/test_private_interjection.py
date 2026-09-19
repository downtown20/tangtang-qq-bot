"""
测试私聊插话 + 情绪闭环（2026-08-14）：
- 打扰约束门控（黑名单/白名单/冷却/2小时内刚聊过）
- 目标优先级（心情告警队列 > 心理陪伴用户沉默≥3天 > 主人 > 近期活跃用户）
- 心理陪伴用户收集
"""

import time
import pytest
from agent.handler_autonomy import AutonomyMixin
from agent.memory import MemorySystem
from agent.scenario import ScenarioManager


def _ts(days_ago: float) -> str:
    """days_ago 天前的时间戳（insert_chat 格式）"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days_ago * 86400))


def _seed_user(store, qq: str, days_ago: float):
    """建档 + 写入一条 days_ago 天前的聊天（生产路径：person 行已存在，insert_chat 更新 last_chat）"""
    store.get_or_create_person(qq)
    store.insert_chat(qq, "x", timestamp=_ts(days_ago))


class FakeHandler(AutonomyMixin):
    """只装私聊插话用到的属性（mixin 方法通过 self.xxx 访问）"""

    def __init__(self, store, **overrides):
        self.memory = MemorySystem(store=store)
        self.scenarios = ScenarioManager("scenarios")
        self.config = {
            "scenario_targets": {"10003": "心理陪伴"},
            "groups": {
                "10005": {"scenario_targets": {"10008": "🧠 心理陪伴"}},
            },
        }
        self.bot_qq = "10000"
        self.owner_qq = "10001"
        self._private_blacklist = set()
        self.reply_only_to = []
        self._last_private_init = {}
        self._care_due = {}
        self.mood_tracker = None
        # 2026-08-16 观察回路：意愿分住在关系场（FakeRel 提供 seek_willingness）
        self.self_state = type("S", (), {"relationships": {}})()
        self._pending_seek = {}
        for k, v in overrides.items():
            setattr(self, k, v)


@pytest.fixture
def h(store):
    return FakeHandler(store)


class TestPrivateGate:
    def test_block_bot_and_blacklist(self, h):
        assert not h._private_gate_ok(h.bot_qq, time.time())
        h._private_blacklist.add("u1")
        assert not h._private_gate_ok("u1", time.time())

    def test_block_reply_only_to(self, h, store):
        h.reply_only_to = ["u1"]
        _seed_user(store, "u1", 1)
        _seed_user(store, "u2", 1)
        assert not h._private_gate_ok("u2", time.time())
        assert h._private_gate_ok("u1", time.time())

    def test_block_cooldown(self, h, store):
        store.insert_chat("u1", "hi", timestamp=_ts(3))  # 3天前聊过——过了2h窗口
        now = time.time()
        h._last_private_init["u1"] = now - 1800  # 30分钟前刚冷启过
        assert not h._private_gate_ok("u1", now)

    def test_block_recent_chat(self, h, store):
        store.insert_chat("u1", "hi")  # 刚刚聊过
        assert not h._private_gate_ok("u1", time.time())

    def test_pass_for_silent_user(self, h, store):
        _seed_user(store, "u1", 1)  # 1天前聊过——过了2h窗口、过冷却
        assert h._private_gate_ok("u1", time.time())


class TestPrivateTargetPriority:
    def test_psychology_user_ids(self, h):
        ids = h._psychology_user_ids()
        assert ids == {"10003", "10008"}

    def test_care_due_first(self, h, store):
        """心情告警队列优先于一切"""
        _seed_user(store, "uA", 2)
        _seed_user(store, h.owner_qq, 5)
        h._care_due = {"uA": "连续3天心情走低"}
        qq, reason, scen = h._pick_private_target(time.time())
        assert qq == "uA"
        assert "心情告警" in reason
        assert scen == "psychology"

    def test_psychology_silent_second(self, h, store):
        """无告警时，沉默≥3天的心理陪伴用户优先于主人"""
        _seed_user(store, "10003", 5)
        _seed_user(store, h.owner_qq, 6)
        qq, reason, scen = h._pick_private_target(time.time())
        assert qq == "10003"
        assert "天没来了" in reason
        assert scen == "psychology"

    def test_owner_third(self, h, store):
        """心理陪伴用户最近活跃 → 轮到主人"""
        _seed_user(store, "10003", 0.5)  # 12小时前——过了2h窗口，但沉默<3天
        _seed_user(store, h.owner_qq, 3)
        qq, reason, scen = h._pick_private_target(time.time())
        assert qq == h.owner_qq

    def test_recent_user_fourth(self, h, store):
        """心理陪伴用户沉默不足3天、主人2小时内活跃 → 近期活跃用户"""
        _seed_user(store, "10003", 0.4)  # 沉默0.4天 < 3天
        _seed_user(store, h.owner_qq, 0.05)   # 1.2小时前活跃 → 门控拦截
        _seed_user(store, "uX", 0.2)          # ~5小时前
        qq, reason, scen = h._pick_private_target(time.time())
        assert qq == "uX"
        assert scen == ""
