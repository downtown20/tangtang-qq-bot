# -*- coding: utf-8 -*-
"""回合回放的 feedback 匹配闸门（2026-10-25 第 17 轮 ④）。

## 这组用例防的是什么

`console_api.turn_replay` 要显示「对方怎么反应」，靠
`store.find_feedback_for_turn(user_qq, after_ts, reply_text)` 去 `feedback` 表里
模糊匹配。它的匹配条件是「两边文本有**前缀关系**」——所以第二个参数必须是她
**真正发出去的那句话**。

而生产代码传的是 `row.get("message")`，即**触发消息的正文**（对方说的那句）。
对方的原话 vs 她的回复，前缀几乎不可能相同 ⇒ `feedback` 长期返回 `None`，
回放里「对方怎么反应」和第 7 段永远是空的。**不是「对方还没反应」，是永远查不到。**

修法：先用 `get_bot_reply_text_for_trigger(trigger_id)` 取回她真正说的话
（`chat_log` 里 `delivery:<触发消息id>:<气泡序号>`，多条气泡按序拼接），
再拿它去匹配。

## 阳性对照（教训 #30/#37）

把 `console_api.turn_replay` 里那句
`reply_text = store.get_bot_reply_text_for_trigger(...)` 换回
`reply_text = str(row.get("message") or "")` →
`test_replay_matches_feedback_via_her_actual_reply` 红。
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.console_api import ConsoleApi  # noqa: E402
from agent.console_store import ConsoleStore  # noqa: E402
from agent.store import Store  # noqa: E402

QQ = "10001"
TRIGGER_KEY = f"v2:private:{QQ}:1792900000:{QQ}:-500"
TRIGGER_TS = "2026-10-25 15:00:00"


def _api(tmp_path):
    """只造 turn_replay 需要的那点依赖：真 Store（记忆库）+ 假 handler + 空 ConsoleStore。"""
    store = Store(str(tmp_path / "memory.db"))
    api = object.__new__(ConsoleApi)
    api.handler = SimpleNamespace(memory=SimpleNamespace(store=store))
    api.store = ConsoleStore(str(tmp_path / "console.db"))
    return api, store


def _seed(store, *, bubble2=True, feedback=True):
    """造一个完整回合：对方的话 → 她的两段回复 → trace → feedback。"""
    trigger_id = store.insert_chat(QQ, "你在吗", group_id="",
                                   timestamp=TRIGGER_TS, event_key=TRIGGER_KEY)
    store.insert_chat(QQ, "在的。", group_id="", is_bot=True,
                      timestamp="2026-10-25 15:00:05",
                      event_key=f"delivery:{trigger_id}:1")
    if bubble2:
        store.insert_chat(QQ, "怎么啦？", group_id="", is_bot=True,
                          timestamp="2026-10-25 15:00:06",
                          event_key=f"delivery:{trigger_id}:2")
    store.record_turn_trace(
        run_id="run-1", event_key=TRIGGER_KEY, scope_id=f"private:{QQ}",
        model="deepseek-v4.1-flash", reasoning_text="她想了想",
        tool_steps=[], prompt_chars=1200, duration_ms=3000,
        prompt_digest={"history_count": 3, "user_bg_labels": ["协议", "记忆"]})
    if feedback:
        with sqlite3.connect(store.db_path) as raw:
            raw.execute(
                "INSERT INTO feedback (bot_reply, user_reaction, user_qq, group_id, "
                "sentiment, confidence, reply_ms, direction_verified, timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("在的。怎么啦？", "没事，就是问问", QQ, "", "positive", 0.8,
                 184, 1, "2026-10-25 15:00:20"))
            raw.commit()
    return trigger_id


def test_bot_reply_text_concatenates_bubbles_in_order(tmp_path):
    _, store = _api(tmp_path)
    trigger_id = _seed(store)
    got = store.get_bot_reply_text_for_trigger(trigger_id)
    assert got == "在的。怎么啦？", f"多气泡必须按 id 升序拼接，实际 {got!r}"


def test_bot_reply_text_does_not_cross_turns(tmp_path):
    """别人的回合 / 别的触发消息的回复不能串进来。"""
    _, store = _api(tmp_path)
    t1 = _seed(store, bubble2=False)
    # 另一个回合（触发消息 id 不同）
    t2 = store.insert_chat(QQ, "第二句", group_id="", timestamp="2026-10-25 15:10:00")
    store.insert_chat(QQ, "第二回合的回复", group_id="", is_bot=True,
                      event_key=f"delivery:{t2}:1")
    assert store.get_bot_reply_text_for_trigger(t1) == "在的。"
    assert store.get_bot_reply_text_for_trigger(t2) == "第二回合的回复"
    # 负数/非法 id 一律空串，不抛异常
    assert store.get_bot_reply_text_for_trigger(-1) == ""
    assert store.get_bot_reply_text_for_trigger("abc") == ""


def test_replay_matches_feedback_via_her_actual_reply(tmp_path):
    """核心：拿她的回复去匹配，feedback 必须命中。

    修复前传的是「你在吗」（对方的话），与 `feedback.bot_reply`「在的。怎么啦？」
    没有前缀关系 → 返回 None。
    """
    api, store = _api(tmp_path)
    _seed(store)
    rep = api.turn_replay(TRIGGER_KEY)
    assert rep is not None
    fb = rep.get("feedback")
    assert fb is not None, (
        "feedback 没匹配到——turn_replay 是不是又拿触发消息去比了？")
    assert fb["bot_reply"] == "在的。怎么啦？"
    assert fb["user_reaction"] == "没事，就是问问"
    assert fb["sentiment"] == "positive"


def test_flow_phase7_carries_reply_and_reaction(tmp_path):
    api, store = _api(tmp_path)
    _seed(store)
    flow = {p["key"]: p for p in api.turn_replay(TRIGGER_KEY)["flow"]}
    send = flow["send"]
    assert send["ok"] is True
    kv = {it["k"]: it["v"] for it in send["items"]}
    assert kv["决定"] == "" or kv["决定"] == "—"   # 没造 decision_runs → 保留占位
    assert "在的。" in kv["正文"], f"第 7 段没显示她说了什么：{kv}"
    assert send["note"] == "", "有反馈时不该报「还没反应」"


def test_missing_feedback_is_reported_not_faked(tmp_path):
    """真的没有反馈时，如实说「还没反应」，不许编。

    ⚠ 注意分工：`send` 段现在从 **chat_log** 取她的正文（`delivery:` 行），
    所以即使 feedback 缺失，`send` 段照样有内容；「还没反应」是**第 9 段**的事。
    """
    api, store = _api(tmp_path)
    _seed(store, feedback=False)
    rep = api.turn_replay(TRIGGER_KEY)
    assert rep["feedback"] is None
    flow = {p["key"]: p for p in rep["flow"]}
    assert flow["send"]["ok"] is True, "她确实说了话——不该因为没反馈就说「没发送」"
    kv = {it["k"]: it["v"] for it in flow["send"]["params"]}
    assert kv["正文全文"].startswith("在的。"), "正文必须来自 chat_log"
    assert "还没反应" in flow["feedback"]["note"]
