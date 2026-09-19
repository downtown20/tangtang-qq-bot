"""P0-D2：严格历史查询 + 记忆证据校验（2026-08-28 协作任务包）

验收点：
  1. history_query：时间范围/顺序/说话人由数据层 SQL 保证（不用 LIKE 关键词替代）
  2. 返回 source chat_log row id、精确 timestamp、speaker QQ、group、is_bot、message_id
  3. 会话权限：只能查当前授权会话；群聊只能查自己或糖糖（跨用户隔离）
  4. 工具目录：history_query 常驻可用
  5. 记忆证据校验（审查 Critical 3）：evidence_quote 必须被证据原文支持；
     推断（claim_type=inferred）或低置信（<0.75）不得写 verified
"""
import asyncio
import json
import time
from pathlib import Path

import numpy as np

from agent.handler import MessageHandler


def _run(coro):
    return asyncio.run(coro)


def _row(store, sql, *params):
    with store._connect() as conn:
        return conn.execute(sql, params).fetchone()


def _insert_chat(store, qq, msg, group="", ts="2026-08-28 10:00:00",
                 mid=0, bot=False):
    return store.insert_chat(qq, msg, group_id=group, is_bot=bot,
                             timestamp=ts,
                             event_key=(f"group:{group}:{mid}" if group else
                                        f"private:{qq}:{mid}") if mid else "")


# ═══════════════════════════════════════════════════════
# 1. Store.query_chat_history：时间范围/顺序/speaker/limit/quarantine
# ═══════════════════════════════════════════════════════

def test_query_group_time_range_and_order(store):
    _insert_chat(store, "1001", "早上好", "g1", "2026-08-28 08:00:00", mid=1)
    _insert_chat(store, "1002", "中午好", "g1", "2026-08-28 12:00:00", mid=2)
    _insert_chat(store, "1001", "晚上好", "g1", "2026-08-28 20:00:00", mid=3)

    rows = store.query_chat_history(
        chat_type="group", chat_id="g1",
        from_ts="2026-08-28 09:00", to_ts="2026-08-28 21:00",
        order="asc", limit=10)
    assert [r["message"] for r in rows] == ["中午好", "晚上好"]  # 时间范围过滤
    assert [r["timestamp"] for r in rows] == [
        "2026-08-28 12:00:00", "2026-08-28 20:00:00"]          # asc 顺序
    # 返回字段完整：source row id / speaker / group / is_bot / message_id
    r = rows[0]
    assert set(r) == {"chat_log_id", "qq_id", "group_id", "is_bot",
                      "message", "timestamp", "message_id"}
    assert r["qq_id"] == "1002" and r["group_id"] == "g1"
    assert r["is_bot"] is False
    assert r["message_id"] == 2          # 从 event_key 解析 platform message_id


def test_query_desc_and_speaker_filter(store):
    _insert_chat(store, "1001", "第一句", "g1", "2026-08-28 08:00:00", mid=1)
    _insert_chat(store, "1002", "第二句", "g1", "2026-08-28 09:00:00", mid=2)
    _insert_chat(store, "1001", "第三句", "g1", "2026-08-28 10:00:00", mid=3)

    rows = store.query_chat_history(chat_type="group", chat_id="g1",
                                    speaker="1001", order="desc", limit=10)
    assert [r["message"] for r in rows] == ["第三句", "第一句"]   # 说话人过滤 + desc


def test_query_private_and_bot_rows(store):
    _insert_chat(store, "1001", "私聊一句", "", "2026-08-28 08:00:00", mid=9)
    _insert_chat(store, "1001", "糖糖回复", "", "2026-08-28 08:01:00", bot=True)
    rows = store.query_chat_history(chat_type="private", chat_id="1001",
                                    order="asc", limit=10)
    assert [r["is_bot"] for r in rows] == [False, True]          # 私聊含 bot 行
    assert rows[1]["qq_id"] == "1001" and rows[1]["message_id"] == 0


def test_query_limit_capped(store):
    for i in range(60):
        _insert_chat(store, "1001", f"m{i}", "g1",
                     f"2026-08-28 {i // 60:02d}:{i % 60:02d}:00")
    rows = store.query_chat_history(chat_type="group", chat_id="g1",
                                    order="asc", limit=999)
    assert len(rows) == 50  # 上限 50


def test_query_excludes_quarantined(store):
    _insert_chat(store, "1001", "真实消息", "g1", "2026-08-28 08:00:00")
    store.insert_chat("1001", "【同时有 2 个人找你，请在一段回复里自然地回应所有人】",
                      group_id="g1", timestamp="2026-08-28 09:00:00")
    store.quarantine_synthetic_chat_logs()
    rows = store.query_chat_history(chat_type="group", chat_id="g1",
                                    order="asc", limit=10)
    assert [r["message"] for r in rows] == ["真实消息"]          # quarantine 排除


# ═══════════════════════════════════════════════════════
# 2. handler 薄适配：会话权限 / scope / 越权拒绝
# ═══════════════════════════════════════════════════════

def _fake_handler(store, owner="owner-1", bot_qq="10000"):
    handler = type("H", (), {
        "memory": type("M", (), {"store": store})(),
        "owner_qq": owner, "bot_qq": bot_qq,
        "_allowed_groups": {"g1"},
    })()
    handler._run_store_io = MessageHandler._run_store_io.__get__(handler)
    return handler


def test_history_query_current_group_scope(store):
    _insert_chat(store, "1001", "早上好", "g1", "2026-08-28 08:00:00", mid=1)
    _insert_chat(store, "1001", "晚上好", "g1", "2026-08-28 20:00:00", mid=2)
    h = _fake_handler(store)
    result = _run(MessageHandler._execute_tool(
        h, "history_query",
        {"order": "asc", "from": "2026-08-28 07:00"},
        "g1", "1001", {"respond": True}))
    assert "早上好" in result and "晚上好" in result
    assert "row=" in result and "msg_id=1" in result and "08:00:00" in result


def test_history_query_first_sentence_asc(store):
    _insert_chat(store, "1001", "第一句", "g1", "2026-08-28 08:00:00", mid=1)
    _insert_chat(store, "1001", "第二句", "g1", "2026-08-28 09:00:00", mid=2)
    h = _fake_handler(store)
    result = _run(MessageHandler._execute_tool(
        h, "history_query", {"order": "asc", "limit": 1},
        "g1", "1001", {"respond": True}))
    assert "第一句" in result and "第二句" not in result  # 第一句精确命中


def test_history_query_private_scope(store):
    _insert_chat(store, "1001", "私聊内容", "", "2026-08-28 08:00:00")
    h = _fake_handler(store)
    result = _run(MessageHandler._execute_tool(
        h, "history_query", {}, "_private_1001", "1001", {"respond": True}))
    assert "私聊内容" in result


def test_history_query_cross_user_rejected(store):
    """群聊查他人发言 → 拒绝（跨用户读取隔离）"""
    _insert_chat(store, "1002", "别人的话", "g1", "2026-08-28 08:00:00")
    h = _fake_handler(store)
    result = _run(MessageHandler._execute_tool(
        h, "history_query", {"speaker": "1002"},
        "g1", "1001", {"respond": True}))
    assert "只能查询自己或糖糖" in result


def test_history_query_no_result(store):
    h = _fake_handler(store)
    result = _run(MessageHandler._execute_tool(
        h, "history_query", {"from": "2020-01-01", "to": "2020-01-02"},
        "g1", "1001", {"respond": True}))
    assert "没有找到" in result


def test_history_query_slow_store_does_not_block_event_loop():
    """历史证据查询的慢 SQLite 读取不能卡住同回合事件循环。"""
    class SlowStore:
        def query_chat_history(self, **_kwargs):
            time.sleep(0.08)
            return []

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "history_query", {}, "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到" in result
    assert ticks > 0


def test_messages_by_date_slow_store_does_not_block_event_loop():
    """按日期读取记忆证据时，慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def get_messages_by_date(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "get_messages_by_date",
                {"date": "2026-08-28", "subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到" in result
    assert ticks > 0


def test_search_keywords_slow_store_does_not_block_event_loop():
    """关键词历史读取的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def search_chat_keywords(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_keywords",
                {"keywords": "咖啡", "subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到" in result
    assert ticks > 0


def test_group_activity_slow_store_does_not_block_event_loop():
    """群活跃读取的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def get_group_activity(self, *_args, **_kwargs):
            time.sleep(0.08)
            return {"msg_count": 0, "group_name": "", "people_count": 0,
                    "top5": []}

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "get_group_activity",
                {"group_id": "g1", "hours": 24},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有消息" in result
    assert ticks > 0


def test_last_conversation_slow_store_does_not_block_event_loop():
    """最近对话读取的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def get_last_conversation(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "get_last_conversation",
                {"subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到" in result
    assert ticks > 0


def test_count_messages_slow_store_does_not_block_event_loop():
    """消息统计的慢 SQLite 读取不能卡住事件循环。"""
    class SlowStore:
        def count_user_messages(self, *_args, **_kwargs):
            time.sleep(0.08)
            return {"total_messages": 0, "keyword_count": 0,
                    "first_seen": "", "last_seen": ""}

    memory = type("M", (), {
        "store": SlowStore(),
        "get_or_create_person": lambda self, _qq: {"nickname": "1001"},
    })()
    h = type("H", (), {
        "memory": memory, "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "count_messages",
                {"subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "总消息数：0条" in result
    assert ticks > 0


def test_first_met_slow_person_lookup_does_not_block_event_loop():
    """初次认识的人物读取不能卡住事件循环。"""
    class SlowMemory:
        def get_or_create_person(self, _qq):
            time.sleep(0.08)
            return {"nickname": "1001", "first_met": "", "total_chats": 0}

    memory = SlowMemory()
    h = type("H", (), {
        "memory": memory, "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "get_first_met",
                {"subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "不记得什么时候第一次见到" in result
    assert ticks > 0


def test_recent_messages_slow_memory_lookup_does_not_block_event_loop():
    """最近消息的人物历史读取不能卡住事件循环。"""
    class SlowMemory:
        def get_user_recent_messages(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    memory = SlowMemory()
    h = type("H", (), {
        "memory": memory, "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "get_recent_messages",
                {"subject_qq": "1001", "limit": 10},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到最近的聊天记录" in result
    assert ticks > 0


def test_search_chat_history_slow_store_does_not_block_event_loop():
    """原始群聊历史读取的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def search_chat_keywords(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = _fake_handler(SlowStore(), bot_qq="10000")

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_chat_history",
                {"query": "咖啡", "subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没有找到相关的原始聊天记录" in result
    assert ticks > 0


def test_private_search_chat_history_slow_index_count_does_not_block_event_loop():
    """私聊聊天索引计数的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def count_chat_index(self, *_args, **_kwargs):
            time.sleep(0.08)
            return 0

    h = _fake_handler(SlowStore())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_chat_history",
                {"query": "咖啡", "subject_qq": "1001"},
                "_private_1001", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "聊天索引尚未构建" in result
    assert ticks > 0


def test_private_search_chat_history_slow_index_query_does_not_block_event_loop():
    """私聊聊天索引语义查询的慢 SQLite 不能卡住事件循环。"""
    class SlowStore:
        def search_chat_index(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

        def count_chat_index(self, *_args, **_kwargs):
            return 0

    h = _fake_handler(SlowStore())
    h.embed_engine = type("E", (), {
        "ready": True,
        "encode": lambda self, _query: [0.1],
    })()

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_chat_history",
                {"query": "咖啡", "subject_qq": "1001"},
                "_private_1001", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "聊天索引尚未构建" in result
    assert ticks > 0


def test_private_search_chat_history_slow_embedding_does_not_block_event_loop():
    """私聊聊天索引的查询向量编码也不能在协程中同步运行。"""
    class FastStore:
        def search_chat_index(self, *_args, **_kwargs):
            return []

        def count_chat_index(self, *_args, **_kwargs):
            return 0

    class SlowEmbed:
        ready = True

        def encode(self, _query):
            time.sleep(0.08)
            return [0.1]

    h = _fake_handler(FastStore())
    h.embed_engine = SlowEmbed()

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_chat_history",
                {"query": "咖啡", "subject_qq": "1001"},
                "_private_1001", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "聊天索引尚未构建" in result
    assert ticks >= 10


def test_private_semantic_history_projects_chat_log_evidence_and_private_scope(store):
    """私聊向量检索必须回投时间/来源，并排除误入索引的群消息。"""
    private_id = _insert_chat(
        store, "1001", "私聊原文", "", "2026-08-28 09:15:00",
    )
    group_id = _insert_chat(
        store, "1001", "群聊误入索引", "g1", "2026-08-28 09:16:00",
    )
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    store.index_chat(private_id, "1001", "私聊清洗文本", vector)
    store.index_chat(group_id, "1001", "群聊清洗文本", vector)

    rows = store.search_chat_index("1001", vector, top_k=5)

    assert [row["chat_id"] for row in rows] == [private_id]
    assert rows[0]["timestamp"] == "2026-08-28 09:15:00"
    assert rows[0]["source"] == f"chat_log#{private_id}"
    assert rows[0]["group_id"] == ""


def test_private_semantic_history_drops_orphan_or_misattributed_index_rows(store):
    """索引行缺少原始 chat_log 或主体不一致时不能被投影为私聊证据。"""
    private_id = _insert_chat(
        store, "1001", "私聊原文", "", "2026-08-28 09:15:00",
    )
    other_id = _insert_chat(
        store, "1002", "另一人的原文", "", "2026-08-28 09:16:00",
    )
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    store.index_chat(private_id, "1001", "正常索引", vector)
    store.index_chat(999999, "1001", "孤儿索引", vector)
    store.index_chat(other_id, "1001", "错主体索引", vector)

    rows = store.search_chat_index("1001", vector, top_k=5)

    assert [row["chat_id"] for row in rows] == [private_id]


def test_private_semantic_history_output_keeps_time_and_source(monkeypatch):
    """工具适配层不能把带证据的语义命中降级成无时间文本。"""
    class FastStore:
        def search_chat_index(self, *_args, **_kwargs):
            return [{
                "chat_id": 42, "text": "昨天说过的原文", "score": 0.91,
                "timestamp": "2026-08-28 09:15:00", "source": "chat_log#42",
                "is_bot_reply": False,
            }]

    h = _fake_handler(FastStore())
    h.embed_engine = type("E", (), {
        "ready": True,
        "encode": lambda self, _query: [0.1],
    })()

    result = asyncio.run(MessageHandler._execute_tool(
        h, "search_chat_history",
        {"query": "昨天", "subject_qq": "1001"},
        "_private_1001", "1001", {"respond": True},
    ))

    assert "2026-08-28 09:15" in result
    assert "chat_log#42" in result
    assert "昨天说过的原文" in result


def test_search_facts_slow_memory_lookup_does_not_block_event_loop():
    """可信事实检索的慢记忆读取不能卡住事件循环。"""
    class SlowMemory:
        def search_fact_clusters(self, *_args, **_kwargs):
            time.sleep(0.08)
            return "(没有可信事实)"

    h = type("H", (), {
        "memory": SlowMemory(), "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
        "self_state": type("S", (), {
            "drives": type("D", (), {"release_by_action": lambda *_a: None})(),
        })(),
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_facts",
                {"subject_qq": "1001", "query": "咖啡"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert result == "(没有可信事实)"
    assert ticks > 0


def test_search_relations_slow_memory_lookup_does_not_block_event_loop():
    """关系记忆检索的慢读取不能卡住事件循环。"""
    class SlowMemory:
        def search_relation_triples(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = type("H", (), {
        "memory": SlowMemory(), "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_relations",
                {"name": "10001", "subject_qq": "10001"},
                "g1", "10001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "没找到关于" in result
    assert ticks > 0


def test_search_memories_slow_recall_does_not_block_event_loop():
    """可信记忆召回的慢读取不能卡住事件循环。"""
    class SlowMemory:
        def recall(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = type("H", (), {
        "memory": SlowMemory(), "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"}, "reranker": None,
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_memories",
                {"query": "咖啡", "subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "记忆库中没有" in result
    assert ticks > 0


def test_search_memories_slow_embedding_does_not_block_event_loop():
    """记忆语义查询的向量编码不能冻结事件循环。"""
    class FastMemory:
        def recall(self, *_args, **_kwargs):
            return []

    class SlowEmbed:
        ready = True

        def encode(self, _query):
            time.sleep(0.08)
            return [0.1]

    h = type("H", (), {
        "memory": FastMemory(), "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"}, "reranker": None,
        "embed_engine": SlowEmbed(),
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_memories",
                {"query": "咖啡", "subject_qq": "1001"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "记忆库中没有" in result
    assert ticks >= 10


def test_search_episodes_slow_reflection_lookup_does_not_block_event_loop():
    """情节摘要读取的慢 SQLite 不能卡住事件循环。"""
    class SlowReflection:
        def search_digests(self, *_args, **_kwargs):
            time.sleep(0.08)
            return []

    h = type("H", (), {
        "memory": type("M", (), {})(), "reflection": SlowReflection(),
        "owner_qq": "owner-1", "bot_qq": "10000", "_allowed_groups": {"g1"},
    })()
    h._run_store_io = MessageHandler._run_store_io.__get__(h)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await MessageHandler._execute_tool(
                h, "search_episodes",
                {"query": "昨天"},
                "g1", "1001", {"respond": True},
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(scenario())
    assert "当前群还没有可用的情节摘要" in result
    assert ticks > 0


def test_history_query_in_catalog():
    """工具目录：history_query 常驻可用（不 deferred）"""
    h = object.__new__(MessageHandler)
    h._allowed_groups = {"g1"}
    tools = h._build_memory_tools("1001", group_id="g1", has_image=False)
    names = {t["function"]["name"] for t in tools}
    assert "history_query" in names
    tool = next(t for t in tools if t["function"]["name"] == "history_query")
    assert not tool.get("_deferred_category")  # 始终可用


# ═══════════════════════════════════════════════════════
# 3. 记忆证据校验（Critical 3）：quote 支持 + 低置信/推断不写 verified
# ═══════════════════════════════════════════════════════

def _apply(store, item, qq="1001"):
    """在事务里应用一条提取项，返回记忆行 dict 或 None"""
    with store._connect() as conn:
        conn.execute("BEGIN")
        n = store._apply_extraction_item(conn, job_id=99, qq_id=qq,
                                         item=item, origin="extracted")
        if n:
            row = conn.execute(
                "SELECT value, trust_level, confidence, evidence_ids FROM memories "
                "WHERE qq_id=? ORDER BY id DESC LIMIT 1", (qq,)
            ).fetchone()
        else:
            row = None
        conn.commit()
    return row


def test_evidence_quote_must_be_supported_by_source(store):
    """quote 必须是证据原文的子串——不匹配的 claim 拒绝入库"""
    chat_id = _insert_chat(store, "1001", "我在北大读计算机",
                           "g1", "2026-08-28 08:00:00", mid=1)
    row = _apply(store, {
        "type": "identity", "value": "在北大读计算机",
        "confidence": 0.95, "claim_type": "stated",
        "evidence_quote": "我在北大读计算机", "evidence_ids": [chat_id],
        "source_group_id": "g1",
    })
    assert row is not None
    assert row[1] == "verified"   # quote 支持 → verified

    # 不匹配的 quote（原文里没有）→ 拒绝
    row2 = _apply(store, {
        "type": "identity", "value": "在清华读书",
        "confidence": 0.95, "claim_type": "stated",
        "evidence_quote": "我在清华读书", "evidence_ids": [chat_id],
        "source_group_id": "g1",
    })
    assert row2 is None  # 未入库——LLM 不能拿真实消息包装幻觉


def test_evidence_quote_absent_is_quarantined(store):
    """旧提取器无 quote → 保留记录但不得进入可信记忆层"""
    chat_id = _insert_chat(store, "1001", "喜欢喝咖啡", "g1",
                           "2026-08-28 08:00:00", mid=1)
    row = _apply(store, {
        "type": "preference", "value": "喜欢喝咖啡",
        "confidence": 0.9, "evidence_ids": [chat_id],
        "source_group_id": "g1",
    })
    assert row is not None and row[1] == "legacy_unverified"


def test_query_date_only_to_is_inclusive_for_the_whole_day(store):
    """日期粒度的 to 边界应包含当天最后一条消息。"""
    _insert_chat(store, "1001", "当天上午", "g1", "2026-08-28 08:00:00")
    _insert_chat(store, "1001", "当天深夜", "g1", "2026-08-28 23:59:59")
    _insert_chat(store, "1001", "次日", "g1", "2026-08-29 00:00:00")
    rows = store.query_chat_history(
        chat_type="group", chat_id="g1", to_ts="2026-08-28", order="asc",
    )
    assert [row["message"] for row in rows] == ["当天上午", "当天深夜"]


def test_inferred_claim_never_verified(store):
    """claim_type=inferred（推断）即使高置信也不得进入 verified"""
    chat_id = _insert_chat(store, "1001", "每天都要来一杯",
                           "g1", "2026-08-28 08:00:00", mid=1)
    row = _apply(store, {
        "type": "preference", "value": "喜欢喝咖啡",
        "confidence": 0.9, "claim_type": "inferred",
        "evidence_quote": "每天都要来一杯", "evidence_ids": [chat_id],
        "source_group_id": "g1",
    })
    assert row is not None
    assert row[1] == "unverified"  # 推断 → 待复核层，不进 trusted recall


def test_low_confidence_never_verified(store):
    """低置信（0.5-0.75）stated 也不得写 verified"""
    chat_id = _insert_chat(store, "1001", "可能是喜欢咖啡吧",
                           "g1", "2026-08-28 08:00:00", mid=1)
    row = _apply(store, {
        "type": "preference", "value": "喜欢咖啡",
        "confidence": 0.6, "claim_type": "stated",
        "evidence_quote": "可能是喜欢咖啡吧", "evidence_ids": [chat_id],
        "source_group_id": "g1",
    })
    assert row is not None
    assert row[1] == "unverified"


def test_extraction_schema_contract():
    """提取 schema 必须要求 evidence_quote/claim_type（防 LLM 不产出）"""
    src = Path("agent/memory.py").read_text(encoding="utf-8")
    assert "evidence_quote" in src
    assert "claim_type" in src
    assert "inferred" in src


# ═══════════════════════════════════════════════════════
# 4. 旧路径收口：_persist_extracted_items → _deduped_remember → insert_memory
# ═══════════════════════════════════════════════════════

def _insert_legacy(store, item, qq="1001"):
    """走 insert_memory 旧路径（带证据校验参数），返回 trust_level"""
    mem_id = store.insert_memory(
        qq, "fact", item["value"], importance=5,
        cognitive="semantic", confidence=item.get("confidence", 0.7),
        origin="extracted", source_group_id=item.get("source_group_id", ""),
        evidence_ids=item.get("evidence_ids", ""),
        evidence_quote=item.get("evidence_quote", ""),
        claim_type=item.get("claim_type", "stated"),
    )
    row = _row(store, "SELECT trust_level FROM memories WHERE id=?", mem_id)
    return row[0] if row else None


def test_legacy_path_quote_stated_verified(store):
    """旧路径：quote 匹配 + stated → verified"""
    chat_id = _insert_chat(store, "1001", "我在北大读计算机",
                           "g1", "2026-08-28 08:00:00", mid=1)
    trust = _insert_legacy(store, {
        "value": "在北大读计算机", "confidence": 0.95,
        "evidence_quote": "我在北大读计算机",
        "evidence_ids": str(chat_id), "source_group_id": "g1",
    })
    assert trust == "verified"


def test_legacy_path_missing_quote_legacy_unverified(store):
    """旧路径收口核心：有 evidence_ids 但无 quote → legacy_unverified
    （来源存在性 ≠ claim 被支持）"""
    chat_id = _insert_chat(store, "1001", "喜欢喝咖啡",
                           "g1", "2026-08-28 08:00:00", mid=1)
    trust = _insert_legacy(store, {
        "value": "喜欢喝咖啡", "confidence": 0.9,
        "evidence_ids": str(chat_id), "source_group_id": "g1",
    })
    assert trust == "legacy_unverified"  # 不能因 evidence_ids 存在而 verified


def test_legacy_path_inferred_legacy_unverified(store):
    """旧路径：claim_type=inferred 即使 quote 匹配也不得 verified"""
    chat_id = _insert_chat(store, "1001", "每天都要来一杯",
                           "g1", "2026-08-28 08:00:00", mid=1)
    trust = _insert_legacy(store, {
        "value": "喜欢喝咖啡", "confidence": 0.9, "claim_type": "inferred",
        "evidence_quote": "每天都要来一杯",
        "evidence_ids": str(chat_id), "source_group_id": "g1",
    })
    assert trust == "legacy_unverified"


def test_legacy_path_bot_evidence_does_not_raise(store):
    """机器人证据（is_bot_reply=1）不能提升普通记忆的信任"""
    bot_id = _insert_chat(store, "10000", "糖糖自己的话",
                          "g1", "2026-08-28 08:00:00", mid=1, bot=True)
    trust = _insert_legacy(store, {
        "value": "糖糖自己的话", "confidence": 0.95,
        "evidence_quote": "糖糖自己的话",
        "evidence_ids": str(bot_id), "source_group_id": "g1",
    })
    assert trust == "legacy_unverified"  # 证据校验拒绝 bot 行 → 不提升


def test_legacy_path_cross_user_evidence_does_not_raise(store):
    """跨用户证据（他人消息）不能提升信任"""
    other_id = _insert_chat(store, "1002", "别人的话",
                            "g1", "2026-08-28 08:00:00", mid=1)
    trust = _insert_legacy(store, {
        "value": "别人的话", "confidence": 0.95,
        "evidence_quote": "别人的话",
        "evidence_ids": str(other_id), "source_group_id": "g1",
    })
    assert trust == "legacy_unverified"


def test_persist_extracted_items_passthrough():
    """旧提取路径必须把 evidence_quote/claim_type 透传给 _deduped_remember"""
    from agent.handler import MessageHandler

    calls = {}

    class _Mem:
        def add_alias(self, *a, **k):
            return None

        def _deduped_remember(self, *a, **k):
            calls.update(k)
            return None

    h = object.__new__(MessageHandler)
    h.memory = _Mem()
    h.embed_engine = None

    h._persist_extracted_items("1001", [{
        "type": "preference", "value": "喜欢喝咖啡",
        "confidence": 0.9, "claim_type": "inferred",
        "evidence_quote": "每天都要来一杯",
        "evidence_ids": [1], "source_group_id": "g1",
    }])
    assert calls.get("evidence_quote") == "每天都要来一杯"
    assert calls.get("claim_type") == "inferred"


# ═══════════════════════════════════════════════════════
# 5. 第二次证据不得污染已 verified 记忆（update_memory_evidence 收口）
# ═══════════════════════════════════════════════════════

def test_second_evidence_unsupported_quote_rejected_no_pollution(store):
    """已 verified 记忆的第二次证据：quote 不被新证据原文支持 → 拒绝 union，
    证据/信任均不污染"""
    chat_a = _insert_chat(store, "1001", "我最喜欢草莓蛋糕",
                          "g1", "2026-08-28 08:00:00", mid=1)
    chat_b = _insert_chat(store, "1001", "想喝咖啡了",
                          "g1", "2026-08-28 09:00:00", mid=2)

    mem_id = store.insert_memory(
        "1001", "like", "喜欢草莓蛋糕", importance=5,
        origin="extracted", source_group_id="g1",
        evidence_ids=str(chat_a),
        evidence_quote="我最喜欢草莓蛋糕", claim_type="stated",
    )
    assert _row(store, "SELECT trust_level FROM memories WHERE id=?", mem_id)[0] == "verified"

    # 第二次证据（quote 指向咖啡——与 claim 无关/不被支持）→ 拒绝
    ok = store.update_memory_evidence(
        mem_id, str(chat_b), "g1", evidence_quote="我最喜欢草莓蛋糕")
    assert ok is False  # 不 union
    assert _row(store, "SELECT evidence_ids FROM memories WHERE id=?", mem_id)[0] == str(chat_a)
    assert _row(store, "SELECT trust_level FROM memories WHERE id=?", mem_id)[0] == "verified"

    # 第二次证据 quote 被支持 → 正常 union
    chat_c = _insert_chat(store, "1001", "草莓蛋糕真的好吃",
                          "g1", "2026-08-28 10:00:00", mid=3)
    ok2 = store.update_memory_evidence(
        mem_id, f"{chat_b},{chat_c}", "g1",
        evidence_quote="草莓蛋糕真的好吃")
    assert ok2 is True
    ids = _row(store, "SELECT evidence_ids FROM memories WHERE id=?", mem_id)[0]
    assert str(chat_c) in ids  # 新证据加入
    assert str(chat_b) in ids


# ═══════════════════════════════════════════════════════
# 6. query_chat_history：to_ts 仅日期应包含整天
# ═══════════════════════════════════════════════════════

def test_query_to_ts_date_only_includes_whole_day(store):
    """to_ts='YYYY-MM-DD' 必须包含当天所有消息（含 23:59 之后）"""
    _insert_chat(store, "1001", "早上消息", "g1", "2026-08-28 08:00:00", mid=1)
    _insert_chat(store, "1001", "深夜消息", "g1", "2026-08-28 23:59:59", mid=2)
    _insert_chat(store, "1001", "次日消息", "g1", "2026-08-29 00:01:00", mid=3)

    rows = store.query_chat_history(
        chat_type="group", chat_id="g1",
        to_ts="2026-08-28", order="asc", limit=10)
    assert [r["message"] for r in rows] == ["早上消息", "深夜消息"]  # 整天包含
    assert "次日消息" not in [r["message"] for r in rows]

    # from_ts 仅日期 → 当天 00:00:00 起
    rows2 = store.query_chat_history(
        chat_type="group", chat_id="g1",
        from_ts="2026-08-28", to_ts="2026-08-28", order="asc", limit=10)
    assert [r["message"] for r in rows2] == ["早上消息", "深夜消息"]
