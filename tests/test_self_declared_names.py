# -*- coding: utf-8 -*-
"""P1 自称名字归档：「名字是X」进 aliases，**绝不进 nickname**。

背景（2026-10-24 取证）：用户说「名字是米雪儿」这类自述会被提取成普通 fact
存进 memories，但没有任何通道能把它提升到人物档案——于是糖糖"记得她说过什么"，
却不知道该怎么称呼她。更麻烦的是：这类自述**可能是角色扮演**（实测那位用户
自称「米雪儿·李」是《卡拉彼丘》的角色，旁人还点明"其实是自称米雪儿的人"）。

所以本文件的判据是「进 aliases 但绝不进 nickname」：
- 进 aliases → 糖糖有据可依地知道"对方这样自称过"，可展示、可撤销、带证据指针
- 不进 nickname → 不让一次角色扮演变成永久身份
"""


def _run_promotion(store, qq_id):
    """在调用方事务里跑一次归档（生产接线点也是这么用的）。"""
    with store._connect() as conn:
        found = store._promote_self_declared_names(conn, qq_id)
        conn.commit()
    return found


def test_self_declared_name_goes_to_aliases(store):
    store.get_or_create_person("71001", "真昵称")
    memory_id = store.insert_memory("71001", "fact", "名字是米雪儿", importance=10)

    assert _run_promotion(store, "71001") == ["米雪儿"]

    with store._connect() as conn:
        row = conn.execute(
            "SELECT alias, source, memory_id FROM aliases WHERE qq_id='71001'"
        ).fetchone()
    assert row[0] == "米雪儿"
    assert row[1] == "self_declared"
    assert row[2] == memory_id, "必须带证据指针，事后才能追溯这个自称从哪来"


def test_self_declared_name_never_touches_nickname(store):
    """核心边界：自称 ≠ 昵称。角色扮演不该变成永久身份。"""
    store.get_or_create_person("71002", "QQ昵称")
    store.insert_memory("71002", "fact", "我是米雪儿·李呀！欧泊最年轻的搜查官")
    store.insert_memory("71002", "fact", "叫我小明")

    _run_promotion(store, "71002")

    with store._connect() as conn:
        nickname = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='71002'").fetchone()[0]
    assert nickname == "QQ昵称", "自称绝不能覆盖 people.nickname"


def test_identity_words_are_not_names(store):
    """「我是中国人 / 我是大二学生 / 自称欧泊第1人」都不是名字。"""
    store.get_or_create_person("71003", "某人")
    for value in ("我是中国人", "我是大二学生", "自称欧泊第1人", "我喜欢看电影"):
        store.insert_memory("71003", "fact", value)

    assert _run_promotion(store, "71003") == []
    with store._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE qq_id='71003'").fetchone()[0]
    assert count == 0


def test_trailing_particle_is_not_part_of_name(store):
    """「叫我尾大狐酱就没问题」里的名字是「尾大狐酱」而不是整句尾巴。

    这条是 2026-10-24 回填 dry-run 时抓到的真实误判：抽出来是
    `尾大狐酱就没问题`。加了虚词阻塞后改为**整条拒绝**（宁可漏认不可错认）——
    宁可少一个待确认的称呼，也不要往档案里写一个假名字。
    """
    assert store._self_name_from_text("叫我尾大狐酱就没问题") == ""
    assert store._self_name_from_text("叫我阿伟") == "阿伟"


def test_self_name_rule_table(store):
    """把识别规则的边界钉成一张表，改规则时这张表会立刻告诉你影响了什么。"""
    cases = {
        "名字是米雪儿": "米雪儿",
        "我叫穷到吃外卖": "穷到吃外卖",
        "叫我阿伟": "阿伟",
        "小明是我的名字": "小明",
        "名字叫米雪儿·糖": "米雪儿·糖",
        "我是中国人": "",
        "我是大二学生": "",
        "自称欧泊第1人": "",
        "我是米雪儿·李呀！欧泊最年轻的搜查官": "",
        "我喜欢看电影": "",
        "叫我尾大狐酱就没问题": "",
        "": "",
    }
    for text, expected in cases.items():
        assert store._self_name_from_text(text) == expected, f"{text!r} 判定错误"


def test_promotion_is_idempotent(store):
    store.get_or_create_person("71004", "某人")
    store.insert_memory("71004", "fact", "我叫小明")

    assert _run_promotion(store, "71004") == ["小明"]
    assert _run_promotion(store, "71004") == [], "第二次不该重复归档"

    with store._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM aliases WHERE qq_id='71004'").fetchone()[0]
    assert count == 1


def test_promotion_writes_revision(store):
    """归档动作要留痕：谁、依据哪条记忆。"""
    store.get_or_create_person("71005", "某人")
    memory_id = store.insert_memory("71005", "fact", "名字是米雪儿")

    _run_promotion(store, "71005")

    rows = [r for r in store.memory_revisions(entity_id="71005", limit=20)
            if r["field"] == "alias"]
    assert len(rows) == 1
    assert rows[0]["after_value"] == "米雪儿"
    assert rows[0]["actor"] == "extract:self_declared_name"
    assert f"#{memory_id}" in rows[0]["reason"]


def test_inactive_memory_is_ignored(store):
    """被推翻的记忆不能再提供名字——否则纠正过的错名会从旁路回来。"""
    store.get_or_create_person("71006", "某人")
    memory_id = store.insert_memory("71006", "fact", "名字是旧名字")
    store.set_memory_status(memory_id, "retracted", actor="test")

    assert _run_promotion(store, "71006") == []


def test_add_alias_records_evidence_pointer(store):
    store.add_alias("71007", "阿伟", source="llm", memory_id=12345)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT alias, source, memory_id FROM aliases WHERE qq_id='71007'"
        ).fetchone()
    assert tuple(row) == ("阿伟", "llm", 12345)


def test_admin_alias_update_persists(store):
    """2026-10-24 P0 的更新：people.nickname 有值后，再传不同昵称也不覆盖。"""
    store.get_or_create_person("71008", "第一个昵称")
    store.get_or_create_person("71008", "第二个昵称")
    with store._connect() as conn:
        assert conn.execute(
            "SELECT nickname FROM people WHERE qq_id='71008'").fetchone()[0] == "第一个昵称"
