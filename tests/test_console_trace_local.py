# -*- coding: utf-8 -*-
"""本地会话的思维链通路闸门（2026-10-25 第 17 轮，桌面端立项 ①）。

## 这组用例防的是什么

主人报：「桌面端**本地发消息**只显示消息本身，不显示思维链等内容。」

取证结论：**数据在、通路断**——

| 证据 | 事实 |
|---|---|
| `turn_traces` | 146 条，全部 `v2:` 键，**146/146** 能 join 上 `chat_log` |
| 本地库 `console_messages.meta_json` | 有 `reasoning_chars`、`ms`、`tokens`、`model` |
| `_turn_meta` | **没有 `event_key`** ⇒ 前端拿不到键，换不回思维链全文 |
| `ConsoleApi.feed` 的 `desktop:` 分支 | `"trace": None, "event_key": ""` ⇒ 前端那套 `TraceCard` 永不渲染 |

修法两处（都在 `agent/console_api.py`）：
1. `_identity_turn` 把 `build_msg` 已经生成的 `_inbound_event.event_key` 写进 meta；
2. `feed` 的 `desktop:` 分支用 `_local_trace_from_meta` 投影成 `ConsoleTraceSummary`。

**前端一行没改**——`Console.tsx:1059` 的 `{m.trace && rid && <TraceCard …/>}`
本来就在，条件一满足就出「💭 思考 N 字 / 展开 / 回放」。

## 阳性对照（教训 #30/#37）

- 把 `_local_trace_from_meta` 的 `if not event_key: return None` 删掉 →
  `test_history_message_without_event_key_has_no_trace` 红；
- 把 `feed` desktop 分支改回 `"trace": None` →
  `test_desktop_feed_projects_trace` 红；
- 把 `_identity_turn` 的 `meta["event_key"] = event_key` 删掉 →
  该字段缺失，端到端（真实发一句）时 feed 里不会出现 trace。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.console_api import ConsoleApi, _local_trace_from_meta  # noqa: E402
from agent.console_store import ConsoleStore  # noqa: E402

#: 与 `desktop/src/lib/api.ts::ConsoleTraceSummary` 严格对齐的字段集。
#: 前端契约注释写着「字段名严格对齐，不要改名」——改名会让 tsc 编译失败，
#: 但后端没有编译期保护，所以这里用集合钉住。
SUMMARY_FIELDS = {
    "run_id", "reasoning_chars", "tool_count", "duration_ms", "status", "event_key",
}


# ── 纯投影 ────────────────────────────────────────────────────────

def test_meta_projects_to_console_trace_summary():
    meta = {
        "ms": 12601, "model": "deepseek-v4.1-flash", "provider": "custom",
        "tokens": 9371, "tools": [], "finish_reason": "tool_calls",
        "reasoning_chars": 476,
        "event_key": "v2:private:10001:10000:10001:-6",
    }
    got = _local_trace_from_meta(meta)
    assert got is not None
    assert set(got) == SUMMARY_FIELDS, "字段集与前端 ConsoleTraceSummary 不一致"
    assert got["reasoning_chars"] == 476
    assert got["duration_ms"] == 12601
    assert got["event_key"] == meta["event_key"]
    assert got["status"] == "completed"
    assert got["run_id"] == ""            # 本地会话没有 run_id，前端用 event_key


def test_history_message_without_event_key_has_no_trace():
    """本次改动之前的回合没有 event_key —— 界面隐藏那块，不能报错。"""
    assert _local_trace_from_meta({"ms": 900, "reasoning_chars": 300}) is None
    assert _local_trace_from_meta(None) is None
    assert _local_trace_from_meta({}) is None


def test_turn_without_reasoning_or_tools_has_no_trace():
    """没思考也没工具 = 没什么可看的，别在气泡下塞空卡片。"""
    got = _local_trace_from_meta({
        "event_key": "v2:private:1:2:1:-3", "reasoning_chars": 0, "tools": []})
    assert got is None


def test_tools_counted_and_dirty_types_tolerated():
    got = _local_trace_from_meta({
        "event_key": "v2:group:1:2:3:4",
        "reasoning_chars": "120",           # 脏类型：字符串数字
        "ms": None,
        "tools": [{"name": "sing"}, {"name": "web_search"}],
    })
    assert got is not None
    assert got["reasoning_chars"] == 120
    assert got["tool_count"] == 2
    assert got["duration_ms"] == 0


# ── feed 投影（走真实的 ConsoleStore） ─────────────────────────────

def _api_with_store(tmp_path):
    """只造出 feed 的 desktop 分支需要的最小依赖。

    走 `object.__new__` 而不是 `__init__`：desktop 分支不碰 handler / loop，
    把整套 MessageHandler 抬进来只会让这条用例变慢且易碎。
    """
    api = object.__new__(ConsoleApi)
    api.store = ConsoleStore(str(tmp_path / "console.db"))
    api.handler = None
    api.guest_qq = ""
    return api


def test_desktop_feed_projects_trace(tmp_path):
    api = _api_with_store(tmp_path)
    sid = api.store.create_session("自检")["id"]
    key = "v2:private:10001:10000:10001:-6"
    api.store.append_message(sid, "user", "在吗？", None)
    api.store.append_message(sid, "tangtang", "在的。", {
        "ms": 4200, "reasoning_chars": 96, "tools": [], "event_key": key})

    res = api.feed(f"desktop:{sid}", 0, 100)
    msgs = res["messages"]
    assert len(msgs) == 2
    user, bot = msgs
    assert user["trace"] is None, "用户消息不该有 trace"
    assert bot["trace"] is not None, "本地会话的糖糖回复必须有 trace 摘要"
    assert bot["trace"]["reasoning_chars"] == 96
    assert bot["trace"]["event_key"] == key
    # 回放走 m.event_key || m.trace.event_key，两处都要有
    assert bot["event_key"] == key


def test_desktop_feed_keeps_old_messages_working(tmp_path):
    """老消息（meta 无 event_key）照常显示，只是没有 trace。"""
    api = _api_with_store(tmp_path)
    sid = api.store.create_session("旧会话")["id"]
    api.store.append_message(sid, "user", "老问题", None)
    api.store.append_message(sid, "tangtang", "老回答", {"ms": 900})
    msgs = api.feed(f"desktop:{sid}", 0, 100)["messages"]
    assert [m["content"] for m in msgs] == ["老问题", "老回答"]
    assert all(m["trace"] is None for m in msgs)
    assert all(m["event_key"] == "" for m in msgs)


def test_desktop_feed_incremental_keeps_trace(tmp_path):
    """增量轮询（after_id>0）也要带上 trace——否则新消息只在首屏有折叠卡。"""
    api = _api_with_store(tmp_path)
    sid = api.store.create_session("增量")["id"]
    api.store.append_message(sid, "user", "问题一", None)
    api.store.append_message(sid, "tangtang", "回答一", {
        "ms": 1000, "reasoning_chars": 10, "event_key": "v2:private:1:2:1:-1"})
    first = api.feed(f"desktop:{sid}", 0, 100)["messages"]
    last_id = first[-1]["id"]
    api.store.append_message(sid, "tangtang", "回答二", {
        "ms": 2000, "reasoning_chars": 20, "event_key": "v2:private:1:2:1:-2"})
    fresh = api.feed(f"desktop:{sid}", last_id, 100)["messages"]
    assert [m["content"] for m in fresh] == ["回答二"]
    assert fresh[0]["trace"] is not None, "增量轮询丢了 trace"
    assert fresh[0]["trace"]["reasoning_chars"] == 20
