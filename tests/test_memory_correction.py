"""批 2 验收：纠正真值闭环（2026-08-16）

事故背景：用户 17:49 纠正「我不爱特摄也不迷时政」，糖糖说「记住了」——
但 30+ 工具全是检索类，零写回，错误记忆原样存活（特摄事故）。
批 2：status 列 + correct_memory/forget_memory 工具 + 权限 + 自忆门控 + 纠正契约。
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from agent import protocols
from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


def _mk_person(store, qq, notes=""):
    store.get_or_create_person(qq, f"用户{qq}")
    if notes:
        store.update_person(qq, notes=notes)


class TestCorrectionService:
    def test_incident_flow_retracts_and_writes_correction(self, memory):
        """事故端到端：撤销旧 like + 写 corrected 事实 + notes 标脏"""
        _mk_person(memory.store, "10001", notes="成展……自称特摄仙人，喜欢特摄……对时政感兴趣。")
        memory.store.insert_memory("10001", "like", "自称特摄仙人，喜欢特摄作品",
                                   importance=5, confidence=0.55)
        memory.store.insert_memory("10001", "like", "对时政方面比较感兴趣",
                                   importance=7, confidence=0.9)

        res = memory.correct_memory("10001", "特摄仙人", "不爱特摄，不是特摄厨")

        assert res["retracted"] >= 1
        assert res["notes_dirty"] is True
        assert res["corrected_id"] > 0

        active = memory.store.query_memories("10001")
        # 旧错误行不再 active
        assert all("特摄仙人" not in m["value"] for m in active if m["key"] == "like")
        # 纠正事实在库、origin/confidence 正确
        corr = [m for m in active if m["key"] == "fact_correction"]
        assert corr and corr[0]["value"] == "不爱特摄，不是特摄厨"
        assert corr[0]["origin"] == "corrected" and corr[0]["confidence"] == 1.0
        # 审计历史保留：include_retracted 可见
        all_rows = memory.store.query_memories("10001", include_retracted=True)
        assert any("特摄仙人" in m["value"] for m in all_rows)
        assert [m for m in all_rows if m["id"] == res["corrected_id"]]

    def test_synthesis_rows_superseded_not_retracted(self, memory):
        """合成行标 superseded——与原始事实区分"""
        memory.store.insert_memory("7001", "profile_synthesis",
                                   "成展自称特摄仙人，对时政感兴趣", importance=4)
        memory.store.insert_memory("7001", "fact_synthesis", "喜欢特摄", importance=4)
        res = memory.correct_memory("7001", "特摄", "不爱特摄")
        assert res["superseded"] == 2 and res["retracted"] == 0
        all_rows = memory.store.query_memories("7001", include_retracted=True)
        syn = [m for m in all_rows if m["key"].endswith("_synthesis")]
        assert all(m["status"] == "superseded" for m in syn)

    def test_cluster_fact_retracted_and_summary_cleared(self, memory):
        cid = memory.store.upsert_fact_cluster("7002", "爱好", "特摄")
        memory.store.update_cluster_summary(cid, "对方喜欢特摄作品")
        fid = memory.store.add_cluster_fact(cid, "7002", "喜欢看特摄")
        res = memory.correct_memory("7002", "特摄", "不爱特摄")
        assert res["retracted"] >= 0 or res["superseded"] >= 0
        facts = memory.store.get_cluster_facts(cid)
        assert all(f["id"] != fid for f in facts)  # active 中已无该事实
        cluster = memory.store.get_fact_clusters("7002")
        assert cluster[0]["summary"] == ""  # 摘要清空

    def test_forget_only_no_correction_written(self, memory):
        memory.store.insert_memory("7003", "like", "喜欢特摄", importance=5)
        res = memory.correct_memory("7003", "喜欢特摄", "")
        assert res["retracted"] == 1 and res["corrected_id"] == 0

    def test_retracted_excluded_from_recall_query(self, memory):
        memory.store.insert_memory("7004", "fact", "喜欢特摄", importance=8)
        memory.correct_memory("7004", "喜欢特摄", "不爱特摄")
        active = memory.store.query_memories("7004")
        # 旧错误行已不可见；只剩纠正事实
        assert all("喜欢特摄" not in m["value"] for m in active)
        assert [m for m in active if m["key"] == "fact_correction"]


class TestToolPermissionAndGating:
    def _handler(self, memory):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.owner_qq = "10001"
        h.memory = memory
        h.embed_engine = None
        return h

    def test_own_fact_correctable_by_self(self, memory):
        memory.store.insert_memory("20001", "like", "自称特摄仙人，喜欢特摄",
                                   importance=5, confidence=0.55)
        h = self._handler(memory)
        ta = {}
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_qq": "20001", "wrong_fact": "特摄仙人", "corrected_fact": "不爱特摄"},
            scope_id="_private_20001", current_user="20001", turn_actions=ta))
        assert "已纠正" in out
        assert ta.get("memory_correction_applied") is True

    def test_third_party_requires_owner(self, memory):
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_qq": "99999", "wrong_fact": "特摄", "corrected_fact": ""},
            scope_id="_private_20002", current_user="20002", turn_actions={}))
        assert "只有主人" in out

    def test_owner_can_correct_third_party(self, memory):
        memory.store.insert_memory("99999", "fact", "喜欢特摄", importance=5)
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "forget_memory",
            {"subject_qq": "99999", "fact": "特摄"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "已忘掉" in out

    def test_empty_user_rejected(self, memory):
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "forget_memory", {"subject_qq": "99999", "fact": "x"},
            scope_id="", current_user="", turn_actions={}))
        assert "无法确认权限" in out


class TestCorrectionContract:
    def test_contract_constant_exists(self):
        assert "correct_memory" in protocols.CORRECTION_CONTRACT
        assert "记住了" in protocols.CORRECTION_CONTRACT

    def test_contract_injected_into_system_prompt(self):
        """契约进入系统提示词（personality 注入）——事故句不命中信号表，必须无条件在场"""
        from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
        p = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖", "糖糖"]))
        prompt = p.build_system_prompt(relationship=Relationship.STRANGER)
        assert protocols.CORRECTION_CONTRACT in prompt

    def test_tools_registered(self):
        """correct_memory/forget_memory 工具定义存在（source 闸门）"""
        import re
        src = open(r"d:/qq-小糖糖/agent/handler.py", encoding="utf-8").read()
        assert '"name": "correct_memory"' in src
        assert '"name": "forget_memory"' in src
        # 执行分支 + 自忆门控在场（防删闸门）
        assert 'name in {"correct_memory", "forget_memory"}' in src
        assert 'turn_actions.get("memory_correction_applied")' in src


class TestImportanceIsNotTruth:
    def test_set_memory_importance_clamps_min_1(self, memory):
        """importance 不能表达「撤销」——setter 钳最小值 1（status 列存在的理由）"""
        mid = memory.store.insert_memory("7005", "fact", "测试", importance=3)
        memory.store.set_memory_importance(mid, 0)
        rows = memory.store.query_memories("7005", include_retracted=True)
        assert rows[0]["importance"] == 1


class TestCodexI1SearchFilters:
    def test_retracted_excluded_from_keyword_search(self, memory):
        """I1 回归：撤销事实不再从全局关键词搜索复活"""
        memory.store.insert_memory("7101", "fact", "喜欢特摄剧", importance=9)
        memory.correct_memory("7101", "喜欢特摄剧", "不爱特摄")
        rows = memory.store.search_memories_by_keyword("特摄")
        assert not any("喜欢特摄剧" in r["value"] for r in rows)


class TestWrongSubjectGate:
    """2026-08-17 事故回归：主人让糖糖纠正「穷到吃外卖」，LLM 只拿到昵称、
    解析不出 QQ，把 subject 填成了当前用户——误撤「大二学生」+ 写入「高一新生」垃圾。
    三道闸门：昵称精确解析 / 解析失败拒绝 / 零匹配拒绝写纠正事实。"""

    def _handler(self, memory):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.owner_qq = "10001"
        h.memory = memory
        h.embed_engine = None
        return h

    def test_subject_name_resolves_exact_nickname(self, memory):
        """事故现场正解：subject_name 精确匹配 people 表 → 纠正落到第三人"""
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        memory.store.insert_memory("20001", "fact", "33岁上班族", importance=5)
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_name": "穷到吃外卖", "wrong_fact": "33岁上班族",
             "corrected_fact": "高一新生"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "已纠正" in out
        active = memory.store.query_memories("20001")
        assert [m for m in active if m["key"] == "fact_correction"]
        # 当前用户（主人）没有任何新写入
        owner_rows = memory.store.query_memories("10001")
        assert not any("高一新生" in m["value"] for m in owner_rows)

    def test_unknown_subject_name_rejected(self, memory):
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_name": "不存在的人", "wrong_fact": "x", "corrected_fact": "y"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "找不到" in out

    def test_zero_match_does_not_write_correction(self, memory):
        """事故回归：wrong_fact 在 subject 记忆里匹配不到任何东西 → 不写纠正事实"""
        memory.store.get_or_create_person("20002", "路人")
        res = memory.correct_memory("20002", "不存在的记忆XYZ", "新事实")
        assert res["matched"] == 0 and res["corrected_id"] == 0
        active = memory.store.query_memories("20002")
        assert not any(m["key"] == "fact_correction" for m in active)

    def test_zero_match_tool_message_asks_confirm(self, memory):
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_qq": "20003", "wrong_fact": "不存在的记忆XYZ", "corrected_fact": "新事实"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "没有找到" in out

    def test_fuzzy_nickname_not_used_for_correction(self, memory):
        """模糊匹配只给查询工具——纠正类破坏性操作只认精确昵称"""
        memory.store.get_or_create_person("20004", "穷到吃外卖")
        out = asyncio.run(self._handler(memory)._execute_tool(
            "correct_memory",
            {"subject_name": "外卖", "wrong_fact": "x", "corrected_fact": "y"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "找不到" in out  # 模糊前缀「外卖」不会被解析成「穷到吃外卖」

    def test_subject_name_at_prefix_normalized(self, memory):
        """2026-08-17 回归：昵称存的是「@忽热忽冷」，LLM 传「忽热忽冷」也要命中"""
        memory.store.get_or_create_person("20005", "@忽热忽冷")
        memory.store.insert_memory("20005", "fact", "在杭州上班", importance=5)
        out = asyncio.run(self._handler(memory)._execute_tool(
            "forget_memory",
            {"subject_name": "忽热忽冷", "fact": "杭州上班"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "已忘掉" in out

    def test_qq_name_conflict_rejected(self, memory):
        """2026-08-17 Codex 全天审查：QQ 与昵称同传必须指向同一人"""
        memory.store.get_or_create_person("20006", "穷到吃外卖")
        h = self._handler(memory)
        out = asyncio.run(h._execute_tool(
            "correct_memory",
            {"subject_qq": "20007", "subject_name": "穷到吃外卖",
             "wrong_fact": "x", "corrected_fact": "y"},
            scope_id="_private_10001", current_user="10001", turn_actions={}))
        assert "不是同一个人" in out


class TestMentionedPeopleResolution:
    """2026-08-17 事故加固：私聊交叉上下文补昵称提及解析——
    LLM 拿到「穷到吃外卖」的 QQ 后，纠正才能落到正确的人头上。"""

    def _handler(self, memory):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.owner_qq = "10001"
        h.bot_qq = "90000"
        h.memory = memory
        h.personality = type("P", (), {"nicknames": ["糖糖"]})()
        return h

    def test_nickname_mention_resolved(self, memory):
        """事故现场：消息里只有昵称没有 QQ → 也能解析出人"""
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("帮我修正一下「穷到吃外卖」的记忆", "10001", 100)
        assert "20001" in out

    def test_qq_digits_still_resolved(self, memory):
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("帮 20001 修正记忆", "10001", 100)
        assert "20001" in out

    def test_short_generic_word_not_resolved(self, memory):
        """2026-08-17 Codex 全天审查：2 字昵称只认显式 @ 提及——
        「我今天吃外卖」不得命中昵称「外卖」"""
        memory.store.get_or_create_person("20001", "外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("我今天吃外卖，好贵", "10001", 100)
        assert out == []

    def test_short_nickname_at_mention_resolved(self, memory):
        memory.store.get_or_create_person("20001", "外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("@外卖 在吗", "10001", 100)
        assert "20001" in out

    def test_at_prefixed_nickname_normalized(self, memory):
        """2026-08-17 Codex 全天审查：库中「@忽热忽冷」与消息「忽热忽冷」对齐"""
        memory.store.get_or_create_person("20001", "@忽热忽冷")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("忽热忽冷刚才说", "10001", 100)
        assert "20001" in out

    def test_order_follows_message_position(self, memory):
        """2026-08-17 Codex 全天审查：最多 3 人按消息出现位置稳定排序"""
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        memory.store.get_or_create_person("20002", "一个包子")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("一个包子比穷到吃外卖先出现", "10001", 100)
        assert out == ["20002", "20001"]

    def test_self_and_bot_excluded(self, memory):
        memory.store.get_or_create_person("10001", "主人")
        memory.store.get_or_create_person("90000", "糖糖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("主人和糖糖都在", "10001", 100)
        assert out == []

    def test_non_owner_low_intimacy_denied(self, memory):
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("穷到吃外卖", "20002", 10)
        assert out == []

    def test_high_intimacy_does_not_grant_third_party_access(self, memory):
        memory.store.get_or_create_person("20001", "穷到吃外卖")
        h = self._handler(memory)
        out = h._resolve_mentioned_people("穷到吃外卖", "20002", 60)
        assert out == []
