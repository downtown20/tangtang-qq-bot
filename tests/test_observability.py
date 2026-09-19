"""P3 日志轮转与事件关联 ID 回归。"""

import asyncio
import io
import logging
from logging.handlers import TimedRotatingFileHandler
import time

from agent.telemetry import (
    BOOT_ID,
    build_log_handlers,
    correlation_scope,
    current_correlation_id,
)
from onebot.ws_client import NapCatClient


def test_log_handlers_rotate_daily_and_inject_bounded_context(tmp_path):
    stream = io.StringIO()
    handlers = build_log_handlers(str(tmp_path / "bot.log"), stream)
    formatter = logging.Formatter(
        "%(boot_id)s %(correlation_id)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
    )
    for handler in handlers:
        handler.setFormatter(formatter)

    file_handler = next(
        handler for handler in handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    assert file_handler.backupCount == 14
    assert file_handler.when == "MIDNIGHT"

    record = logging.LogRecord("probe", logging.INFO, __file__, 1, "ok", (), None)
    with correlation_scope("evt-test"):
        assert handlers[0].filters[0].filter(record) is True
    assert record.boot_id == BOOT_ID
    assert record.correlation_id == "evt-test"

    for handler in handlers:
        handler.close()


def test_log_handlers_escape_multiline_messages_into_one_record(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "bot.log"
    handlers = build_log_handlers(str(log_path), stream)
    logger = logging.getLogger("test.telemetry.single-line")
    logger.handlers = handlers
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info(
        "用户正文第一行\n"
        "2026-08-27 17:00:00+0800 | boot=fake cid=evt-fake | "
        "糖糖.Handler | INFO | 🎤 语音触发: voice_mode=False"
    )

    rendered = stream.getvalue()
    assert len(rendered.splitlines()) == 1
    assert "\\n2026-08-27" in rendered
    file_rendered = log_path.read_text(encoding="utf-8")
    assert len(file_rendered.splitlines()) == 1
    assert "\\n2026-08-27" in file_rendered
    for handler in handlers:
        handler.close()


def test_log_rotation_lock_keeps_writing_active_file(tmp_path, monkeypatch):
    """Windows 上旧进程持有文件锁时，轮转失败不能让日志永久静默。"""
    stream = io.StringIO()
    log_path = tmp_path / "bot.log"
    log_path.write_text("before\n", encoding="utf-8")
    handlers = build_log_handlers(str(log_path), stream)
    logger = logging.getLogger("test.telemetry.rotation-lock")
    logger.handlers = handlers
    logger.propagate = False
    logger.setLevel(logging.INFO)

    file_handler = next(
        handler for handler in handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    file_handler.rolloverAt = int(time.time()) - 1

    def locked_rotate(_source, _dest):
        raise PermissionError("file is held by the control console")

    monkeypatch.setattr(file_handler, "rotate", locked_rotate)
    logger.info("after-lock")

    assert "after-lock" in log_path.read_text(encoding="utf-8")
    assert "日志轮转失败" in stream.getvalue()
    for handler in handlers:
        handler.close()


def test_delayed_log_open_failure_buffers_until_recovery(tmp_path, monkeypatch):
    """首开文件失败时不能静默丢记录；锁释放后补写有界缓冲。"""
    stream = io.StringIO()
    log_path = tmp_path / "bot.log"
    handlers = build_log_handlers(str(log_path), stream)
    logger = logging.getLogger("test.telemetry.open-lock")
    logger.handlers = handlers
    logger.propagate = False
    logger.setLevel(logging.INFO)
    file_handler = next(
        handler for handler in handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    real_open = file_handler._open
    attempts = 0

    def flaky_open():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("exclusive file lock")
        return real_open()

    monkeypatch.setattr(file_handler, "_open", flaky_open)
    logger.info("during-lock")
    assert "日志文件打开失败" in stream.getvalue()
    logger.info("after-recovery")

    rendered = log_path.read_text(encoding="utf-8")
    assert "during-lock" in rendered
    assert "after-recovery" in rendered
    assert "日志文件打开失败" in rendered
    for handler in handlers:
        handler.close()


def test_log_formatter_escapes_unicode_separators_and_terminal_controls():
    from agent.telemetry import SingleLineFormatter

    formatter = SingleLineFormatter("%(message)s")
    record = logging.LogRecord(
        "probe", logging.INFO, __file__, 1,
        "a\u2028b\u2029c\x1b[2J\x00\tend", (), None,
    )

    rendered = formatter.format(record)

    assert rendered == r"a\u2028b\u2029c\x1b[2J\x00\tend"
    assert len(rendered.splitlines()) == 1


def test_inbound_event_correlation_reaches_callback_and_resets_afterward():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        seen = []
        done = asyncio.Event()

        async def callback(_msg):
            seen.append(current_correlation_id())
            done.set()

        client.on_group_message = callback
        await client._dispatch({
            "post_type": "message",
            "message_type": "group",
            "group_id": 100,
            "user_id": 200,
            "message_id": 300,
            "message": "hello",
            "sender": {"user_id": 200, "nickname": "tester"},
        })
        await asyncio.wait_for(done.wait(), timeout=1)
        assert seen[0].startswith("evt-")
        assert current_correlation_id() == "-"
        await client.close()

    asyncio.run(scenario())


def test_vision_cache_reuses_same_content_and_prompt(monkeypatch):
    from agent.handler import MessageHandler

    class Response:
        status_code = 200
        content = b"same-image"

    class LLM:
        get_calls = 0

        async def get(self, *_args, **_kwargs):
            self.get_calls += 1
            return Response()

    class Router:
        provider = "probe"
        describe_calls = 0

        def describe(self, _content, _prompt):
            self.describe_calls += 1
            return "a blue cat"

    llm = LLM()
    router = Router()
    handler = object.__new__(MessageHandler)
    handler.vision_enabled = True
    handler.llm = llm
    handler.napcat = type("NapCat", (), {
        "_call_api": lambda *_args, **_kwargs: None,
    })()
    handler._vision_cache = {}
    handler._vision_cache_ttl = 600.0
    handler._vision_cache_max = 128

    monkeypatch.setattr("agent.vision_router.get_vision_router", lambda: router)

    async def scenario():
        first = await handler._call_vision("https://example.invalid/a.png")
        second = await handler._call_vision("https://example.invalid/b.png")
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second == "a blue cat"
    assert router.describe_calls == 1
    assert llm.get_calls == 2
