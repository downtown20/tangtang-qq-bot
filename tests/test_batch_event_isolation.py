"""任务A：原始事件与批处理隔离（2026-08-28 协作任务包，先写红测再实现）

验收点：
  1. 群/私批处理先逐条保存真实 event/message/user/chat/raw/segments，合并文本只作 LLM 视图
  2. 同批两个用户的消息绝不会出现在同一个用户的 chat_log 事实行
  3. 合并视图不产生单一用户 synthetic chat_log 行（handler 对 _batched_events 跳过 log_chat）
  4. 重复事件（同 scope+platform_message_id）幂等——只落一次、只计一次账
  5. 逐条行保留真实原始时间戳与顺序（第一句/精确时间查询不破坏）
  6. 历史 synthetic 行 quarantine 可回滚：标记 → 读路径排除 → 恢复
  7. quarantine 匹配模式与 batcher 生成格式锁定（防格式漂移）
"""
import asyncio
import json
import time

import pytest

from agent.message_batcher import MessageBatcher


class _FakeMemory:
    """最小 memory 门面：只暴露 batcher 用到的 log_chat"""

    def __init__(self, store):
        self.store = store

    def log_chat(self, *args, **kwargs):
        return self.store.insert_chat(*args, **kwargs)


class _FakeHandler:
    """最小 handler 门面：batcher 只依赖 h.handle_* 与 h.memory"""

    def __init__(self, store):
        self.store = store
        self.memory = _FakeMemory(store)
        self.calls = []
        self.bot_qq = "10000"

    async def handle_group_message(self, msg):
        self.calls.append(("group", msg))

    async def handle_private_message(self, msg):
        self.calls.append(("private", msg))


def _run(coro):
    return asyncio.run(coro)


def _group_msg(user, nick, text, mid, ts, raw=None):
    return {
        "type": "group", "group_id": "g1", "user_id": str(user),
        "nickname": nick, "message": text,
        "raw_message": raw or text, "message_id": mid, "time": ts,
    }


def _rows(store):
    with store._connect() as conn:
        return conn.execute(
            "SELECT id, qq_id, group_id, message, timestamp, raw_message, "
            "segments, event_key, is_synthetic, quarantined_at "
            "FROM chat_log ORDER BY id"
        ).fetchall()


# ═══════════════════════════════════════════════════════
# 1. 群批处理：逐条落真实事件，各自归属各自用户
# ═══════════════════════════════════════════════════════

def test_group_batch_persists_each_message_to_its_owner(store):
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    t0 = int(time.time()) - 10
    m1 = _group_msg("1001", "小明", "明天去爬山吗", mid=1001, ts=t0,
                    raw="明天去爬山吗[CQ:image,file=a.jpg]")
    m2 = _group_msg("1002", "小红", "我也想去！", mid=1002, ts=t0 + 3)
    assert _run(b.enqueue_group(m1))
    assert _run(b.enqueue_group(m2))
    _run(b._process("group", "g1", 0))

    rows = _rows(store)
    # 两条真实事件各自成行，归属各自用户——不归第一人
    assert len(rows) == 2
    assert rows[0][1] == "1001" and "爬山" in rows[0][3]
    assert rows[1][1] == "1002" and "我也想去" in rows[1][3]
    # 原始 raw/segments/幂等键/时间全部保留
    assert rows[0][5] == "明天去爬山吗[CQ:image,file=a.jpg]"
    segs = json.loads(rows[0][6])
    assert any(s["type"] == "cq" for s in segs)
    assert any(s["type"] == "text" for s in segs)
    assert rows[0][7].startswith("v2:group:g1:") and rows[0][7].endswith(":1001")
    assert rows[1][7].startswith("v2:group:g1:") and rows[1][7].endswith(":1002")
    # 精确时间保留（第一句查询不破坏）：按消息自带 time 落库、顺序递增
    expect_ts0 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))
    expect_ts1 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 + 3))
    assert rows[0][4] == expect_ts0
    assert rows[1][4] == expect_ts1
    # 合并视图到达 handler 且带标记；视图不冒充真实事件 id
    assert h.calls and h.calls[0][0] == "group"
    combined = h.calls[0][1]
    assert combined.get("_batched_events") is True
    assert "同时有 2 个人找你" in combined["message"]
    assert combined["message_id"] == 0


def test_group_batch_context_keeps_original_event_keys(store):
    """合并视图的 ChatContext 必须绑定每条真实入站事件，而非 synthetic 键。"""
    from agent.handler import _chat_context_for_message

    h = _FakeHandler(store)
    b = MessageBatcher(h)
    now = int(time.time())
    msgs = [
        _group_msg("1001", "小明", "a", 6101, now),
        _group_msg("1002", "小红", "b", 6102, now + 1),
    ]
    _run(b._reply_batched_group("g1", msgs))
    combined = h.calls[0][1]
    context = _chat_context_for_message(
        "group", combined, current_message=combined["message"],
        history_messages=[],
    )
    assert context.source_event_keys == (
        f"v2:group:g1:{now}:1001:6101",
        f"v2:group:g1:{now + 1}:1002:6102",
    )


# ═══════════════════════════════════════════════════════
# 2. 合并视图不得落成单一用户 chat_log 行
# ═══════════════════════════════════════════════════════

def test_merged_view_is_not_a_chat_log_row(store):
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    now = int(time.time())
    _run(b.enqueue_group(_group_msg("1001", "小明", "a", 2001, now)))
    _run(b.enqueue_group(_group_msg("1002", "小红", "b", 2002, now + 1)))
    _run(b._process("group", "g1", 0))
    assert len(_rows(store)) == 2  # 只有两条真实行，绝无合成行


# ═══════════════════════════════════════════════════════
# 3. 私聊批处理：同一人多条逐条落库
# ═══════════════════════════════════════════════════════

def test_private_batch_persists_each_message(store):
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    t0 = int(time.time())
    m1 = _group_msg("1001", "小明", "第一句", 3001, t0)
    m1.pop("group_id")
    m1["type"] = "private"
    m2 = dict(m1, message="第二句", message_id=3002, time=t0 + 5)
    assert _run(b.enqueue_private(m1))
    assert _run(b.enqueue_private(m2))
    _run(b._process("private", "1001", 0))

    rows = _rows(store)
    assert len(rows) == 2
    assert {r[3] for r in rows} == {"第一句", "第二句"}
    assert rows[0][2] == ""  # 私聊 group_id 为空
    assert rows[0][7].startswith("v2:private:1001:") and rows[0][7].endswith(":3001")
    assert rows[1][7].startswith("v2:private:1001:") and rows[1][7].endswith(":3002")
    assert h.calls[0][1].get("_batched_events") is True


# ═══════════════════════════════════════════════════════
# 4. 重复事件幂等
# ═══════════════════════════════════════════════════════

def test_duplicate_event_is_idempotent(store):
    store.get_or_create_person("1001", "小明")
    id1 = store.insert_chat("1001", "第一次", group_id="g1", raw_message="第一次",
                            event_key="group:g1:9001", timestamp="2026-08-28 10:00:00")
    dup = store.insert_chat("1001", "第一次", group_id="g1", raw_message="第一次",
                            event_key="group:g1:9001", timestamp="2026-08-28 10:00:05")
    assert id1 is not None and dup is None
    assert len(_rows(store)) == 1
    with store._connect() as conn:
        n = conn.execute("SELECT total_chats FROM people WHERE qq_id='1001'").fetchone()
    assert n is not None and n[0] == 1  # 重复事件不重复记账


def test_replayed_batch_does_not_reenter_handler(store):
    """进程重启后的同批事件重放不得再次触发 LLM/副作用。"""
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    now = int(time.time())
    msgs = [
        _group_msg("1001", "小明", "a", 9101, now),
        _group_msg("1002", "小红", "b", 9102, now + 1),
    ]

    _run(b._reply_batched_group("g1", [dict(item) for item in msgs]))
    assert len(h.calls) == 1
    _run(b._reply_batched_group("g1", [dict(item) for item in msgs]))

    assert len(h.calls) == 1
    assert len(_rows(store)) == 2


def test_no_message_id_does_not_dedupe(store):
    a = store.insert_chat("1001", "x", event_key="")
    b = store.insert_chat("1001", "y", event_key="")
    assert a is not None and b is not None


# ═══════════════════════════════════════════════════════
# 5. quarantine 可回滚 + 读路径排除
# ═══════════════════════════════════════════════════════

def test_quarantine_marks_only_synthetic_and_rolls_back(store):
    store.insert_chat("1001", "真实消息1", group_id="g1")
    store.insert_chat("1001",
                      "【同时有 3 个人找你，请在一段回复里自然地回应所有人。】\n小明 说：x\n",
                      group_id="g1")
    store.insert_chat("1002",
                      "【对方连续发了多条消息，这是完整的对话链。请只做一次回复】\n")
    store.insert_chat("1002", "真实消息2")

    n = store.quarantine_synthetic_chat_logs()
    assert n == 2
    rows = _rows(store)
    marked = [r for r in rows if r[8] == 1]
    assert len(marked) == 2
    assert all(r[9] for r in marked)  # quarantined_at 非空
    assert all(r[9] == "" for r in rows if r[8] == 0)  # 真实行不受影响
    # 幂等：再次执行不重复标记
    assert store.quarantine_synthetic_chat_logs() == 0
    # 回滚
    ids = [r[0] for r in marked]
    assert store.unquarantine_chat_log(ids) == 2
    assert all(r[9] == "" for r in _rows(store))


def test_read_paths_exclude_quarantined(store):
    store.insert_chat("1001", "真实消息", group_id="g1", timestamp="2026-08-28 09:00:00")
    store.insert_chat("1001", "【同时有 2 个人找你，请在一段回复里自然地回应所有人】",
                      group_id="g1", timestamp="2026-08-28 09:01:00")
    # 标记前：历史查询能命中合成行（证明排除确实是 quarantine 生效）
    hits = store.search_chat_history("1001", ["同时有"], group_id="g1")
    assert any("同时有" in h["message"] for h in hits)
    assert store.quarantine_synthetic_chat_logs() == 1
    # 标记后：历史查询 / 最近消息 / 提取数据源 / 最近对话 全部排除
    hits = store.search_chat_history("1001", ["同时有"], group_id="g1")
    assert all("同时有" not in h["message"] for h in hits)
    recent = store.get_user_recent_messages("1001", group_id="g1")
    assert all("同时有" not in r for r in recent)
    unproc = store.get_unprocessed_messages("1001", 0, limit=20)
    assert all("同时有" not in m["message"] for m in unproc)
    dialog = store.get_recent_dialogue("1001", group_id="g1")
    assert all("同时有" not in r for r in dialog)


# ═══════════════════════════════════════════════════════
# 6. quarantine 模式与 batcher 生成格式锁定
# ═══════════════════════════════════════════════════════

def test_quarantine_pattern_locks_with_batcher_format(store):
    """batcher 生成的合并文本必须能被 quarantine 识别（防格式漂移）"""
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    now = int(time.time())
    _run(b.enqueue_group(_group_msg("1001", "小明", "a", 4001, now)))
    _run(b.enqueue_group(_group_msg("1002", "小红", "b", 4002, now + 1)))
    _run(b._process("group", "g1", 0))
    combined = h.calls[0][1]["message"]
    # 模拟历史旧代码行为：把合并文本当 chat_log 落库
    store.insert_chat("1001", combined, group_id="g1")
    assert store.quarantine_synthetic_chat_logs() >= 1

    h2 = _FakeHandler(store)
    b2 = MessageBatcher(h2)
    m = _group_msg("1003", "小刚", "x", 4003, now)
    m.pop("group_id")
    m["type"] = "private"
    _run(b2.enqueue_private(m))
    _run(b2.enqueue_private(dict(m, message="y", message_id=4004, time=now + 1)))
    _run(b2._process("private", "1003", 0))
    combined_p = h2.calls[0][1]["message"]
    store.insert_chat("1003", combined_p)
    assert store.quarantine_synthetic_chat_logs() >= 1


# ═══════════════════════════════════════════════════════
# 7. 单条消息路径不受影响（不预落库、无标记）
# ═══════════════════════════════════════════════════════

def test_single_message_path_unchanged(store):
    h = _FakeHandler(store)
    b = MessageBatcher(h)
    now = int(time.time())
    _run(b.enqueue_group(_group_msg("1001", "小明", "单条", 5001, now)))
    _run(b._process("group", "g1", 0))
    # 单条路径不预落库——由 handler 正常路径负责（原行为不变）
    assert len(_rows(store)) == 0
    assert h.calls[0][1].get("_batched_events") is None
    assert h.calls[0][1].get("_batched") is True


# ═══════════════════════════════════════════════════════
# 8. handler 对合并视图跳过 log_chat 的契约（AST 闸门）
# ═══════════════════════════════════════════════════════

def test_handler_skips_log_chat_for_merged_view():
    """handler 两个入口必须对 _batched_events 视图跳过 log_chat（防好心修复删掉）"""
    import ast
    from pathlib import Path
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = {"handle_group_message", "handle_private_message"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            body = ast.get_source_segment(src, node)
            assert "_batched_events" in body, f"{node.name} 缺少合并视图跳过契约"
            found.add(node.name)
    assert found == targets


# ═══════════════════════════════════════════════════════
# 9. quarantine 过滤贯穿真实消费者（Codex 复核缺口）
# ═══════════════════════════════════════════════════════

def test_real_consumers_exclude_quarantined(store):
    """Codex 复核缺口：quarantine 必须被所有真实消费者（搜索/统计/backlog/
    原始事实查询）过滤，而不是只挡第一层读路径。"""
    import sqlite3
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    store.get_or_create_person("1001", "小明")
    store.get_or_create_person("1002", "小蓝")
    # 1001：g1 真实 → 私聊真实 → g2 合成（最后一行）
    store.insert_chat("1001", "真实群消息1", group_id="g1", timestamp=now)
    store.insert_chat("1001", "真实私聊消息", timestamp=now)
    store.insert_chat("1001",
                      "【同时有 2 个人找你，请在一段回复里自然地回应所有人】",
                      group_id="g2", timestamp=now)
    # 1002：只有一条私聊合成——quarantine 后应完全消失
    store.insert_chat("1002",
                      "【对方连续发了多条消息，这是完整的对话链】", timestamp=now)

    # ── 标记前：真实消费者能看到合成行 ──
    # 搜索（群 search_chat_history 实际入口）
    hits = store.search_chat_keywords("1001", ["找你"], group_id="g2")
    assert any("同时有" in h["message"] for h in hits)
    # 用户统计
    stats = store.count_user_messages("1001", keyword="找你")
    assert stats["total_messages"] == 3
    assert stats["keyword_count"] == 1
    # 最近对话
    lines = store.get_last_conversation("10000", "1001", limit=20)
    assert any("同时有" in l for l in lines)
    # 最后发言群（合成行在 g2，真实行在 g1）
    assert store.find_last_group("1001") == "g2"
    # 早期私聊
    early = store.get_earliest_chats("1002", limit=5)
    assert any("对方连续发了" in e["message"] for e in early)
    # 按日期查询
    by_date = store.get_messages_by_date("1001", today)
    assert len(by_date) == 3
    # 群活跃统计
    act = store.get_group_activity("g2", hours=24)
    assert act["msg_count"] == 1
    # 活跃用户扫描
    active = [r[0] for r in store.get_recent_active_users(days=3)]
    assert "1002" in active
    # backlog 聚合
    scan = dict((q, n) for q, n, _ in store.get_unprocessed_backlog({}))
    assert scan["1001"] == 3 and scan["1002"] == 1
    persisted = {q: n for q, n, _ in store.get_persisted_unprocessed_backlog()}
    assert persisted["1001"] == 3 and persisted["1002"] == 1
    snap = store.get_extraction_backlog_snapshot()
    assert snap["total_user_messages"] == 4
    assert snap["backlog_messages"] == 4
    assert snap["backlog_users"] == 2

    # ── 标记后：全部排除 ──
    assert store.quarantine_synthetic_chat_logs() == 2
    hits = store.search_chat_keywords("1001", ["找你"], group_id="g2")
    assert all("同时有" not in h["message"] for h in hits)
    stats = store.count_user_messages("1001", keyword="找你")
    assert stats["total_messages"] == 2
    assert stats["keyword_count"] == 0
    lines = store.get_last_conversation("10000", "1001", limit=20)
    assert all("同时有" not in l for l in lines)
    assert store.find_last_group("1001") == "g1"
    early = store.get_earliest_chats("1002", limit=5)
    assert all("对方连续发了" not in e["message"] for e in early)
    by_date = store.get_messages_by_date("1001", today)
    assert len(by_date) == 2
    act = store.get_group_activity("g2", hours=24)
    assert act["msg_count"] == 0
    active = [r[0] for r in store.get_recent_active_users(days=3)]
    assert "1002" not in active
    scan = dict((q, n) for q, n, _ in store.get_unprocessed_backlog({}))
    assert scan["1001"] == 2 and "1002" not in scan
    persisted = {q: n for q, n, _ in store.get_persisted_unprocessed_backlog()}
    assert persisted["1001"] == 2 and "1002" not in persisted
    snap = store.get_extraction_backlog_snapshot()
    assert snap["total_user_messages"] == 2
    assert snap["backlog_messages"] == 2
    assert snap["backlog_users"] == 1


# ═══════════════════════════════════════════════════════
# 10. insert_chat 收窄：非去重完整性错误必须抛出（Codex 复核）
# ═══════════════════════════════════════════════════════

def test_insert_chat_raises_non_dedupe_integrity_errors(store):
    """只有「非空 event_key 的唯一键冲突」幂等返回 None；
    NOT NULL 等其他完整性错误必须继续抛出，不得静默吞错。"""
    import sqlite3
    store.insert_chat("1001", "x", event_key="group:g1:1")
    # 重复 event_key → None（幂等，行为不变）
    assert store.insert_chat("1001", "x", event_key="group:g1:1") is None
    # qq_id 为 None 违反 NOT NULL → 必须抛出，绝不能被当成重复事件吞掉
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_chat(None, "x")
