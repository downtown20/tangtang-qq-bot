# -*- coding: utf-8 -*-
"""T58 记忆召回预算：裁决权归 LLM（2026-10-15）。

旧契约：注入多少记忆由代码定死（`_bge_pick_memories` 三处硬编码 6 +
`format_compact_memories(max_facts=6)` + `search_memories` 执行器 `results[:3]`），
她只有接收权。这与「系统不替 LLM 做判断」（CLAUDE.md 核心原则）同族——
系统替她决定了「这一轮回忆多少」。

新契约：系统只给一个默认冒上来的量（MEMORY_RECALL_DEFAULT）与安全护栏；
真正的用量由她调 search_memories 的 limit（要几条）/detail（多详细）决定。
设计意图：人的记忆不是一股脑冒出来的——默认浮上来两三条，想深挖自己再回忆。

同批修掉的 D1：synthesize_profile 的行首剥离正则吃掉了年份。
"""
import asyncio
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# ⚠ 不要把模块本身 import 成 memory：与本文件下面的 `memory` fixture 同名，
#   fixture 会在测试函数里**遮蔽**模块名（AttributeError 只有真跑才看得见）。
from agent import protocols
from agent.memory import MemorySystem, _strip_list_marker


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)

_HANDLER_SRC = (Path(__file__).parent.parent / "agent" / "handler.py").read_text(
    encoding="utf-8")


class TestListMarkerStrip:
    """D1：行首剥离正则原来会把 2026年10月5日 啃成 年10月5日。"""

    def test_list_marker_strip_keeps_year(self):
        cases = [
            ("2026年10月5日休学一年后回归学校", "2026年10月5日休学一年后回归学校"),
            ("- 在雷霆服务器当过管理员，高考那年一边备考一边管机房",
             "在雷霆服务器当过管理员，高考那年一边备考一边管机房"),
            ("3. 住在校外", "住在校外"),
            ("19.找一个女朋友", "找一个女朋友"),
            ("1、顿号编号", "顿号编号"),
            ("• 圆点开头", "圆点开头"),
            ("· 中点开头", "中点开头"),
        ]
        for raw, want in cases:
            assert _strip_list_marker(raw) == want, raw

    def test_old_regex_really_ate_the_year(self):
        """反例钉住：旧写法必须确实吃掉年份——否则上面那条是假绿（教训 #30）。"""
        old = re.sub(r'^[\s\-•·\d.]+\s*', '', "2026年10月5日休学一年后回归学校")
        assert old == "年10月5日休学一年后回归学校"


class TestRecallBudgetKnobs:
    """她给的量就是预算；系统的职责只剩默认值与护栏。"""

    def test_default_is_smaller_than_the_old_hardcode(self):
        assert 1 <= protocols.MEMORY_RECALL_DEFAULT < 6

    def test_her_number_is_respected(self):
        assert protocols.clamp_recall_limit(7) == 7
        assert protocols.clamp_recall_limit("12") == 12
        assert protocols.clamp_recall_limit(1) == 1

    def test_missing_or_garbage_falls_back(self):
        assert protocols.clamp_recall_limit(None) == protocols.MEMORY_RECALL_FETCH
        assert protocols.clamp_recall_limit("") == protocols.MEMORY_RECALL_FETCH
        assert protocols.clamp_recall_limit("随便") == protocols.MEMORY_RECALL_FETCH

    def test_ceiling_is_a_guardrail_not_a_budget(self):
        assert protocols.clamp_recall_limit(9999) == protocols.MEMORY_RECALL_MAX
        assert protocols.clamp_recall_limit(-3) == 1

    def test_detail_levels(self):
        assert protocols.normalize_recall_detail("full") == ("full", 0)
        assert protocols.normalize_recall_detail("brief") == ("brief", 40)
        assert protocols.normalize_recall_detail("normal") == ("normal", 90)
        assert protocols.normalize_recall_detail("乱填") == ("normal", 90)
        assert protocols.normalize_recall_detail(None) == ("normal", 90)


class TestRecallHint:
    """告知行只报告事实，且绝大多数轮次不占 token。"""

    def test_reports_rest_and_the_knobs(self):
        hint = protocols.memory_recall_hint(3, 20)
        assert "17" in hint
        assert "search_memories" in hint

    def test_silent_when_nothing_left(self):
        """没有剩余候选 → 空串：不能每轮白占 token。"""
        assert protocols.memory_recall_hint(3, 3) == ""
        assert protocols.memory_recall_hint(3, 0) == ""
        assert protocols.memory_recall_hint(0, 0) == ""

    def test_hint_carries_no_order(self):
        """系统只报告事实，不写指令——是「由你定」，不是「你必须回忆」。"""
        hint = protocols.memory_recall_hint(2, 9)
        for banned in ("必须", "你应该", "请"):
            assert banned not in hint, banned


class TestPassivePushUsesTheBudget:
    """被动注入走默认量，并把「还有多少没拿出来」告诉她。"""

    def _candidates(self, memory, qq_id, n=8):
        memory.store.get_or_create_person(qq_id, "测试")
        for i in range(n):
            memory.store.insert_memory(
                qq_id, "fact", "测试事实编号" + str(i) + "，内容够长以便进候选",
                importance=6, origin="manual")
        return memory.recall(qq_id, limit=50)

    def test_push_size_and_hint(self, memory):
        from agent.handler import MessageHandler

        candidates = self._candidates(memory, "9101")
        seen = {}

        def fake_pick(cands, msg, top_k=None, **kw):
            seen["k"] = top_k
            return cands[:top_k]

        fake = types.SimpleNamespace(memory=memory, _bge_pick_memories=fake_pick)
        text, selected = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "9101", candidates, "随便说点什么吧今天", include_profile=False))

        assert seen["k"] == protocols.MEMORY_RECALL_DEFAULT, "推送条数不再由调用点定死"
        assert len(selected) == protocols.MEMORY_RECALL_DEFAULT
        assert "search_memories" in text, "必须告诉她还有多少没列出来"

    def test_no_candidates_stays_empty(self, memory):
        from agent.handler import MessageHandler
        fake = types.SimpleNamespace(memory=memory, _bge_pick_memories=lambda c, m, **k: c)
        text, selected = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "9202", [], "随便说点什么吧今天"))
        assert text == "" and selected is None


class TestSearchMemoriesTakesHerBudget:
    """工具 schema 与执行器都必须认她的 limit/detail。"""

    def test_keyword_fallback_also_honours_the_budget(self, memory):
        """降级链最底层（无 BGE / 无 Reranker）也必须按预算给量——
        别在兜底路径上把硬编码复活（项目规矩：加分支就补对应路径的回归）。"""
        from agent.handler import MessageHandler
        memory.store.get_or_create_person("9303", "测试")
        # importance 必须 >= 8：注入前有低置信度闸门（重要度>=8 或 置信度>=0.75
        # 或 episodic），用 6 会被整批滤掉，测试就变成在测空列表
        for _ in range(6):
            memory.store.insert_memory(
                "9303", "fact", "喜欢画画和特摄的日常记录", importance=8, origin="manual")
        candidates = memory.recall("9303", limit=50)
        # 真对象而不是 SimpleNamespace：降级链末端要调 self._keyword_pick_memories
        h = object.__new__(MessageHandler)
        h.memory = memory
        h.embed_engine = None
        h.reranker = None

        default_got = MessageHandler._bge_pick_memories(
            h, candidates, "你还喜欢画画吗")
        assert len(default_got) == protocols.MEMORY_RECALL_DEFAULT

        asked_got = MessageHandler._bge_pick_memories(
            h, candidates, "你还喜欢画画吗", 2)
        assert len(asked_got) == 2

        # 阴性对照：真的没有交集时不许硬塞
        none_got = MessageHandler._bge_pick_memories(
            h, candidates, "明天股票行情会涨还是跌")
        assert none_got == []

    def test_the_old_run_matching_was_dead(self):
        """阳性对照：旧写法在同一对文本上交集必为空——否则上面那条是假绿。"""
        import re
        runs = lambda t: set(re.findall(r'[一-鿿\w]{2,}', t.lower()))
        assert runs("你还喜欢画画吗") & runs("喜欢画画和特摄的日常记录") == set()

    def test_schema_exposes_both_knobs(self):
        i = _HANDLER_SRC.index('"name": "search_memories"')
        block = _HANDLER_SRC[i:i + 1500]
        assert '"limit"' in block and '"detail"' in block
        assert "由你自己决定" in block

    def test_executor_uses_her_limit_not_a_hardcoded_three(self):
        i = _HANDLER_SRC.index('if name == "search_memories":')
        block = _HANDLER_SRC[i:i + 3500]
        assert "clamp_recall_limit" in block
        assert "for m in results[:recall_limit]:" in block
        # 注释里允许提到旧写法，但**代码**不许再有硬编码 3
        assert "for m in results[:3]:" not in block, "旧的硬编码 3 条不许复活"
        assert "limit=max(recall_limit, _protocols.MEMORY_RECALL_FETCH)" in block
        assert "normalize_recall_detail" in block

