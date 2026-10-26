# -*- coding: utf-8 -*-
"""记忆变更留痕（memory_revisions）：写入侧全覆盖 + 闸门。

设计说明与取证矩阵见 docs/开发规划/记忆变更留痕_20261024.md。

本文件回答两个问题：
1. **行为**：每一种「改记忆」的操作，是不是真的留下了可复原的流水
   （旧值 + 新值 + 谁改的 + 为什么）？
2. **闸门**：未来新增的写方法会不会悄悄绕过留痕？

闸门自带阳性对照（教训 #30/#37）：`test_gate_detects_a_real_unlogged_write`
拿一段**故意不留痕**的假源码喂给同一个扫描器，验证它确实会红。如果扫描器的
正则/判定逻辑失效，这条对照会一起绿——所以它同时是扫描器的自检。
"""

import ast
import re
from pathlib import Path

STORE_SOURCE = Path(__file__).resolve().parents[1] / "agent" / "store.py"

# 改记忆类表却**刻意**不留痕的方法——每一个都必须写理由，理由就在这条注释下面。
# 判据：**统计漂移 / 流程游标 / 成员计数** 不是对「记忆内容或真值」的更正。
# 反例提醒：`set_memory_importance`（显式设定权重）**必须**留痕——它和白名单里的
# `reinforce_memory_by_id`（召回时 +1）在语义上是两件事，别混。
INTENTIONAL_BLIND_WRITERS = {
    "reinforce_memory_by_id": "召回强化的统计漂移，信息量由 recall_count/last_recalled 承载",
    "set_intimacy": "亲密度是关系强度刻度，不属于记忆真值",
    "add_intimacy": "同上",
    "insert_chat": "people.total_chats 是聊天条数计数",
    "_update_relationship_timestamp": "关系摘要的游标快照（记 total_chats 当时的值）",
    "add_cluster_fact": "fact_clusters.fact_count 是成员条数计数",
}

TRACKED_TABLES = ("memories", "cluster_facts", "fact_clusters", "people")
_WRITE_PATTERN = re.compile(
    r"\b(UPDATE|DELETE\s+FROM)\s+(" + "|".join(TRACKED_TABLES) + r")\b", re.I,
)


def _scan_source(source: str):
    """返回 [(方法名, 涉及表集合, 是否调用了 _record_revision)]。"""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tables, guarded = set(), False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                tables.update(
                    match.group(2).lower() for match in _WRITE_PATTERN.finditer(sub.value)
                )
            elif isinstance(sub, ast.JoinedStr):
                flat = "".join(
                    part.value for part in sub.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                tables.update(
                    match.group(2).lower() for match in _WRITE_PATTERN.finditer(flat)
                )
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_record_revision"):
                guarded = True
        if tables:
            found.append((node.name, tables, guarded))
    return found


def _write_methods():
    return _scan_source(STORE_SOURCE.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════
# 闸门
# ═══════════════════════════════════════════════════════════════

def test_every_memory_write_is_traced_or_whitelisted():
    """改了记忆类表，就必须留痕——除非在理由表里。新增写方法忘了标 actor 会在这里红。"""
    missing = [
        name for name, _tables, guarded in _write_methods()
        if not guarded and name not in INTENTIONAL_BLIND_WRITERS
    ]
    assert not missing, (
        "以下方法修改了记忆类表，却没写 memory_revisions，也不在理由表里："
        f"{missing}。要么调 self._record_revision(...)，要么在 "
        "INTENTIONAL_BLIND_WRITERS 里写明为什么不该留痕。"
    )


def test_blind_writer_whitelist_has_no_dead_entries():
    """理由表只能收**真实存在且确实没留痕**的写方法——防止它退化成垃圾场。"""
    actual = {name: guarded for name, _tables, guarded in _write_methods()}
    for name in INTENTIONAL_BLIND_WRITERS:
        assert name in actual, f"理由表里的 {name} 已不存在，删掉这条"
        assert actual[name] is False, f"{name} 现在留痕了，从理由表移除"


def test_gate_detects_a_real_unlogged_write():
    """阳性对照：扫描器必须能抓出一段故意不留痕的写方法。

    如果这条绿了而上面那条也绿，说明扫描器确实在工作；如果扫描器坏了
    （正则写错、AST 遍历漏掉），这里会红——因为假源码里的 sneaky_update
    有 UPDATE 却没留痕，任何正确的判定都应该把它标成 False。
    """
    sample = (
        "class Store:\n"
        "    def sneaky_update(self, memory_id):\n"
        "        conn.execute('UPDATE memories SET value=? WHERE id=?', ('x', memory_id))\n"
        "\n"
        "    def sneaky_delete(self, memory_id):\n"
        "        conn.execute(f'DELETE FROM people WHERE qq_id={memory_id}')\n"
        "\n"
        "    def logged_update(self, memory_id):\n"
        "        conn.execute('UPDATE memories SET status=? WHERE id=?', ('retracted', memory_id))\n"
        "        self._record_revision(conn, entity='memories', entity_id=memory_id,\n"
        "                              op='status', actor='test')\n"
        "\n"
        "    def unrelated_reader(self):\n"
        "        return conn.execute('SELECT value FROM chat_log')\n"
    )
    found = {name: guarded for name, _tables, guarded in _scan_source(sample)}
    assert found == {
        "sneaky_update": False,
        "sneaky_delete": False,
        "logged_update": True,
    }, f"扫描器判定不符预期：{found}"


def test_revision_table_schema_has_no_foreign_key_to_memories(store):
    """流水表不能挂 memories 外键——它的职责之一就是记录「这一行已经被删了」。"""
    with store._connect() as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_revisions'"
        ).fetchone()
    assert ddl and ddl[0], "memory_revisions 表不存在"
    assert "FOREIGN KEY" not in str(ddl[0]).upper()


# ═══════════════════════════════════════════════════════════════
# 基础行为
# ═══════════════════════════════════════════════════════════════

def _revisions(store, entity_id, field=""):
    rows = store.memory_revisions(entity_id=entity_id, limit=200)
    if field:
        rows = [row for row in rows if row["field"] == field]
    return rows


def test_record_revision_defaults_actor_to_unknown(store):
    """没标 actor 就落 unknown——它是「忘记标」的自白，不是合法取值。"""
    with store._connect() as conn:
        store._record_revision(
            conn, entity="memories", entity_id=999, op="update",
            field="value", before="a", after="b",
        )
        conn.commit()
    rows = _revisions(store, 999)
    assert len(rows) == 1
    assert rows[0]["actor"] == "unknown"
    assert rows[0]["before_value"] == "a"
    assert rows[0]["after_value"] == "b"


def test_record_revision_stringifies_dict_snapshot(store):
    """删除快照是整行 dict——必须存成可再解析的 JSON，不是 Python repr。"""
    import json

    with store._connect() as conn:
        store._record_revision(
            conn, entity="memories", entity_id=1000, op="delete",
            before={"id": 1000, "value": "旧内容", "importance": 3},
        )
        conn.commit()
    row = _revisions(store, 1000)[0]
    parsed = json.loads(row["before_value"])
    assert parsed["value"] == "旧内容"
    assert parsed["importance"] == 3


# ═══════════════════════════════════════════════════════════════
# 状态类
# ═══════════════════════════════════════════════════════════════

def test_set_memory_status_records_before_after_and_reason(store):
    memory_id = store.insert_memory("50001", "fact", "他住在一楼")
    assert store.set_memory_status(
        memory_id, "retracted", actor="tool:correct_memory",
        reason="纠正：他说不住一楼",
    ) is True

    row = _revisions(store, memory_id, field="status")[0]
    assert row["before_value"] == "active"
    assert row["after_value"] == "retracted"
    assert row["actor"] == "tool:correct_memory"
    assert "纠正" in row["reason"]
    assert row["qq_id"] == "50001"
    assert row["op"] == "status"


def test_set_memory_status_noop_does_not_record(store):
    """幂等重放不该被写成一连串「变更」。"""
    memory_id = store.insert_memory("50002", "fact", "他养了猫")
    assert store.set_memory_status(memory_id, "active", actor="test") is False
    assert store.memory_revisions(entity_id=memory_id) == []


def test_set_memory_status_on_missing_row_returns_false(store):
    assert store.set_memory_status(987654, "retracted", actor="test") is False
    assert store.memory_revisions(entity_id=987654) == []


# ═══════════════════════════════════════════════════════════════
# 内容覆盖（旧值必须可复原）
# ═══════════════════════════════════════════════════════════════

def test_update_memory_value_keeps_old_text(store):
    """语义去重合并会就地覆盖 value——这是旧值永久消失的写点。"""
    memory_id = store.insert_memory("50003", "like", "他喜欢喝咖啡")
    assert store.update_memory_value(
        memory_id, "他喜欢喝手冲咖啡", actor="dedup:semantic",
        reason="语义去重合并（相似度 0.91）",
    ) is True

    row = _revisions(store, memory_id, field="value")[0]
    assert row["before_value"] == "他喜欢喝咖啡"
    assert row["after_value"] == "他喜欢喝手冲咖啡"
    assert "0.91" in row["reason"]
    assert store.get_memory_by_id(memory_id)["value"] == "他喜欢喝手冲咖啡"


def test_update_memory_value_same_text_does_not_record(store):
    memory_id = store.insert_memory("50004", "like", "他喜欢下雨天")
    store.update_memory_value(memory_id, "他喜欢下雨天", actor="test")
    assert _revisions(store, memory_id, field="value") == []


def test_set_memory_importance_is_traced(store):
    """显式设定权重是决策（与召回强化的统计漂移不同）。"""
    memory_id = store.insert_memory("50005", "fact", "他刚换工作", importance=4)
    store.set_memory_importance(memory_id, 8, actor="dedup:exact",
                                reason="重复出现，提高权重")
    row = _revisions(store, memory_id, field="importance")[0]
    assert row["before_value"] == "4"
    assert row["after_value"] == "8"


def test_reinforce_memory_is_deliberately_not_traced(store):
    """召回强化刻意不进流水——它每天会发生很多次，记它只会淹没真正的更正。"""
    memory_id = store.insert_memory("50006", "fact", "他喜欢看电影")
    store.reinforce_memory_by_id(memory_id, "2026-10-24 07:00:00")
    store.reinforce_memory_by_id(memory_id, "2026-10-24 08:00:00")
    assert store.memory_revisions(entity_id=memory_id) == []
    # 但计数列要如实记录，否则「权重怎么涨的」无处可查
    assert store.get_memory_by_id(memory_id)["importance"] == 5


# ═══════════════════════════════════════════════════════════════
# 删除（行消失前必须留快照）
# ═══════════════════════════════════════════════════════════════

def test_delete_memory_by_id_snapshots_row(store):
    memory_id = store.insert_memory("50007", "event", "他去了杭州", importance=7)
    assert store.delete_memory_by_id(
        memory_id, actor="console", reason="人工清理",
    ) is True

    row = _revisions(store, memory_id)[0]
    assert row["op"] == "delete"
    assert "他去了杭州" in row["before_value"]
    assert store.get_memory_by_id(memory_id) is None


def test_delete_memory_by_id_missing_row_is_not_traced(store):
    assert store.delete_memory_by_id(888888, actor="test") is False
    assert store.memory_revisions(entity_id=888888) == []


def test_delete_memories_for_user_snapshots_every_row(store):
    first = store.insert_memory("50008", "fact", "他喜欢猫")
    second = store.insert_memory("50008", "fact", "他住在二楼")
    third = store.insert_memory("50009", "fact", "别人的记忆，不该被删")

    assert store.delete_memories_for_user(
        "50008", actor="command:/记忆 清空", reason="用户主动清空",
    ) == 2

    snapshots = store.memory_revisions(qq_id="50008", limit=50)
    assert {row["entity_id"] for row in snapshots} == {str(first), str(second)}
    assert all(row["op"] == "delete" for row in snapshots)
    assert store.get_memory_by_id(third) is not None


def test_delete_stale_memories_snapshots_before_purge(store):
    stale = store.insert_memory(
        "50010", "said", "很久以前的一句闲聊", importance=2,
        timestamp="2020-01-01 00:00:00",
    )
    keep = store.insert_memory("50010", "said", "新的闲聊", importance=2)

    removed = store.delete_stale_memories(90, actor="decay:stale_cleanup")
    assert removed == 1

    row = _revisions(store, stale)[0]
    assert row["op"] == "delete"
    assert "很久以前的一句闲聊" in row["before_value"]
    assert store.get_memory_by_id(keep) is not None


# ═══════════════════════════════════════════════════════════════
# 取代 / 合成
# ═══════════════════════════════════════════════════════════════

def test_link_superseded_records_pointer(store):
    old_id = store.insert_memory("50011", "fact_synthesis", "旧画像", origin="summarized")
    new_id = store.insert_memory("50011", "fact_synthesis", "新画像", origin="summarized")
    assert store.set_memory_status(old_id, "superseded", actor="test") is True

    assert store.link_superseded(
        [old_id], new_id, actor="tool:correct_memory", reason="被纠正事实取代",
    ) == 1

    rows = _revisions(store, old_id, field="superseded_by")
    assert len(rows) == 1
    assert rows[0]["after_value"] == str(new_id)


def test_link_superseded_is_idempotent(store):
    """重放不能把同一次取代写成多次变更。"""
    old_id = store.insert_memory("50012", "fact_synthesis", "旧", origin="summarized")
    new_id = store.insert_memory("50012", "fact_synthesis", "新", origin="summarized")
    store.set_memory_status(old_id, "superseded", actor="test")
    store.link_superseded([old_id], new_id, actor="test")
    store.link_superseded([old_id], new_id, actor="test")
    assert len(_revisions(store, old_id, field="superseded_by")) == 1


def test_replace_synthesis_memories_traces_both_branches(store):
    old_id = store.insert_memory(
        "50013", "fact_synthesis", "旧合成事实", origin="summarized",
    )
    new_ids = store.replace_synthesis_memories(
        "50013", "fact_synthesis", ["新合成事实"], actor="synthesis:fact",
    )
    assert len(new_ids) == 1
    row = _revisions(store, old_id)[0]
    assert row["op"] == "supersede"
    assert row["after_value"] == "superseded"
    assert "被 #" in row["reason"]

    # 新集合为空 = 否定，不是取代
    retracted_ids = store.replace_synthesis_memories(
        "50013", "fact_synthesis", [], actor="synthesis:fact",
    )
    assert retracted_ids == []
    latest = _revisions(store, new_ids[0])[0]
    assert latest["op"] == "retract"
    assert latest["after_value"] == "retracted"
    assert "空" in latest["reason"]


def test_replace_profile_snapshot_keeps_previous_notes(store):
    """画像换版是「她对这个人的认知」整体更换，旧版必须可追溯。"""
    store.get_or_create_person("50014", "小A")
    store.update_person("50014", notes="旧画像：他喜欢猫",
                        actor="manual", reason="人工写入")
    old_memory = store.insert_memory(
        "50014", "profile_synthesis", "旧画像合成行", origin="summarized",
    )
    store.set_memory_importance(old_memory, 4, actor="test")

    store.replace_profile_snapshot(
        "50014", "新画像：他喜欢狗", ["他养了一只狗"],
        actor="synthesis:profile",
    )

    notes_rows = [
        row for row in store.memory_revisions(qq_id="50014", limit=100)
        if row["entity"] == "people" and row["field"] == "notes"
    ]
    assert any(
        "旧画像：他喜欢猫" in row["before_value"]
        and "新画像：他喜欢狗" in row["after_value"]
        for row in notes_rows
    )
    old_rows = _revisions(store, old_memory, field="status")
    assert old_rows and old_rows[0]["after_value"] == "superseded"


# ═══════════════════════════════════════════════════════════════
# people 字段
# ═══════════════════════════════════════════════════════════════

def test_update_person_traces_notes_but_not_statistics(store):
    store.get_or_create_person("50015", "小B")
    store.update_person("50015", notes="他喜欢喝咖啡", actor="manual", reason="初始化")
    store.update_person("50015", notes="他喜欢喝绿茶", actor="manual", reason="纠正")
    store.update_person("50015", intimacy=42, total_chats=True)

    notes_rows = [
        row for row in store.memory_revisions(qq_id="50015", limit=100)
        if row["field"] in {"notes", "notes_trust_level", "notes_source_ids"}
    ]
    assert any(
        row["field"] == "notes"
        and row["before_value"] == "他喜欢喝咖啡"
        and row["after_value"] == "他喜欢喝绿茶"
        for row in notes_rows
    )
    # 亲密度/聊天计数不进流水
    assert not any(
        row["field"] in {"intimacy", "total_chats"}
        for row in store.memory_revisions(qq_id="50015", limit=100)
    )


def test_set_birthday_keeps_previous_value(store):
    store.get_or_create_person("50016", "小C")
    store.set_birthday("50016", "3月15日", actor="command:/生日 设置")
    store.set_birthday("50016", "04-20", actor="command:/生日 设置")

    rows = _revisions(store, "50016", field="birthday")
    assert rows[0]["before_value"] == "03-15"
    assert rows[0]["after_value"] == "04-20"


def test_set_relationship_summary_keeps_previous_value(store):
    store.get_or_create_person("50017", "小D")
    store.set_relationship_summary("50017", "刚开始认识", actor="synthesis:relationship")
    store.set_relationship_summary("50017", "已经很熟了", actor="synthesis:relationship")

    rows = _revisions(store, "50017", field="relationship_summary")
    assert rows[0]["before_value"] == "刚开始认识"
    assert rows[0]["after_value"] == "已经很熟了"


# ═══════════════════════════════════════════════════════════════
# 事实簇
# ═══════════════════════════════════════════════════════════════

def test_cluster_fact_retraction_keeps_summary(store):
    """清空簇摘要前必须先留旧摘要——摘要是归纳结论，清了拼不回来。"""
    cluster_id = store.upsert_fact_cluster(
        "50018", "preference", "饮品偏好", summary="他喜欢咖啡和茶",
    )
    fact_id = store.add_cluster_fact(cluster_id, "50018", "他喜欢咖啡")

    store.retract_cluster_fact(
        fact_id, "retracted", actor="tool:correct_memory", reason="纠正：他不喜欢咖啡",
    )

    summary_rows = _revisions(store, cluster_id, field="summary")
    assert summary_rows and summary_rows[0]["before_value"] == "他喜欢咖啡和茶"
    status_rows = _revisions(store, fact_id, field="status")
    assert status_rows and status_rows[0]["after_value"] == "retracted"


def test_cluster_summary_and_title_are_traced(store):
    cluster_id = store.upsert_fact_cluster(
        "50019", "habit", "作息", summary="早睡",
    )
    store.update_cluster_summary(cluster_id, "晚睡晚起", actor="synthesis:fact_cluster")
    store.set_cluster_title(cluster_id, "作息习惯", actor="synthesis:cluster_merge")

    summary_rows = _revisions(store, cluster_id, field="summary")
    assert summary_rows[0]["before_value"] == "早睡"
    assert summary_rows[0]["after_value"] == "晚睡晚起"
    title_rows = _revisions(store, cluster_id, field="title")
    assert title_rows[0]["after_value"] == "作息习惯"


def test_delete_cluster_snapshots_cluster_and_its_facts(store):
    cluster_id = store.upsert_fact_cluster(
        "50020", "identity", "身份", summary="学生",
    )
    fact_id = store.add_cluster_fact(cluster_id, "50020", "他是大学生")

    assert store.delete_cluster(
        cluster_id, actor="synthesis:cluster_merge", reason="簇合并",
    ) is True

    cluster_row = _revisions(store, cluster_id)[0]
    assert cluster_row["op"] == "delete"
    assert "学生" in cluster_row["before_value"]
    fact_row = _revisions(store, fact_id)[0]
    assert fact_row["op"] == "delete"
    assert "他是大学生" in fact_row["before_value"]
    assert store.delete_cluster(cluster_id, actor="test") is False


# ═══════════════════════════════════════════════════════════════
# 读取与统计
# ═══════════════════════════════════════════════════════════════

def test_memory_revisions_filters_and_orders(store):
    first = store.insert_memory("50021", "fact", "第一条")
    second = store.insert_memory("50022", "fact", "第二条")
    store.set_memory_status(first, "retracted", actor="a1")
    store.set_memory_status(second, "retracted", actor="a2")

    by_qq = store.memory_revisions(qq_id="50021")
    assert {row["entity_id"] for row in by_qq} == {str(first)}

    by_actor = store.memory_revisions(actor="a2")
    assert {row["entity_id"] for row in by_actor} == {str(second)}

    both = store.memory_revisions(limit=50)
    assert len(both) == 2
    assert both[0]["id"] > both[1]["id"], "流水必须按时间倒序（新的在前）"


def test_memory_revision_stats_counts_actors(store):
    first = store.insert_memory("50023", "fact", "甲")
    second = store.insert_memory("50023", "fact", "乙")
    store.set_memory_status(first, "retracted", actor="tool:correct_memory")
    store.set_memory_status(second, "superseded", actor="synthesis:fact")

    stats = store.memory_revision_stats()
    assert stats["total"] == 2
    actors = {row["actor"] for row in stats["by_actor"]}
    assert actors == {"tool:correct_memory", "synthesis:fact"}
    assert {row["op"] for row in stats["by_op"]} == {"status"}
