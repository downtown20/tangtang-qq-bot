# -*- coding: utf-8 -*-
"""纠正优先档注入（2026-10-25）——主人决策「corrected 优先」的机器闸门。

## 背景

`TRUSTED_TRUST_LEVELS` 是**集合**：verified / manual / corrected 平级放行。但同一
件事可能同时存在两条都能注入的记忆——一条是 ta 亲口纠正的结论（corrected），
一条是提取器当时以为对的事实（verified）。注入文本原来把它们写成同一个样子
（都是 💭），「信谁」于是变成她的自由发挥。

现场（2026-10-25 查库）：`mem#17589`（corrected「成展是大学大二学生
（不是高中生）」）与 `mem#18121`（verified「今年7月刚靠指标生名额考上一中，
是高一新生」）属于同一个人、同时 active、同时注入，无任何仲裁——而后者来自
`chat#199048`，那是主人**转述别人**（「穷到吃外卖」）的一句话，被记成了
10001 自己的事实。纠正流程当时只撤掉了 17475/17476，没碰到还处于 legacy
的 18121，10-24 的证据回挂又把它升成了 verified。

主人 2026-10-25 拍板：**corrected 优先**。实现方式是让「哪条被纠正过」可见，
**不替 LLM 判定哪条是真的**：
    corrected → `✏️ 内容`，且排在最前，块尾补一条冲突仲裁契约
    verified  → `💭 内容`（不变）
    legacy    → `💭~ 内容`（模糊档，不变）

## 本文件的三条闸门

1. **标记跟数据走**——双向断言（corrected 必带 ✏️、verified 必不带）。
   单向断言在「全部加标记」的实现下也会绿（教训 #37）。
2. **排序**——corrected 必须排在 verified 前面。构造时故意把顺序反过来传，
   否则「本来就在前面」会让这条测试假绿。
3. **不丢弃**——两组都在时条数必须不变。「优先」是呈现顺序，不是过滤；
   把它做成过滤等于系统替 LLM 判真假（违背单脑架构）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols
from agent.memory import MemorySystem
from agent.store import TRUSTED_TRUST_LEVELS, TRUST_INJECT_PRIORITY


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


def _insert(store, qq_id: str, value: str, *, trust: str,
            importance: int = 8, confidence: float = 0.9,
            key: str = "fact") -> int:
    """插一条记忆并按指定 trust_level 定级（模拟真实存量行列）。"""
    mem_id = store.insert_memory(
        qq_id, key, value, importance=importance, confidence=confidence,
        origin="manual",
    )
    with store._connect() as conn:
        conn.execute("UPDATE memories SET trust_level=? WHERE id=?", (trust, mem_id))
        conn.commit()
    return mem_id


def _format(memory, qq_id, selected):
    return memory.format_compact_memories(
        qq_id, selected=selected, include_profile=False)


class TestPriorityTable:
    """优先级表本身就是判据——配置错了，后面的行为断言全都会跟着错。"""

    def test_corrected_is_strictly_highest(self):
        assert (TRUST_INJECT_PRIORITY["corrected"]
                > TRUST_INJECT_PRIORITY["manual"]
                > TRUST_INJECT_PRIORITY["verified"]), (
            "纠正优先档的位次必须严格递减——平级排序等于没有仲裁")

    def test_every_injectable_level_has_a_rank(self):
        """能在注入里出现的等级都必须有位次，否则它静默落到 0（等于最末）。"""
        for level in TRUSTED_TRUST_LEVELS:
            assert level in TRUST_INJECT_PRIORITY, (
                f"{level} 能进注入但没有排序位次——它会静默落到默认值")


class TestMarkFollowsTheData:
    """闸门 1：标记由 trust_level 决定，双向断言。"""

    def test_corrected_entry_gets_pen_mark(self, memory):
        qq = "7101"
        memory.store.get_or_create_person(qq, "成展")
        _insert(memory.store, qq, "成展是大学大二学生，读湘南学院",
                trust="corrected", key="fact_correction")
        entries = memory.recall(qq, limit=10)
        assert len(entries) == 1, "纠正条目没召回出来，测的就不是这条路径"

        out = _format(memory, qq, entries)
        assert protocols.CORRECTED_MARK in out, "corrected 记忆必须带 ✏️"
        assert f"✏️ 成展是大学大二学生" in out
        assert protocols.CORRECTED_MEMORY_CONTRACT in out, "块尾必须有仲裁契约"
        # 纠正档属于可信档，**不许**被当成模糊档
        assert protocols.UNCERTAIN_MARK not in out, "corrected 不许带 ~"

    def test_verified_entry_has_no_pen_mark(self, memory):
        """阴性对照：verified 不许带 ✏️，也不该出现那段仲裁契约。

        没有这一条，前一半测试在「全都加 ✏️」的实现下也会绿（教训 #37）。
        """
        qq = "7102"
        memory.store.get_or_create_person(qq, "成展")
        _insert(memory.store, qq, "成展今年7月刚考上一中", trust="verified")
        entries = memory.recall(qq, limit=10)
        assert len(entries) == 1

        out = _format(memory, qq, entries)
        assert "  💭 成展今年7月刚考上一中" in out
        assert protocols.CORRECTED_MARK not in out
        assert protocols.CORRECTED_MEMORY_CONTRACT not in out, (
            "没有纠正行时不许注入契约——每轮白占 tokens")

    def test_is_corrected_only_for_corrected_level(self, memory):
        for level, expected in (
            ("corrected", True),
            ("verified", False),
            ("manual", False),
            ("legacy_unverified", False),
            ("unverified", False),
            (None, False),
        ):
            entry = type("M", (), {"key": "fact", "value": "某件事",
                                   "trust_level": level})
            assert MemorySystem.is_corrected(entry) is expected, level

    def test_is_corrected_does_not_trust_origin(self, memory):
        """`origin='corrected'` 但 trust_level 被降级时，必须不再当纠正档。

        为什么钉这条：`origin` 记「这条怎么来的」，`trust_level` 记「它现在有
        多可信」。两个字段分查就会漂移——注入层的唯一判定源必须是 trust_level
        （与 is_uncertain 同一口径）。
        """
        entry = type("M", (), {"key": "fact_correction", "value": "某件事",
                               "origin": "corrected", "trust_level": "legacy_unverified"})
        assert MemorySystem.is_corrected(entry) is False


class TestOrderingAndNoDrop:
    """闸门 2 + 3：corrected 排前，且一条都不丢。"""

    def _seed(self, memory, qq):
        memory.store.get_or_create_person(qq, "成展")
        _insert(memory.store, qq, "成展今年7月刚考上一中，是高一新生",
                trust="verified", importance=9, confidence=0.95)
        _insert(memory.store, qq, "成展在杭州做过外卖骑手",
                trust="verified", importance=8, confidence=0.9)
        _insert(memory.store, qq, "成展是大学大二学生，不是高中生",
                trust="corrected", importance=8, confidence=1.0,
                key="fact_correction")
        entries = memory.recall(qq, limit=10)
        assert len(entries) == 3
        return entries

    def test_corrected_line_comes_first(self, memory):
        """故意把 corrected 放到传入列表**最后**——排序不生效就会露馅。"""
        qq = "7201"
        entries = self._seed(memory, qq)
        # 原始顺序：让 corrected 永远在最后一位，与「理想顺序」相反
        ordered = [m for m in entries if not MemorySystem.is_corrected(m)] + \
                  [m for m in entries if MemorySystem.is_corrected(m)]
        assert MemorySystem.is_corrected(ordered[-1]), "构造失败：corrected 不在末尾"

        out = _format(memory, qq, ordered)
        lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
        pen_idx = [i for i, ln in enumerate(lines) if ln.startswith(protocols.CORRECTED_MARK)]
        dot_idx = [i for i, ln in enumerate(lines) if ln.startswith("💭")]
        assert pen_idx, f"没有 ✏️ 行：\n{out}"
        assert dot_idx, f"没有 💭 行：\n{out}"
        assert max(pen_idx) < min(dot_idx), (
            f"纠正行没排到最前（✏️ 在 {pen_idx}，💭 在 {dot_idx}）：\n{out}")

    def test_nothing_is_dropped(self, memory):
        """「优先」是呈现顺序，不是过滤——两条互斥记忆必须都还在。"""
        qq = "7202"
        entries = self._seed(memory, qq)
        out = _format(memory, qq, entries)

        assert "成展是大学大二学生，不是高中生" in out, "纠正结论被过滤掉了"
        assert "成展今年7月刚考上一中，是高一新生" in out, (
            "旧的 verified 说法被系统替 LLM 过滤掉了——那等于系统判真假")
        assert "成展在杭州做过外卖骑手" in out
        # 三行的前缀各一份，不多不少。
        # ⚠ 必须**按行首**统计而不是全文 count：契约文本里也含 ✏️（它在解释
        # 这个图标什么意思），全文 count 会把契约那两处一起数进去——那是
        # 「数了实现细节」而不是「数了行」。
        lines = [ln.strip() for ln in out.split("\n")]
        pen_lines = [ln for ln in lines if ln.startswith(protocols.CORRECTED_MARK)]
        dot_lines = [ln for ln in lines if ln.startswith("💭")]
        assert len(pen_lines) == 1, f"纠正行应恰好 1 条，实际 {len(pen_lines)}：{pen_lines}"
        assert len(dot_lines) == 2, f"verified 行应恰好 2 条，实际 {len(dot_lines)}：{dot_lines}"

    def test_manual_sorts_between_corrected_and_verified(self, memory):
        """manual（人工录入）位次在 corrected 与 verified 之间——三档都要能被区分。"""
        qq = "7203"
        memory.store.get_or_create_person(qq, "小明")
        _insert(memory.store, qq, "小明养了一只叫豆豆的猫",
                trust="verified", importance=8)
        _insert(memory.store, qq, "小明的生日是3月5日",
                trust="manual", importance=8)
        _insert(memory.store, qq, "小明不叫小明，叫小明同学",
                trust="corrected", importance=8, key="fact_correction")
        entries = memory.recall(qq, limit=10)
        out = _format(memory, qq, entries)
        lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
        pen = next(i for i, ln in enumerate(lines) if ln.startswith(protocols.CORRECTED_MARK))
        dot = next(i for i, ln in enumerate(lines) if ln.startswith("💭"))
        assert pen < dot


class TestMarkSurvivesIntoBackgroundBlock:
    """标记必须活到最终注入的背景块里——只在中间态出现等于没修。"""

    def test_mark_reaches_memory_block(self, memory):
        qq = "7301"
        memory.store.get_or_create_person(qq, "成展")
        _insert(memory.store, qq, "成展是大学大二学生，不是高中生",
                trust="corrected", key="fact_correction")
        entries = memory.recall(qq, limit=10)
        text = _format(memory, qq, entries)

        block = protocols.memory_block("成展", text)
        assert protocols.CORRECTED_MARK in block, "标记没活到背景块 = 她看不到"
        assert protocols.CORRECTED_MEMORY_CONTRACT in block
