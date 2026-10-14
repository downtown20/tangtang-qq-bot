"""跨异步任务的轻量关联上下文与日志 handler 工厂。"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections import deque
import logging
from logging.handlers import TimedRotatingFileHandler
import secrets
import time


BOOT_ID = secrets.token_hex(4)
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
LOG_FORMAT = (
    "%(asctime)s | boot=%(boot_id)s cid=%(correlation_id)s | "
    "%(name)s | %(levelname)s | %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S%z"


def current_correlation_id() -> str:
    return _correlation_id.get()


def new_correlation_id(prefix: str = "evt") -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


@contextmanager
def correlation_scope(correlation_id: str):
    token = _correlation_id.set(str(correlation_id or "-"))
    try:
        yield
    finally:
        _correlation_id.reset(token)


class CorrelationFilter(logging.Filter):
    """为所有日志记录注入有界的 boot/correlation 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.boot_id = BOOT_ID
        record.correlation_id = current_correlation_id()
        return True


class SingleLineFormatter(logging.Formatter):
    """将一条 LogRecord 固定成一条物理行，阻断正文伪造日志记录。"""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        escaped: list[str] = []
        for char in rendered:
            code = ord(char)
            if char == "\r":
                escaped.append("\\r")
            elif char == "\n":
                escaped.append("\\n")
            elif char == "\t":
                escaped.append("\\t")
            elif char in ("\u2028", "\u2029"):
                escaped.append("\\u%04x" % code)
            elif code < 0x20 or code == 0x7F:
                escaped.append("\\x%02x" % code)
            else:
                escaped.append(char)
        return "".join(escaped)


class ResilientTimedRotatingFileHandler(TimedRotatingFileHandler):
    """轮转被 Windows 文件锁阻断时，退回继续追加当前日志。"""

    _MAX_PENDING_LINES = 1000

    def __init__(self, *args, fallback_stream=None, **kwargs):
        self._fallback_stream = fallback_stream
        self._pending_lines: deque[str] = deque()
        self._pending_dropped = 0
        self._file_unavailable = False
        super().__init__(*args, **kwargs)

    def _render_internal_warning(self, message: str) -> str:
        record = logging.LogRecord(
            "糖糖.Telemetry", logging.WARNING, __file__, 0,
            message, (), None,
        )
        for log_filter in self.filters:
            if not log_filter.filter(record):
                return ""
        return self.format(record)

    def _write_fallback(self, rendered: str) -> None:
        if not rendered or self._fallback_stream is None:
            return
        try:
            self._fallback_stream.write(rendered + self.terminator)
            self._fallback_stream.flush()
        except Exception:
            pass

    def _queue_pending(self, rendered: str) -> None:
        if not rendered:
            return
        if len(self._pending_lines) >= self._MAX_PENDING_LINES:
            self._pending_lines.popleft()
            self._pending_dropped += 1
        self._pending_lines.append(rendered)

    def _flush_pending(self) -> None:
        while self._pending_lines:
            self.stream.write(self._pending_lines[0] + self.terminator)
            self._pending_lines.popleft()
        if self._pending_dropped:
            rendered = self._render_internal_warning(
                f"日志不可写期间有 {self._pending_dropped} 条记录超出缓冲上限"
            )
            if rendered:
                self.stream.write(rendered + self.terminator)
            self._pending_dropped = 0
        self.flush()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            if self._pending_lines or self._pending_dropped:
                self._flush_pending()
            logging.StreamHandler.emit(self, record)
            self._file_unavailable = False
        except OSError as exc:
            if self.stream is not None:
                try:
                    self.stream.close()
                except OSError:
                    pass
                self.stream = None
            if not self._file_unavailable:
                warning = self._render_internal_warning(
                    "日志文件打开失败，记录进入有界内存缓冲并等待恢复: "
                    f"{type(exc).__name__}: {exc}"
                )
                self._queue_pending(warning)
                self._write_fallback(warning)
                self._file_unavailable = True
            self._queue_pending(self.format(record))
        except Exception:
            self.handleError(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            # TimedRotatingFileHandler 会先关闭 stream 再 rename；Windows 上
            # 控制台或观察器持有旧文件时 rename 可能失败。若不恢复 stream，
            # 后续每条日志都会再次轮转并全部丢失。
            if self.stream is None:
                self.stream = self._open()
            now = int(time.time())
            self.rolloverAt = self.computeRollover(now)
            while self.rolloverAt <= now:
                self.rolloverAt += self.interval
            warning = (
                "日志轮转失败，继续写入当前文件；将在下个轮转点重试: "
                f"{type(exc).__name__}: {exc}"
            )
            rendered = self._render_internal_warning(warning)
            self.stream.write(rendered + self.terminator)
            self.flush()
            self._write_fallback(rendered)


def build_log_handlers(log_path: str, stream) -> list[logging.Handler]:
    """生成控制台 + 按天轮转文件 handler，保留 14 天。"""
    console = logging.StreamHandler(stream)
    file_handler = ResilientTimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        delay=True,
        fallback_stream=stream,
    )
    correlation_filter = CorrelationFilter()
    formatter = SingleLineFormatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    console.addFilter(correlation_filter)
    file_handler.addFilter(correlation_filter)
    console.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    return [console, file_handler]
