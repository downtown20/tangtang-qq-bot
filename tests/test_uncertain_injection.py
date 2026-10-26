# -*- coding: utf-8 -*-
"""不确定性三档注入（2026-10-24）——三向闸�� + 独立配额。

方案：`docs/开发规划/不确定性注入_技术方案_20261024.md`。

要解决的：信任门此前是**二值**（verified/manual/corrected 注入，其余静默丢弃），
10,060 条活跃���忆在对话里完全不存在。真实的人会说「我印象里你提过去医院，
是不是我记错了」——那是元认知。补上中间那一档：可信 → `💭 内容`，
模糊 → `💭~ 内容` + 块尾行为契约，retracted 仍不注入。

本文件的三条闸门对应用户可观察的行为，不是内部实现：
1. 标记必须**跟着数据走**（可信不带、模糊必带）——双向断言，防「全都加」
   和「全都不加」两种假绿；
2. 模糊路有独立配额，**不挤占**可信路（构造 30 可信 + 30 模糊）；
3. 群聊恒为二值门（只开私聊）——这是隐私边界，不是可调参数。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols
from agent.memory import MemorySystem

_HANDLER_SRC = (Path(__file__).parent.parent / "agent" / "handler.py").read_text(
    encoding="utf-8")


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


def _set_trust(store, memory_id: int, level: str) -> None:
    """插一条记忆并强改 trust_level（模拟 legacy 存量）。"""
    with store._connect() as conn:
        conn.execute("UPDATE memories SET trust_level=? WHERE id=?", (level, memory_id))
        conn.commit()


def _insert(store, qq_id: str, value: str, *, trust: str,
            importance: int = 6, confidence: float = 0.8,
            key: str = "fact") -> int:
    """按指定 trust_level 插入记忆，返回 id。"""
    mem_id = store.insert_memory(
        qq_id, key, value, importance=importance, confidence=confidence,
        origin="manual",
    )
    _set_trust(store, mem_id, trust)
    return mem_id


class TestMarkFollowsTheData:
    """闸门 1：标记由 trust_level 决定，双向断言。"""

    def test_uncertain_entry_gets_tilde(self, memory):
        memory.store.get_or_create_person("6001", "测试")
        _insert(memory.store, "6001", "在杭州做过一段外卖骑手", trust="legacy_unverified")
        candidates = memory.recall_uncertain("6001", limit=10)
        assert candidates, "模糊档召回为空，测的就不是这条路径"

        out = memory.format_compact_memories("6001", selected=candidates)
        assert protocols.UNCERTAIN_MARK in out, "模糊记忆必须带 ~ 标记"
        assert protocols.UNCERTAIN_MEMORY_CONTRACT in out, "块尾必须有行为契约"
        # 反向：不能是「💭 在杭州做过…」那种平铺直叙
        assert "  💭 在杭州做过一段外卖骑手" not in out

    def test_trusted_entry_has_no_tilde(self, memory):
        """阴性对照：可信记忆不许带 ~，也不该出现那段契约。

        没有这一条，第一半测试在「全部加标记」的实现下也会绿（教训 #37）。
        """
        memory.store.get_or_create_person("6002", "测试")
        _insert(memory.store, "6002", "在杭州上班，做前端开发", trust="verified")
        candidates = memory.recall("6002", limit=10)
        assert candidates

        out = memory.format_compact_memories("6002", selected=candidates)
        assert "  💭 在杭州上班，做前端开发" in out
        assert protocols.UNCERTAIN_MARK not in out
        assert protocols.UNCERTAIN_MEMORY_CONTRACT not in out, (
            "没有模糊行时不许注入契约——每轮白占 tokens")

    def test_null_trust_level_counts_as_uncertain(self, memory):
        """NULL 等同 legacy（口径字典 §二）——fail-closed 的方向是「说得含糊」。"""
        entry = type("M", (), {"key": "fact", "value": "某件事", "trust_level": None})
        assert MemorySystem.is_uncertain(entry) is True

    def test_trusted_levels_are_not_uncertain(self, memory):
        for level in ("verified", "manual", "corrected"):
            entry = type("M", (), {"key": "fact", "value": "某件事", "trust_level": level})
            assert MemorySystem.is_uncertain(entry) is False, level


class TestStoreGate:
    """反选条件与放行集合严格互补；互斥参数必须抛错。"""

    def test_uncertain_only_is_complement_of_trusted(self, store):
        _insert(store, "6101", "可信的事实陈述内容", trust="verified")
        _insert(store, "6101", "模糊的历史残片内容", trust="legacy_unverified")
        _insert(store, "6101", "中间态的不确定内容", trust="unverified")

        trusted = {r["id"] for r in store.query_memories("6101", trusted_only=True)}
        unsure = {r["id"] for r in store.query_memories("6101", uncertain_only=True)}
        assert trusted and unsure
        assert trusted & unsure == set(), "两档重叠 = 同一行既能确定又模糊"
        all_ids = {r["id"] for r in store.query_memories("6101")}
        assert trusted | unsure == all_ids, "两档必须覆盖全部活跃行（漏了的行等于消失）"

    def test_both_flags_raise_instead_of_returning_empty(self, store):
        """同时为真必须抛错：静默空集会被读成「这个人没有记忆」。"""
        with pytest.raises(ValueError):
            store.query_memories("6102", trusted_only=True, uncertain_only=True)

    def test_quality_gates_are_pushed_into_sql(self, memory):
        """门槛必须在 SQL 里收窄，不是取回来再筛（噪声会吃满窗口）。"""
        memory.store.get_or_create_person("6103", "测试")
        _insert(memory.store, "6103", "低重要度碎片内容", trust="legacy_unverified",
                importance=2)
        _insert(memory.store, "6103", "低置信度可疑内容", trust="legacy_unverified",
                importance=9, confidence=0.5)
        _insert(memory.store, "6103", "达标的高质量旧事", trust="legacy_unverified",
                importance=7, confidence=0.9)

        got = memory.recall_uncertain("6103", limit=10)
        values = [m.value for m in got]
        assert "达标的高质量旧事" in values
        assert "低重要度碎片内容" not in values
        assert "低置信度可疑内容" not in values


class TestQuotaDoesNotCrowdOut:
    """闸门 2：模糊不许挤掉可信（方案 3.2 的核心诉求）。"""

    @staticmethod
    def _seed(memory, qq_id, n_trusted=30, n_uncertain=30):
        memory.store.get_or_create_person(qq_id, "测试")
        trusted_ids = set()
        for i in range(n_trusted):
            trusted_ids.add(_insert(
                memory.store, qq_id, f"可信事实第{i}条，内容足够长以便进候选窗口",
                trust="verified", importance=8, confidence=1.0))
        for i in range(n_uncertain):
            _insert(
                memory.store, qq_id, f"模糊旧事第{i}条，内容足够长以便进候选窗口",
                trust="legacy_unverified", importance=8, confidence=1.0)
        return trusted_ids

    def test_trusted_recall_never_contains_uncertain(self, memory):
        self._seed(memory, "6201")
        trusted = memory.recall("6201", limit=25)
        assert len(trusted) == 25, "可信路必须仍能取满 25 条"
        assert all(not MemorySystem.is_uncertain(m) for m in trusted), (
            "可信路里混进了模糊记忆 = 两池混排，可信档被注水")

    def test_uncertain_recall_is_capped_at_its_own_quota(self, memory):
        self._seed(memory, "6202")
        unsure = memory.recall_uncertain("6202",
                                         limit=protocols.UNCERTAIN_RECALL_LIMIT)
        assert len(unsure) <= protocols.UNCERTAIN_RECALL_LIMIT
        assert all(MemorySystem.is_uncertain(m) for m in unsure)

    def test_merge_keeps_trusted_first_and_uncertain_separate(self, memory):
        """两路合并：可信在前、模糊在后，且返回值里的 selected 只含可信。"""
        import asyncio
        import types

        from agent.handler import MessageHandler

        self._seed(memory, "6203")
        trusted = memory.recall("6203", limit=25)
        unsure = memory.recall_uncertain(
            "6203", limit=protocols.UNCERTAIN_RECALL_LIMIT)
        assert trusted and unsure

        def fake_pick(cands, msg, top_k=None, **kw):
            return cands[:top_k]

        fake = types.SimpleNamespace(memory=memory, _bge_pick_memories=fake_pick)
        text, selected = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "6203", trusted, "你还记得我以前说过什么吗",
            include_profile=False, uncertain_candidates=unsure))

        assert len(selected or []) == protocols.MEMORY_RECALL_DEFAULT, (
            "selected 只装可信条目，条数不受模糊路影响（它被当 trusted_memory_ids 用）")
        assert all(not MemorySystem.is_uncertain(m) for m in (selected or [])), (
            "模糊条目混进 selected = 伪造证据等级")

        # ⚠ 这里钉**字面 2**，不引用 protocols.UNCERTAIN_INJECT_MAX：
        # 断言如果跟着常量走，「把常量调大」的变异会让断言一起放宽 = 假绿
        # （教训 #37 形态 ②：判据测的是环境/配置，不是行为）。
        n_uncertain_lines = text.count(protocols.UNCERTAIN_MARK)
        assert n_uncertain_lines <= 2, (
            f"模糊注入 {n_uncertain_lines} 条，超出独立配额（上限定为 2）")
        assert n_uncertain_lines >= 1, "模糊候选非空却没注入任何一条 = 这条路没接上"

        # 顺序即优先级：可信行（💭 但非 💭~）必须全部出现在模糊行之前。
        # 没有这条断言，「合并顺序反转」的变异会假绿——顺序反了不改变条数。
        first_uncertain = text.index(protocols.UNCERTAIN_MARK)
        trusted_lines = [i for i, line in enumerate(text.split("\n"))
                         if line.strip().startswith("💭")
                         and not line.strip().startswith(protocols.UNCERTAIN_MARK)]
        assert trusted_lines, "可信行一条都没有，测不出顺序"
        uncertain_line_no = text[:first_uncertain].count("\n")
        assert max(trusted_lines) < uncertain_line_no, (
            "模糊行排到了可信行前面——顺序反转会让「可能记错」的东西先入眼")

    def test_without_uncertain_candidates_nothing_changes(self, memory):
        """关掉开关（不传模糊候选）时，行为与改造前一致——回滚靠它。"""
        import asyncio
        import types

        from agent.handler import MessageHandler

        self._seed(memory, "6204")
        trusted = memory.recall("6204", limit=25)
        fake = types.SimpleNamespace(
            memory=memory, _bge_pick_memories=lambda c, m, **k: c[:k.get("top_k")])
        text, selected = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "6204", trusted, "你还记得我以前说过什么吗", include_profile=False))
        assert protocols.UNCERTAIN_MARK not in text
        assert protocols.UNCERTAIN_MEMORY_CONTRACT not in text


class TestPrivateOnly:
    """闸门 3：只开私聊——群聊恒为二值门（隐私边界）。"""

    def test_group_path_never_passes_uncertain_candidates(self):
        """AST 闸门：_build_semantic_memories 的两个调用点里，
        只有私聊那个能传 uncertain_candidates。"""
        import ast

        tree = ast.parse(_HANDLER_SRC)
        callers = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name == "_build_semantic_memories":
                    kw = {k.arg: k.value for k in node.keywords if k.arg}
                    callers.append(kw)
        assert len(callers) == 2, f"调用点数变了：{len(callers)}"
        with_uncertain = [c for c in callers if "uncertain_candidates" in c]
        assert len(with_uncertain) == 1, "必须且只有一个调用点传模糊候选（私聊）"
        assert isinstance(with_uncertain[0]["include_profile"], ast.Constant)
        assert with_uncertain[0]["include_profile"].value is True, (
            "传模糊候选的那个调用点必须是 include_profile=True（私聊）")

    def test_switch_is_an_explicit_boolean(self):
        """开关必须是显式布尔：`config.get(..., False)` 的默认值只兜「键不存在」，
        不能兜「键写成了字符串 "false"」——那种情况 bool("false") 是 True，
        等于配置写反了却静默开启（教训 #11 同族：破坏性开关不许含糊）。"""
        import yaml

        cfg = yaml.safe_load(
            (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
        value = (cfg.get("memory") or {}).get("uncertain_injection")
        assert isinstance(value, bool), f"开关必须是 true/false 字面量，实际是 {value!r}"

    def test_group_recall_uses_trusted_only(self):
        """群聊召回必须仍带 trusted_only=True（存量契约）。"""
        i = _HANDLER_SRC.index("memory.group_recall")
        block = _HANDLER_SRC[i:i + 900]
        assert "trusted_only" not in block or "trusted_only=True" in block
        assert "uncertain" not in block, "群聊召回不许接模糊路"


class TestEndToEndPrivateTurn:
    """端到端场景：一次私聊回合，注入块真实长什么样。

    闸门测试证明「条件满足时标记会出现」，这里证明**真实场景下它确实出现**，
    并且关掉开关后逐字节回到旧行为（回滚路径不是纸面上的）。"""
    MSG = "豆豆这只猫是从哪里领养的你还记得吗"

    def _seed(self, store, qq_id):
        store.get_or_create_person(qq_id, "小明")
        _insert(store, qq_id, "小明养了一只叫豆豆的猫",
                trust="verified", importance=8, confidence=0.9)
        _insert(store, qq_id, "小明在杭州做前端开发工作",
                trust="verified", importance=8, confidence=0.9)
        _insert(store, qq_id, "豆豆这只猫是从流浪站领养的",
                trust="legacy_unverified", importance=8, confidence=0.9)

    def test_private_turn_injects_uncertain_line(self, memory):
        import asyncio
        import types

        from agent.handler import MessageHandler

        self._seed(memory.store, "6301")
        trusted = memory.recall("6301", limit=25, query_text=self.MSG)
        unsure = memory.recall_uncertain(
            "6301", limit=protocols.UNCERTAIN_RECALL_LIMIT, query_text=self.MSG)
        assert unsure, "模糊档没召回出来，这个场景就白测了"

        def fake_pick(cands, msg, top_k=None, **kw):
            return cands[:top_k]

        fake = types.SimpleNamespace(memory=memory, _bge_pick_memories=fake_pick)
        text, _ = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "6301", trusted, self.MSG,
            include_profile=False, uncertain_candidates=unsure))

        assert protocols.UNCERTAIN_MARK in text
        assert protocols.UNCERTAIN_MEMORY_CONTRACT in text
        # 记忆块最终是要过 memory_block() 进背景池的——确认标记能活到那里
        block = protocols.memory_block("小明", text)
        assert protocols.UNCERTAIN_MARK in block, "标记必须活到最终注入的背景块里"

    def test_off_switch_is_byte_identical_to_old_behaviour(self, memory):
        """关掉开关 = 与改造前逐字节一致（回滚不是纸面上的）。"""
        import asyncio
        import types

        from agent.handler import MessageHandler

        self._seed(memory.store, "6302")
        trusted = memory.recall("6302", limit=25, query_text=self.MSG)

        def fake_pick(cands, msg, top_k=None, **kw):
            return cands[:top_k]

        fake = types.SimpleNamespace(memory=memory, _bge_pick_memories=fake_pick)
        text, _ = asyncio.run(MessageHandler._build_semantic_memories(
            fake, "6302", trusted, self.MSG, include_profile=False))

        assert protocols.UNCERTAIN_MARK not in text
        assert protocols.UNCERTAIN_MEMORY_CONTRACT not in text
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue  # 空行不是内容行
            assert stripped.startswith(("📋", "🖼️", "💭", "⚠️")), (
                f"出现了新形态的行：{line!r}")
