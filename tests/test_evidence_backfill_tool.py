"""严格子串回挂工具（P2 立项研究 · 第一期）的验收。

验收标准来自立项文档：工具幂等（重跑不重复写）；命中只发生在"原文原样出现"
的情况；每条落库都有留痕；回滚可用。

为什么用 importlib 而不是 `from tools.回挂记忆证据 import ...`：
中文模块名在 Windows 上依赖文件系统编码与 locale（本仓 shell 是 Git Bash /
pwsh，历史上已经踩过 GBK 解码的坑，教训 #37 形态 ③）。文件路径加载绕开这件事，
测的还是同一份代码。
"""

import importlib.util
from pathlib import Path

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "回挂记忆证据.py"
_spec = importlib.util.spec_from_file_location("evidence_backfill", _TOOL_PATH)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


def _run(store, *, apply: bool, batch: str) -> dict:
    """整库跑一轮。fixture 是空库，所以库里只有本用例造的数据。"""
    return backfill.run(store, apply=apply, batch=batch, qq="", limit=0)


def _revisions(store, memory_id: int) -> list[tuple]:
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT actor, field, before_value, after_value FROM memory_revisions "
            "WHERE entity='memories' AND entity_id=? ORDER BY id", (memory_id,)
        ).fetchall()]


def _reverifications(store, memory_id: int) -> list[tuple]:
    with store._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT reviewer, previous_trust_level, new_trust_level "
            "FROM memory_reverification_events WHERE memory_id=? ORDER BY id",
            (memory_id,)).fetchall()]


def _evidence_count(store) -> int:
    with store._connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0])


def test_apply_lands_evidence_and_leaves_trail(store):
    """命中 → 写证据 + 升 trusted + 两条留痕（流水 + 复核事件）都在。"""
    chat_id = store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1",
                                is_bot=False, timestamp="2026-10-20 12:00:00")
    memory_id = store.insert_memory(
        "40001", "fact", "下周要去成都看熊猫", importance=6,
        source_group_id="g1", timestamp="2026-10-20 12:00:05")

    result = _run(store, apply=True, batch="b1")

    assert result["attempted"] == 1
    assert result["matched"] == 1
    assert result["landed"] == 1
    assert result["rejected_by_validator"] == 0
    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "verified"
    assert store.get_memory_evidence(memory_id) == [
        {"chat_id": chat_id, "relation": "supports"}]
    actors = [item for item in _revisions(store, memory_id)
              if item[0] == "backfill:evidence:b1"]
    assert {item[1] for item in actors} >= {"evidence_ids", "trust_level"}
    assert _reverifications(store, memory_id) == [
        ("backfill:substring:b1", "legacy_unverified", "verified")]


def test_second_run_is_idempotent(store):
    """重跑不重复写：候选查询本身就排除已可信的行。"""
    store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1", is_bot=False)
    store.insert_memory("40001", "fact", "下周要去成都看熊猫", importance=6,
                        source_group_id="g1")
    first = _run(store, apply=True, batch="b1")
    assert first["landed"] == 1
    second = _run(store, apply=True, batch="b1")
    assert second["attempted"] == 0
    assert second["landed"] == 0
    assert _evidence_count(store) == 1


def test_dry_run_writes_nothing(store):
    """默认预演：只报告会挂多少，一条都不写。"""
    store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1", is_bot=False)
    memory_id = store.insert_memory("40001", "fact", "下周要去成都看熊猫",
                                    importance=6, source_group_id="g1")
    result = _run(store, apply=False, batch="b1")
    assert result["matched"] == 1
    assert result["landed"] == 0
    assert result["samples"][0]["dry_run"] == "would_land"
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"
    assert _evidence_count(store) == 0
    assert _revisions(store, memory_id) == []


def test_dry_run_prediction_matches_apply(store):
    """预演的数字必须能预测真跑的结果——否则 dry-run 就是在骗人。"""
    store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1", is_bot=False)
    store.insert_memory("40001", "fact", "下周要去成都看熊猫", importance=6,
                        source_group_id="g1")
    preview = _run(store, apply=False, batch="p1")
    applied = _run(store, apply=True, batch="p2")
    assert preview["matched"] == applied["landed"]


def test_same_sentence_across_two_groups_is_ambiguous(store):
    """同一句话在两个群出现过 → 来源无法确定 → 不挂（宁可少挂，不可挂错）。"""
    store.insert_chat("40002", "我们明天去看电影吧", group_id="g1", is_bot=False)
    store.insert_chat("40002", "我们明天去看电影吧", group_id="g2", is_bot=False)
    memory_id = store.insert_memory("40002", "fact", "明天去看电影", importance=5)
    result = _run(store, apply=True, batch="b1")
    assert result["ambiguous"] == 1
    assert result["landed"] == 0
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"
    assert _evidence_count(store) == 0


def test_declared_group_mismatch_is_not_linked(store):
    """记忆声明来自 g2，原话却只在 g1 出现 → 不是同一条语境，不挂。"""
    store.insert_chat("40003", "我养了一只叫豆豆的橘猫", group_id="g1", is_bot=False)
    memory_id = store.insert_memory("40003", "fact", "养了一只叫豆豆的橘猫",
                                    importance=5, source_group_id="g2")
    result = _run(store, apply=True, batch="b1")
    assert result["unmatched"] == 1
    assert result["landed"] == 0
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"


def test_bot_reply_is_not_evidence(store):
    """只有对方说的话能给记忆当证据——糖糖自己说过的话不能自证。"""
    store.insert_chat("40004", "我住在杭州西湖边", is_bot=True)
    memory_id = store.insert_memory("40004", "fact", "住在杭州西湖边", importance=5)
    result = _run(store, apply=True, batch="b1")
    assert result["unmatched"] == 1
    assert result["landed"] == 0
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"


def test_short_anchor_is_refused():
    """太短的"记忆"（<6 字）在几万条闲聊里必然出现，不能当锚点。"""
    assert backfill.MIN_ANCHOR_CHARS == 6
    messages = [{"id": 1, "group_id": "", "timestamp": "",
                 "message": "喜欢猫", "norm": "喜欢猫"}]
    plan = backfill.match_memory({"value": "喜欢猫"}, messages)
    assert plan["status"] == "too_short"


def test_rollback_restores_legacy_and_removes_evidence(store):
    """按批次可撤销：trust 打回上一档、证据行删掉，且重复撤销无副作用。"""
    chat_id = store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1",
                                is_bot=False)
    memory_id = store.insert_memory("40001", "fact", "下周要去成都看熊猫",
                                    importance=6, source_group_id="g1")
    assert _run(store, apply=True, batch="b9")["landed"] == 1
    assert store.get_memory_evidence(memory_id) == [
        {"chat_id": chat_id, "relation": "supports"}]

    result = backfill.rollback(store, batch="b9", keep_evidence=False)

    assert result["candidates"] == 1
    assert result["rolled_back"] == 1
    assert result["evidence_removed"] == 1
    assert result["evidence_ids_restored"] == 1
    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "legacy_unverified"
    # evidence_ids 也必须还原：否则留下「legacy 记忆却挂着证据 CSV」的不一致
    assert row["evidence_ids"] == ""
    assert store.get_memory_evidence(memory_id) == []
    again = backfill.rollback(store, batch="b9", keep_evidence=False)
    assert again["rolled_back"] == 0


def test_rollback_only_touches_its_own_batch(store):
    """批次隔离：撤销 b2 不能动 b1 提升的行。

    批次是"每次运行"的概念——必须在 b1 跑完**之后**才插第二条候选，
    否则 b1 会把它一起收走（那测的就不是隔离，而是"b2 没活可干"）。
    """
    store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1", is_bot=False)
    first = store.insert_memory("40001", "fact", "下周要去成都看熊猫",
                                importance=6, source_group_id="g1")
    assert _run(store, apply=True, batch="b1")["landed"] == 1

    store.insert_chat("40002", "我养了一只叫豆豆的橘猫", group_id="g1", is_bot=False)
    second = store.insert_memory("40002", "fact", "养了一只叫豆豆的橘猫",
                                 importance=6, source_group_id="g1")
    assert _run(store, apply=True, batch="b2")["landed"] == 1

    backfill.rollback(store, batch="b2", keep_evidence=False)

    assert store.get_memory_by_id(first)["trust_level"] == "verified"
    assert store.get_memory_by_id(second)["trust_level"] == "legacy_unverified"


def test_rollback_keeps_preexisting_evidence_rows(store):
    """回滚只删本批次插入的行——回挂之前就存在的同 (memory, chat) 证据行必须留着。

    实测副本上 2038 条里就有 8 条属于这种情况（legacy 行早带着一条审计线索
    关系行）。无条件按 (memory, chat) 删会把它们一起清掉，那就不是"撤销这一批"
    而是"删掉不属于它的数据"。判据是 created_at 与本次复核时间一致。
    """
    chat_id = store.insert_chat("40001", "我下周要去成都看熊猫", group_id="g1",
                                is_bot=False)
    memory_id = store.insert_memory("40001", "fact", "下周要去成都看熊猫",
                                    importance=6, source_group_id="g1")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_evidence (memory_id, chat_id, relation, created_at) "
            "VALUES (?,?,'supports','2026-01-01 00:00:00')",
            (memory_id, chat_id),
        )
        conn.commit()

    assert _run(store, apply=True, batch="b7")["landed"] == 1
    result = backfill.rollback(store, batch="b7", keep_evidence=False)

    assert result["rolled_back"] == 1
    assert result["evidence_removed"] == 0      # INSERT OR IGNORE：本次没新增
    assert result["evidence_ids_restored"] == 1
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"
    assert store.get_memory_by_id(memory_id)["evidence_ids"] == ""
    assert store.get_memory_evidence(memory_id) == [
        {"chat_id": chat_id, "relation": "supports"}]


def test_min_importance_filter_skips_low_importance(store):
    """小批次试跑：`min_importance` 之外的行一条都不许动。

    这条防的是"参数接了但没进 SQL"——那种情况下所有候选都会被处理，
    而界面上只会显示"跑成功"，看不出批次实际放大了。
    """
    store.insert_chat("40005", "我最近在准备考研复试", group_id="g1", is_bot=False)
    low = store.insert_memory("40005", "fact", "最近在准备考研复试", importance=5,
                              source_group_id="g1")
    high = store.insert_memory("40005", "fact", "准备考研复试", importance=9,
                               source_group_id="g1")

    result = backfill.run(store, apply=True, batch="b8", qq="", limit=0,
                          min_importance=8)

    assert result["min_importance"] == 8
    assert result["attempted"] == 1
    assert store.get_memory_by_id(high)["trust_level"] == "verified"
    assert store.get_memory_by_id(low)["trust_level"] == "legacy_unverified"


def test_rollback_restores_source_group_id(store):
    """回挂会顺带把 source_group_id 确定下来——回滚必须还原，否则 legacy 记忆被钉在群里。

    为什么这条重要：`update_memory_evidence` 对 scope 为空的 legacy 行会按证据消息
    的群补上 scope（"作用域随之确定"）。实测生产库首批 411 条里有 361 条属于这种。
    回滚如果只降 trust_level，这些记忆就永久挂在某个群上——虽然 legacy 不会被召回，
    但「回滚」这个词就不该有残留。
    """
    store.insert_chat("40006", "我明天要去公司面试", group_id="g1", is_bot=False)
    memory_id = store.insert_memory("40006", "fact", "明天要去公司面试", importance=7)

    assert _run(store, apply=True, batch="b10")["landed"] == 1
    assert store.get_memory_by_id(memory_id)["source_group_id"] == "g1"

    result = backfill.rollback(store, batch="b10", keep_evidence=False)

    assert result["rolled_back"] == 1
    assert result["source_group_restored"] == 1
    assert store.get_memory_by_id(memory_id)["source_group_id"] == ""


def test_rollback_accepts_semantic_batch_prefix(store):
    """语义回挂用 `backfill:semantic:` 前缀写 reviewer——回滚必须也认它。

    两个工具共享同一个「按批次撤销」的能力。只认 substring 前缀的话，语义批次
    一写就撤不掉——那等于「可回滚」只对一半批次数成立（注释里却写着都能撤）。
    """
    chat_id = store.insert_chat("40007", "我最近在学吉他", group_id="g1", is_bot=False)
    memory_id = store.insert_memory("40007", "fact", "在学吉他", importance=6,
                                    source_group_id="g1")
    promoted = store.update_memory_evidence(
        memory_id, str(chat_id), "g1", "在学吉他",
        allow_legacy_promotion=True, reviewer="backfill:semantic:sem1",
        review_reason="语义回挂（测试）", actor="backfill:evidence:sem1",
    )
    assert promoted
    assert store.get_memory_by_id(memory_id)["trust_level"] == "verified"

    result = backfill.rollback(store, batch="sem1", keep_evidence=False)

    assert result["rolled_back"] == 1
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"
