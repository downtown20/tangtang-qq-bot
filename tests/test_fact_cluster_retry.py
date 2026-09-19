import asyncio
import json
import time
import types

from agent.handler import MessageHandler


class _Store:
    def __init__(self):
        self.values = {}

    def kv_get(self, key):
        return self.values.get(key, "")

    def kv_set(self, key, value):
        self.values[key] = value

    def get_unprocessed_messages(self, user_id, cursor, limit, include_bot_replies=True):
        return [
            {"id": index, "timestamp": "2026-08-28 12:00", "message": "消息"}
            for index in range(1, 9)
        ]


def test_fact_cluster_failure_backs_off_same_cursor():
    async def scenario():
        store = _Store()
        calls = {"n": 0}

        async def extract_fact_clusters(**kwargs):
            calls["n"] += 1
            return {"ok": False, "new_facts": 0}

        memory = types.SimpleNamespace(
            store=store,
            extract_fact_clusters=extract_fact_clusters,
            get_or_create_person=lambda user_id: {"nickname": "测试"},
        )
        handler = object.__new__(MessageHandler)
        handler.memory = memory
        handler._llm_lock = asyncio.Lock()
        handler.embed_engine = None
        handler.metrics = types.SimpleNamespace(incr=lambda *args: None)

        await handler._extract_fact_clusters_task("u1")
        assert calls["n"] == 1
        state = json.loads(store.kv_get("fact_cluster_retry:u1"))
        assert state["cursor"] == 0
        assert state["failures"] == 1
        assert state["next_retry_at"] > time.time()

        await handler._extract_fact_clusters_task("u1")
        assert calls["n"] == 1

    asyncio.run(scenario())


def test_fact_cluster_success_clears_backoff_state():
    async def scenario():
        store = _Store()

        async def extract_fact_clusters(**kwargs):
            return {"ok": True, "new_facts": 0}

        memory = types.SimpleNamespace(
            store=store,
            extract_fact_clusters=extract_fact_clusters,
            get_or_create_person=lambda user_id: {"nickname": "测试"},
        )
        handler = object.__new__(MessageHandler)
        handler.memory = memory
        handler._llm_lock = asyncio.Lock()
        handler.embed_engine = None
        handler.metrics = types.SimpleNamespace(incr=lambda *args: None)
        store.kv_set(
            "fact_cluster_retry:u1",
            json.dumps({"cursor": 0, "failures": 3, "next_retry_at": 0}),
        )

        await handler._extract_fact_clusters_task("u1")
        assert store.kv_get("fact_cluster_retry:u1") == ""
        assert store.kv_get("fact_cluster_last_chat_id:u1") == "8"

    asyncio.run(scenario())


def test_fact_cluster_failure_gets_an_independent_retry(monkeypatch):
    async def scenario():
        store = _Store()
        calls = {"n": 0}

        async def extract_fact_clusters(**kwargs):
            calls["n"] += 1
            return (
                {"ok": False, "new_facts": 0}
                if calls["n"] == 1
                else {"ok": True, "new_facts": 0}
            )

        memory = types.SimpleNamespace(
            store=store,
            extract_fact_clusters=extract_fact_clusters,
            get_or_create_person=lambda user_id: {"nickname": "测试"},
        )
        handler = object.__new__(MessageHandler)
        handler.memory = memory
        handler._llm_lock = asyncio.Lock()
        handler.embed_engine = None
        handler.metrics = types.SimpleNamespace(incr=lambda *args: None)
        scheduled = []
        handler._safe_task = lambda coro, name="": scheduled.append(coro)

        # 让定时器立即到期。

        async def immediate_sleep(_delay):
            return None

        monkeypatch.setattr(asyncio, "sleep", immediate_sleep)
        await handler._extract_fact_clusters_task("u1")
        retry = json.loads(store.kv_get("fact_cluster_retry:u1"))
        retry["next_retry_at"] = 0
        store.kv_set("fact_cluster_retry:u1", json.dumps(retry))
        assert len(scheduled) == 1
        await scheduled[0]

        assert calls["n"] == 2
        assert store.kv_get("fact_cluster_last_chat_id:u1") == "8"
        assert store.kv_get("fact_cluster_retry:u1") == ""

    asyncio.run(scenario())
