# -*- coding: utf-8 -*-
"""会客厅镜像的会话口径闸门（2026-10-25 第 17 轮）。

## 这组用例防的是什么

主人报：「左侧的私聊和群聊似乎是混着的：虽然标着私聊标签，但聊天记录显示的全是
群里的内容，私聊并非真正的私聊。」

取证（全库真实数据，见 `docs/诊断/第17轮_私聊镜像口径_20261025.md`）：

| 事实 | 数字 |
|---|---|
| `chat_log` 真私聊行（`group_id=''`） | 21,435 |
| `chat_log` 群消息行 | 279,784 |
| 按 `qq_id` 分组、**不看** `group_id` 时，12 个活跃「私聊」会话里群消息占比 | 11 个 >82%，其中 4 个 100% |

根因：`Store.list_chat_scopes('private')` 与 `Store.read_chat_feed('private', qq)`
只按 `qq_id` 过滤，**漏了 `group_id` 为空**这一条。同文件另有 17 处私聊口径都写了
`COALESCE(group_id,'')=''`（`get_user_recent_messages` / `_chat_scope` / 活跃统计 /
未读计数…），只有这两个函数漏——所以这不是设计意图，是漏网（教训 #38：同一个物理量
的换算点必须全部一致）。

## 为什么必须写成闸门

`read_chat_feed` / `list_chat_scopes` 在此之前**一条测试都没有**（`grep` 全项目
`tests/` 零命中），所以这个 bug 活了多久没人知道。而它不报错、不抛异常：私聊会话
「有内容、能滚动、看着正常」，只是内容全是群里的——和教训 #38/#41/#42 同族
（静默错数据 + 没有尺子）。

## 断言方式（不靠肉眼）

每个用例都用**另一条 sqlite3 连接**反查 `chat_log.group_id`，不经过被测函数自证。
这正是桌面端立项 §2② 写的验收：「私聊会话里 `SELECT group_id FROM chat_log
WHERE id IN (显示的 id)` 必须全为空；群会话里必须全等于该群号」。

## 阳性对照（教训 #30/#37：检查器必须能红）

删掉 `store.list_chat_scopes` 里那行 `where.append("COALESCE(group_id,'') = ''")`
与 `read_chat_feed` 的 `scope_cond` 后半段 → 本文件
`test_private_scope_excludes_group_only_user`、
`test_private_feed_ids_are_all_direct_messages`、
`test_private_scope_counts_only_direct_messages` 三条立刻红。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.store import Store  # noqa: E402

GROUP_A = "30001"
GROUP_B = "30002"
QQ_MIXED = "10001"    # 群里说过话，也真私聊过
QQ_GROUP_ONLY = "10002"  # **只在群里**说过话，从没私聊
QQ_PRIVATE_ONLY = "10003"
QQ_QUARANTINED = "10004"


@pytest.fixture()
def db(tmp_path):
    """造一个能分辨「私聊 / 群聊」的最小库。

    关键安排两件事：
      1. `QQ_GROUP_ONLY` **只在群里**发言 —— 修复前他会出现在私聊列表里
         （正文全是群消息），这正是主人看到的症状；修复后他必须消失。
      2. **群里那条比私聊那条更新** —— 修复前 `last_message`/`last_id`
         会指向群消息，私聊会话的预览和未读判断全部失真。
    """
    store = Store(str(tmp_path / "memory.db"))
    store.insert_chat(QQ_MIXED, "私聊第一句", group_id="",
                      timestamp="2026-10-20 10:00:00")
    store.insert_chat(QQ_MIXED, "私聊第二句", group_id="",
                      timestamp="2026-10-20 10:01:00")
    # ⚠ 糖糖回复的 `qq_id` 是**对话对象**，不是她自己（全库实测：同一个
    # qq_id 下 bot=0/bot=1 两半各占一半）。写成 QQ_BOT 会被 `qq_id=` 过滤掉，
    # 让「不误伤她的回复」这条断言假绿。
    store.insert_chat(QQ_MIXED, "糖糖在私聊里回的一句", group_id="", is_bot=True,
                      timestamp="2026-10-20 10:02:00")
    # 群里的发言，时间更新（修复前会抢走 last_message）
    store.insert_chat(QQ_MIXED, "群里第一句", group_id=GROUP_A,
                      timestamp="2026-10-20 11:00:00")
    store.insert_chat(QQ_MIXED, "群里第二句", group_id=GROUP_B,
                      timestamp="2026-10-20 11:01:00")
    store.insert_chat(QQ_MIXED, "群里第三句", group_id=GROUP_A,
                      timestamp="2026-10-20 11:02:00")
    # 从没私聊过的人，只在群里
    for i in range(4):
        store.insert_chat(QQ_GROUP_ONLY, f"群里第{i}句", group_id=GROUP_A,
                          timestamp=f"2026-10-20 12:0{i}:00")
    store.insert_chat(QQ_PRIVATE_ONLY, "只有私聊", group_id="",
                      timestamp="2026-10-20 13:00:00")
    # 被隔离的脏行：两个列表都不许出现
    store.insert_chat(QQ_QUARANTINED, "隔离脏行", group_id="",
                      timestamp="2026-10-20 14:00:00")
    with sqlite3.connect(store.db_path) as raw:
        raw.execute("UPDATE chat_log SET quarantined_at='2026-10-21 00:00:00' "
                    "WHERE qq_id=?", (QQ_QUARANTINED,))
    return store


def _group_ids_of(store, msg_ids):
    """另一条连接反查 group_id——不经过被测函数自证。"""
    if not msg_ids:
        return []
    marks = ",".join("?" * len(msg_ids))
    with sqlite3.connect(store.db_path) as raw:
        rows = raw.execute(
            f"SELECT id, COALESCE(group_id,'') FROM chat_log WHERE id IN ({marks})",
            tuple(int(i) for i in msg_ids)).fetchall()
    return [g for _, g in rows]


# ── 私聊侧 ────────────────────────────────────────────────────────

def test_private_scope_excludes_group_only_user(db):
    """只在群里说过话的人，**不许**出现在私聊列表里。

    修复前他就是主人看到的那个「标着私聊、内容全是群消息」的会话。
    """
    scopes = db.list_chat_scopes("private")
    sids = {s["scope_id"] for s in scopes}
    assert QQ_GROUP_ONLY not in sids, (
        f"只在群里发言的 {QQ_GROUP_ONLY} 被当成了私聊会话——"
        "private 口径漏了 group_id 为空这一条")
    assert QQ_MIXED in sids
    assert QQ_PRIVATE_ONLY in sids


def test_private_scope_counts_only_direct_messages(db):
    """私聊会话的条数/预览/最后一条，都只能算私聊行。

    QQ_MIXED 共 6 条：私聊 2 条（对方）+ 群 3 条 + 糖糖私聊 1 条。
    """
    scopes = {s["scope_id"]: s for s in db.list_chat_scopes("private")}
    s = scopes[QQ_MIXED]
    assert s["message_count"] == 3, (
        f"私聊会话算进了群消息：期望 3 条（2 对方 + 1 糖糖），实际 {s['message_count']}")
    assert s["last_message"] == "糖糖在私聊里回的一句", (
        f"私聊预览显示的是群消息：{s['last_message']!r}")

    # last_id 必须指向私聊行（前端未读判断 hasUnread 用它）
    assert _group_ids_of(db, [s["last_id"]]) == [""], "last_id 指向了群消息行"


def test_private_feed_ids_are_all_direct_messages(db):
    """核心断言：私聊 feed 返回的每一条，反查 group_id 都必须为空。"""
    msgs, _ = db.read_chat_feed("private", QQ_MIXED)
    assert msgs, "私聊 feed 竟然为空——真私聊行被误伤了吗"
    gids = _group_ids_of(db, [m["id"] for m in msgs])
    assert set(gids) == {""}, (
        f"私聊 feed 混进了群消息（group_id={set(gids) - {''}}）——"
        "read_chat_feed 的 private 分支漏了 group_id 过滤")


def test_private_feed_keeps_bot_direct_replies(db):
    """反向验证不误伤：糖糖的私聊回复挂在空串 group_id 上，必须保留。

    （全库实测 `is_bot_reply=1` 有 10,505 条空串 / 9,669 条群；
     若她的私聊回复挂在群里，加过滤就会把她的回复一起滤掉。）
    """
    msgs, _ = db.read_chat_feed("private", QQ_MIXED)
    contents = [m["content"] for m in msgs]
    assert "糖糖在私聊里回的一句" in contents, "糖糖的私聊回复被过滤误伤了"
    bot = [m for m in msgs if m["role"] == "tangtang"]
    assert bot and bot[0]["speaker"] == "糖糖"


def test_private_feed_isolation_and_incremental(db):
    """隔离行不出现；增量（after_id）同样遵守私聊口径。"""
    msgs, _ = db.read_chat_feed("private", QQ_QUARANTINED)
    assert msgs == [], "被隔离的脏行泄漏进了私聊 feed"

    all_msgs, _ = db.read_chat_feed("private", QQ_MIXED)
    assert all_msgs, "先决条件：私聊 feed 非空"
    first_id = all_msgs[0]["id"]
    later, _ = db.read_chat_feed("private", QQ_MIXED, after_id=first_id)
    assert all(m["id"] > first_id for m in later)
    assert set(_group_ids_of(db, [m["id"] for m in later])) <= {""}, (
        "增量拉取混进了群消息——after_id 分支同样要遵守私聊口径")


def test_private_scope_excludes_bot_qq(db):
    """`exclude_id`（剔除糖糖自己）仍然生效，别被新条件打乱参数顺序。

    注意 `exclude_id` 过滤的是 `qq_id`，而糖糖的回复挂在**对话对象**名下，
    所以这里用 QQ_PRIVATE_ONLY 验证「参数确实生效且顺序没乱」。
    """
    scopes = {s["scope_id"] for s in db.list_chat_scopes("private",
                                                         exclude_id=QQ_PRIVATE_ONLY)}
    assert QQ_PRIVATE_ONLY not in scopes
    assert QQ_MIXED in scopes


# ── 群聊侧（对照：这一侧本来就是对的，别改坏） ──────────────────────

def test_group_feed_and_scope_stay_group_only(db):
    msgs, _ = db.read_chat_feed("group", GROUP_A)
    assert msgs, "群 feed 为空"
    assert set(_group_ids_of(db, [m["id"] for m in msgs])) == {GROUP_A}, (
        "群 feed 混进了别的群或私聊的消息")

    scopes = {s["scope_id"]: s for s in db.list_chat_scopes("group")}
    assert GROUP_A in scopes and GROUP_B in scopes
    # 私聊行 group_id='' 不会成为一个「群」
    assert "" not in scopes
    # GROUP_A：QQ_MIXED 2 条（群里第一句/第三句）+ QQ_GROUP_ONLY 4 条
    assert scopes[GROUP_A]["message_count"] == 6
    assert scopes[GROUP_A]["last_message"] == "群里第3句"
