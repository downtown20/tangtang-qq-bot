"""2026-10-25 第五轮自查：证据存在 ≠ 语义归属

## 缺陷

`update_memory_evidence`（legacy → verified 那条路）原来只有一道门：
**有被原文逐字支持的 quote**。

但 quote 只证明**这句话说过**，不证明**这条记忆是关于 ta 本人的事实**。
同一句原话可以是：小说情节、玩笑、转述、引用、假设。

现场（2026-10-25 抽样，`memory_reverification_events` 4629 条全量查）：

| 记忆 | 原文内容 | 置信度 | 被升成 |
|---|---|---|---|
| `mem#25510` | 「小说开头设定主角是广告公司策划，凌晨三点改完第十八版方案后猝死（小说情节内容）」 | 0.5 | verified |
| `mem#19337` | 「自称是Ezfic大神」 | 0.5 | verified |
| `mem#11960` | 「在等别人救我」 | 0.5 | verified |
| `mem#8643` | 「独自一人在天台抽烟」 | 0.5 | verified |

共同特征：**confidence < 0.7**（提取它的 LLM 当时就说不确定），而回挂把
「这句话有出处」当成了「这条事实可信」。全库这类共 155 条（<0.6 的 78 条 +
0.6-0.69 的 77 条），它们现在都是 verified、**都在被注入**。

## 口径不一致才是根因

新提取路径 `insert_memory` 对 verified 的要求是：
`evidence_quote 存在` **且** `claim_type == "stated"` **且** `confidence >= 0.75`。

而重验证这条路**从来没有置信度门槛**——同一件事两套判据。
"""
import pytest

TEXT = "我下周要去成都看熊猫"
VALUE = "下周要去成都看熊猫"


def _make(store, *, confidence: float, group: str = "g1", origin: str = "extracted"):
    """造一条 legacy_unverified 记忆 + 一句被逐字支持的原话。

    注意 `origin` 用 UPDATE 改而不是传参：`insert_memory(origin="manual")` 会让
    它直接落成 trusted 等级，"legacy + 非 extracted" 这个组合只能手工构造。
    """
    chat_id = store.insert_chat("40001", TEXT, group, False,
                                timestamp="2026-10-20 12:00:00")
    mid = store.insert_memory(
        "40001", "fact", VALUE, importance=6, confidence=confidence,
        source_group_id=group, timestamp="2026-10-20 12:00:05",
    )
    if origin != "extracted":
        with store._connect() as conn:
            conn.execute("UPDATE memories SET origin=? WHERE id=?", (origin, mid))
            conn.commit()
    return mid, chat_id


def _promote(store, mid: int, chat_id: int, *, group: str = "g1",
             quote: str = TEXT, reviewer: str = "test:round5",
             reason: str = "语义回挂：原话逐字支持"):
    return store.update_memory_evidence(
        mid, str(chat_id), source_group_id=group, evidence_quote=quote,
        allow_legacy_promotion=True, reviewer=reviewer, review_reason=reason,
    )


def _trust(store, mid: int) -> str:
    with store._connect() as conn:
        return str(conn.execute(
            "SELECT trust_level FROM memories WHERE id=?", (mid,)).fetchone()[0])


class TestConfidenceGate:
    def test_low_confidence_is_not_promoted(self, store):
        """0.5 的小说情节不能再被升成 verified——这就是 mem#25510 那条"""
        mid, chat_id = _make(store, confidence=0.5)
        assert _promote(store, mid, chat_id) is False
        assert _trust(store, mid) == "legacy_unverified"

    def test_below_threshold_rejected(self, store):
        mid, chat_id = _make(store, confidence=0.69)
        assert _promote(store, mid, chat_id) is False

    def test_at_threshold_accepted(self, store):
        """0.7 是常规提取的默认置信度，必须放行（否则大量正常记忆被误伤）"""
        mid, chat_id = _make(store, confidence=0.7)
        assert _promote(store, mid, chat_id) is True
        assert _trust(store, mid) == "verified"

    def test_high_confidence_still_promoted(self, store):
        """回归闸门：不能为了挡低置信度把正常路径也关了"""
        mid, chat_id = _make(store, confidence=0.9)
        assert _promote(store, mid, chat_id) is True
        assert _trust(store, mid) == "verified"

    def test_threshold_is_documented_constant(self):
        """门槛必须是具名常量——散在 if 里的魔数改不动也测不到"""
        from agent.store import LEGACY_PROMOTION_MIN_CONFIDENCE
        assert LEGACY_PROMOTION_MIN_CONFIDENCE == 0.7

    def test_gate_is_inside_legacy_branch_only(self):
        """源码闸门：置信度门槛只能卡 legacy 升级那条路——

        已经 verified 的记忆补证据（比如纠正后再补一条引用）不该被置信度挡住，
        那是「补证据」不是「升级」。
        """
        from pathlib import Path
        import ast
        src = (Path(__file__).resolve().parents[1] / "agent" / "store.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "update_memory_evidence":
                fn = node
                break
        assert fn is not None
        # 门槛出现在 is_legacy 的 if 分支内
        found = False
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) \
                    and node.test.id == "is_legacy":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Compare):
                        names = [n.id for n in ast.walk(inner) if isinstance(n, ast.Name)]
                        if "LEGACY_PROMOTION_MIN_CONFIDENCE" in names:
                            found = True
        assert found, (
            "置信度门槛不在 is_legacy 分支里——可能误伤了「已 verified 补证据」的路径"
        )


class TestExistingGatesUnchanged:
    """第五轮只加了一道门，原有四道必须原样——回归保护。"""

    def test_requires_allow_flag(self, store):
        mid, chat_id = _make(store, confidence=0.9)
        assert store.update_memory_evidence(
            mid, str(chat_id), source_group_id="g1", evidence_quote=TEXT) is False

    def test_requires_reviewer(self, store):
        mid, chat_id = _make(store, confidence=0.9)
        assert _promote(store, mid, chat_id, reviewer="") is False

    def test_requires_reason(self, store):
        mid, chat_id = _make(store, confidence=0.9)
        assert _promote(store, mid, chat_id, reason="") is False

    def test_requires_quote_in_source(self, store):
        """引文必须在原文里——编造引文永远过不去（这条是回挂的立身之本）"""
        mid, chat_id = _make(store, confidence=0.9)
        assert _promote(store, mid, chat_id, quote="他下周要去成都看熊猫") is False

    def test_requires_extracted_origin(self, store):
        mid, chat_id = _make(store, confidence=0.9, origin="manual")
        assert _promote(store, mid, chat_id) is False

    def test_already_verified_refill_not_blocked_by_confidence(self, store):
        """已 verified 的记忆补证据：不受置信度门槛影响（它本来就不是升级路径）"""
        chat_id = store.insert_chat("40002", TEXT, "g1", False,
                                    timestamp="2026-10-20 12:00:00")
        mid = store.insert_memory(
            "40002", "fact", VALUE, importance=6, confidence=0.5,
            evidence_quote=TEXT, claim_type="stated", evidence_ids=str(chat_id),
            source_group_id="g1", timestamp="2026-10-20 12:00:05")
        before = _trust(store, mid)
        assert before == "verified", f"造数据失败，trust={before}"
        # 补一条同组证据：is_legacy=False，不该被置信度门槛挡住
        chat2 = store.insert_chat("40002", "成都的熊猫很可爱", "g1", False,
                                  timestamp="2026-10-20 12:01:00")
        ok = store.update_memory_evidence(
            mid, str(chat2), source_group_id="g1",
            evidence_quote="成都的熊猫很可爱",
            allow_legacy_promotion=True, reviewer="test:round5", review_reason="补证据")
        assert ok is True
        assert _trust(store, mid) == "verified"
