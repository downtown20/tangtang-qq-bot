"""
私聊主动说话频控测试（2026-08-15）

用户要求：冷场对象减少主动说话；活跃对象可以每天主动聊。
_private_gate_ok 活跃度感知冷却：
- 普通路径：<1天活跃→24h冷却（每天可聊）；1-3天→48h；≥3天冷场→72h
- care_priority（心情告警/心理陪伴沉默关心）：保持 6h，不受活跃度压制
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler

NOW = 1_000_000.0


def _make(silent_days, last_init_hours_ago):
    h = object.__new__(MessageHandler)
    h.bot_qq = "10000"
    h._private_blacklist = set()
    h.reply_only_to = []
    h._days_silent = lambda qq, now: silent_days
    # 2026-08-16 观察回路：冷却与意愿分住关系场（重启不丢）
    h.self_state = types.SimpleNamespace(relationships={})
    h._seek_uncertain = {}
    h._save_state_kv = lambda *_args: True
    if last_init_hours_ago is not None:
        from agent.self_state import RelationshipField
        rel = RelationshipField(qq_id="12345")
        rel.last_seek_ts = NOW - last_init_hours_ago * 3600
        h.self_state.relationships["12345"] = rel
    return h


class TestActivityAwareCooldown:
    def test_active_user_daily_ok(self):
        """活跃用户（沉默<1天）：25小时前主动过 → 冷却已过，可以再主动"""
        h = _make(silent_days=0.2, last_init_hours_ago=25)
        assert h._private_gate_ok("12345", NOW) is True

    def test_active_user_within_cooldown_blocked(self):
        """活跃用户（沉默<1天）：23小时前主动过 → 24h 冷却未过，拦截"""
        h = _make(silent_days=0.2, last_init_hours_ago=23)
        assert h._private_gate_ok("12345", NOW) is False

    def test_cold_user_long_cooldown(self):
        """冷场用户（沉默≥3天）：70小时前主动过 → 72h 冷却未过，拦截（冷场少说）"""
        h = _make(silent_days=5, last_init_hours_ago=70)
        assert h._private_gate_ok("12345", NOW) is False

    def test_cold_user_after_cooldown(self):
        """冷场用户：73小时前主动过 → 72h 已过"""
        h = _make(silent_days=5, last_init_hours_ago=73)
        assert h._private_gate_ok("12345", NOW) is True

    def test_care_priority_keeps_6h(self):
        """心情告警/心理陪伴：沉默 5 天也不压制——7小时前主动过即可再来（6h 冷却）"""
        h = _make(silent_days=5, last_init_hours_ago=7)
        assert h._private_gate_ok("12345", NOW, care_priority=True) is True

    def test_care_priority_within_6h_blocked(self):
        h = _make(silent_days=5, last_init_hours_ago=5)
        assert h._private_gate_ok("12345", NOW, care_priority=True) is False

    def test_recent_chat_no_cold_start(self):
        """2小时内刚聊过 → 无论冷却如何都不冷启"""
        h = _make(silent_days=0.03, last_init_hours_ago=None)  # ~43分钟
        assert h._private_gate_ok("12345", NOW, care_priority=True) is False

    def test_uncertain_private_send_survives_restart_cooldown(self):
        h = _make(silent_days=5, last_init_hours_ago=None)
        h._seek_uncertain = {"12345": NOW - 60}

        assert h._private_gate_ok("12345", NOW, care_priority=True) is False


class TestRecentDialogue:
    """get_recent_dialogue：双方发言都含、标记谁说的、按时间正序（2026-08-15）"""

    def test_both_sides_marked(self, store):
        store.insert_chat("777", "糖糖在吗", is_bot=False, timestamp="2026-08-15 10:00:00")
        store.insert_chat("777", "在的喵～", is_bot=True, timestamp="2026-08-15 10:01:00")
        store.insert_chat("777", "想你了", is_bot=False, timestamp="2026-08-15 10:02:00")
        lines = store.get_recent_dialogue("777", limit=5)
        assert len(lines) == 3
        assert lines[0].startswith("[2026-08-15 10:00") and "ta: 糖糖在吗" in lines[0]
        assert "糖糖: 在的喵～" in lines[1]
        assert "ta: 想你了" in lines[2]

    def test_empty_returns_list(self, store):
        assert store.get_recent_dialogue("nobody", limit=5) == []

    def test_private_dialogue_excludes_group_messages(self, store):
        store.insert_chat("777", "私聊内容", group_id="", is_bot=False)
        store.insert_chat("777", "A群秘密", group_id="group-a", is_bot=False)
        store.insert_chat("777", "B群秘密", group_id="group-b", is_bot=False)

        lines = store.get_recent_dialogue("777", limit=10, group_id="")

        joined = "\n".join(lines)
        assert "私聊内容" in joined
        assert "A群秘密" not in joined and "B群秘密" not in joined

    def test_private_callers_explicitly_request_private_scope(self):
        import inspect
        from agent.handler import MessageHandler
        from agent.handler_autonomy import AutonomyMixin
        from agent.handler_commands import CommandRouter

        autonomy = inspect.getsource(AutonomyMixin._check_private_initiative)
        relay = inspect.getsource(CommandRouter._do_relay)
        natural_action = inspect.getsource(MessageHandler._execute_natural_action)
        constructor = inspect.getsource(MessageHandler.__init__)

        assert "get_recent_dialogue" in autonomy and "group_id=\"\"" in autonomy
        assert "_run_store_io" in autonomy
        # P0-D1：/传话改为原话 relay，不再把目标人的私聊历史注入隐藏
        # LLM 背景；统一发送 helper 的回执与归因契约另测。
        assert "execute_send_action" in relay and 'mode="relay"' in relay
        assert "recall_formatted(" in natural_action and "source_group_id=\"\"" in natural_action
        assert "self.memory.recall(" in constructor and "source_group_id=\"\"" in constructor

    def test_private_injection_recall_crosses_scope(self):
        """私聊**注入召回**跨作用域（None），群聊注入仍限本群——2026-10-24 决策。

        单向性是这条改动的全部前提：
          · 私聊召回群记忆安全——`memories.qq_id` 是记忆主体，召回的仍是「关于
            当前对话对象」的记忆，只是跨了场景；「你在某个群说过的话」他本人都在场。
          · 群聊召回私聊记忆**永远不允许**——那是当众复述私聊，
            `memory_access` 已把「私聊查群记录」定成隐私红线（见本文件上面的用例）。

        为什么用源码断言：这段逻辑在 `handle_private_message` 内部（一千多行的
        协程），没有可单测的接缝；而"传了哪个 scope"正是唯一要钉住的东西。
        """
        import inspect
        from agent.handler import MessageHandler

        private_src = inspect.getsource(MessageHandler.handle_private_message)
        group_src = inspect.getsource(MessageHandler.handle_group_message)

        assert "source_group_id=None" in private_src, "私聊注入召回必须跨域"
        assert "source_group_id=group_id" in group_src, "群聊注入召回必须锁本群"
        # 反向红线：群聊路径里不许出现跨域召回
        assert "source_group_id=None" not in group_src

        # 上面三条只证明「存在一个 None / 一个 group_id」——**存在**不等于**全部**。
        # 2026-10-26 实证：`handle_private_message` 里有两处 recall（对话路径 + 模糊档），
        # 把其中一处改回锁作用域，上面三条照样绿（`变异/变异_scope.json` 的 S1 就是
        # 这么假绿的，锚点修好后才暴露出来）。而 `agent/store.py` 的 `query_memories`
        # 用的是 `if source_group_id is not None`（严格判断），所以 `None`（跨域）与
        # `""`（只搜私聊域）**语义完全不同**，差一个字符就退化。必须逐处核对。
        import re

        recall_scope = (r"self\.memory\.recall(?:_uncertain)?\s*,"
                        r"[^)]*?source_group_id\s*=\s*([^,\n)]+)")
        private_scopes = [v.strip() for v in re.findall(recall_scope, private_src, re.S)]
        group_scopes = [v.strip() for v in re.findall(recall_scope, group_src, re.S)]
        assert private_scopes, "私聊路径里必须能定位到注入召回的 scope 实参"
        assert group_scopes, "群聊路径里必须能定位到注入召回的 scope 实参"
        assert all(v == "None" for v in private_scopes), (
            "私聊注入召回必须**每一处**都跨作用域(None)，实际=" + repr(private_scopes))
        assert all(v == "group_id" for v in group_scopes), (
            "群聊注入召回必须**每一处**都锁本群(group_id)，实际=" + repr(group_scopes))
