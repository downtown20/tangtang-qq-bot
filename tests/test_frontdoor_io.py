"""前台消息入口的慢 Store 边界回归。"""

import ast
import asyncio
import inspect
import threading
import textwrap
import time
from types import SimpleNamespace

from agent.handler import MessageHandler


def test_group_cast_context_store_read_is_threaded():
    """群聊人物关系图的 Store 读取不得直接阻塞消息事件循环。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_group_message.__wrapped__)
    )


def test_interjection_semantic_dedup_encoding_is_threaded():
    """插话语义去重的 BGE 编码不得直接运行在消息协程。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_group_message.__wrapped__)
    )
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    encode_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "encode"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "embed_engine"
    ]
    assert encode_calls
    for attribute in encode_calls:
        current = attribute
        wrapped = False
        while current in parents:
            current = parents[current]
            if (
                isinstance(current, ast.Await)
                and isinstance(current.value, ast.Call)
                and getattr(current.value.func, "id", "") == "run_bounded_blocking"
            ):
                wrapped = True
                break
        assert wrapped
    tree = ast.parse(source)
    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "attr", "") == "_run_store_io"
        and any(
            isinstance(child, ast.Attribute)
            and child.attr == "_build_cast_context"
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )


def test_preference_store_read_is_threaded_in_group_and_private_paths():
    """每条前台消息注入偏好时，SQLite 读取必须走 Store worker。"""
    for method in (
        MessageHandler.handle_group_message.__wrapped__,
        MessageHandler.handle_private_message.__wrapped__,
    ):
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "get_preferences"
        ]
        assert calls
        for call in calls:
            current = call
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == "_run_store_io"
                ):
                    wrapped = True
                    break
            assert wrapped


def test_group_daily_bonus_and_anniversary_store_reads_are_threaded():
    """群消息热路径中的每日加成/纪念日读取不得直连 SQLite。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_group_message.__wrapped__)
    )
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    for helper_name in ("_check_daily_bonus", "_check_anniversary"):
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == helper_name
        ]
        assert calls
        for attribute in calls:
            current = attribute
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == "_run_store_io"
                ):
                    wrapped = True
                    break
            assert wrapped


def test_group_memory_recall_and_history_use_bounded_workers():
    """群聊上下文的记忆召回/用户历史读取必须经过统一并发门。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_group_message.__wrapped__)
    )
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def assert_wrapped(attribute_name, wrapper_name):
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == attribute_name
        ]
        assert calls
        for attribute in calls:
            current = attribute
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Call)
                    and (
                        getattr(current.func, "id", "") == wrapper_name
                        or getattr(current.func, "attr", "") == wrapper_name
                    )
                ):
                    wrapped = True
                    break
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "id", "") == wrapper_name
                ) or (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == wrapper_name
                ):
                    wrapped = True
                    break
            assert wrapped

    assert_wrapped("recall", "run_bounded_blocking")
    assert_wrapped("get_user_recent_messages", "_run_store_io")


def test_private_memory_recall_uses_bounded_worker():
    """私聊上下文的记忆召回必须与群聊使用同一 CPU 门。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_private_message.__wrapped__)
    )
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    recall_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "recall"
    ]
    assert recall_calls
    for attribute in recall_calls:
        current = attribute
        wrapped = False
        while current in parents:
            current = parents[current]
            if (
                isinstance(current, ast.Call)
                and getattr(current.func, "id", "") == "run_bounded_blocking"
            ):
                wrapped = True
                break
        assert wrapped
def test_reinforce_store_write_is_threaded_in_group_and_private_paths():
    """群聊/私聊回合的记忆强化写入不得直接阻塞事件循环。"""
    for method in (
        MessageHandler.handle_group_message.__wrapped__,
        MessageHandler.handle_private_message.__wrapped__,
    ):
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        reinforce_attributes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "reinforce"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "memory"
        ]
        assert reinforce_attributes
        for attribute in reinforce_attributes:
            current = attribute
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == "_run_store_io"
                ):
                    wrapped = True
                    break
            assert wrapped


def test_image_store_write_is_threaded_in_group_and_private_paths():
    """群聊/私聊图片描述写回的 CAS/fallback 不得直连 SQLite。"""
    for method in (
        MessageHandler.handle_group_message.__wrapped__,
        MessageHandler.handle_private_message.__wrapped__,
    ):
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        helper_attributes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "_enrich_image_message_store"
        ]
        assert helper_attributes
        for attribute in helper_attributes:
            current = attribute
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == "_run_store_io"
                ):
                    wrapped = True
                    break
            assert wrapped


def test_image_store_helper_keeps_cas_then_fallback_order_in_worker():
    """图片写回 helper 在工作线程中先精确 CAS，失败后才 fallback。"""
    class Store:
        def __init__(self):
            self.calls = []
            self.threads = []

        def enrich_chat_message(self, chat_id, enriched):
            self.calls.append(("cas", chat_id, enriched))
            self.threads.append(threading.get_ident())
            return False

        def enrich_latest_image_message(self, qq_id, group_id, enriched):
            self.calls.append(("fallback", qq_id, group_id, enriched))
            self.threads.append(threading.get_ident())
            return True

    store = Store()
    handler = object.__new__(MessageHandler)
    handler.memory = type("Memory", (), {"store": store})()
    handler._run_store_io = MessageHandler._run_store_io.__get__(handler)
    main_thread = threading.get_ident()

    result = asyncio.run(handler._run_store_io(
        "enrich_image_message",
        handler._enrich_image_message_store,
        42, "user", "group", "（图片描述）",
    ))

    assert result is True
    assert [item[0] for item in store.calls] == ["cas", "fallback"]
    assert store.threads and all(thread_id != main_thread for thread_id in store.threads)


def test_feedback_reflection_read_is_threaded_in_group_and_private_paths():
    """反馈反思材料及其消费游标写回不得直连 SQLite。"""
    for method in (
        MessageHandler.handle_group_message.__wrapped__,
        MessageHandler.handle_private_message.__wrapped__,
    ):
        source = textwrap.dedent(inspect.getsource(method))
        tree = ast.parse(source)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        helper_attributes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "_get_feedback_reflection_context"
        ]
        assert helper_attributes
        for attribute in helper_attributes:
            current = attribute
            wrapped = False
            while current in parents:
                current = parents[current]
                if (
                    isinstance(current, ast.Await)
                    and isinstance(current.value, ast.Call)
                    and getattr(current.value.func, "attr", "") == "_run_store_io"
                ):
                    wrapped = True
                    break
            assert wrapped


def test_private_cross_context_store_reads_are_threaded():
    """私聊交叉人物档案的批量读取不得直接阻塞事件循环。"""
    source = textwrap.dedent(
        inspect.getsource(MessageHandler.handle_private_message.__wrapped__)
    )
    tree = ast.parse(source)
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    helper_attributes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "_build_private_cross_context"
    ]
    assert helper_attributes
    for attribute in helper_attributes:
        current = attribute
        wrapped = False
        while current in parents:
            current = parents[current]
            if (
                isinstance(current, ast.Await)
                and isinstance(current.value, ast.Call)
                and getattr(current.value.func, "attr", "") == "_run_store_io"
            ):
                wrapped = True
                break
        assert wrapped

    assert any(
        isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "attr", "") == "_run_store_io"
        and any(
            isinstance(child, ast.Attribute)
            and child.attr == "find_last_group"
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )


def test_private_cross_context_helper_keeps_all_store_reads_in_worker():
    """交叉上下文 helper 内的档案读取统一在线程池执行。"""
    class Store:
        def __init__(self):
            self.threads = []

        def _record(self):
            self.threads.append(threading.get_ident())

        def list_people_nicknames(self):
            self._record()
            return [("peer", "小明")]

        def person_exists(self, _qq_id):
            self._record()
            return True

        def get_or_create_person(self, _qq_id, _nickname=""):
            self._record()
            return {
                "nickname": "小明", "intimacy": 60,
                "notes": "喜欢下棋", "notes_dirty": False,
                "notes_trust_level": "verified",
            }

    class Memory:
        def __init__(self, store):
            self.store = store

        def active_notes(self, qq_id):
            person = self.store.get_or_create_person(qq_id, "")
            return person.get("notes", "")

        def get_recent_context(self, *_args, **_kwargs):
            return ""

    store = Store()
    handler = object.__new__(MessageHandler)
    handler.memory = Memory(store)
    handler.owner_qq = "owner"
    handler.bot_qq = "bot"
    handler.personality = type("Personality", (), {"nicknames": []})()
    handler._run_store_io = MessageHandler._run_store_io.__get__(handler)
    main_thread = threading.get_ident()

    async def run():
        mentioned = await handler._run_store_io(
            "resolve_mentioned_people",
            handler._resolve_mentioned_people,
            "@小明 最近怎么样", "owner", 100,
        )
        return await handler._run_store_io(
            "build_private_cross_context",
            handler._build_private_cross_context,
            mentioned,
            {},
        )

    result = asyncio.run(run())

    assert "小明(peer)" in result
    assert store.threads and all(thread_id != main_thread for thread_id in store.threads)


def test_group_member_write_does_not_block_event_loop():
    """群成员资料写入很慢时，前台事件循环仍应可调度。"""
    class SlowStore:
        def upsert_group_member(self, *_args, **_kwargs):
            time.sleep(0.08)

        def has_chat_event(self, *_args, **_kwargs):
            return False

    class Memory:
        def __init__(self):
            self.store = SlowStore()
            self.person_thread = None
            self.update_thread = None
            self.log_thread = None

        def add_to_buffer(self, *_args, **_kwargs):
            return None

        def get_or_create_person(self, *_args, **_kwargs):
            time.sleep(0.08)
            self.person_thread = threading.get_ident()
            return {}

        def update_person(self, *_args, **_kwargs):
            time.sleep(0.08)
            self.update_thread = threading.get_ident()
            return None

        def log_chat(self, *_args, **_kwargs):
            time.sleep(0.08)
            self.log_thread = threading.get_ident()
            return 123

    class Batcher:
        async def enqueue_group(self, _msg):
            return False

    class ConvTracker:
        def is_engaged(self, *_args, **_kwargs):
            return False

    class SelfState:
        def tick(self):
            return None

    class Interjection:
        def record_response(self, *_args, **_kwargs):
            return None

    class GroupStyles:
        def feed(self, *_args, **_kwargs):
            return None

        async def maybe_update_topics(self, *_args, **_kwargs):
            return None

    class Napcat:
        async def send_group_message(self, *_args, **_kwargs):
            return True

    async def command(*_args, **_kwargs):
        return "收到"

    h = object.__new__(MessageHandler)
    h.bot_qq = "10000"
    h.memory = Memory()
    h._group_blacklist = set()
    h._robot_ids = set()
    h._allowed_groups = {"g1"}
    h._blocked_groups = set()
    h._pending_dispatch_reserved = False
    h._conv_tracker = ConvTracker()
    h.batcher = Batcher()
    h.self_state = SelfState()
    h._take_feedback_candidate = lambda *_args, **_kwargs: None
    h._capture_recent_image = lambda *_args, **_kwargs: None
    h._track_group_power = lambda *_args, **_kwargs: None
    h._group_power = {"g1": {"owner": "u1", "admins": set()}}
    h._msg_burst_count = 0
    h._last_msg_time = 0
    h.voice_enabled = False
    h._check_voice_block_toggle = lambda *_args, **_kwargs: None
    h._handle_group_command = command
    h.napcat = Napcat()
    h.interjection = Interjection()
    h.group_styles = GroupStyles()
    h._collect_alias_candidates = lambda *_args, **_kwargs: None
    h._extraction_counter = {}
    h.reply_only_to = {"someone-else"}
    h.config = {"bot": {"name": "糖糖"}, "behavior": {"sticker_steal": False}}
    h._run_store_io = MessageHandler._run_store_io.__get__(h)
    main_thread = threading.get_ident()

    msg = {
        "group_id": "g1", "user_id": "u1", "nickname": "测试",
        "message": "普通消息", "raw_message": "普通消息", "message_id": 1,
        "time": 1787875200, "role": "member", "title": "",
        "card": "测试卡",
    }

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            await MessageHandler.handle_group_message.__wrapped__(h, msg)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks > 0
    assert h.memory.person_thread != main_thread
    assert h.memory.update_thread != main_thread
    assert h.memory.log_thread != main_thread


def test_command_intimacy_slow_memory_does_not_block_event_loop():
    """异步命令读取人物/统计时，慢 SQLite 不得冻结前台事件循环。"""
    from agent.handler_commands import CommandRouter

    main_thread = threading.get_ident()

    class Memory:
        def __init__(self):
            self.thread_ids = []

        def get_stats(self, _user_id):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.05)
            return {
                "intimacy": 10, "intimacy_grade": "熟悉",
                "relationship": "朋友", "total_chats": 1,
                "memory_count": 0,
            }

    memory = Memory()
    router = CommandRouter(SimpleNamespace(memory=memory))

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await router._cmd_intimacy("u1", "")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert "亲密度：10/100" in result
    assert ticks > 0
    assert memory.thread_ids and all(
        thread_id != main_thread for thread_id in memory.thread_ids
    )


def test_album_scan_slow_store_does_not_block_event_loop():
    """相册巡检的聊天记录读取必须经过线程边界。"""
    from agent.album_patrol import AlbumLiker

    main_thread = threading.get_ident()

    class Store:
        def __init__(self):
            self.thread_ids = []

        def find_recent_image_senders(self, _group_id, _bot_qq):
            self.thread_ids.append(threading.get_ident())
            time.sleep(0.05)
            return []

    store = Store()
    handler = SimpleNamespace(
        memory=SimpleNamespace(store=store), bot_qq="bot",
    )
    liker = AlbumLiker(SimpleNamespace(), handler)

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        result = await liker.scan_and_like("g1")
        stop = True
        await task
        return ticks, result

    ticks, result = asyncio.run(scenario())

    assert result["album_count"] == 0
    assert ticks > 0
    assert store.thread_ids and all(
        thread_id != main_thread for thread_id in store.thread_ids
    )
