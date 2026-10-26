# -*- coding: utf-8 -*-
"""P0 昵称同步：档案昵称「只补空、不覆盖」。

背景（2026-10-24 取证）：`people.nickname` 只在档案**创建**那一刻写入，事后永不
更新；而 17 个 `get_or_create_person` 调用点里只有群关系图那个真的传昵称，其余
全传空。于是「谁先被不传昵称的路径创建，谁就永久空白」——实测 27 人昵称为空，
其中一位聊了 1886 次、有 252 字画像，档案里连名字都没有。

本文件钉住三件事：
1. 空 → 用传入值补上（且留下 memory_revisions 流水）
2. 非空 → **绝不覆盖**（`people.nickname` 是跨群稳定身份，群名片/QQ 昵称会变）
3. 不因为一条消息就给陌生 qq 造档案（随机数字污染 people 的老教训）
"""


def test_get_or_create_person_fills_empty_nickname(store):
    """档案创建时没带昵称，之后某条路径带了 → 补上。"""
    store.get_or_create_person("70001", "")
    assert store.get_or_create_person("70001", "米雪儿")["nickname"] == "米雪儿"

    with store._connect() as conn:
        stored = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='70001'").fetchone()[0]
    assert stored == "米雪儿"


def test_get_or_create_person_does_not_overwrite_existing(store):
    """已有昵称绝不覆盖——这是「稳定身份」的语义边界。"""
    store.get_or_create_person("70002", "旧名字")
    person = store.get_or_create_person("70002", "新群名片")
    assert person["nickname"] == "旧名字"

    with store._connect() as conn:
        stored = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='70002'").fetchone()[0]
    assert stored == "旧名字"


def test_nickname_backfill_writes_revision(store):
    """补齐动作必须留痕：谁补的、从什么改成什么。"""
    store.get_or_create_person("70003", "")
    store.get_or_create_person("70003", "补上的名字")

    rows = store.memory_revisions(entity_id="70003", limit=20)
    nickname_rows = [row for row in rows if row["field"] == "nickname"]
    assert len(nickname_rows) == 1
    row = nickname_rows[0]
    assert row["entity"] == "people"
    assert row["before_value"] == ""
    assert row["after_value"] == "补上的名字"
    assert row["actor"] == "sync:person_create"


def test_touch_person_nickname_fills_empty(store):
    store.get_or_create_person("70004", "")
    assert store.touch_person_nickname("70004", "入口补的") is True

    with store._connect() as conn:
        stored = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='70004'").fetchone()[0]
    assert stored == "入口补的"
    rows = [r for r in store.memory_revisions(entity_id="70004", limit=20)
            if r["field"] == "nickname"]
    assert rows and rows[0]["actor"] == "sync:message_entry"


def test_touch_person_nickname_never_overwrites(store):
    store.get_or_create_person("70005", "原昵称")
    assert store.touch_person_nickname("70005", "改了的新昵称") is False

    with store._connect() as conn:
        stored = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='70005'").fetchone()[0]
    assert stored == "原昵称"
    assert store.memory_revisions(entity_id="70005") == []


def test_touch_person_nickname_does_not_create_person(store):
    """陌生 qq 不该因为一条消息就造出 people 行（随机数字污染档案的老教训）。"""
    assert store.touch_person_nickname("999988887777", "某人") is False
    with store._connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM people WHERE qq_id='999988887777'").fetchone()
    assert exists is None


def test_touch_person_nickname_is_idempotent(store):
    """重复调用只写一次，不刷流水。"""
    store.get_or_create_person("70006", "")
    assert store.touch_person_nickname("70006", "同一个名字") is True
    assert store.touch_person_nickname("70006", "同一个名字") is False
    rows = [r for r in store.memory_revisions(entity_id="70006", limit=20)
            if r["field"] == "nickname"]
    assert len(rows) == 1


def test_touch_person_nickname_sanitizes_control_chars(store):
    """昵称是用户可控文本，会流进提示词——净化必须在写库前生效。"""
    store.get_or_create_person("70007", "")
    store.touch_person_nickname("70007", "坏\x00名\n字\u202e")
    with store._connect() as conn:
        stored = conn.execute(
            "SELECT nickname FROM people WHERE qq_id='70007'").fetchone()[0]
    assert stored == "坏名字"
    assert "\n" not in stored


def test_touch_person_nickname_ignores_blank_input(store):
    store.get_or_create_person("70008", "")
    assert store.touch_person_nickname("70008", "   ") is False
    assert store.touch_person_nickname("", "名字") is False


def test_empty_nickname_still_falls_back_to_qq_in_readers(store):
    """补不上时读取侧仍回退 qq_id（既有降级行为不变）——这里只确认不抛。"""
    store.get_or_create_person("70009", "")
    person = store.get_or_create_person("70009", "")
    assert person["nickname"] == ""
