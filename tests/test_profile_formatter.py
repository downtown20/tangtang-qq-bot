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

class TestPrivateProfileInjection:
    """2026-10-12 主人拍板：**私聊注入人物画像**，群聊不注入。

    CLAUDE.md 六-2 的三层个人化是「怎么聊（场景）→ **她是谁（people.notes）** →
    聊什么（memories）」。审计 P1-4 查出「她是谁」这层在主对话路径上根本没生效
    （唯一的调用点传 include_profile=False）。决策：把它打开——画像属于「我认识你」，
    只在跟本人一对一（私聊）时成立；群里注入等于把甲的画像摊给乙看。
    """

    @staticmethod
    def _candidates(memory, qq_id):
        """走**生产同一条路**取候选：私聊路径的 candidates 是 memory.recall 的
        Memory 对象，不是 query_memories 的 dict——format_compact_memories 要的是前者。"""
        memory.store.get_or_create_person(qq_id, "测试")
        memory.store.update_person(
            qq_id, notes="成展，大二学生，住在郴州市苏仙区，喜欢画画和特摄。")
        memory.store.insert_memory(
            qq_id, "fact", "在杭州上班工作", importance=6, origin="manual")
        return memory.recall(qq_id, limit=10)

    def test_include_profile_switch_actually_works(self, memory):
        import asyncio
        import types

        from agent.handler import MessageHandler

        candidates = self._candidates(memory, "7777")
        fake = types.SimpleNamespace(
            memory=memory,
            _bge_pick_memories=lambda cands, msg, **kw: cands[:2],
        )
        with_profile, _ = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "7777", candidates, "你还记得我在哪上班吗", include_profile=True))
        without, _ = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "7777", candidates, "你还记得我在哪上班吗", include_profile=False))

        assert "🖼️" in with_profile, "开关打开时画像必须进上下文（私聊）"
        assert "🖼️" not in without, "开关关闭时不许注入（群聊靠它守住）"
        assert protocols.PROFILE_CAVEAT_SHORT in with_profile, "画像必须带准确性标注"

    def test_exactly_one_call_site_injects_profile(self):
        """AST 闸门：必须且只有**一个**调用点注入画像（私聊）。

        防止有人"顺手补齐"把它挪到群聊——那会把甲的画像摊给乙看。
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).parent.parent / "agent" / "handler.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        callers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name == "_build_semantic_memories":
                    kw = {k.arg: k.value for k in node.keywords if k.arg}
                    callers.append(kw.get("include_profile"))
        assert len(callers) == 2, f"调用点数变了：{len(callers)}"
        injected = [c for c in callers if isinstance(c, ast.Constant) and c.value is True]
        assert len(injected) == 1, "必须且只有一个调用点注入画像（私聊）"


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
