"""批 1b 验收：画像出口统一契约（2026-08-16）

事故背景：私聊画像全文注入且免 token 截断；_search_people/_get_profile_and_recent/
recall_formatted 无 caveat 裸奔；合成行挂 💭（真实记录）名头。批 1b 收口：
profile_text 唯一截断契约 + caveat 独立成行 + 合成行分轨 🖼️。
"""
from unittest.mock import MagicMock

import pytest

from agent import protocols
from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


class TestProfileText:
    def test_short_notes_unchanged(self):
        assert protocols.profile_text("画画爱好者") == "画画爱好者"

    def test_empty_or_none(self):
        assert protocols.profile_text("") == ""
        assert protocols.profile_text(None) == ""

    def test_long_truncates_at_natural_break(self):
        notes = "成展，大二学生。" + "和糖糖一起做项目。" * 20
        out = protocols.profile_text(notes)
        assert len(out) <= protocols.PROFILE_MAX_LEN
        assert out.endswith(("。", "；", "，", "、"))

    def test_long_no_break_hard_cut(self):
        out = protocols.profile_text("啊" * 300)
        assert len(out) <= protocols.PROFILE_MAX_LEN


class TestCompactSynthesisChannel:
    def test_synthesis_key_gets_image_tag(self, memory):
        """合成行分轨——fact_synthesis 不挂 💭（真实记录）名头"""
        memory.store.get_or_create_person("9001", "测试")
        selected = [type("M", (), {"key": "fact_synthesis", "value": "自称特摄仙人，喜欢特摄"})]
        out = memory.format_compact_memories("9001", selected=selected)
        assert "🖼️" in out and "💭" not in out

    def test_fact_key_gets_thought_tag(self, memory):
        memory.store.get_or_create_person("9002", "测试")
        selected = [type("M", (), {"key": "fact", "value": "在杭州上班工作"})]
        out = memory.format_compact_memories("9002", selected=selected)
        assert "💭" in out

    def test_profile_uses_unified_truncation(self, memory):
        """compact 画像走 profile_text 契约（≤50 字）"""
        memory.store.get_or_create_person("9003", "测试")
        memory.store.update_person("9003", notes="成展，大二学生。" + "很长的画像。" * 30)
        out = memory.format_compact_memories("9003", selected=[])
        img_line = [l for l in out.split("\n") if l.strip().startswith("🖼️")]
        assert img_line
        assert len(img_line[0]) <= 72  # 前缀4字 + 画像≤50字 + caveat
        assert "很长的画像。" * 30 not in out  # 截断生效


class TestRecallFormatted:
    def test_profile_has_standalone_caveat_line(self, memory):
        memory.store.get_or_create_person("9010", "测试")
        memory.store.update_person("9010", notes="成展，大二学生，住在郴州市苏仙区，喜欢画画和特摄。")
        memory.store.insert_memory(
            "9010", "fact", "在杭州上班工作", importance=6, origin="manual"
        )
        out = memory.recall_formatted("9010")
        assert "🖼️" in out
        assert protocols.PROFILE_CAVEAT_LINE in out

    def test_synthesis_excluded_from_core_channel(self, memory):
        """合成行不进「重要」频道——与真实记忆分开"""
        memory.store.get_or_create_person("9011", "测试")
        memory.store.insert_memory(
            "9011", "fact", "在杭州上班", importance=6, origin="manual"
        )
        memory.store.insert_memory("9011", "fact_synthesis", "喜欢特摄", importance=8)
        out = memory.recall_formatted("9011")
        assert "喜欢特摄" not in out
        assert "在杭州上班" in out

    def test_private_generation_scope_excludes_group_memory_and_global_profile(self, memory):
        memory.store.get_or_create_person("9012", "测试")
        memory.store.update_person("9012", notes="全局画像不能自动进入生成型私聊。")
        memory.store.insert_memory(
            "9012", "fact", "私聊确认的事实", importance=6,
            origin="manual", source_group_id="",
        )
        memory.store.insert_memory(
            "9012", "fact", "群里才说的秘密", importance=6,
            origin="manual", source_group_id="group-a",
        )

        out = memory.recall_formatted("9012", source_group_id="")

        assert "私聊确认的事实" in out
        assert "群里才说的秘密" not in out
        assert "全局画像" not in out


class TestHandlerOutlets:
    def test_search_people_caveat_and_truncation(self):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = MagicMock()
        long_notes = "画像。" * 200
        h.memory.store.search_people.return_value = [
            {"qq_id": "123", "nickname": "甲", "notes": long_notes},
            {"qq_id": "456", "nickname": "乙", "notes": ""},
        ]
        out = h._search_people("甲")
        assert protocols.PROFILE_CAVEAT_LINE in out
        assert len(out) < 1000  # 截断生效——此前全文裸奔

    def test_get_profile_and_recent_caveat(self):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = MagicMock()
        h.memory.store.get_or_create_person.return_value = {
            "nickname": "甲",
            "notes": "成展，大二学生。" + "爱好。" * 100,
            "intimacy": 30,
        }
        h.memory.store.get_recent_chats.return_value = []
        out = h._get_profile_and_recent("123")
        assert protocols.PROFILE_CAVEAT_LINE in out
        assert len(out) < 600
