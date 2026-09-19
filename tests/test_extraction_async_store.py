"""后台记忆提取的 SQLite 调用不能阻塞事件循环。"""

import asyncio
import json
import logging
import threading
import time
import types
from pathlib import Path

from agent.async_io import STORE_IO_MAX_CONCURRENCY
from agent.handler_autonomy import AutonomyMixin
from agent.handler import MessageHandler
from agent.memory import MemorySystem


def test_autonomous_startup_cleanup_marks_day_via_store_worker():
    """启动清理完成标记不能在自治事件循环中直接写 SQLite。"""
    src = Path("agent/handler_autonomy.py").read_text(encoding="utf-8")
    startup = src[src.index("async def _autonomous_loop"):src.index("def _maybe_daily_metrics_aggregate")]
    assert '"autonomy.mark_cleanup_ran"' in startup
    assert "await self._run_store_io" in startup
    assert "                mark_cleanup_ran(self)" not in startup


class _SlowQueueStore:
    def __init__(self):
        self.thread_ids = []

    def get_extraction_queue_health(self):
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.08)
        return {"total_open": 8}


class _ReadyJobStore:
    def __init__(self):
        self.thread_ids = []

    def _pause(self):
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.02)

    def list_resumable_extraction_jobs(self, limit=10):
        self._pause()
        return [{
            "id": 7,
            "qq_id": "20002",
            "status": "ready",
            "direction": "forward",
            "cursor_chat_id": 42,
            "created_at": "2026-08-30 00:00:00",
        }]

    def complete_extraction_job(self, job_id, origin="extracted"):
        self._pause()
        return {"status": "done", "cursor_chat_id": 42, "count": 0}


class _DoneJobStore:
    def __init__(self):
        self.thread_ids = []

    def _pause(self):
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.08)

    def create_extraction_job(self, *args, **kwargs):
        self._pause()
        return {"id": 8, "status": "done", "created_now": False}

    def get_extraction_job_messages_with_integrity(self, job_id):
        self._pause()
        return [], []

    def get_extraction_cursor(self, user_id, direction):
        self._pause()
        return 42


class _FactClusterStore:
    def __init__(self):
        self.thread_ids = []
        self.clusters = []

    def _pause(self):
        self.thread_ids.append(threading.get_ident())
        time.sleep(0.1)

    def get_fact_clusters(self, _qq_id):
        self._pause()
        return list(self.clusters)

    def upsert_fact_cluster(self, subject_qq, category, title, summary,
                            permanence, embedding=None):
        self._pause()
        cluster = {
            "id": 1, "subject_qq": subject_qq, "category": category,
            "title": title, "summary": summary,
        }
        self.clusters = [cluster]
        return 1

    def add_cluster_fact(self, **_kwargs):
        self._pause()
        return 1

    def get_cluster_facts(self, _cluster_id):
        self._pause()
        return [{"importance": 5, "fact": "喜欢看特摄", "confidence": 0.9,
                 "evidence_ids": "1"}]

    def get_fact_cluster(self, _cluster_id):
        self._pause()
        return self.clusters[0]

    def update_cluster_summary(self, *_args):
        self._pause()


def _handler(store, cls=AutonomyMixin):
    handler = object.__new__(cls)
    handler.memory = types.SimpleNamespace(
        store=store,
        _last_extracted_id={},
    )
    handler._extracting_users = set()
    handler.metrics = types.SimpleNamespace(incr=lambda *args, **kwargs: None)
    return handler


def test_stale_store_probe_runs_off_event_loop():
    """慢 SQLite 探针期间事件循环仍应获得调度机会。"""
    store = _SlowQueueStore()
    handler = _handler(store, MessageHandler)
    main_thread = threading.get_ident()

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            assert await handler._maybe_extract_stale(
                max_per_cycle=1, max_open=8,
            ) == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(run())
    assert ticks > 0
    assert store.thread_ids
    assert all(thread_id != main_thread for thread_id in store.thread_ids)


def test_ready_job_recovery_keeps_store_calls_sequential_and_offloaded():
    """ready→done 恢复顺序不变，且数据库调用不回到主事件循环线程。"""
    store = _ReadyJobStore()
    handler = _handler(store)
    main_thread = threading.get_ident()

    assert asyncio.run(handler._resume_extraction_jobs(limit=1)) == 1
    assert len(store.thread_ids) == 2
    assert all(thread_id != main_thread for thread_id in store.thread_ids)


def test_extraction_batch_store_wait_does_not_block_event_loop():
    """实际提取批次的数据库边界也必须保持事件循环可调度。"""
    store = _DoneJobStore()
    handler = _handler(store, MessageHandler)
    main_thread = threading.get_ident()

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await handler._process_extraction_batch(
                user_id="20002",
                messages=[],
                nickname="测试",
                existing_summary="",
                direction="forward",
                origin="extracted",
                llm_call=lambda *_args, **_kwargs: None,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(run())
    assert result["completed"] is True
    assert result["cursor_chat_id"] == 42
    assert ticks > 0
    assert len(store.thread_ids) == 3
    assert all(thread_id != main_thread for thread_id in store.thread_ids)


def test_fact_cluster_store_calls_are_offloaded_from_event_loop():
    """事实簇提取和摘要更新的 SQLite 调用不能阻塞主事件循环。"""
    store = _FactClusterStore()
    memory = object.__new__(MemorySystem)
    memory.store = store
    main_thread = threading.get_ident()
    calls = {"n": 0}

    async def fake_llm(_system, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps([{
                "topic": "兴趣", "category": "preference",
                "fact": "喜欢看特摄", "confidence": 0.9,
                "evidence_ids": [1],
            }], ensure_ascii=False)
        return "用户喜欢看特摄，这是已记录的兴趣。"

    messages = [{
        "id": 1, "timestamp": "2026-08-30 10:00", "message": "我喜欢看特摄",
        "is_bot_reply": False,
    }]

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            result = await memory.extract_fact_clusters(
                qq_id="20003", messages=messages, nickname="测试",
                llm_call=fake_llm, embed_engine=None,
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(run())
    assert result["ok"] is True
    assert result["new_facts"] == 1
    assert ticks > 0
    assert len(store.thread_ids) == 7
    assert all(thread_id != main_thread for thread_id in store.thread_ids)


def test_memory_extraction_frontdoor_store_reads_are_offloaded():
    """语义提取任务的入口读取也不能把 Store 调用带回事件循环。"""
    main_thread = threading.get_ident()

    class Store:
        def __init__(self):
            self.thread_ids = []

        def _record(self):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.02)

        def count_active_fact_memories(self, _user_id):
            self._record()
            return 0

        def get_latest_chat_id(self, _user_id):
            self._record()
            return 0

        def kv_get(self, _key):
            self._record()
            return ""

    class Memory:
        def __init__(self, store):
            self.store = store
            self.thread_ids = []

        def _record(self):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.02)

        def get_unprocessed_messages(self, _user_id, **_kwargs):
            self._record()
            return [
                {"id": i, "message": "用户消息", "timestamp": "2026-08-30 10:00"}
                for i in range(1, 4)
            ]

        def get_or_create_person(self, _user_id):
            self._record()
            return {"nickname": "测试"}

        def active_notes(self, _user_id):
            self._record()
            return ""

        async def _aggregate_episodes(self, _user_id):
            return 0

    store = Store()
    memory = Memory(store)
    handler = object.__new__(MessageHandler)
    handler.memory = memory
    handler._llm_lock = asyncio.Lock()
    handler._pending_alias_candidates = {}
    handler._last_synthesis_count = {}
    handler._last_consolidation_count = {}
    handler.embed_engine = None
    handler._save_extraction_state = lambda: None
    handler._safe_task = lambda coro, **_kwargs: coro.close()

    async def fake_process(**_kwargs):
        return {"completed": True, "count": 0}

    handler._process_extraction_batch = fake_process

    asyncio.run(handler._do_extract_memories("20004"))

    calls = memory.thread_ids + store.thread_ids
    assert len(calls) == 6
    assert all(thread_id != main_thread for thread_id in calls)


def test_extraction_io_waits_for_cancelled_thread_before_propagating():
    """取消提取任务时，底层 SQLite 线程必须先收口再释放临时数据库。"""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_io():
        started.set()
        release.wait(timeout=2)
        finished.set()
        return "done"

    handler = _handler(types.SimpleNamespace())

    async def run():
        task = asyncio.create_task(
            handler._run_extraction_io("cancel_probe", slow_io),
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert finished.is_set()

    asyncio.run(run())


def test_store_io_is_bounded_and_reports_queue_wait(caplog):
    """高并发 Store 不得无限占用线程池，排队应在慢调用日志中可见。"""
    handler = object.__new__(AutonomyMixin)
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow_io(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.14)
        with guard:
            active -= 1
        return value

    async def run():
        return await asyncio.gather(*(
            handler._run_store_io("get_recent_chats", slow_io, value)
            for value in range(STORE_IO_MAX_CONCURRENCY + 4)
        ))

    with caplog.at_level(logging.WARNING, logger="糖糖.Autonomy"):
        result = asyncio.run(run())

    assert result == list(range(STORE_IO_MAX_CONCURRENCY + 4))
    assert peak == STORE_IO_MAX_CONCURRENCY
    assert any("queue_wait_ms=" in record.message for record in caplog.records)


def test_store_write_io_is_single_writer_but_reads_keep_parallelism():
    """同一事件循环内写事务串行，读/未知调用仍使用原有并发门。"""
    handler = object.__new__(AutonomyMixin)
    active = 0
    peak = 0
    guard = threading.Lock()

    def slow_write(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return value

    async def run():
        return await asyncio.gather(*(
            handler._run_store_io("persist_write_probe", slow_write, value)
            for value in range(6)
        ))

    assert asyncio.run(run()) == list(range(6))
    assert peak == 1


def test_daily_metrics_aggregate_keeps_store_and_buffer_boundaries():
    """每日聚合的持久化 I/O 在线程中，短期缓冲清理留在事件循环。"""
    class Store:
        def __init__(self):
            self.thread_ids = []
            self.values = {}

        def _pause(self):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.08)

        def kv_get(self, key):
            self._pause()
            return self.values.get(key, "")

        def kv_set(self, key, value):
            self._pause()
            self.values[key] = value

        def get_zero_memory_users(self, min_chats=50):
            assert min_chats == 50
            self._pause()
            return []

        def get_people_with_memories_gt(self, **kwargs):
            assert kwargs == {
                "min_count": 10, "empty_notes": True, "limit": 10000,
            }
            self._pause()
            return []

        def count_memories_for(self, qq_id):
            assert qq_id == "owner"
            self._pause()
            return 3

    class Memory:
        def __init__(self, store):
            self.store = store
            self.cleanup_threads = []
            self.buffer_threads = []

        def cleanup_stale_memories(self, days=90):
            assert days == 90
            self.cleanup_threads.append(threading.get_ident())
            time.sleep(0.08)
            return 0

        def cleanup_stale_buffers(self, max_idle_hours=24):
            assert max_idle_hours == 24
            self.buffer_threads.append(threading.get_ident())

    store = Store()
    memory = Memory(store)
    handler = object.__new__(AutonomyMixin)
    handler.memory = memory
    handler.owner_qq = "owner"
    main_thread = threading.get_ident()

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await handler._maybe_daily_metrics_aggregate()
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(run())
    assert ticks > 0
    assert store.thread_ids and all(
        thread_id != main_thread for thread_id in store.thread_ids
    )
    assert memory.cleanup_threads and all(
        thread_id != main_thread for thread_id in memory.cleanup_threads
    )
    assert memory.buffer_threads == [main_thread]


def test_private_initiative_target_reads_leave_event_loop_responsive():
    """自治私聊目标筛选的同步 Store 读取在线程中执行。"""
    class Store:
        def __init__(self):
            self.thread_ids = []

        def get_or_create_person(self, qq_id, _nickname=""):
            assert qq_id == "owner"
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.08)
            return {
                "nickname": "主人", "last_chat": "2026-08-25 10:00:00",
            }

    class Memory:
        def __init__(self, store):
            self.store = store

        def get_or_create_person(self, qq_id):
            return self.store.get_or_create_person(qq_id)

    store = Store()
    handler = object.__new__(AutonomyMixin)
    handler.memory = Memory(store)
    handler.bot_qq = "bot"
    handler.owner_qq = "owner"
    handler._private_blacklist = set()
    handler.reply_only_to = ["owner"]
    handler._care_due = {}
    handler._seek_uncertain = {}
    handler.config = {"scenario_targets": {}, "groups": {}}
    handler.scenarios = {}
    handler.self_state = types.SimpleNamespace(relationships={})
    handler._run_store_io = AutonomyMixin._run_store_io.__get__(handler)
    main_thread = threading.get_ident()

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            result = await handler._pick_private_target_async(time.time())
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        return ticks, result

    ticks, result = asyncio.run(run())
    assert result and result[0] == "owner"
    assert ticks > 0
    assert store.thread_ids and all(
        thread_id != main_thread for thread_id in store.thread_ids
    )
