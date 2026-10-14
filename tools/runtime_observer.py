"""小糖糖生产运行观察器。

默认只读 ``memory.db`` 和 ``tangtang.log``，不调用 QQ/LLM，
不保存消息正文、QQ 号或群号。观察结果使用三态：
PASS / FAIL / INSUFFICIENT；未触发的功能永不算 PASS。
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import statistics
import sys
import time
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.paths import log_path  # noqa: E402  日志落盘位置单一事实源（2026-10-15 T55-F）
from agent.store import ReadOnlyStore


PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT"
OBSERVER_HEARTBEAT_KEY = "health:observer_last_run"
MEMORY_TREND_MIN_SECONDS = 300.0
FOREGROUND_LLM_P95_MAX_MS = 30_000.0
BACKGROUND_LLM_P95_MAX_MS = 60_000.0
LLM_TRANSPORT_ERROR_BUDGET = 2
MAX_LATENCY_SAMPLES = 20_000
MAX_CORRELATION_TOKENS = 100_000
CORRELATION_SETTLE_GRACE_SECONDS = 180.0
# GPT/语音请求可能在观察器启动前已开始；LLM 客户端超时约 120s，
# GPT-SoVITS 最多 3×60s 重试并含间隔。边界宽限覆盖一次合法收尾，
# 但超过该窗口的无起始/无配对完成仍会被判为完整性失败。
CORRELATION_BOUNDARY_GRACE_SECONDS = 210.0
LATENCY_BUCKET_UPPER_MS = (
    100.0, 250.0, 500.0, 750.0, 1_000.0, 1_500.0, 2_000.0,
    3_000.0, 5_000.0, 7_500.0, 10_000.0, 15_000.0, 20_000.0,
    25_000.0, 30_000.0, 40_000.0, 60_000.0, 90_000.0, 120_000.0,
    180_000.0, 300_000.0, 600_000.0, float("inf"),
)
CORE_DOMAINS = (
    "qq_gateway", "sending", "memory", "llm", "process_stability",
    "runtime_logs", "observer_integrity", "background_tasks", "local_services",
)
# 这些 logger 由定时/后台循环使用。普通入站消息只记作 traffic，不能据此
# 宣称后台任务健康；至少要看到一个后台 logger 的真实运行样本。
BACKGROUND_LOGGERS = frozenset({
    "糖糖.Autonomy",
    "糖糖.Memory",
    "糖糖.Tasks",
    "糖糖.Scheduler",
    "糖糖.Personalization",
    "糖糖.ImageShare",
    "糖糖.CatchUp",
    "糖糖.HealthCheck",
    "糖糖.ServiceMgr",
    "糖糖.Opinion",
    "糖糖.Album",
})
BACKGROUND_SUCCESS_PREFIXES = {
    "糖糖.Autonomy": (
        "🔥 自治循环已启动", "🔥 驱动力触发", "🔥 内部消化",
        "🧠 积压准入", "📝 画像补全触发", "🔥 糖糖主动发起",
        "🔥 糖糖选择不主动发起", "🫀 自治循环心跳",
        "🧠 提取 worker 周期",
    ),
    "糖糖.Memory": (
        "📌 ", "📖 Episode聚合", "🏗️ 事实簇提取", "🧠 批量填充",
        "🧠 启动时填充",
    ),
    "糖糖.Tasks": (
        "📋 任务提醒系统已启动", "📋 任务提醒已发送", "🫀 任务提醒循环心跳",
    ),
    "糖糖.Scheduler": (
        "⏰ 定时调度器已启动", "⏰ 任务 #", "⏰ 触发任务 #", "🫀 定时调度心跳",
    ),
    "糖糖.Personalization": (
        "🎂 生日祝福检查已启动", "🎂 生日祝福已确认", "🫀 生日检查心跳",
    ),
    "糖糖.ImageShare": (
        "🖼 图片分享已启动", "🖼 图片分享已确认", "🫀 图片分享心跳",
    ),
    "糖糖.CatchUp": (
        "📬 开始离线补读", "📬 离线补读完成", "📬 已补发记录",
        "📬 LLM 摘要完成", "📬 注入 ",
    ),
    "糖糖.HealthCheck": ("🩺 启动健康检查",),
    "糖糖.ServiceMgr": ("🔧 GPT-SoVITS API", "🔧 GPT-SoVITS 已就绪"),
    "糖糖.Opinion": (
        "📋 恢复 ", "📋 征集邀请已发", "📋 意见窗口自动关闭",
        "📋 意见征集自动收尾", "📋 意见文档已导出",
    ),
    "糖糖.Album": (
        "💝 每日定时点赞", "每日定时点赞完成", "群点赞完成",
    ),
    "糖糖.Handler": (
        "🧘 反思整合完成", "🧠 启动时填充", "📚 知识库向量预热完成",
        "💝 每日点赞循环启动", "🫀 反思循环心跳",
        "🧠 提取生命周期",
    ),
}
BACKGROUND_HEARTBEAT_PREFIXES = {
    "糖糖.Autonomy": ("🫀 自治循环心跳",),
    "糖糖.Tasks": ("🫀 任务提醒循环心跳",),
    "糖糖.Scheduler": ("🫀 定时调度心跳",),
    "糖糖.Personalization": ("🫀 生日检查心跳",),
    "糖糖.ImageShare": ("🫀 图片分享心跳",),
    "糖糖.Handler": ("🫀 反思循环心跳",),
}
# 这些是当前始终启动的长期循环。可选模块（生日/图片/意见征集等）不在
# 此表中：没有触发时保持 INSUFFICIENT，而不会把“未启用”误报成故障。
REQUIRED_BACKGROUND_HEARTBEATS = {
    # startup_grace 覆盖首次启动时的合法长操作；首次心跳之后仍按
    # max_age/max_gap 严格检查，避免把启动宽限误当成永久免检。
    "糖糖.Autonomy": {
        "period_seconds": 600.0, "min_samples": 1,
        "startup_grace_seconds": 3600.0,
    },
    "糖糖.Tasks": {
        "period_seconds": 60.0, "min_samples": 1,
        "startup_grace_seconds": 600.0,
    },
    "糖糖.Scheduler": {
        "period_seconds": 30.0, "min_samples": 1,
        "startup_grace_seconds": 600.0,
    },
    "糖糖.Handler": {
        "period_seconds": 1800.0, "min_samples": 2,
        "startup_grace_seconds": 3600.0,
    },
}
BACKGROUND_HEARTBEAT_GRACE_SECONDS = 300.0
BACKGROUND_FAILURE_PREFIXES = {
    "糖糖.Autonomy": (
        "🔥 自治循环异常", "🔥 主动发起失败", "🧠 提取 worker 异常",
        "🧠 恢复提取任务", "📦 回填异常", "📝 画像补全扫描失败",
        "🔥 自治积压准入异常", "🔥 自治积压扫描异常", "🔥 自治启动子步骤异常",
        "💌 私聊插话异常", "💌 私聊插话失败",
    ),
    "糖糖.Memory": ("事实簇提取失败",),
    "糖糖.Tasks": ("任务检查异常", "任务提醒异常", "任务提醒发送失败"),
    "糖糖.Scheduler": (
        "定时调度检查出错", "无法持久化发送占位",
    ),
    "糖糖.Personalization": ("生日检查出错", "生日祝福发送未确认", "生日祝福发送响应丢失"),
    "糖糖.ImageShare": ("图片分享出错", "图片分享发送未确认", "无法持久化图片发送尝试"),
    "糖糖.CatchUp": ("📬 补读失败", "📬 补回复失败", "📬 LLM 补回复生成失败", "📬 状态保存失败", "📬 补发去重状态"),
    "糖糖.Opinion": ("📋 征集后台任务异常", "📋 征集邀请失败", "📋 邀请文案生成失败"),
    "糖糖.Album": ("💝 每日点赞执行出错",),
    "糖糖.ServiceMgr": (
        "🔧 GPT-SoVITS 进程异常", "🔧 GPT-SoVITS 端口有响应但 TTS 不健康",
        "🔧 GPT-SoVITS 启动超时", "🔧 GPT-SoVITS 启动失败", "🔧 找不到 Python 解释器",
    ),
    "糖糖.Handler": (
        "🔥 后台任务异常", "🧘 反思循环异常", "📚 知识库预热失败",
        "⚠ BGE 后台加载异常", "💝 每日点赞执行出错", "🧠 记忆 embedding 填充失败",
        "轻量LLM调用重试仍失败", "LLM生成私信失败", "LLM生成群发言失败",
        "事实簇提取失败", "画像合成失败", "记忆整合失败",
        "⚠ LLM 工具循环+收尾轮后仍无文字回复",
    ),
    "糖糖.HealthCheck": ("🩺 [warn]", "🩺 [error]"),
}
BACKGROUND_WARNING_LOGGERS = frozenset({
    "糖糖.Autonomy", "糖糖.Memory", "糖糖.Tasks", "糖糖.Scheduler",
    "糖糖.Personalization", "糖糖.ImageShare", "糖糖.CatchUp",
    "糖糖.Opinion", "糖糖.ServiceMgr", "糖糖.Album", "糖糖.HealthCheck",
})
BACKGROUND_FAILURE_WORDS = (
    "异常", "失败", "出错", "错误", "未确认", "响应丢失", "损坏", "不可用",
)
EXPECTED_ALBUM_LIMIT_MARKERS = (
    "OIDB error 20003",
    "今日同一好友点赞数已达上限",
)

_LOG_PREFIX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[+-]\d{4})?)"
    r" \| boot=(?P<boot>[^ ]+) cid=(?P<cid>[^ ]+) \| "
    r"(?P<logger>[^|]+) \| (?P<level>[^|]+) \| (?P<message>.*)$"
)
_LLM_LATENCY_RE = re.compile(
    r"^📝 流式完成(?:\(降级非流式\))?:.*?\((\d+(?:\.\d+)?)s,"
)
_TOOL_RE = re.compile(r"^🔧 Tool调用: ([A-Za-z0-9_]+)\(")


@dataclass
class LogCursor:
    """按字节追读日志；保留未完整 UTF-8 行，文件截断时自动从头读。"""

    path: Path
    start_at_end: bool = True

    def __post_init__(self):
        self.path = Path(self.path)
        stat = self.path.stat() if self.path.exists() else None
        self.offset = stat.st_size if self.start_at_end and stat else 0
        self.initial_boot_id, self.initial_boot_at = (
            self._read_initial_boot(self.offset)
            if self.start_at_end and stat else ("", "")
        )
        self._file_id = (stat.st_dev, stat.st_ino) if stat else None
        self._carry = b""
        self.issues: Counter[str] = Counter()
        if stat is None:
            self.issues["initial_file_missing"] += 1
        self._missing_pending = False
        self._missing_confirmed = False

    def _read_initial_boot(self, offset: int) -> tuple[str, str]:
        """从观察边界前的短尾巴锚定当前 boot，不把历史正文载入内存。"""
        if offset <= 0:
            return "", ""
        try:
            with self.path.open("rb") as handle:
                start = max(0, offset - 256 * 1024)
                handle.seek(start)
                tail = handle.read(offset - start)
        except OSError:
            return "", ""
        for raw_line in reversed(tail.splitlines()):
            line = raw_line.decode("utf-8", errors="replace")
            match = _LOG_PREFIX_RE.match(line)
            if match:
                return (
                    str(match.group("boot") or ""),
                    str(match.group("ts") or ""),
                )
        return "", ""

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    def _find_rotated_file(self) -> Path | None:
        if self._file_id is None:
            return None

        def _mtime(candidate: Path) -> float:
            try:
                return candidate.stat().st_mtime
            except OSError:
                # 轮转保留策略可能在扫描期间删除旧档；把它排到最后，
                # 后续 identity 检查会安全地跳过已消失的文件。
                return 0.0

        candidates = sorted(
            self.path.parent.glob(f"{self.path.name}.*"),
            key=_mtime,
            reverse=True,
        )
        return next(
            (candidate for candidate in candidates
             if self._identity(candidate) == self._file_id),
            None,
        )

    def _decode(self, chunk: bytes, *, final: bool = False) -> list[str]:
        if not chunk and not (final and self._carry):
            return []
        data = self._carry + chunk
        parts = data.split(b"\n")
        self._carry = parts.pop()
        if final and self._carry:
            parts.append(self._carry)
            self._carry = b""
        return [
            part.rstrip(b"\r").decode("utf-8", errors="replace")
            for part in parts
        ]

    def read_new(self, *, final: bool = False) -> list[str]:
        if not self.path.exists():
            if self._file_id is not None and not self._missing_confirmed:
                # TimedRotatingFileHandler 在 rename 与新文件 open 之间可能
                # 留出一个轮询间隙；第一次缺失先暂挂，连续缺失或最终收尾
                # 才记为不可恢复的证据缺口。
                if final or self._missing_pending:
                    self.issues["file_missing"] += 1
                    self._missing_confirmed = True
                else:
                    self._missing_pending = True
            return []
        if self._missing_pending or self._missing_confirmed:
            if self._missing_confirmed:
                self.issues["file_missing_recovered"] += 1
            self._missing_pending = False
            self._missing_confirmed = False
        stat = self.path.stat()
        file_id = (stat.st_dev, stat.st_ino)
        rotated_lines: list[str] = []
        if self._file_id is not None and file_id != self._file_id:
            archived = self._find_rotated_file()
            if archived is not None:
                try:
                    with archived.open("rb") as handle:
                        handle.seek(self.offset)
                        rotated_lines = self._decode(handle.read(), final=True)
                except OSError:
                    self.issues["rotation_read_failures"] += 1
                    self._carry = b""
            else:
                self.issues["rotation_tail_missing"] += 1
                self._carry = b""
            self.offset = 0
        self._file_id = file_id
        if stat.st_size < self.offset:
            self.issues["file_truncations"] += 1
            self.offset = 0
            self._carry = b""
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        return rotated_lines + self._decode(chunk, final=final)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 1)


def _percentile_fine(values: list[float], fraction: float) -> float:
    """0-1 分数域的分位数——1 位小数会毁掉 rerank 精度（0.875→0.9）。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 3)


def _latency_bucket(value: float) -> int:
    for index, upper in enumerate(LATENCY_BUCKET_UPPER_MS):
        if value <= upper:
            return index
    return len(LATENCY_BUCKET_UPPER_MS) - 1


def _histogram_percentile(
    histogram: object, fraction: float, max_value: float = 0.0,
) -> float:
    if not isinstance(histogram, (list, tuple)):
        return 0.0
    counts = []
    for value in histogram:
        try:
            counts.append(max(0, int(value)))
        except (TypeError, ValueError):
            counts.append(0)
    if not counts or not sum(counts):
        return 0.0
    target = max(1, math.ceil(sum(counts) * fraction))
    seen = 0
    for index, count in enumerate(counts[:len(LATENCY_BUCKET_UPPER_MS)]):
        seen += count
        if seen >= target:
            upper = LATENCY_BUCKET_UPPER_MS[index]
            # 最后一档可能包含任意大的值；保留真实最大值，避免把
            # 600s 误报成固定 600s 上界。
            return round(max_value if math.isinf(upper) else upper, 1)
    return round(max_value, 1)


def parse_log_events(lines: Iterable[str]) -> dict:
    """把日志压缩成低基数事件；不返回 message/cid/用户文本。"""
    events: Counter[str] = Counter()
    events["tts_failure_observed"] = 0
    events["tts_failure_recovered"] = 0
    tools: Counter[str] = Counter()
    llm_latency_ms: list[float] = []
    foreground_llm_latency_ms: list[float] = []
    background_llm_latency_ms: list[float] = []
    knowledge_retrieval = {
        "calls": 0,
        "hits": 0,
        "empty": 0,
        "sources": 0,
        "latency_ms": [],
        "path_counts": Counter(),
        "match_type_counts": Counter(),
        "rerank_score": [],
    }
    latency_histograms = {
        "all": [0] * len(LATENCY_BUCKET_UPPER_MS),
        "foreground": [0] * len(LATENCY_BUCKET_UPPER_MS),
        "background": [0] * len(LATENCY_BUCKET_UPPER_MS),
    }
    latency_max_ms = {name: 0.0 for name in latency_histograms}
    latency_samples_dropped = 0
    boot_ids: set[str] = set()
    recovery_inbound_tokens: set[str] = set()
    recovery_llm_completed_tokens: set[str] = set()
    recovery_send_confirmed_tokens: set[str] = set()
    events_by_boot: dict[str, Counter[str]] = {}
    llm_started_tokens: set[str] = set()
    llm_completed_tokens: set[str] = set()
    llm_started_at: dict[str, str] = {}
    llm_completed_at: dict[str, str] = {}
    voice_started_tokens: set[str] = set()
    voice_dispatched_tokens: set[str] = set()
    voice_completed_tokens: set[str] = set()
    voice_started_at: dict[str, str] = {}
    voice_dispatched_at: dict[str, str] = {}
    voice_completed_at: dict[str, str] = {}
    background_by_logger: dict[str, dict[str, object]] = {}
    tts_repairs_by_boot: dict[str, dict[str, int]] = {}
    gpt_exit_codes: Counter[str] = Counter()
    gpt_exit_lifetime_max_s = 0.0
    gpt_exit_last: dict[str, object] = {}
    gpt_exit_keys: set[tuple[str, str, str, str]] = set()
    boot_first_at: dict[str, str] = {}
    latest_log_at = ""
    latest_boot = ""

    def record_background(
        logger_name: str, timestamp: str, boot_id: str, *, heartbeat: bool = False,
    ) -> None:
        record = background_by_logger.setdefault(
            logger_name,
            {
                "activity": 0,
                "heartbeats": 0,
                "first_at": timestamp,
                "last_at": timestamp,
                "first_heartbeat_at": "",
                "last_heartbeat_at": "",
                "last_heartbeat_boot": boot_id,
                "heartbeat_boots": {},
            },
        )
        record["activity"] = int(record.get("activity", 0) or 0) + 1
        record["first_at"] = min(str(record.get("first_at") or timestamp), timestamp)
        previous_last = str(record.get("last_at") or timestamp)
        record["last_at"] = max(previous_last, timestamp)
        if heartbeat:
            record["heartbeats"] = int(record.get("heartbeats", 0) or 0) + 1
            first = str(record.get("first_heartbeat_at") or timestamp)
            last = str(record.get("last_heartbeat_at") or timestamp)
            record["first_heartbeat_at"] = min(first, timestamp)
            record["last_heartbeat_at"] = max(last, timestamp)
            if timestamp >= last:
                record["last_heartbeat_boot"] = boot_id
            # 按 boot 分开保存心跳边界和最大间隔。仅保留聚合值会把
            # “开头/结尾各一次、中间停摆数小时”误判为健康。
            heartbeat_boots = record.setdefault("heartbeat_boots", {})
            if isinstance(heartbeat_boots, dict):
                boot_record = heartbeat_boots.setdefault(
                    boot_id,
                    {
                        "count": 0,
                        "first_at": timestamp,
                        "last_at": timestamp,
                        "max_gap_seconds": 0.0,
                    },
                )
                if isinstance(boot_record, dict):
                    previous = str(boot_record.get("last_at") or "")
                    if previous and timestamp >= previous:
                        previous_epoch = _timestamp_epoch(previous)
                        current_epoch = _timestamp_epoch(timestamp)
                        if previous_epoch is not None and current_epoch is not None:
                            boot_record["max_gap_seconds"] = max(
                                float(boot_record.get("max_gap_seconds", 0.0) or 0.0),
                                max(0.0, current_epoch - previous_epoch),
                            )
                    boot_record["count"] = int(boot_record.get("count", 0) or 0) + 1
                    boot_record["first_at"] = min(
                        str(boot_record.get("first_at") or timestamp), timestamp,
                    )
                    if timestamp >= previous:
                        boot_record["last_at"] = timestamp

    def record_latency(target: list[float], histogram_name: str, value: float) -> None:
        nonlocal latency_samples_dropped
        histogram = latency_histograms[histogram_name]
        histogram[_latency_bucket(value)] += 1
        latency_max_ms[histogram_name] = max(latency_max_ms[histogram_name], value)
        if len(target) < MAX_LATENCY_SAMPLES:
            target.append(value)
        else:
            latency_samples_dropped += 1

    for raw in lines:
        physical_line = str(raw).rstrip("\r\n")
        match = _LOG_PREFIX_RE.match(physical_line)
        if not match:
            if physical_line.strip():
                events["unstructured_lines"] += 1
                if re.search(r"\b(?:ERROR|CRITICAL|TRACEBACK)\b", physical_line, re.I):
                    events["unstructured_error_lines"] += 1
            continue
        events["structured_lines"] += 1
        boot_id = match.group("boot")
        timestamp = match.group("ts")
        boot_ids.add(boot_id)
        if boot_id not in boot_first_at or timestamp < boot_first_at[boot_id]:
            boot_first_at[boot_id] = timestamp
        if timestamp >= latest_log_at:
            latest_log_at = timestamp
            latest_boot = boot_id
        level = match.group("level").strip().upper()
        logger_name = match.group("logger").strip()
        message = match.group("message")
        boot_events = events_by_boot.setdefault(boot_id, Counter())
        correlation_id = str(match.group("cid") or "")
        correlation_token = (
            f"{boot_id}:{correlation_id}" if correlation_id != "-" else ""
        )

        is_background_activity = any(
            message.startswith(prefix)
            for prefix in BACKGROUND_SUCCESS_PREFIXES.get(logger_name, ())
        )
        is_background_heartbeat = any(
            message.startswith(prefix)
            for prefix in BACKGROUND_HEARTBEAT_PREFIXES.get(logger_name, ())
        )
        if is_background_activity:
            events["background_activity_lines"] += 1
            record_background(
                logger_name, timestamp, boot_id, heartbeat=is_background_heartbeat,
            )
        if is_background_heartbeat:
            heartbeat_errors = re.search(r"\berrors=(\d+)\b", message)
            if heartbeat_errors and int(heartbeat_errors.group(1)) > 0:
                # 自治循环会把被内部吞掉的子步骤异常汇总到心跳；
                # 不能因最终仍写出 heartbeat 就把该轮当成成功。
                events["background_task_failures"] += int(
                    heartbeat_errors.group(1)
                )

        # 私聊正文出于隐私只由 Handler 在主人私聊路径记录；NapCat 的
        # _handle_private 不写正文日志。保留 SnowLuma 兼容旧版/取证日志，
        # 但不能只认 SnowLuma，否则真实私聊样本会永久显示不足。
        is_inbound_private = (
            logger_name in {"糖糖.SnowLuma", "糖糖.Handler"}
            and message.startswith("📩 [私聊]")
        )
        is_inbound_group = (
            logger_name == "糖糖.SnowLuma" and message.startswith("💬 [群:")
        )
        if is_inbound_private:
            events["inbound_private"] += 1
            boot_events["inbound"] += 1
            if correlation_token:
                recovery_inbound_tokens.add(correlation_token)
        elif is_inbound_group:
            events["inbound_group"] += 1
            boot_events["inbound"] += 1
            if correlation_token:
                recovery_inbound_tokens.add(correlation_token)
        # SnowLuma 入站日志和 ASR 成功日志都带用户原文。事件识别必须只看
        # 固定前缀，不能让用户在正文里伪造“工具成功/语音已发送”等监控证据。
        if is_inbound_private or is_inbound_group:
            continue

        background_failure = (
            level in {"WARNING", "ERROR"}
            and any(
                message.startswith(prefix)
                for prefix in BACKGROUND_FAILURE_PREFIXES.get(logger_name, ())
            )
        )
        if (
            level in {"WARNING", "ERROR"}
            and logger_name == "糖糖.Scheduler"
            and message.startswith("⏰ 任务 #")
            and any(marker in message for marker in ("发送未确认", "发送失败", "响应丢失"))
        ):
            background_failure = True
        if (
            not background_failure
            and logger_name in BACKGROUND_WARNING_LOGGERS
            and level == "ERROR"
        ):
            background_failure = True
        if (
            not background_failure
            and logger_name in BACKGROUND_WARNING_LOGGERS
            and level == "WARNING"
            and any(word in message for word in BACKGROUND_FAILURE_WORDS)
        ):
            background_failure = True
        if (
            background_failure
            and logger_name == "糖糖.Album"
            and any(marker in message for marker in EXPECTED_ALBUM_LIMIT_MARKERS)
        ):
            # QQ 的每日点赞配额是外部业务终态，不代表后台循环崩溃。
            # 单独计数供报告展示，但不污染 background_tasks/album FAIL。
            events["album_quota_exhausted"] += 1
            background_failure = False
        if background_failure:
            events["background_task_failures"] += 1
            if logger_name == "糖糖.Album":
                events["album_failures"] += 1
            elif logger_name == "糖糖.HealthCheck":
                events["health_check_failures"] += 1

        tts_ready = (
            logger_name == "糖糖.ServiceMgr"
            and level == "INFO"
            and message.startswith("🔧 GPT-SoVITS 已就绪")
        )
        if logger_name == "糖糖.ServiceMgr" and (background_failure or tts_ready):
            tts_repair = tts_repairs_by_boot.setdefault(
                boot_id,
                {
                    "observed": 0,
                    "recovered": 0,
                    "unmatched_failures": 0,
                    "ready_seen": 0,
                },
            )
            if background_failure:
                events["tts_failure_observed"] += 1
                tts_repair["observed"] += 1
                tts_repair["unmatched_failures"] += 1
            else:
                recovered_now = tts_repair["unmatched_failures"]
                events["tts_failure_recovered"] += recovered_now
                tts_repair["recovered"] += recovered_now
                tts_repair["unmatched_failures"] = 0
                tts_repair["ready_seen"] = 1

        if (
            logger_name == "糖糖.ServiceMgr"
            and message.startswith("🔧 GPT-SoVITS 就绪后")
            and " pid=" in message
        ):
            exit_match = re.search(
                r"\(code=(?P<code>-?\d+)\)\s+pid=(?P<pid>\S+)\s+"
                r"lifetime=(?P<lifetime>[0-9]+(?:\.[0-9]+)?)s",
                message,
            )
            if exit_match:
                code = exit_match.group("code")
                pid = exit_match.group("pid")
                lifetime_text = exit_match.group("lifetime")
                exit_key = (boot_id, pid, code, lifetime_text)
                if exit_key not in gpt_exit_keys:
                    gpt_exit_keys.add(exit_key)
                    events["gpt_ready_child_exits"] += 1
                    gpt_exit_codes[code] += 1
                    lifetime_s = float(lifetime_text)
                    gpt_exit_lifetime_max_s = max(
                        gpt_exit_lifetime_max_s, lifetime_s,
                    )
                    if timestamp >= str(gpt_exit_last.get("timestamp") or ""):
                        gpt_exit_last = {
                            "timestamp": timestamp,
                            "pid": pid,
                            "code": code,
                            "lifetime_s": lifetime_s,
                        }
            else:
                events["gpt_exit_metadata_missing"] += 1

        if logger_name == "糖糖.Album" and message.startswith((
            "💝 每日定时点赞", "每日定时点赞完成", "群",
        )):
            events["album_success"] += 1
        if logger_name == "糖糖.HealthCheck" and message.startswith(
            "🩺 启动健康检查"
        ):
            events["health_check_runs"] += 1

        if logger_name == "糖糖.Handler" and message.startswith("🔄 正在调用LLM生成回复"):
            events["llm_started"] += 1
            if match.group("cid") != "-":
                llm_started_tokens.add(
                    f"{match.group('boot')}:{match.group('cid')}"
                )
                llm_started_at[
                    f"{match.group('boot')}:{match.group('cid')}"
                ] = timestamp
        latency = _LLM_LATENCY_RE.search(message)
        if latency and logger_name == "糖糖.Handler":
            events["llm_completed"] += 1
            latency_ms = float(latency.group(1)) * 1000
            record_latency(llm_latency_ms, "all", latency_ms)
            if match.group("cid") == "-":
                record_latency(background_llm_latency_ms, "background", latency_ms)
            else:
                record_latency(foreground_llm_latency_ms, "foreground", latency_ms)
        if logger_name == "糖糖.Handler" and message.startswith("✅ LLM回合完成"):
            events["llm_turn_completed"] += 1
            boot_events["llm_completed"] += 1
            if correlation_token:
                recovery_llm_completed_tokens.add(correlation_token)
            if match.group("cid") != "-":
                llm_completed_tokens.add(
                    f"{match.group('boot')}:{match.group('cid')}"
                )
                llm_completed_at[
                    f"{match.group('boot')}:{match.group('cid')}"
                ] = timestamp
        if logger_name in {"糖糖.Handler", "糖糖.Memory", "糖糖.CatchUp"} and message.startswith((
            "LLM调用失败", "❌ LLM调用失败", "轻量LLM调用重试仍失败",
            "🧠 LLM 提取失败", "📬 LLM 补回复生成失败",
            "⚠ LLM 工具循环+收尾轮后仍无文字回复",
        )):
            events["llm_failed"] += 1
        if (
            logger_name == "糖糖.CatchUp"
            and message.startswith("📬 群 ")
            and "LLM 摘要失败" in message
        ):
            events["llm_failed"] += 1
        is_llm_transport = (
            "LLM" in logger_name
            or logger_name == "httpx" and "chat/completions" in message
        )
        if "429" in message and is_llm_transport:
            events["llm_429"] += 1
        if re.search(r"\b5\d\d\b", message) and is_llm_transport:
            events["llm_5xx"] += 1
        if logger_name == "糖糖.SnowLuma" and message.startswith(("📤 群消息已发送 →", "📤 私聊已发送 →", "📤 群临时会话已发送 →")):
            message_id_match = re.search(r"\bmsg_id=(-?\d+)\b", message)
            if message_id_match and int(message_id_match.group(1)) != 0:
                events["send_confirmed_log"] += 1
                boot_events["send_confirmed"] += 1
                if correlation_token:
                    recovery_send_confirmed_tokens.add(correlation_token)
            else:
                # “已发送”只是历史日志文案；没有非零 message_id 就没有
                # QQ 侧送达证据，必须与 confirmed 互斥地计入 uncertain。
                events["send_uncertain_log"] += 1
        if logger_name in {"糖糖.SnowLuma", "糖糖.Handler", "糖糖.Tasks", "糖糖.Autonomy", "糖糖.ImageShare", "糖糖.Personalization", "糖糖.Scheduler"} and level in {"WARNING", "ERROR"} and (
            "发送未确认" in message
            or "未确认送达" in message
            or "发送可疑" in message
        ):
            events["send_uncertain_log"] += 1
        if logger_name in {"糖糖.SnowLuma", "糖糖.Handler", "糖糖.Tasks", "糖糖.Autonomy", "糖糖.ImageShare", "糖糖.Personalization", "糖糖.Scheduler"} and level in {"WARNING", "ERROR"} and "发送失败" in message:
            events["send_failed_log"] += 1

        tool = _TOOL_RE.search(message) if logger_name == "糖糖.Handler" else None
        if tool:
            name = tool.group(1)
            tools[name] += 1
            events["tool_calls"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🚫 工具参数不是合法 JSON"):
            events["tool_invalid_arguments"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🪟 窗口决策 | action=reply"):
            events["window_reply"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🪟 窗口决策 | action=skip"):
            events["window_skip"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🪟 窗口渐变退出"):
            events["window_fade"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🖼 按需识图:"):
            events["vision_succeeded"] += 1
        if logger_name.startswith("糖糖.Vision") and "识图失败" in message:
            events["vision_failed"] += 1
        if logger_name == "糖糖.Handler" and message.startswith((
            "🎨 Sticker Tool →", "🎨 Sticker ActionPlan →",
        )):
            delivered = re.search(
                r"attempted=(\d+) confirmed=(\d+) uncertain=(\d+) failed=(\d+)",
                message,
            )
            if delivered:
                events["sticker_attempted"] += int(delivered.group(1))
                events["sticker_confirmed"] += int(delivered.group(2))
                events["sticker_uncertain"] += int(delivered.group(3))
                events["sticker_failed"] += int(delivered.group(4))
        if logger_name == "糖糖.Handler" and message.startswith((
            "🎨 Sticker Tool 送达不完整 →", "🎨 Sticker ActionPlan 送达不完整 →",
        )):
            delivered = re.search(
                r"attempted=(\d+) confirmed=(\d+) uncertain=(\d+) failed=(\d+)",
                message,
            )
            if delivered:
                events["sticker_attempted"] += int(delivered.group(1))
                events["sticker_confirmed"] += int(delivered.group(2))
                events["sticker_uncertain"] += int(delivered.group(3))
                events["sticker_failed"] += int(delivered.group(4))
        if logger_name == "糖糖.Handler" and message.startswith("🎤 语音触发:"):
            events["voice_attempted"] += 1
            if match.group("cid") != "-":
                voice_started_tokens.add(
                    f"{match.group('boot')}:{match.group('cid')}"
                )
                voice_started_at[
                    f"{match.group('boot')}:{match.group('cid')}"
                ] = timestamp
        if logger_name == "糖糖.Handler" and message.startswith("🎙️ 语音发送:"):
            events["voice_dispatch"] += 1
            if match.group("cid") != "-":
                voice_dispatched_tokens.add(
                    f"{match.group('boot')}:{match.group('cid')}"
                )
                voice_dispatched_at[
                    f"{match.group('boot')}:{match.group('cid')}"
                ] = timestamp
        if logger_name == "糖糖.Handler" and message.startswith(("🎤 语音已发送", "🎙️ 语音已发送")):
            events["voice_succeeded"] += 1
            if match.group("cid") != "-":
                voice_completed_tokens.add(
                    f"{match.group('boot')}:{match.group('cid')}"
                )
                voice_completed_at[
                    f"{match.group('boot')}:{match.group('cid')}"
                ] = timestamp
        if logger_name in {"糖糖.Handler", "糖糖.Voice"} and message.startswith((
            "语音生成失败", "语音发送失败", "TTS 生成失败",
            "🎙️ 语音发送响应丢失", "🎙️ 语音文件已生成，但发送失败",
            "🎙️ 语音文件已生成，发送已接受但QQ未确认送达",
            "GPT-SoVITS 3次尝试均失败",
        )):
            events["voice_failed"] += 1
        if logger_name == "糖糖.ASR" and message.startswith("🎤 ASR →"):
            events["audio_input_succeeded"] += 1
        if logger_name == "糖糖.ASR" and re.match(
            r"^(?:❌ |⚠️ )?(?:语音识别失败|SILK 解码失败|ffmpeg 转码失败)",
            message,
        ):
            events["audio_input_failed"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🎤 Tool唱歌已确认:"):
            events["singing_confirmed"] += 1
        if logger_name in {"糖糖.Handler", "糖糖.Tasks"} and message.startswith(("🎤 Tool唱歌音频未确认:", "❌ 唱歌失败", "唱歌失败")):
            events["singing_failed"] += 1
        if logger_name == "糖糖.Voice" and message.startswith("🎙️ GPT-SoVITS 模型已切换:") and match.group("cid") != "-":
            events["role_voice_switched"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("🎨 表情包切换:"):
            events["role_stickers_switched"] += 1
        if logger_name == "糖糖.Handler" and message.startswith("📚 Knowledge Tool |"):
            outcome_match = re.search(r"\boutcome=(success|empty|error|invalid)\b", message)
            if outcome_match:
                events[f"knowledge_{outcome_match.group(1)}"] += 1
        if logger_name == "糖糖.Knowledge" and message.startswith("📚 Knowledge Retrieval |"):
            retrieval = re.search(
                r"\bhits=(\d+)\s+sources=(\d+)\s+paths=([^\s]+)\s+latency_ms=([0-9]+(?:\.[0-9]+)?)\b",
                message,
            )
            if retrieval:
                hits = max(0, int(retrieval.group(1)))
                sources = max(0, int(retrieval.group(2)))
                knowledge_retrieval["calls"] += 1
                knowledge_retrieval["hits"] += hits
                knowledge_retrieval["sources"] += sources
                if hits == 0:
                    knowledge_retrieval["empty"] += 1
                latency_ms = float(retrieval.group(4))
                if len(knowledge_retrieval["latency_ms"]) < MAX_LATENCY_SAMPLES:
                    knowledge_retrieval["latency_ms"].append(latency_ms)
                for path in retrieval.group(3).split(","):
                    path_name = path.split(":", 1)[0].strip()
                    if path_name and path_name != "none":
                        knowledge_retrieval["path_counts"][path_name] += 1
                trace_match = re.search(r"\btrace=(\[[^\s]*\])(?:\s|$)", message)
                if trace_match:
                    try:
                        trace_items = json.loads(trace_match.group(1))
                    except json.JSONDecodeError:
                        trace_items = None
                    if not isinstance(trace_items, list) or len(trace_items) > 3:
                        events["knowledge_trace_malformed"] += 1
                    else:
                        for item in trace_items:
                            if (not isinstance(item, dict)
                                    or set(item) != {"match_type", "rerank_score"}):
                                events["knowledge_trace_malformed"] += 1
                                continue
                            match_type = item["match_type"]
                            paths = str(match_type).split("+")
                            if (not isinstance(match_type, str) or not match_type
                                    or any(path not in {
                                        "fts", "keyword", "semantic", "expansion",
                                    } for path in paths)):
                                events["knowledge_trace_malformed"] += 1
                                continue
                            knowledge_retrieval["match_type_counts"][match_type] += 1
                            score = item["rerank_score"]
                            if score is None:
                                continue
                            if (isinstance(score, bool)
                                    or not isinstance(score, (int, float))
                                    or not math.isfinite(float(score))):
                                events["knowledge_trace_malformed"] += 1
                                continue
                            if len(knowledge_retrieval["rerank_score"]) < MAX_LATENCY_SAMPLES:
                                knowledge_retrieval["rerank_score"].append(float(score))
            else:
                events["knowledge_trace_malformed"] += 1
        if logger_name == "糖糖.Autonomy" and message.startswith(("🔥 糖糖主动发起 →", "💌 私聊插话 →")):
            events["autonomy_confirmed"] += 1
        if logger_name == "糖糖.Autonomy" and level == "WARNING" and (
            "自治发送未确认" in message
            or "主动私聊" in message and "未确认" in message
        ):
            events["autonomy_uncertain"] += 1
        if logger_name == "糖糖.Tasks" and message.startswith("📋 任务提醒已发送:"):
            events["reminder_confirmed"] += 1
        if logger_name == "糖糖.Personalization" and message.startswith("🎂 生日祝福已确认 →"):
            events["birthday_confirmed"] += 1
        if logger_name == "糖糖.ImageShare" and message.startswith("🖼 图片分享已确认 →"):
            events["image_share_confirmed"] += 1
        if level == "ERROR":
            events["log_errors"] += 1
        elif level == "WARNING":
            events["log_warnings"] += 1
    return {
        "events": dict(events),
        "tools": dict(tools),
        "llm_latency_ms": llm_latency_ms,
        "foreground_llm_latency_ms": foreground_llm_latency_ms,
        "background_llm_latency_ms": background_llm_latency_ms,
        "knowledge_retrieval": {
            **knowledge_retrieval,
            "path_counts": dict(knowledge_retrieval["path_counts"]),
            "match_type_counts": dict(knowledge_retrieval["match_type_counts"]),
        },
        "latency_histograms": latency_histograms,
        "latency_max_ms": latency_max_ms,
        "latency_histogram_complete": True,
        "latency_samples_dropped": latency_samples_dropped,
        "llm_started_tokens": sorted(llm_started_tokens),
        "llm_completed_tokens": sorted(llm_completed_tokens),
        "llm_started_at": llm_started_at,
        "llm_completed_at": llm_completed_at,
        "voice_started_tokens": sorted(voice_started_tokens),
        "voice_dispatched_tokens": sorted(voice_dispatched_tokens),
        "voice_completed_tokens": sorted(voice_completed_tokens),
        "voice_started_at": voice_started_at,
        "voice_dispatched_at": voice_dispatched_at,
        "voice_completed_at": voice_completed_at,
        "background_by_logger": background_by_logger,
        "tts_repairs_by_boot": tts_repairs_by_boot,
        "gpt_exit_codes": dict(gpt_exit_codes),
        "gpt_exit_lifetime_max_s": round(gpt_exit_lifetime_max_s, 3),
        "gpt_exit_last": gpt_exit_last,
        "boot_first_at": boot_first_at,
        "latest_log_at": latest_log_at,
        "latest_boot": latest_boot,
        "boot_ids": sorted(boot_ids),
        "recovery_inbound_tokens": sorted(recovery_inbound_tokens),
        "recovery_llm_completed_tokens": sorted(recovery_llm_completed_tokens),
        "recovery_send_confirmed_tokens": sorted(recovery_send_confirmed_tokens),
        "boot_count": len(boot_ids),
        "events_by_boot": {
            boot_id: dict(counts) for boot_id, counts in events_by_boot.items()
        },
    }


def merge_log_summaries(*summaries: dict) -> dict:
    """合并分批解析结果；只保留低基数计数、延迟和临时配对 token。"""
    events: Counter[str] = Counter()
    events["tts_failure_observed"] = 0
    events["tts_failure_recovered"] = 0
    tools: Counter[str] = Counter()
    list_fields = (
        "llm_latency_ms",
        "foreground_llm_latency_ms",
        "background_llm_latency_ms",
    )
    set_fields = (
        "llm_started_tokens", "llm_completed_tokens",
        "voice_started_tokens", "voice_dispatched_tokens", "voice_completed_tokens",
        "boot_ids", "recovery_inbound_tokens", "recovery_llm_completed_tokens",
        "recovery_send_confirmed_tokens",
    )
    time_fields = (
        "llm_started_at", "llm_completed_at",
        "voice_started_at", "voice_dispatched_at", "voice_completed_at",
    )
    merged_lists = {name: [] for name in list_fields}
    merged_knowledge_retrieval = {
        "calls": 0,
        "hits": 0,
        "empty": 0,
        "sources": 0,
        "latency_ms": [],
        "path_counts": Counter(),
        "match_type_counts": Counter(),
        "rerank_score": [],
    }
    merged_histograms = {
        name: [0] * len(LATENCY_BUCKET_UPPER_MS)
        for name in ("all", "foreground", "background")
    }
    merged_latency_max = {
        name: 0.0 for name in merged_histograms
    }
    latency_histogram_complete = True
    merged_sets = {name: set() for name in set_fields}
    merged_times: dict[str, dict[str, str]] = {name: {} for name in time_fields}
    merged_background: dict[str, dict[str, object]] = {}
    merged_tts_repairs: dict[str, dict[str, int]] = {}
    merged_gpt_exit_codes: Counter[str] = Counter()
    merged_gpt_exit_lifetime_max_s = 0.0
    merged_gpt_exit_last: dict[str, object] = {}
    merged_boot_first_at: dict[str, str] = {}
    merged_events_by_boot: dict[str, Counter[str]] = {}
    merged_latest = {"latest_log_at": "", "latest_boot": ""}
    latency_samples_dropped = 0
    correlation_tokens_dropped = 0
    correlation_boundary_tolerant = False
    for summary in summaries:
        if not summary:
            continue
        # 观察器正式模式从日志尾部开始追读；该标记必须随累计摘要
        # 传播，否则下一个 merge 调用会悄悄退回严格边界配对。
        correlation_boundary_tolerant = (
            correlation_boundary_tolerant
            or bool(summary.get("correlation_boundary_tolerant"))
        )
        events.update(summary.get("events") or {})
        tools.update(summary.get("tools") or {})
        incoming_knowledge = summary.get("knowledge_retrieval") or {}
        if isinstance(incoming_knowledge, dict):
            for key in ("calls", "hits", "empty", "sources"):
                try:
                    merged_knowledge_retrieval[key] += max(
                        0, int(incoming_knowledge.get(key, 0) or 0)
                    )
                except (TypeError, ValueError):
                    events["knowledge_trace_malformed"] += 1
            incoming_latency = incoming_knowledge.get("latency_ms") or []
            if isinstance(incoming_latency, (list, tuple)):
                values = []
                for value in incoming_latency:
                    try:
                        parsed = float(value)
                    except (TypeError, ValueError):
                        events["knowledge_trace_malformed"] += 1
                        continue
                    if parsed >= 0:
                        values.append(parsed)
                merged_knowledge_retrieval["latency_ms"].extend(values)
                if len(merged_knowledge_retrieval["latency_ms"]) > MAX_LATENCY_SAMPLES:
                    overflow = len(merged_knowledge_retrieval["latency_ms"]) - MAX_LATENCY_SAMPLES
                    del merged_knowledge_retrieval["latency_ms"][:overflow]
                    latency_samples_dropped += overflow
            elif incoming_latency:
                events["knowledge_trace_malformed"] += 1
            incoming_paths = incoming_knowledge.get("path_counts") or {}
            if isinstance(incoming_paths, dict):
                for path_name, count in incoming_paths.items():
                    try:
                        merged_knowledge_retrieval["path_counts"][str(path_name)] += max(
                            0, int(count or 0)
                        )
                    except (TypeError, ValueError):
                        events["knowledge_trace_malformed"] += 1
            elif incoming_paths:
                events["knowledge_trace_malformed"] += 1
            incoming_match_types = incoming_knowledge.get("match_type_counts") or {}
            if isinstance(incoming_match_types, dict):
                for match_type, count in incoming_match_types.items():
                    paths = str(match_type).split("+")
                    if (not isinstance(match_type, str) or not match_type
                            or any(path not in {
                                "fts", "keyword", "semantic", "expansion",
                            } for path in paths)):
                        events["knowledge_trace_malformed"] += 1
                        continue
                    try:
                        merged_knowledge_retrieval["match_type_counts"][match_type] += max(
                            0, int(count or 0)
                        )
                    except (TypeError, ValueError):
                        events["knowledge_trace_malformed"] += 1
            elif incoming_match_types:
                events["knowledge_trace_malformed"] += 1
            incoming_rerank_scores = incoming_knowledge.get("rerank_score") or []
            if isinstance(incoming_rerank_scores, (list, tuple)):
                scores = []
                for value in incoming_rerank_scores:
                    if (isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(float(value))):
                        events["knowledge_trace_malformed"] += 1
                        continue
                    scores.append(float(value))
                merged_knowledge_retrieval["rerank_score"].extend(scores)
                if len(merged_knowledge_retrieval["rerank_score"]) > MAX_LATENCY_SAMPLES:
                    overflow = len(merged_knowledge_retrieval["rerank_score"]) - MAX_LATENCY_SAMPLES
                    del merged_knowledge_retrieval["rerank_score"][:overflow]
            elif incoming_rerank_scores:
                events["knowledge_trace_malformed"] += 1
        incoming_gpt_codes = summary.get("gpt_exit_codes") or {}
        if isinstance(incoming_gpt_codes, dict):
            for code, count in incoming_gpt_codes.items():
                try:
                    merged_gpt_exit_codes[str(code)] += max(0, int(count or 0))
                except (TypeError, ValueError):
                    events["gpt_exit_metadata_missing"] += 1
        elif incoming_gpt_codes:
            events["gpt_exit_metadata_missing"] += 1
        try:
            merged_gpt_exit_lifetime_max_s = max(
                merged_gpt_exit_lifetime_max_s,
                float(summary.get("gpt_exit_lifetime_max_s", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            events["gpt_exit_metadata_missing"] += 1
        incoming_gpt_last = summary.get("gpt_exit_last") or {}
        if isinstance(incoming_gpt_last, dict):
            incoming_timestamp = str(incoming_gpt_last.get("timestamp") or "")
            current_timestamp = str(merged_gpt_exit_last.get("timestamp") or "")
            if incoming_timestamp and incoming_timestamp >= current_timestamp:
                merged_gpt_exit_last = dict(incoming_gpt_last)
        elif incoming_gpt_last:
            events["gpt_exit_metadata_missing"] += 1
        for boot_id, incoming in (summary.get("events_by_boot") or {}).items():
            if isinstance(incoming, dict):
                merged_events_by_boot.setdefault(str(boot_id), Counter()).update(
                    incoming
                )
        incoming_tts_repairs = summary.get("tts_repairs_by_boot") or {}
        if isinstance(incoming_tts_repairs, dict):
            for boot_id, incoming in incoming_tts_repairs.items():
                if not isinstance(incoming, dict):
                    continue
                current = merged_tts_repairs.setdefault(
                    str(boot_id),
                    {
                        "observed": 0,
                        "recovered": 0,
                        "unmatched_failures": 0,
                        "ready_seen": 0,
                    },
                )
                incoming_observed = max(0, int(
                    incoming.get("observed", incoming.get("started", 0)) or 0
                ))
                incoming_recovered = max(0, int(incoming.get("recovered", 0) or 0))
                incoming_unmatched = max(0, int(
                    incoming.get(
                        "unmatched_failures",
                        incoming.get(
                            "unmatched_started",
                            incoming_observed - incoming_recovered,
                        ),
                    ) or 0
                ))
                incoming_ready_seen = bool(
                    incoming.get("ready_seen")
                    or incoming.get("unmatched_ready")
                    or incoming_recovered
                )
                # 一个“已就绪”证明该 boot 在此刻恢复，会关闭此前同一
                # TTS 修复 episode 中的全部失败；随后发生的失败仍保留。
                cross_recovered = (
                    current["unmatched_failures"] if incoming_ready_seen else 0
                )
                current["observed"] += incoming_observed
                current["recovered"] += incoming_recovered + cross_recovered
                current["unmatched_failures"] = (
                    incoming_unmatched
                    if incoming_ready_seen
                    else current["unmatched_failures"] + incoming_unmatched
                )
                current["ready_seen"] = int(
                    bool(current["ready_seen"] or incoming_ready_seen)
                )
                events["tts_failure_recovered"] += cross_recovered
        latency_samples_dropped += int(summary.get("latency_samples_dropped", 0) or 0)
        # 手工/旧版摘要可能没有完整直方图契约；一旦混入，累计摘要
        # 不能再把有限列表的 p95 冒充全量 p95。
        if summary.get("latency_histogram_complete") is not True:
            latency_histogram_complete = False
        incoming_histograms = summary.get("latency_histograms")
        incoming_max = summary.get("latency_max_ms") or {}
        if not isinstance(incoming_histograms, dict):
            latency_histogram_complete = False
        else:
            for histogram_name, target in merged_histograms.items():
                incoming = incoming_histograms.get(histogram_name)
                if not isinstance(incoming, (list, tuple)):
                    latency_histogram_complete = False
                    continue
                if len(incoming) != len(target):
                    latency_histogram_complete = False
                    continue
                for index, value in enumerate(incoming):
                    try:
                        target[index] += max(0, int(value))
                    except (TypeError, ValueError):
                        latency_histogram_complete = False
                try:
                    merged_latency_max[histogram_name] = max(
                        merged_latency_max[histogram_name],
                        float(incoming_max.get(histogram_name, 0.0) or 0.0),
                    )
                except (TypeError, ValueError):
                    latency_histogram_complete = False
        for name in list_fields:
            values = list(summary.get(name) or [])
            if len(values) > MAX_LATENCY_SAMPLES:
                latency_samples_dropped += len(values) - MAX_LATENCY_SAMPLES
                values = values[-MAX_LATENCY_SAMPLES:]
            merged_lists[name].extend(values)
            if len(merged_lists[name]) > MAX_LATENCY_SAMPLES:
                overflow = len(merged_lists[name]) - MAX_LATENCY_SAMPLES
                del merged_lists[name][:overflow]
                latency_samples_dropped += overflow
        for name in set_fields:
            merged_sets[name].update(summary.get(name) or [])
            if name != "boot_ids" and len(merged_sets[name]) > MAX_CORRELATION_TOKENS:
                overflow = len(merged_sets[name]) - MAX_CORRELATION_TOKENS
                # token 只用于配对完整性；保留排序后的尾部，避免集合无限增长。
                kept = sorted(merged_sets[name])[-MAX_CORRELATION_TOKENS:]
                merged_sets[name] = set(kept)
                correlation_tokens_dropped += overflow
        for name in time_fields:
            incoming_times = summary.get(name) or {}
            if not isinstance(incoming_times, dict):
                continue
            target_times = merged_times[name]
            for token, timestamp in incoming_times.items():
                token = str(token)
                timestamp = str(timestamp or "")
                if timestamp:
                    target_times[token] = timestamp
            if len(target_times) > MAX_CORRELATION_TOKENS:
                overflow = len(target_times) - MAX_CORRELATION_TOKENS
                kept_tokens = sorted(target_times)[-MAX_CORRELATION_TOKENS:]
                merged_times[name] = {
                    token: target_times[token] for token in kept_tokens
                }
                correlation_tokens_dropped += overflow
        for boot_id, timestamp in (summary.get("boot_first_at") or {}).items():
            boot_id = str(boot_id)
            timestamp = str(timestamp or "")
            if timestamp and (
                boot_id not in merged_boot_first_at
                or timestamp < merged_boot_first_at[boot_id]
            ):
                merged_boot_first_at[boot_id] = timestamp
        for logger_name, incoming in (summary.get("background_by_logger") or {}).items():
            if not isinstance(incoming, dict):
                continue
            current = merged_background.setdefault(
                str(logger_name),
                {
                    "activity": 0,
                    "heartbeats": 0,
                    "first_at": "",
                    "last_at": "",
                    "first_heartbeat_at": "",
                    "last_heartbeat_at": "",
                    "last_heartbeat_boot": "",
                    "heartbeat_boots": {},
                },
            )
            current["activity"] = int(current["activity"] or 0) + int(
                incoming.get("activity", 0) or 0
            )
            current["heartbeats"] = int(current["heartbeats"] or 0) + int(
                incoming.get("heartbeats", 0) or 0
            )
            for first_key in ("first_at", "first_heartbeat_at"):
                value = str(incoming.get(first_key) or "")
                if value:
                    current[first_key] = min(
                        str(current.get(first_key) or value), value,
                    )
            for last_key in ("last_at", "last_heartbeat_at"):
                value = str(incoming.get(last_key) or "")
                if value and value >= str(current.get(last_key) or ""):
                    current[last_key] = value
                    if last_key == "last_heartbeat_at":
                        current["last_heartbeat_boot"] = str(
                            incoming.get("last_heartbeat_boot") or ""
                        )
            incoming_boots = incoming.get("heartbeat_boots") or {}
            current_boots = current.setdefault("heartbeat_boots", {})
            if isinstance(incoming_boots, dict) and isinstance(current_boots, dict):
                for boot_id, incoming_boot in incoming_boots.items():
                    if not isinstance(incoming_boot, dict):
                        continue
                    boot_id = str(boot_id)
                    incoming_first = str(incoming_boot.get("first_at") or "")
                    incoming_last = str(incoming_boot.get("last_at") or "")
                    incoming_count = int(incoming_boot.get("count", 0) or 0)
                    incoming_gap = float(
                        incoming_boot.get("max_gap_seconds", 0.0) or 0.0
                    )
                    existing = current_boots.get(boot_id)
                    if not isinstance(existing, dict):
                        current_boots[boot_id] = {
                            "count": incoming_count,
                            "first_at": incoming_first,
                            "last_at": incoming_last,
                            "max_gap_seconds": incoming_gap,
                        }
                        continue
                    existing_last = str(existing.get("last_at") or "")
                    # 主循环按时间顺序合并累计摘要和新间隔；记录跨批次
                    # 的间隔，避免边界处长时间停摆被隐藏。
                    cross_gap = 0.0
                    if existing_last and incoming_first and incoming_first >= existing_last:
                        previous_epoch = _timestamp_epoch(existing_last)
                        current_epoch = _timestamp_epoch(incoming_first)
                        if previous_epoch is not None and current_epoch is not None:
                            cross_gap = max(0.0, current_epoch - previous_epoch)
                    existing["count"] = int(existing.get("count", 0) or 0) + incoming_count
                    if incoming_first:
                        existing["first_at"] = min(
                            str(existing.get("first_at") or incoming_first), incoming_first,
                        )
                    if incoming_last and incoming_last >= existing_last:
                        existing["last_at"] = incoming_last
                    existing["max_gap_seconds"] = max(
                        float(existing.get("max_gap_seconds", 0.0) or 0.0),
                        incoming_gap,
                        cross_gap,
                    )
        for key in ("latest_log_at",):
            value = str(summary.get(key) or "")
            if value >= str(merged_latest.get(key) or ""):
                merged_latest[key] = value
                merged_latest["latest_boot"] = str(summary.get("latest_boot") or "")
    return {
        "events": dict(events),
        "tools": dict(tools),
        **merged_lists,
        **{name: sorted(values) for name, values in merged_sets.items()},
        **merged_times,
        "background_by_logger": merged_background,
        "knowledge_retrieval": {
            **merged_knowledge_retrieval,
            "path_counts": dict(merged_knowledge_retrieval["path_counts"]),
            "match_type_counts": dict(
                merged_knowledge_retrieval["match_type_counts"]
            ),
        },
        "tts_repairs_by_boot": merged_tts_repairs,
        "gpt_exit_codes": dict(merged_gpt_exit_codes),
        "gpt_exit_lifetime_max_s": round(merged_gpt_exit_lifetime_max_s, 3),
        "gpt_exit_last": merged_gpt_exit_last,
        "boot_first_at": merged_boot_first_at,
        "events_by_boot": {
            boot_id: dict(counts)
            for boot_id, counts in merged_events_by_boot.items()
        },
        "latency_histograms": merged_histograms,
        "latency_max_ms": merged_latency_max,
        "latency_histogram_complete": latency_histogram_complete,
        "latest_log_at": merged_latest["latest_log_at"],
        "latest_boot": merged_latest["latest_boot"],
        "latency_samples_dropped": latency_samples_dropped,
        "correlation_tokens_dropped": correlation_tokens_dropped,
        "correlation_boundary_tolerant": correlation_boundary_tolerant,
        "boot_count": len(merged_sets["boot_ids"]),
    }


def _public_log_summary(summary: dict, *, compact: bool = False) -> dict:
    """落盘版本不含 raw cid/boot；配对 token 仅在观察器进程内存在。"""
    hidden = {
        "llm_started_tokens", "llm_completed_tokens",
        "voice_started_tokens", "voice_dispatched_tokens",
        "voice_completed_tokens", "boot_ids", "latest_boot", "boot_first_at",
        "recovery_inbound_tokens", "recovery_llm_completed_tokens",
        "recovery_send_confirmed_tokens", "tts_repairs_by_boot", "events_by_boot",
        "initial_boot_id", "initial_boot_at",
        "llm_started_at", "llm_completed_at", "voice_started_at",
        "voice_dispatched_at", "voice_completed_at",
    }
    if compact:
        hidden.update({
            "llm_latency_ms", "foreground_llm_latency_ms",
            "background_llm_latency_ms",
        })
    result = {
        key: value for key, value in summary.items() if key not in hidden
    }
    # 心跳 boot 只供进程内跨重启配对，不能写入脱敏 JSONL/报告。
    if isinstance(result.get("background_by_logger"), dict):
        result["background_by_logger"] = {
            str(logger_name): {
                key: value for key, value in record.items()
                if key not in {"last_heartbeat_boot", "heartbeat_boots"}
            }
            for logger_name, record in result["background_by_logger"].items()
            if isinstance(record, dict)
        }
    return result


def _port_open(port: int, timeout: float = 0.25) -> bool:
    """检查本机监听端口，不向应用端口发起副作用连接。

    反向 WS 端口不是 HTTP 探针；裸 TCP connect 会让 websockets 记录
    ``400 Bad Request``，污染被观察进程的日志。优先读取本机连接表，
    psutil 不可用或权限不足时才退回旧 socket 探针（兼容最小环境）。
    """
    try:
        import psutil

        target = int(port)
        for connection in psutil.net_connections(kind="tcp"):
            if str(getattr(connection, "status", "")).upper() != "LISTEN":
                continue
            address = getattr(connection, "laddr", None)
            local_port = getattr(address, "port", None)
            if local_port is None and isinstance(address, tuple) and len(address) >= 2:
                local_port = address[1]
            if local_port == target:
                return True
        return False
    except (ImportError, OSError, RuntimeError):
        # 精简运行环境可能没有 psutil，保留可工作的兼容回退。
        pass
    except Exception:
        # 连接表读取可能因 Windows 权限/瞬时进程退出失败；探针本身不能
        # 让观察器崩溃，回退到有界 socket 检查。
        pass
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _required_local_services(config_path: str | Path | None) -> dict[str, bool]:
    """从运行配置推导本地服务必需性，不把配置正文写入观察结果。"""
    required = {
        "napcat": True,
        "reverse_ws": True,
        "gpt_sovits": False,
    }
    if not config_path:
        return required
    try:
        import yaml

        config = yaml.safe_load(
            Path(config_path).read_text(encoding="utf-8")
        ) or {}
        voice = config.get("voice") or {}
        if bool(voice.get("enabled", True)):
            provider = str(voice.get("provider") or "").strip().lower()
            provider = provider.replace("_", "-")
            if provider in {"gpt-sovits", "gptsovits"}:
                required["gpt_sovits"] = True
    except Exception:
        # 配置不可读、YAML 损坏或精简环境缺少 PyYAML 时保持原有保守基线；
        # 观察器本身仍可报告 QQ/数据库状态，不能因辅助配置阻断取证。
        pass
    return required


def capture_snapshot(
    db_path: str | Path, *, now: datetime | None = None,
    after_memory_id: int | None = None,
    config_path: str | Path | None = PROJECT_ROOT / "config.yaml",
) -> dict:
    """只读生产数据库；快照不包含用户列表、消息正文或 outbox 正文。"""
    probe_started = time.perf_counter()
    captured = now or datetime.now().astimezone()
    required_services = _required_local_services(config_path)
    snapshot = {
        "captured_at": captured.isoformat(timespec="seconds"),
        "database": {"ok": False},
        "services": {
            "napcat": {"required": required_services["napcat"], "up": _port_open(3000)},
            "reverse_ws": {"required": required_services["reverse_ws"], "up": _port_open(3001)},
            "gpt_sovits": {"required": required_services["gpt_sovits"], "up": _port_open(9880)},
        },
        "errors": [],
    }
    try:
        store = ReadOnlyStore(str(db_path))
        snapshot["database"] = store.get_runtime_observer_snapshot(
            captured, after_memory_id=after_memory_id,
        )
    except Exception as exc:
        snapshot["errors"].append(f"database:{type(exc).__name__}:{exc}")
    snapshot["observer"] = {
        "probe_duration_ms": round(
            (time.perf_counter() - probe_started) * 1000, 1
        ),
    }
    return snapshot


def _domain(status: str, summary: str, evidence: dict | None = None) -> dict:
    return {"status": status, "summary": summary, "evidence": evidence or {}}


def _counter_integrity(snapshots: list[dict]) -> dict:
    """检查跨快照计数是否回退；回退通常意味着 DB 恢复/分区丢失。"""
    regressions: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    names = tuple(ReadOnlyStore._RUNTIME_METRIC_NAMES)
    for before, after in zip(snapshots, snapshots[1:]):
        before_db = before.get("database", {})
        after_db = after.get("database", {})
        if not before_db.get("ok") or not after_db.get("ok"):
            continue
        before_total = before_db.get("metrics_total")
        after_total = after_db.get("metrics_total")
        if isinstance(before_total, dict) and isinstance(after_total, dict):
            # 只有新契约明确声明完整指标表时，缺键才算观察器/数据契约损坏。
            # 旧版或手工构造的快照可能只含少数键，仍可对共有键做回退检查。
            complete = bool(
                before_db.get("metrics_total_complete")
                or after_db.get("metrics_total_complete")
            )
            for name in names:
                if name not in before_total or name not in after_total:
                    if complete:
                        missing[name] += 1
                else:
                    try:
                        if int(after_total[name]) < int(before_total[name]):
                            regressions[name] += 1
                    except (TypeError, ValueError):
                        missing[name] += 1
            continue
        # 旧快照只有日分区计数时，跨日重置是合法的；同一天回退才是异常。
        before_day = str(before.get("captured_at", ""))[:10]
        after_day = str(after.get("captured_at", ""))[:10]
        if before_day != after_day:
            continue
        before_metrics = before_db.get("metrics") or {}
        after_metrics = after_db.get("metrics") or {}
        for name in names:
            if name in before_metrics and name in after_metrics:
                try:
                    if int(after_metrics[name]) < int(before_metrics[name]):
                        regressions[name] += 1
                except (TypeError, ValueError):
                    missing[name] += 1
    return {
        "regressions": dict(sorted(regressions.items())),
        "missing": dict(sorted(missing.items())),
    }


def _series_regressions(snapshots: list[dict], path: tuple[str, ...]) -> int:
    """统计只增计数在相邻快照间回退的次数；缺字段不作回退判断。"""
    regressions = 0
    for before, after in zip(snapshots, snapshots[1:]):
        before_value: object = before
        after_value: object = after
        try:
            for key in path:
                before_value = before_value[key]  # type: ignore[index]
                after_value = after_value[key]  # type: ignore[index]
            if int(after_value) < int(before_value):
                regressions += 1
        except (KeyError, TypeError, ValueError):
            continue
    return regressions


def _timestamp_epoch(value: object) -> float | None:
    try:
        normalized = str(value).replace(" ", "T", 1)
        # logging 的 ``%z`` 在 Python 3.10 输出 ``+0800``，而
        # fromisoformat 对该版本的偏移解析要求 ``+08:00``。
        offset = re.search(r"([+-])(\d{2})(\d{2})$", normalized)
        if offset:
            normalized = (
                normalized[:offset.start()]
                + f"{offset.group(1)}{offset.group(2)}:{offset.group(3)}"
            )
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _metric_delta(before: dict, after: dict, name: str) -> int:
    first_db = before.get("database", {})
    last_db = after.get("database", {})
    first_metrics = first_db.get("metrics_total") or first_db.get("metrics", {})
    last_metrics = last_db.get("metrics_total") or last_db.get("metrics", {})
    first = int(first_metrics.get(name, 0) or 0)
    last = int(last_metrics.get(name, 0) or 0)
    return max(0, last - first)


def _memory_counter_delta(snapshots: list[dict], name: str) -> int:
    """累计派生的日计数；跨 SQLite 日期分区时把新分区值计入。"""
    incremental_name = (
        "new_reasoning_leaks" if name == "reasoning_leaks_today"
        else "new_duplicate_rows" if name == "duplicate_groups_today"
        else ""
    )
    if incremental_name and all(
        incremental_name in item.get("database", {}).get("memory", {})
        for item in snapshots
    ):
        return sum(
            int(item.get("database", {}).get("memory", {}).get(incremental_name, 0) or 0)
            for item in snapshots[1:]
        )
    total_name = name.replace("_today", "_total")
    if all(
        total_name in item.get("database", {}).get("memory", {})
        for item in snapshots
    ):
        total = 0
        for previous, current in zip(snapshots, snapshots[1:]):
            first = int(previous["database"]["memory"].get(total_name, 0) or 0)
            last = int(current["database"]["memory"].get(total_name, 0) or 0)
            total += max(0, last - first)
        return total
    total = 0
    for previous, current in zip(snapshots, snapshots[1:]):
        previous_memory = previous.get("database", {}).get("memory", {})
        current_memory = current.get("database", {}).get("memory", {})
        first = int(previous_memory.get(name, 0) or 0)
        last = int(current_memory.get(name, 0) or 0)
        first_day = previous_memory.get("counter_day") or str(
            previous.get("captured_at", "")
        )[:10]
        last_day = current_memory.get("counter_day") or str(
            current.get("captured_at", "")
        )[:10]
        total += max(0, last if first_day != last_day else last - first)
    return total


def evaluate_observation(snapshots: list[dict], log_summary: dict) -> dict:
    """纯函数验收：任一 FAIL 压过样本不足；只有所有已要求域 PASS 才总 PASS。"""
    events = Counter(log_summary.get("events") or {})
    tools = Counter(log_summary.get("tools") or {})
    latency = [float(value) for value in log_summary.get("llm_latency_ms") or []]
    foreground_latency = [
        float(value) for value in log_summary.get("foreground_llm_latency_ms") or []
    ]
    background_latency = [
        float(value) for value in log_summary.get("background_llm_latency_ms") or []
    ]
    latency_histograms = log_summary.get("latency_histograms") or {}
    latency_maxima = log_summary.get("latency_max_ms") or {}
    histogram_complete = bool(log_summary.get("latency_histogram_complete"))

    def histogram_counts(name: str) -> list[int] | None:
        nonlocal histogram_complete
        raw = latency_histograms.get(name) if isinstance(latency_histograms, dict) else None
        if not isinstance(raw, (list, tuple)) or len(raw) != len(LATENCY_BUCKET_UPPER_MS):
            histogram_complete = False
            return None
        try:
            counts = [int(value) for value in raw]
        except (TypeError, ValueError):
            histogram_complete = False
            return None
        if any(value < 0 for value in counts):
            histogram_complete = False
            return None
        return counts

    all_histogram = histogram_counts("all") if histogram_complete else None
    foreground_histogram = (
        histogram_counts("foreground") if histogram_complete else None
    )
    background_histogram = (
        histogram_counts("background") if histogram_complete else None
    )
    histogram_complete = bool(
        histogram_complete
        and all_histogram is not None
        and foreground_histogram is not None
        and background_histogram is not None
    )

    def histogram_max(name: str, fallback: list[float]) -> float:
        if histogram_complete:
            try:
                return round(float(latency_maxima.get(name, 0.0) or 0.0), 1)
            except (AttributeError, TypeError, ValueError):
                pass
        return round(max(fallback), 1) if fallback else 0.0

    all_samples = sum(all_histogram) if histogram_complete else len(latency)
    foreground_samples = (
        sum(foreground_histogram) if histogram_complete else len(foreground_latency)
    )
    background_samples = (
        sum(background_histogram) if histogram_complete else len(background_latency)
    )
    p50_ms = (
        _histogram_percentile(all_histogram, 0.50, histogram_max("all", latency))
        if histogram_complete else round(statistics.median(latency), 1) if latency else 0.0
    )
    p95_ms = (
        _histogram_percentile(all_histogram, 0.95, histogram_max("all", latency))
        if histogram_complete else _percentile(latency, 0.95)
    )
    foreground_p95_ms = (
        _histogram_percentile(
            foreground_histogram, 0.95, histogram_max("foreground", foreground_latency),
        )
        if histogram_complete else _percentile(foreground_latency, 0.95)
    )
    background_p95_ms = (
        _histogram_percentile(
            background_histogram, 0.95, histogram_max("background", background_latency),
        )
        if histogram_complete else _percentile(background_latency, 0.95)
    )
    domains: dict[str, dict] = {}

    if not snapshots:
        return {
            "overall": FAIL,
            "domains": {},
            "errors": ["无任何快照，观察器无法验收"],
        }
    before, after = snapshots[0], snapshots[-1]
    observation_start_epoch = _timestamp_epoch(before.get("captured_at"))
    observation_end_epoch = _timestamp_epoch(after.get("captured_at"))
    probe_errors = [error for item in snapshots for error in item.get("errors", [])]
    counter_integrity = _counter_integrity(snapshots)
    counter_regressions = counter_integrity["regressions"]
    counter_missing = counter_integrity["missing"]
    counter_integrity_failed = bool(counter_regressions or counter_missing)
    chat_log_id_regressions = _series_regressions(
        snapshots, ("database", "chat_id_high_water"),
    )
    metric_integrity = {
        "invalid_values": sum(
            int(item.get("database", {}).get("metric_integrity", {}).get("invalid_values", 0) or 0)
            for item in snapshots
        ),
        "negative_values": sum(
            int(item.get("database", {}).get("metric_integrity", {}).get("negative_values", 0) or 0)
            for item in snapshots
        ),
    }
    metric_integrity_failed = any(metric_integrity.values())

    required_services = sorted({
        name
        for item in snapshots
        for name, state in item.get("services", {}).items()
        if state.get("required")
    })
    required_down_samples = {
        name: sum(
            1 for item in snapshots
            if not item.get("services", {}).get(name, {}).get("up")
        )
        for name in required_services
    }
    gateway_service_names = frozenset(("napcat", "reverse_ws"))
    gateway_down_samples = {
        name: count for name, count in required_down_samples.items()
        if name in gateway_service_names
    }
    gateway_down = [name for name, count in gateway_down_samples.items() if count]
    local_required_services = [
        name for name in required_services if name not in gateway_service_names
    ]
    local_down_samples = {
        name: required_down_samples[name] for name in local_required_services
    }
    local_down = [name for name, count in local_down_samples.items() if count]
    before_inbox = before.get("database", {}).get("inbox", {})
    after_inbox = after.get("database", {}).get("inbox", {})
    inbox_new_failed = max(
        0, int(after_inbox.get("failed", 0) or 0)
        - int(before_inbox.get("failed", 0) or 0),
    )
    inbox_new_uncertain = max(
        0, int(after_inbox.get("uncertain", 0) or 0)
        - int(before_inbox.get("uncertain", 0) or 0),
    )
    inbox_new_unknown = max(
        0, int(after_inbox.get("unknown", 0) or 0)
        - int(before_inbox.get("unknown", 0) or 0),
    )
    inbox_processed_regressed = (
        int(after_inbox.get("processed", 0) or 0)
        < int(before_inbox.get("processed", 0) or 0)
    )
    dropped = sum(
        _metric_delta(before, after, name) for name in (
            "gateway_events_dropped_global", "gateway_events_dropped_scopes",
            "gateway_events_dropped_scope_queue", "gateway_callback_errors",
            "gateway_inbox_errors",
        )
    )
    gateway_evidence = {
        "required_down": gateway_down,
        "down_samples": gateway_down_samples,
        "dropped_or_callback_errors": dropped,
        "inbound_private": events["inbound_private"],
        "inbound_group": events["inbound_group"],
        "inbox_processed": int(after_inbox.get("processed", 0) or 0),
        "inbox_open": int(after_inbox.get("open", 0) or 0),
        "inbox_new_failed": inbox_new_failed,
        "inbox_new_uncertain": inbox_new_uncertain,
        "inbox_new_unknown": inbox_new_unknown,
        "inbox_failed_total": int(after_inbox.get("failed", 0) or 0),
        "inbox_uncertain_total": int(after_inbox.get("uncertain", 0) or 0),
        "inbox_unknown": int(after_inbox.get("unknown", 0) or 0),
        "inbox_processed_regressed": inbox_processed_regressed,
        "persistent_duplicates": _metric_delta(
            before, after, "gateway_events_deduped_persistent",
        ),
        "counter_regressions": counter_regressions,
        "counter_missing": counter_missing,
    }
    gateway_counter_names = {
        "gateway_events_dropped_global", "gateway_events_dropped_scopes",
        "gateway_events_dropped_scope_queue", "gateway_callback_errors",
        "gateway_inbox_errors",
    }
    gateway_counter_failed = any(
        name in gateway_counter_names
        for name in (*counter_regressions.keys(), *counter_missing.keys())
    )
    inbox_failed = bool(
        gateway_evidence["inbox_new_failed"]
        or gateway_evidence["inbox_new_uncertain"]
        or gateway_evidence["inbox_new_unknown"]
        or inbox_processed_regressed
    )
    if gateway_down or dropped or gateway_counter_failed or inbox_failed:
        gateway_status = FAIL
    elif not gateway_evidence["inbound_private"] or not gateway_evidence["inbound_group"]:
        gateway_status = INSUFFICIENT
    else:
        gateway_status = PASS
    domains["qq_gateway"] = _domain(gateway_status, {
        PASS: "私聊和群聊入口均有真实入站样本",
        FAIL: "QQ网关不可用、计数回退或入站事件存在失败/不确定状态",
        INSUFFICIENT: "端口在线，但私聊/群聊真实入站样本不完整",
    }[gateway_status], gateway_evidence)

    local_service_evidence = {
        "required": local_required_services,
        "required_down": local_down,
        "down_samples": local_down_samples,
        "gpt_ready_child_exits": int(events["gpt_ready_child_exits"]),
        "gpt_exit_codes": dict(log_summary.get("gpt_exit_codes") or {}),
        "gpt_exit_lifetime_max_s": float(
            log_summary.get("gpt_exit_lifetime_max_s", 0.0) or 0.0
        ),
        "gpt_exit_last": dict(log_summary.get("gpt_exit_last") or {}),
    }
    local_service_status = FAIL if local_down else PASS
    domains["local_services"] = _domain(
        local_service_status,
        "配置的本地依赖均在线" if local_service_status == PASS
        else "配置的本地依赖不可用",
        local_service_evidence,
    )

    send_evidence = {
        "attempts": _metric_delta(before, after, "gateway_send_attempts"),
        "confirmed": _metric_delta(before, after, "gateway_send_confirmed"),
        "uncertain": _metric_delta(before, after, "gateway_send_uncertain"),
        "failed": _metric_delta(before, after, "gateway_send_failed"),
        "log_confirmed": events["send_confirmed_log"],
        "log_uncertain": events["send_uncertain_log"],
        "log_failed": events["send_failed_log"],
        "outbox_open": int(after.get("database", {}).get("outbox", {}).get("open", 0) or 0),
        "outbox_uncertain": int(after.get("database", {}).get("outbox", {}).get("uncertain", 0) or 0),
        "outbox_dead": int(after.get("database", {}).get("outbox", {}).get("dead", 0) or 0),
        "outbox_confirmed_unaccounted": int(
            after.get("database", {}).get("outbox", {}).get(
                "confirmed_unaccounted", 0,
            ) or 0
        ),
        "outbox_confirmed_conflict": int(
            after.get("database", {}).get("outbox", {}).get(
                "confirmed_conflict", 0,
            ) or 0
        ),
        "outbox_unknown": int(after.get("database", {}).get("outbox", {}).get("unknown", 0) or 0),
        "outbox_stale_sending": int(
            after.get("database", {}).get("outbox", {}).get("stale_sending", 0) or 0
        ),
        "linked_invariant_violations": int(
            after.get("database", {}).get("outbox", {}).get(
                "linked_invariant_violations", 0,
            ) or 0
        ),
        "bare_sending_tasks": int(
            after.get("database", {}).get("outbox", {}).get(
                "bare_sending_tasks", 0,
            ) or 0
        ),
        "persistent_bare_sending_tasks": min(
            int(before.get("database", {}).get("outbox", {}).get(
                "bare_sending_tasks", 0,
            ) or 0),
            int(after.get("database", {}).get("outbox", {}).get(
                "bare_sending_tasks", 0,
            ) or 0),
        ),
        "task_action_schema_missing": bool(
            after.get("database", {}).get("outbox", {}).get(
                "task_action_schema_missing", False,
            )
        ),
        "counter_regressions": {
            name: count for name, count in counter_regressions.items()
            if name.startswith("gateway_send_")
        },
        "counter_missing": {
            name: count for name, count in counter_missing.items()
            if name.startswith("gateway_send_")
        },
    }
    send_counter_failed = bool(
        send_evidence["counter_regressions"] or send_evidence["counter_missing"]
    )
    if (
        send_evidence["uncertain"]
        or send_evidence["failed"]
        or send_evidence["log_uncertain"]
        or send_evidence["log_failed"]
        or send_evidence["outbox_open"]
        or send_evidence["outbox_uncertain"]
        or send_evidence["outbox_dead"]
        or send_evidence["outbox_confirmed_unaccounted"]
        or send_evidence["outbox_confirmed_conflict"]
        or send_evidence["outbox_unknown"]
        or send_evidence["outbox_stale_sending"]
        or send_evidence["linked_invariant_violations"]
        or send_evidence["persistent_bare_sending_tasks"]
        or send_evidence["task_action_schema_missing"]
        or send_counter_failed
    ):
        send_status = FAIL
    elif send_evidence["attempts"] == 0 and send_evidence["log_confirmed"] == 0:
        send_status = INSUFFICIENT
    elif send_evidence["attempts"] and send_evidence["confirmed"] != send_evidence["attempts"]:
        send_status = FAIL
    else:
        send_status = PASS
    domains["sending"] = _domain(send_status, {
        PASS: "本区间发送均确认送达",
        FAIL: "存在未确认/失败/outbox待处理发送",
        INSUFFICIENT: "本区间没有发送样本",
    }[send_status], send_evidence)

    if len(snapshots) < 2 or not before.get("database", {}).get("ok") or not after.get("database", {}).get("ok"):
        domains["memory"] = _domain(INSUFFICIENT, "记忆容量需要至少两个可读快照")
    else:
        first_backlog = before["database"]["backlog"]
        last_backlog = after["database"]["backlog"]
        first_memory = before["database"].get("memory", {})
        last_memory = after["database"].get("memory", {})
        max_memory_id_before = int(first_memory.get("max_id", 0) or 0)
        max_memory_id_after = int(last_memory.get("max_id", 0) or 0)
        id_high_water_before = first_memory.get("id_high_water")
        id_high_water_after = last_memory.get("id_high_water")
        try:
            memory_id_regression = (
                id_high_water_before is not None
                and id_high_water_after is not None
                and int(id_high_water_after) < int(id_high_water_before)
            )
        except (TypeError, ValueError):
            memory_id_regression = True
        total_messages_regressions = _series_regressions(
            snapshots, ("database", "backlog", "total_user_messages"),
        )
        elapsed = max(
            1.0,
            (datetime.fromisoformat(after["captured_at"]) - datetime.fromisoformat(before["captured_at"])).total_seconds(),
        )
        arrivals = max(0, int(last_backlog["total_user_messages"]) - int(first_backlog["total_user_messages"]))
        serviced = max(0, int(first_backlog["backlog_messages"]) + arrivals - int(last_backlog["backlog_messages"]))
        policy_aware_backlog = (
            "eligible_messages" in first_backlog
            and "eligible_messages" in last_backlog
        )
        eligible_before = int(first_backlog.get(
            "eligible_messages", first_backlog["backlog_messages"]
        ) or 0)
        eligible_after = int(last_backlog.get(
            "eligible_messages", last_backlog["backlog_messages"]
        ) or 0)
        memory_evidence = {
            "backlog_before": int(first_backlog["backlog_messages"]),
            "backlog_after": int(last_backlog["backlog_messages"]),
            "oldest_before": first_backlog.get("oldest_at", ""),
            "oldest_after": last_backlog.get("oldest_at", ""),
            "eligible_backlog_before": eligible_before,
            "eligible_backlog_after": eligible_after,
            "eligible_oldest_before": first_backlog.get("eligible_oldest_at", ""),
            "eligible_oldest_after": last_backlog.get("eligible_oldest_at", ""),
            "deferred_backlog_before": int(first_backlog.get(
                "deferred_messages", 0 if not policy_aware_backlog else
                max(0, int(first_backlog["backlog_messages"]) - eligible_before),
            ) or 0),
            "deferred_backlog_after": int(last_backlog.get(
                "deferred_messages", 0 if not policy_aware_backlog else
                max(0, int(last_backlog["backlog_messages"]) - eligible_after),
            ) or 0),
            "arrivals": arrivals,
            "serviced": serviced,
            "arrival_rate_per_hour": round(arrivals * 3600 / elapsed, 2),
            "service_rate_per_hour": round(serviced * 3600 / elapsed, 2),
            "queue_dead": int(last_backlog.get("dead", after["database"].get("queue", {}).get("dead", 0)) or 0),
            "queue_unknown": int(after["database"].get("queue", {}).get("unknown", 0) or 0),
            "reasoning_leaks_at_start": int(before["database"].get("memory", {}).get("reasoning_leaks_today", 0) or 0),
            "reasoning_leaks_at_end": int(after["database"].get("memory", {}).get("reasoning_leaks_today", 0) or 0),
            "duplicate_groups_at_start": int(before["database"].get("memory", {}).get("duplicate_groups_today", 0) or 0),
            "duplicate_groups_at_end": int(after["database"].get("memory", {}).get("duplicate_groups_today", 0) or 0),
            "trusted_memories": int(after["database"].get("memory", {}).get("trusted", 0) or 0),
            "untrusted_active_memories": max(
                0,
                int(after["database"].get("memory", {}).get("active", 0) or 0)
                - int(after["database"].get("memory", {}).get("trusted", 0) or 0),
            ),
            "unknown_trust_levels": int(
                after["database"].get("memory", {}).get("trust_levels", {}).get("unknown", 0) or 0
            ),
            "max_memory_id_before": max_memory_id_before,
            "max_memory_id_after": max_memory_id_after,
            "id_high_water_before": id_high_water_before,
            "id_high_water_after": id_high_water_after,
            "memory_id_regression": memory_id_regression,
            "chat_log_id_regressions": chat_log_id_regressions,
            "total_messages_regressions": total_messages_regressions,
            "observed_seconds": round(elapsed, 1),
        }
        # 记忆债务的聊天游标只能说明“消息变少了”，不能证明持久提取
        # job 真正走完了 LLM→ready→done。把允许名单中的计数做区间
        # 增量，作为独立证据；不依赖高基数日志，也不改变旧容量判定口径。
        lifecycle = {
            "jobs_created": _metric_delta(before, after, "extract_jobs_created"),
            "jobs_admitted": _metric_delta(before, after, "extract_jobs_admitted"),
            "jobs_deferred": _metric_delta(before, after, "extract_jobs_deferred"),
            "lease_acquired": _metric_delta(before, after, "extract_jobs_lease_acquired"),
            "lease_missed": _metric_delta(before, after, "extract_jobs_lease_missed"),
            "jobs_ready": _metric_delta(before, after, "extract_jobs_ready"),
            "jobs_completed": _metric_delta(before, after, "extract_jobs_completed"),
            "jobs_failed": _metric_delta(before, after, "extract_jobs_failed"),
            "jobs_dead": _metric_delta(before, after, "extract_jobs_dead"),
            "jobs_requeued": _metric_delta(before, after, "extract_jobs_requeued"),
            "missing_messages": _metric_delta(
                before, after, "extract_jobs_missing_messages"
            ),
            "llm_started": _metric_delta(before, after, "extract_job_llm_started"),
            "llm_succeeded": _metric_delta(before, after, "extract_job_llm_succeeded"),
            "llm_failed": _metric_delta(before, after, "extract_job_llm_failed"),
            "attempts": _metric_delta(before, after, "extract_attempts"),
            "outcomes": _metric_delta(before, after, "extract_outcomes_total"),
            "memories": _metric_delta(before, after, "extract_memories_total"),
        }
        lifecycle_warnings = []
        if lifecycle["llm_succeeded"] + lifecycle["llm_failed"] > lifecycle["llm_started"]:
            lifecycle_warnings.append("llm_terminal_exceeds_started")
        if lifecycle["attempts"] != lifecycle["llm_started"]:
            lifecycle_warnings.append("attempts_mismatch_llm_started")
        if lifecycle["missing_messages"] > lifecycle["jobs_dead"]:
            lifecycle_warnings.append("missing_messages_not_quarantined")
        lifecycle["warnings"] = lifecycle_warnings
        lifecycle["observed"] = any(
            lifecycle[name] > 0 for name in lifecycle if name != "warnings"
        )
        memory_evidence["extraction_lifecycle"] = lifecycle
        memory_evidence["new_reasoning_leaks"] = _memory_counter_delta(
            snapshots, "reasoning_leaks_today",
        )
        memory_evidence["new_duplicate_groups"] = _memory_counter_delta(
            snapshots, "duplicate_groups_today",
        )
        hygiene_fail = (
            memory_evidence["queue_dead"] > 0
            or memory_evidence["queue_unknown"] > 0
            or memory_evidence["unknown_trust_levels"] > 0
            or memory_evidence["new_reasoning_leaks"] > 0
            or memory_evidence["new_duplicate_groups"] > 0
            or memory_evidence["memory_id_regression"]
            or memory_evidence["chat_log_id_regressions"]
            or memory_evidence["total_messages_regressions"]
            or bool(memory_evidence["extraction_lifecycle"]["warnings"])
        )
        if hygiene_fail:
            memory_status = FAIL
        elif elapsed < MEMORY_TREND_MIN_SECONDS:
            memory_status = INSUFFICIENT
        elif policy_aware_backlog:
            if eligible_after == 0:
                memory_status = PASS if eligible_before > 0 else INSUFFICIENT
            else:
                eligible_oldest_advanced = (
                    bool(memory_evidence["eligible_oldest_before"])
                    and bool(memory_evidence["eligible_oldest_after"])
                    and memory_evidence["eligible_oldest_after"]
                    > memory_evidence["eligible_oldest_before"]
                )
                memory_status = (
                    PASS
                    if eligible_after < eligible_before and eligible_oldest_advanced
                    else FAIL
                )
        elif memory_evidence["backlog_after"] == 0:
            # 空积压只能说明当前没有债务；若区间内没有到达或消费，不能
            # 据此证明提取链真的运行过。
            memory_status = (
                PASS if memory_evidence["arrivals"] or memory_evidence["serviced"]
                else INSUFFICIENT
            )
        else:
            oldest_advanced = (
                bool(memory_evidence["oldest_before"])
                and bool(memory_evidence["oldest_after"])
                and memory_evidence["oldest_after"] > memory_evidence["oldest_before"]
            )
            capacity_ok = (
                memory_evidence["service_rate_per_hour"]
                > memory_evidence["arrival_rate_per_hour"]
                and memory_evidence["backlog_after"]
                < memory_evidence["backlog_before"]
                and oldest_advanced
            )
            memory_status = PASS if capacity_ok else FAIL
        domains["memory"] = _domain(
            memory_status,
            "到期记忆债务、最老积压或真值卫生未达标" if memory_status == FAIL else (
                "观察期过短或没有真实记忆工作量" if memory_status == INSUFFICIENT
                else "到期记忆债务下降且真值卫生未见失败"
            ),
            memory_evidence,
        )

    llm_evidence = {
        "started": events["llm_started"],
        "completed": events["llm_turn_completed"],
        "stream_completions": events["llm_completed"],
        "failed": events["llm_failed"],
        "rate_limited": events["llm_429"],
        "server_errors": events["llm_5xx"],
        "transport_errors": events["llm_429"] + events["llm_5xx"],
        "transport_error_budget": LLM_TRANSPORT_ERROR_BUDGET,
        "samples": all_samples,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "max_ms": histogram_max("all", latency),
        "foreground_samples": foreground_samples,
        "foreground_p95_ms": foreground_p95_ms,
        "background_samples": background_samples,
        "background_p95_ms": background_p95_ms,
        "foreground_p95_budget_ms": FOREGROUND_LLM_P95_MAX_MS,
        "background_p95_budget_ms": BACKGROUND_LLM_P95_MAX_MS,
        "latency_histogram_complete": histogram_complete,
        "latency_samples_dropped": int(log_summary.get("latency_samples_dropped", 0) or 0),
        "correlation_tokens_dropped": int(
            log_summary.get("correlation_tokens_dropped", 0) or 0
        ),
    }
    started_tokens = set(log_summary.get("llm_started_tokens") or [])
    completed_tokens = set(log_summary.get("llm_completed_tokens") or [])
    started_at = log_summary.get("llm_started_at") or {}
    completed_at = log_summary.get("llm_completed_at") or {}
    unfinished_tokens = started_tokens - completed_tokens
    unmatched_tokens = completed_tokens - started_tokens
    recent_unfinished = 0
    stale_unfinished = 0
    for token in unfinished_tokens:
        started_epoch = _timestamp_epoch(
            started_at.get(token) if isinstance(started_at, dict) else None
        )
        if (
            started_epoch is not None
            and observation_end_epoch is not None
            and 0 <= observation_end_epoch - started_epoch
            <= CORRELATION_SETTLE_GRACE_SECONDS
        ):
            recent_unfinished += 1
        else:
            stale_unfinished += 1
    boundary_unmatched = 0
    if log_summary.get("correlation_boundary_tolerant") and observation_start_epoch is not None:
        for token in unmatched_tokens:
            completed_epoch = _timestamp_epoch(
                completed_at.get(token) if isinstance(completed_at, dict) else None
            )
            if (
                completed_epoch is not None
                and completed_epoch <= observation_start_epoch + CORRELATION_BOUNDARY_GRACE_SECONDS
            ):
                boundary_unmatched += 1
    llm_evidence["unfinished_foreground"] = stale_unfinished
    llm_evidence["unfinished_recent"] = recent_unfinished
    llm_evidence["unmatched_completion"] = len(unmatched_tokens) - boundary_unmatched
    llm_evidence["boundary_unmatched_completion"] = boundary_unmatched
    llm_evidence["completion_without_latency"] = bool(
        llm_evidence["completed"] and llm_evidence["samples"] == 0
    )
    latency_slo_failed = (
        foreground_samples > 0
        and llm_evidence["foreground_p95_ms"] > FOREGROUND_LLM_P95_MAX_MS
    ) or (
        background_samples > 0
        and llm_evidence["background_p95_ms"] > BACKGROUND_LLM_P95_MAX_MS
    )
    if (
        llm_evidence["failed"]
        or llm_evidence["unfinished_foreground"]
        or llm_evidence["unmatched_completion"]
        or llm_evidence["transport_errors"] > LLM_TRANSPORT_ERROR_BUDGET
        or latency_slo_failed
    ):
        llm_status = FAIL
    elif (
        llm_evidence["latency_samples_dropped"] and not histogram_complete
        or llm_evidence["correlation_tokens_dropped"]
        or llm_evidence["completion_without_latency"]
        or llm_evidence["unfinished_recent"]
        or llm_evidence["boundary_unmatched_completion"]
    ):
        # 有完整直方图时，原始延迟列表截断不影响全区间 p95；配对 token
        # 被截断仍会破坏“调用均收尾”的证据，因此必须保持样本不足。
        llm_status = INSUFFICIENT
    elif llm_evidence["completed"] == 0:
        llm_status = INSUFFICIENT
    else:
        llm_status = PASS
    domains["llm"] = _domain(llm_status, {
        PASS: "LLM在本区间有完整返回",
        FAIL: "LLM有失败或未收尾调用",
        INSUFFICIENT: "LLM延迟样本被截断或本区间没有完整对话样本",
    }[llm_status], llm_evidence)

    tool_evidence = {
        "calls": sum(tools.values()),
        "by_name": dict(sorted(tools.items())),
        "invalid_arguments": events["tool_invalid_arguments"],
    }
    tool_status = (
        FAIL if tool_evidence["invalid_arguments"]
        else PASS if tool_evidence["calls"]
        else INSUFFICIENT
    )
    domains["llm_tools"] = _domain(
        tool_status,
        "Native Tool Calling 有真实执行样本" if tool_status == PASS else (
            "LLM 生成了无效工具参数" if tool_status == FAIL
            else "本区间没有工具调用样本"
        ),
        tool_evidence,
    )

    window_metric_names = (
        "window_candidate", "window_entry_direct", "window_continuation",
        "window_routed", "window_decisions_total", "window_decisions_reply",
        "window_decisions_skip", "window_fade_after_silence",
    )
    window_metrics = {
        name: _metric_delta(before, after, name) for name in window_metric_names
    }
    candidate = window_metrics["window_candidate"]
    entry_direct = window_metrics["window_entry_direct"]
    continuation = window_metrics["window_continuation"]
    decisions_total = window_metrics["window_decisions_total"]
    decisions_reply = window_metrics["window_decisions_reply"]
    decisions_skip = window_metrics["window_decisions_skip"]
    logged_reply = events["window_reply"]
    logged_skip = events["window_skip"]
    logged_total = logged_reply + logged_skip
    violations: list[str] = []
    if decisions_total > candidate:
        violations.append("decisions_exceed_candidates")
    if candidate != entry_direct + continuation:
        violations.append("candidate_entry_breakdown_mismatch")
    if decisions_total != decisions_reply + decisions_skip:
        violations.append("decision_outcome_breakdown_mismatch")
    # 指标最多每 5 分钟落盘，而日志实时写入。观察结束边界可能已经看到
    # reply/skip 日志、尚未看到对应计数 flush；这是证据未收齐，不是假故障，
    # 但也不能把不完整漏斗判成 PASS。
    counter_lag = max(0, logged_total - decisions_total)
    dialog_evidence = {
        "candidate": candidate,
        "entry_direct": entry_direct,
        "continuation": continuation,
        "routed": window_metrics["window_routed"],
        "decisions_total": decisions_total,
        "reply": decisions_reply,
        "skip": decisions_skip,
        "fade": window_metrics["window_fade_after_silence"],
        "logged_reply": logged_reply,
        "logged_skip": logged_skip,
        "logged_fade": events["window_fade"],
        "counter_lag": counter_lag,
        "violations": violations,
    }
    after_window = (after.get("database", {}).get("window", {})
                    if isinstance(after, dict) else {})
    durable_invalid = int(after_window.get("invalid_events", 0) or 0)
    durable_future = int(after_window.get("future_events", 0) or 0)
    dialog_evidence["durable_window"] = {
        "event_count": int(after_window.get("event_count", 0) or 0),
        "max_id": int(after_window.get("max_id", 0) or 0),
        "invalid_events": durable_invalid,
        "future_events": durable_future,
        "schema_missing": bool(after_window.get("schema_missing", False)),
    }
    if durable_invalid or durable_future or after_window.get("schema_missing"):
        violations.append("durable_window_integrity_failure")
    if violations:
        dialog_status = FAIL
        dialog_summary = "窗口决策漏斗计数不一致"
    elif counter_lag:
        dialog_status = INSUFFICIENT
        dialog_summary = "窗口日志已出现，但持久计数尚未收齐"
    elif decisions_total:
        dialog_status = PASS
        dialog_summary = "窗口候选、入口和自主决策漏斗有完整样本；自然度仍需累计"
    else:
        dialog_status = INSUFFICIENT
        dialog_summary = "本区间无完整窗口自主决策样本"
    domains["dialog"] = _domain(
        dialog_status,
        dialog_summary,
        dialog_evidence,
    )

    # 主动事件与 LLM 决策的持久边界：只读快照已有这两组数据，但此前
    # 报告没有单独展示，人工验收无法区分“没有样本”和“引用损坏”。
    # 只计算观察区间新增量，避免历史累计失败污染当前判断。
    def _section_count(snapshot: dict, section: str, key: str) -> int | None:
        value = snapshot.get("database", {}).get(section)
        if not isinstance(value, dict):
            return None
        try:
            return max(0, int(value.get(key, 0) or 0))
        except (TypeError, ValueError):
            return None

    def _section_delta(section: str, key: str) -> int:
        first = _section_count(before, section, key)
        last = _section_count(after, section, key)
        return max(0, last - first) if first is not None and last is not None else 0

    decision_runs_delta = _section_delta("decision_runs", "total")
    proactive_decision_runs_delta = _section_delta(
        "proactive", "proactive_decision_runs",
    )
    proactive_events_delta = _section_delta("proactive", "total")
    proactive_bound_delta = _section_delta("proactive", "bound")
    decision_corrupt_delta = _section_delta("decision_runs", "corrupt")
    decision_unknown_delta = _section_delta("decision_runs", "unknown")
    proactive_uncertain_delta = _section_delta("proactive", "uncertain")
    proactive_unknown_delta = _section_delta("proactive", "unknown")
    proactive_invalid_delta = _section_delta("proactive", "invalid_payload")
    proactive_decision_corrupt_delta = _section_delta(
        "proactive", "proactive_decision_runs_corrupt",
    )
    proactive_decision_unknown_delta = _section_delta(
        "proactive", "proactive_decision_runs_unknown",
    )
    decision_schema_missing = bool(
        (before.get("database", {}).get("decision_runs") or {}).get("schema_missing")
        or (after.get("database", {}).get("decision_runs") or {}).get("schema_missing")
    )
    proactive_schema_missing = bool(
        (before.get("database", {}).get("proactive") or {}).get("schema_missing")
        or (after.get("database", {}).get("proactive") or {}).get("schema_missing")
    )
    proactive_decision_violations: list[str] = []
    if decision_schema_missing:
        proactive_decision_violations.append("decision_runs_schema_missing")
    if proactive_schema_missing:
        proactive_decision_violations.append("proactive_schema_missing")
    if proactive_decision_corrupt_delta:
        proactive_decision_violations.append("new_decision_run_corrupt")
    if proactive_decision_unknown_delta:
        proactive_decision_violations.append("new_decision_run_unknown")
    if proactive_uncertain_delta:
        proactive_decision_violations.append("new_proactive_uncertain")
    if proactive_unknown_delta:
        proactive_decision_violations.append("new_proactive_unknown")
    if proactive_invalid_delta:
        proactive_decision_violations.append("new_proactive_invalid_payload")
    proactive_decision_evidence = {
        "decision_runs_delta": decision_runs_delta,
        "proactive_decision_runs_delta": proactive_decision_runs_delta,
        "proactive_events_delta": proactive_events_delta,
        "proactive_bound_delta": proactive_bound_delta,
        "decision_corrupt_delta": decision_corrupt_delta,
        "decision_unknown_delta": decision_unknown_delta,
        "proactive_decision_corrupt_delta": proactive_decision_corrupt_delta,
        "proactive_decision_unknown_delta": proactive_decision_unknown_delta,
        "proactive_uncertain_delta": proactive_uncertain_delta,
        "proactive_unknown_delta": proactive_unknown_delta,
        "proactive_invalid_payload_delta": proactive_invalid_delta,
        "violations": proactive_decision_violations,
    }
    if proactive_decision_violations:
        proactive_decision_status = FAIL
        proactive_decision_summary = "主动事件/DecisionRun 有损坏或未确认状态"
    elif (proactive_events_delta and proactive_decision_runs_delta
          and proactive_bound_delta):
        proactive_decision_status = PASS
        proactive_decision_summary = "主动事件与已绑定终态 DecisionRun 均有新增样本"
    else:
        proactive_decision_status = INSUFFICIENT
        proactive_decision_summary = "本区间没有完整主动事件/DecisionRun 样本"
    domains["proactive_decisions"] = _domain(
        proactive_decision_status,
        proactive_decision_summary,
        proactive_decision_evidence,
    )

    voice_started_tokens = set(log_summary.get("voice_started_tokens") or [])
    voice_dispatched_tokens = set(log_summary.get("voice_dispatched_tokens") or [])
    voice_completed_tokens = set(log_summary.get("voice_completed_tokens") or [])
    voice_started_at = log_summary.get("voice_started_at") or {}
    voice_dispatched_at = log_summary.get("voice_dispatched_at") or {}
    voice_completed_at = log_summary.get("voice_completed_at") or {}
    voice_correlated_attempts = voice_started_tokens | voice_dispatched_tokens
    voice_unfinished_tokens = voice_correlated_attempts - voice_completed_tokens
    voice_recent_unfinished = 0
    voice_stale_unfinished = 0
    for token in voice_unfinished_tokens:
        candidates = []
        if isinstance(voice_started_at, dict) and voice_started_at.get(token):
            candidates.append(voice_started_at[token])
        if isinstance(voice_dispatched_at, dict) and voice_dispatched_at.get(token):
            candidates.append(voice_dispatched_at[token])
        started_epoch = min(
            (epoch for epoch in (_timestamp_epoch(value) for value in candidates)
             if epoch is not None),
            default=None,
        )
        if (
            started_epoch is not None
            and observation_end_epoch is not None
            and 0 <= observation_end_epoch - started_epoch
            <= CORRELATION_SETTLE_GRACE_SECONDS
        ):
            voice_recent_unfinished += 1
        else:
            voice_stale_unfinished += 1
    voice_unmatched_tokens = voice_completed_tokens - voice_correlated_attempts
    voice_boundary_unmatched = 0
    if log_summary.get("correlation_boundary_tolerant") and observation_start_epoch is not None:
        for token in voice_unmatched_tokens:
            completed_epoch = _timestamp_epoch(
                voice_completed_at.get(token)
                if isinstance(voice_completed_at, dict) else None
            )
            if (
                completed_epoch is not None
                and completed_epoch <= observation_start_epoch + CORRELATION_BOUNDARY_GRACE_SECONDS
            ):
                voice_boundary_unmatched += 1
    voice_attempted = max(
        events["voice_attempted"], events["voice_dispatch"],
        len(voice_correlated_attempts),
    )
    voice_evidence = {
        "attempted": voice_attempted,
        "triggered": events["voice_attempted"],
        "dispatches": events["voice_dispatch"],
        "succeeded": events["voice_succeeded"],
        "failed": events["voice_failed"],
        "correlated_attempts": len(voice_correlated_attempts),
        "unfinished": voice_stale_unfinished,
        "unfinished_recent": voice_recent_unfinished,
        "unmatched_completion": len(voice_unmatched_tokens) - voice_boundary_unmatched,
        "boundary_unmatched_completion": voice_boundary_unmatched,
        "correlation_tokens_dropped": int(
            log_summary.get("correlation_tokens_dropped", 0) or 0
        ),
    }
    voice_status = (
        FAIL if (
            voice_evidence["failed"]
            or voice_evidence["unfinished"]
            or voice_evidence["unmatched_completion"]
            or voice_evidence["succeeded"] < max(
                0, voice_evidence["attempted"] - voice_evidence["unfinished_recent"]
            )
        )
        else INSUFFICIENT if (
            voice_evidence["unfinished_recent"]
            or voice_evidence["boundary_unmatched_completion"]
            or voice_evidence["correlation_tokens_dropped"]
        )
        else PASS if voice_evidence["attempted"]
        else INSUFFICIENT
    )
    domains["voice"] = _domain(voice_status, "语音链样本完整" if voice_status == PASS else ("语音生成/发送存在失败" if voice_status == FAIL else "本区间未触发语音"), voice_evidence)

    audio_input_evidence = {
        "succeeded": events["audio_input_succeeded"],
        "failed": events["audio_input_failed"],
    }
    audio_input_status = (
        FAIL if audio_input_evidence["failed"]
        else PASS if audio_input_evidence["succeeded"]
        else INSUFFICIENT
    )
    domains["audio_input"] = _domain(
        audio_input_status,
        "语音输入转写成功" if audio_input_status == PASS else (
            "语音输入解码或转写失败" if audio_input_status == FAIL
            else "本区间未收到语音输入"
        ),
        audio_input_evidence,
    )

    singing_evidence = {
        "confirmed": events["singing_confirmed"],
        "failed_or_uncertain": events["singing_failed"],
    }
    singing_status = (
        FAIL if singing_evidence["failed_or_uncertain"]
        else PASS if singing_evidence["confirmed"]
        else INSUFFICIENT
    )
    domains["singing"] = _domain(
        singing_status,
        "唱歌音频已确认送达" if singing_status == PASS else (
            "唱歌生成失败或发送未确认" if singing_status == FAIL
            else "本区间未触发唱歌"
        ),
        singing_evidence,
    )

    role_evidence = {
        "voice_model_switches": events["role_voice_switched"],
        "sticker_library_switches": events["role_stickers_switched"],
    }
    role_status = (
        PASS if role_evidence["sticker_library_switches"]
        else FAIL if role_evidence["voice_model_switches"]
        else INSUFFICIENT
    )
    domains["role_switch"] = _domain(
        role_status,
        "角色命令已越过音色/人格步骤并完成表情包切换" if role_status == PASS else (
            "角色权重已切换，但未见最终表情包切换" if role_status == FAIL
            else "本区间未切换角色"
        ),
        role_evidence,
    )

    vision_evidence = {
        "vision_success": events["vision_succeeded"],
        "vision_failed": events["vision_failed"],
    }
    vision_status = (
        FAIL if vision_evidence["vision_failed"]
        else PASS if vision_evidence["vision_success"]
        else INSUFFICIENT
    )
    domains["vision"] = _domain(
        vision_status,
        "识图链有真实成功样本" if vision_status == PASS else (
            "识图链出现失败" if vision_status == FAIL else "本区间未触发识图"
        ),
        vision_evidence,
    )

    sticker_evidence = {
        "sticker_tool_calls": tools["send_stickers"],
        "sticker_attempted": events["sticker_attempted"],
        "sticker_confirmed": events["sticker_confirmed"],
        "sticker_uncertain": events["sticker_uncertain"],
        "sticker_failed": events["sticker_failed"],
    }
    if (
        sticker_evidence["sticker_uncertain"]
        or sticker_evidence["sticker_failed"]
        or sticker_evidence["sticker_tool_calls"] > 0
        and sticker_evidence["sticker_attempted"] == 0
        or sticker_evidence["sticker_tool_calls"] > sticker_evidence["sticker_attempted"]
        or sticker_evidence["sticker_attempted"]
        and sticker_evidence["sticker_confirmed"] != sticker_evidence["sticker_attempted"]
    ):
        sticker_status = FAIL
    elif sticker_evidence["sticker_confirmed"]:
        sticker_status = PASS
    else:
        sticker_status = INSUFFICIENT
    domains["sticker"] = _domain(
        sticker_status,
        "贴图已确认送达" if sticker_status == PASS else (
            "贴图失败、未确认或工具调用后无送达证据"
            if sticker_status == FAIL else "本区间未触发贴图"
        ),
        sticker_evidence,
    )

    knowledge_calls = tools["search_knowledge"] + tools["read_document"]
    knowledge_evidence = {
        "search_or_read_calls": knowledge_calls,
        "success": events["knowledge_success"],
        "empty": events["knowledge_empty"],
        "error_or_invalid": events["knowledge_error"] + events["knowledge_invalid"],
    }
    retrieval_trace = log_summary.get("knowledge_retrieval") or {}
    retrieval_latencies = []
    retrieval_rerank_scores = []
    if isinstance(retrieval_trace, dict):
        raw_trace_latencies = retrieval_trace.get("latency_ms") or []
        if isinstance(raw_trace_latencies, (list, tuple)):
            for value in raw_trace_latencies:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if parsed >= 0:
                    retrieval_latencies.append(parsed)
        raw_rerank_scores = retrieval_trace.get("rerank_score") or []
        if isinstance(raw_rerank_scores, (list, tuple)):
            for value in raw_rerank_scores:
                if (isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))):
                    continue
                retrieval_rerank_scores.append(float(value))
    knowledge_evidence.update({
        "retrieval_trace_calls": int(
            retrieval_trace.get("calls", 0) or 0
        ) if isinstance(retrieval_trace, dict) else 0,
        "retrieval_trace_hits": int(
            retrieval_trace.get("hits", 0) or 0
        ) if isinstance(retrieval_trace, dict) else 0,
        "retrieval_trace_empty": int(
            retrieval_trace.get("empty", 0) or 0
        ) if isinstance(retrieval_trace, dict) else 0,
        "retrieval_trace_sources": int(
            retrieval_trace.get("sources", 0) or 0
        ) if isinstance(retrieval_trace, dict) else 0,
        "retrieval_trace_latency_p50_ms": _percentile(retrieval_latencies, 0.50),
        "retrieval_trace_latency_p95_ms": _percentile(retrieval_latencies, 0.95),
        "retrieval_trace_paths": dict(
            retrieval_trace.get("path_counts") or {}
        ) if isinstance(retrieval_trace, dict)
        and isinstance(retrieval_trace.get("path_counts") or {}, dict) else {},
        "retrieval_trace_match_types": dict(
            retrieval_trace.get("match_type_counts") or {}
        ) if isinstance(retrieval_trace, dict)
        and isinstance(retrieval_trace.get("match_type_counts") or {}, dict) else {},
        "retrieval_trace_rerank_score_p50": _percentile_fine(
            retrieval_rerank_scores, 0.50,
        ),
        "retrieval_trace_rerank_score_p95": _percentile_fine(
            retrieval_rerank_scores, 0.95,
        ),
    })
    knowledge_outcomes = (
        knowledge_evidence["success"]
        + knowledge_evidence["empty"]
        + knowledge_evidence["error_or_invalid"]
    )
    knowledge_evidence["outcomes"] = knowledge_outcomes
    if knowledge_evidence["error_or_invalid"]:
        knowledge_status = FAIL
    elif knowledge_calls and knowledge_outcomes < knowledge_calls:
        knowledge_status = FAIL
    elif knowledge_evidence["success"]:
        knowledge_status = PASS
    elif knowledge_calls and not knowledge_evidence["empty"]:
        knowledge_status = FAIL
    else:
        knowledge_status = INSUFFICIENT
    domains["knowledge_tools"] = _domain(
        knowledge_status,
        "知识检索取得了真实内容" if knowledge_status == PASS else (
            "知识工具报错或调用后缺少结果证据" if knowledge_status == FAIL
            else "本区间未取得知识内容（未调用或未命中）"
        ),
        knowledge_evidence,
    )

    autonomy_evidence = {"confirmed": events["autonomy_confirmed"], "uncertain": events["autonomy_uncertain"]}
    autonomy_status = FAIL if autonomy_evidence["uncertain"] else (PASS if autonomy_evidence["confirmed"] else INSUFFICIENT)
    domains["autonomy"] = _domain(autonomy_status, "自治行为已确认送达" if autonomy_status == PASS else ("自治发送未确认" if autonomy_status == FAIL else "本区间无自治发言样本"), autonomy_evidence)

    durable_reminder_confirmed = max(
        0,
        int(after.get("database", {}).get("outbox", {}).get(
            "task_confirmed_total", 0,
        ) or 0)
        - int(before.get("database", {}).get("outbox", {}).get(
            "task_confirmed_total", 0,
        ) or 0),
    )
    reminder_evidence = {
        "confirmed": max(
            int(events["reminder_confirmed"]), durable_reminder_confirmed,
        ),
        "durable_confirmed": durable_reminder_confirmed,
        "log_confirmed": int(events["reminder_confirmed"]),
    }
    domains["reminders"] = _domain(
        PASS if reminder_evidence["confirmed"] else INSUFFICIENT,
        "提醒已确认送达" if reminder_evidence["confirmed"] else "本区间无到期提醒",
        reminder_evidence,
    )

    for domain_name, count, success_summary, missing_summary in (
        ("birthdays", events["birthday_confirmed"], "生日祝福已确认送达", "本区间无生日祝福样本"),
        ("image_shares", events["image_share_confirmed"], "定时图片已确认送达", "本区间无定时图片样本"),
    ):
        domains[domain_name] = _domain(
            PASS if count else INSUFFICIENT,
            success_summary if count else missing_summary,
            {"confirmed": count},
        )

    runtime_evidence = {
        "structured_lines": events["structured_lines"],
        "unstructured_lines": events["unstructured_lines"],
        "unstructured_error_lines": events["unstructured_error_lines"],
        "errors": events["log_errors"],
        "warnings": events["log_warnings"],
    }
    runtime_status = (
        FAIL if runtime_evidence["unstructured_error_lines"]
        else INSUFFICIENT if runtime_evidence["errors"] or runtime_evidence["unstructured_lines"]
        else PASS if runtime_evidence["structured_lines"]
        else INSUFFICIENT
    )
    domains["runtime_logs"] = _domain(
        runtime_status,
        "运行区间未见 ERROR 日志" if runtime_status == PASS else (
            "存在未分类 ERROR，需结合具体功能域人工复核"
            if runtime_evidence["errors"] else "没有读到结构化运行日志"
        ),
        runtime_evidence,
    )

    observer_evidence = {
        "rotation_tail_missing": events["log_rotation_tail_missing"],
        "rotation_read_failures": events["log_rotation_read_failures"],
        "file_truncations": events["log_file_truncations"],
        "file_missing": events["log_file_missing"],
        "file_missing_recovered": events["log_file_missing_recovered"],
        "unstructured_lines": events["unstructured_lines"],
        "unstructured_error_lines": events["unstructured_error_lines"],
        "counter_regressions": counter_regressions,
        "counter_missing": counter_missing,
        "chat_log_id_regressions": chat_log_id_regressions,
        "metric_integrity": metric_integrity,
        "probe_duration_ms": {
            "samples": sum(
                1 for item in snapshots
                if item.get("observer", {}).get("probe_duration_ms") is not None
            ),
            "p95": _percentile([
                float(item["observer"]["probe_duration_ms"])
                for item in snapshots
                if item.get("observer", {}).get("probe_duration_ms") is not None
            ], 0.95),
            "max": round(max([
                float(item["observer"]["probe_duration_ms"])
                for item in snapshots
                if item.get("observer", {}).get("probe_duration_ms") is not None
            ] or [0.0]), 1),
        },
    }
    observer_status = (
        FAIL if observer_evidence["rotation_tail_missing"]
        or observer_evidence["rotation_read_failures"]
        or observer_evidence["file_truncations"]
        or observer_evidence["file_missing"]
        or observer_evidence["chat_log_id_regressions"]
        or metric_integrity_failed
        or counter_integrity_failed
        or observer_evidence["unstructured_error_lines"]
        else PASS
    )
    if observer_status == PASS and observer_evidence["unstructured_lines"]:
        observer_status = INSUFFICIENT
    domains["observer_integrity"] = _domain(
        observer_status,
        {
            PASS: "日志、指标与高水位证据完整",
            FAIL: "日志、指标或高水位存在完整性失败",
            INSUFFICIENT: "存在非结构化日志，监测证据不足",
        }[observer_status],
        observer_evidence,
    )

    background_records = log_summary.get("background_by_logger") or {}
    observation_end = _timestamp_epoch(after.get("captured_at"))
    latest_boot = str(log_summary.get("latest_boot") or "")
    boot_first_at = log_summary.get("boot_first_at") or {}
    observation_start = _timestamp_epoch(before.get("captured_at"))
    observed_seconds = (
        max(
            0.0,
            (_timestamp_epoch(after.get("captured_at")) or 0.0)
            - (observation_start or 0.0),
        )
        if observation_start is not None and _timestamp_epoch(after.get("captured_at")) is not None
        else 0.0
    )
    required_heartbeats: dict[str, dict] = {}
    stale_required: list[str] = []
    missing_required: list[str] = []
    for logger_name, contract in REQUIRED_BACKGROUND_HEARTBEATS.items():
        record = background_records.get(logger_name) or {}
        heartbeat_boots = record.get("heartbeat_boots") or {}
        current_boot_record = (
            heartbeat_boots.get(latest_boot)
            if latest_boot and isinstance(heartbeat_boots, dict)
            else None
        )
        if not isinstance(current_boot_record, dict):
            # 兼容旧版/手工摘要：没有分 boot 统计时只能使用聚合边界，
            # 但不能在已识别新 boot 时拿旧 boot 的心跳背书。
            if latest_boot and str(record.get("last_heartbeat_boot") or "") not in ("", latest_boot):
                current_boot_record = {}
            else:
                current_boot_record = record
        count = int(current_boot_record.get("count", current_boot_record.get("heartbeats", 0)) or 0)
        first_at = str(current_boot_record.get("first_at", current_boot_record.get("first_heartbeat_at", "")) or "")
        last_at = str(current_boot_record.get("last_at", current_boot_record.get("last_heartbeat_at", "")) or "")
        heartbeat_boot = str(record.get("last_heartbeat_boot") or latest_boot)
        max_gap_seconds = float(current_boot_record.get("max_gap_seconds", 0.0) or 0.0)
        last_epoch = _timestamp_epoch(last_at)
        max_age = (
            float(contract["period_seconds"]) * 3
            + BACKGROUND_HEARTBEAT_GRACE_SECONDS
        )
        age = (
            max(0.0, observation_end - last_epoch)
            if observation_end is not None and last_epoch is not None else None
        )
        first_epoch = _timestamp_epoch(first_at)
        boot_start_value = (
            boot_first_at.get(latest_boot)
            if isinstance(boot_first_at, dict) and latest_boot else None
        )
        boot_start_epoch = _timestamp_epoch(boot_start_value)
        effective_start = observation_start
        if boot_start_epoch is not None and (
            effective_start is None or boot_start_epoch > effective_start
        ):
            effective_start = boot_start_epoch
        first_age = (
            max(0.0, first_epoch - effective_start)
            if first_epoch is not None and effective_start is not None else None
        )
        startup_grace = float(
            contract.get("startup_grace_seconds", max_age) or max_age
        )
        # 短观测期允许后台刚启动尚未完成第一轮；达到两个宽限窗口后，
        # 首次心跳也必须及时出现，否则“末尾才恢复”会假绿。
        late_start = bool(
            observed_seconds >= max(max_age * 2, startup_grace * 2)
            and first_age is not None
            and first_age > startup_grace
        )
        required_heartbeats[logger_name] = {
            "count": count,
            "first_at": first_at,
            "last_at": last_at,
            "first_age_seconds": round(first_age, 1) if first_age is not None else None,
            "age_seconds": round(age, 1) if age is not None else None,
            "max_age_seconds": max_age,
            "startup_grace_seconds": startup_grace,
            "max_gap_seconds": round(max_gap_seconds, 1),
            "max_gap_exceeded": max_gap_seconds > max_age,
            "late_start": late_start,
            "min_samples": int(contract["min_samples"]),
            "latest_boot_match": bool(
                not latest_boot or heartbeat_boot == latest_boot
            ),
        }
        if count < int(contract["min_samples"]):
            missing_required.append(logger_name)
        elif age is None:
            missing_required.append(logger_name)
        elif latest_boot and heartbeat_boot != latest_boot:
            # 新进程已经出现日志，但该循环仍只有旧 boot 的心跳，不能
            # 用旧进程的活性替当前进程背书。
            missing_required.append(logger_name)
        elif max_gap_seconds > max_age or late_start:
            stale_required.append(logger_name)
        elif age > max_age:
            stale_required.append(logger_name)
    tts_repairs_by_boot = log_summary.get("tts_repairs_by_boot") or {}
    recovered_failures = sum(
        min(
            max(0, int(repair.get("observed", 0) or 0)),
            max(0, int(repair.get("recovered", 0) or 0)),
        )
        for repair in tts_repairs_by_boot.values()
        if isinstance(repair, dict)
    )
    raw_failures = events["background_task_failures"]
    unresolved_failures = max(0, raw_failures - recovered_failures)
    background_evidence = {
        "activity_lines": events["background_activity_lines"],
        "raw_failures": raw_failures,
        "recovered_failures": recovered_failures,
        "failures": unresolved_failures,
        "required_heartbeats": required_heartbeats,
        "missing_required": missing_required,
        "stale_required": stale_required,
        "optional_observed": sorted(
            name for name in background_records
            if name not in REQUIRED_BACKGROUND_HEARTBEATS
        ),
    }
    if background_evidence["failures"]:
        domains["background_tasks"] = _domain(
            FAIL, "有未成功回收的后台任务", background_evidence,
        )
    elif background_evidence["stale_required"]:
        domains["background_tasks"] = _domain(
            FAIL, "必需后台循环心跳已超出允许间隔", background_evidence,
        )
    elif background_evidence["missing_required"]:
        domains["background_tasks"] = _domain(
            INSUFFICIENT, "必需后台循环心跳样本不足",
            background_evidence,
        )
    else:
        domains["background_tasks"] = _domain(
            PASS, "必需后台循环均有新鲜心跳且未见异常", background_evidence,
        )

    visible_boots = {str(item) for item in (log_summary.get("boot_ids") or []) if item}
    initial_boot_id = str(log_summary.get("initial_boot_id") or "")
    initial_boot_at = str(log_summary.get("initial_boot_at") or "")
    baseline_required = bool(log_summary.get("baseline_required"))
    baseline_at_epoch = _timestamp_epoch(initial_boot_at)
    observation_start_epoch = _timestamp_epoch(before.get("captured_at"))
    baseline_age_seconds = (
        observation_start_epoch - baseline_at_epoch
        if baseline_at_epoch is not None and observation_start_epoch is not None
        else None
    )
    baseline_fresh = bool(
        initial_boot_id
        and baseline_age_seconds is not None
        and 0.0 <= baseline_age_seconds <= 600.0
    )
    baseline_complete = baseline_fresh if baseline_required else bool(visible_boots)
    observed_boots = set(visible_boots)
    if initial_boot_id:
        observed_boots.add(initial_boot_id)
    boot_count = len(observed_boots)
    observed_restarts = max(0, boot_count - 1)
    expected_restarts = log_summary.get("expected_restarts")
    if expected_restarts is not None:
        expected_restarts = max(0, int(expected_restarts))
    latest_boot = str(log_summary.get("latest_boot") or initial_boot_id)
    latest_boot_events = (log_summary.get("events_by_boot") or {}).get(
        latest_boot, {}
    )
    latest_prefix = f"{latest_boot}:"
    inbound_tokens = {
        token for token in (log_summary.get("recovery_inbound_tokens") or [])
        if str(token).startswith(latest_prefix)
    }
    llm_completed_tokens = {
        token for token in (log_summary.get("recovery_llm_completed_tokens") or [])
        if str(token).startswith(latest_prefix)
    }
    send_confirmed_tokens = {
        token for token in (log_summary.get("recovery_send_confirmed_tokens") or [])
        if str(token).startswith(latest_prefix)
    }
    same_turn_recovered = bool(
        inbound_tokens & llm_completed_tokens & send_confirmed_tokens
    )
    required_services = [
        service for service in (after.get("services") or {}).values()
        if service.get("required")
    ]
    latest_boot_recovery = {
        "inbound": bool(latest_boot_events.get("inbound", 0)),
        "llm_completed": bool(latest_boot_events.get("llm_completed", 0)),
        "send_confirmed": bool(latest_boot_events.get("send_confirmed", 0)),
        "same_turn": same_turn_recovered,
        "background_ready": domains["background_tasks"]["status"] == PASS,
        "services_up": bool(required_services) and all(
            service.get("up") for service in required_services
        ),
    }
    stability_evidence = {
        "mode": "restart_recovery" if expected_restarts is not None else "stability",
        "baseline_complete": baseline_complete,
        "baseline_age_seconds": (
            round(baseline_age_seconds, 1)
            if baseline_age_seconds is not None else None
        ),
        "boot_count": boot_count,
        "observed_restarts": observed_restarts,
        "expected_restarts": expected_restarts,
        "latest_boot_recovery": latest_boot_recovery,
    }
    if not baseline_complete:
        stability_status = INSUFFICIENT
        stability_summary = "观察边界无法锚定初始启动分区"
    elif expected_restarts is None:
        stability_status = PASS if observed_restarts == 0 else INSUFFICIENT
        stability_summary = (
            "本区间只观察到一个启动分区"
            if stability_status == PASS
            else "普通稳定性观测跨过重启，不能证明单实例连续稳定"
        )
    elif observed_restarts > expected_restarts:
        stability_status = FAIL
        stability_summary = "观察到超出计划的额外重启"
    elif observed_restarts < expected_restarts:
        stability_status = INSUFFICIENT
        stability_summary = "计划中的重启恢复场景尚未完整发生"
    elif expected_restarts and not all(latest_boot_recovery.values()):
        stability_status = INSUFFICIENT
        stability_summary = "重启次数匹配，但最新进程缺少端到端恢复证据"
    else:
        stability_status = PASS
        stability_summary = "启动连续性与计划一致"
    domains["process_stability"] = _domain(
        stability_status, stability_summary, stability_evidence,
    )

    album_evidence = {
        "success": events["album_success"],
        "failures": events["album_failures"],
        "quota_exhausted": events["album_quota_exhausted"],
    }
    album_status = (
        FAIL if album_evidence["failures"]
        else PASS if album_evidence["success"]
        else INSUFFICIENT
    )
    domains["album"] = _domain(
        album_status,
        "点赞链有成功样本" if album_status == PASS else (
            "点赞链出现失败" if album_status == FAIL else "本区间未触发点赞"
        ),
        album_evidence,
    )

    health_check_evidence = {
        "runs": events["health_check_runs"],
        "failures": events["health_check_failures"],
    }
    health_check_status = (
        FAIL if health_check_evidence["failures"]
        else PASS if health_check_evidence["runs"]
        else INSUFFICIENT
    )
    domains["health_check"] = _domain(
        health_check_status,
        "健康检查已运行且无异常" if health_check_status == PASS else (
            "健康检查报告异常" if health_check_status == FAIL
            else "本区间没有健康检查样本"
        ),
        health_check_evidence,
    )

    errors = probe_errors
    if errors or any(item["status"] == FAIL for item in domains.values()):
        coverage = FAIL
    elif any(item["status"] == INSUFFICIENT for item in domains.values()):
        coverage = INSUFFICIENT
    else:
        coverage = PASS
    if errors:
        overall = FAIL
    elif any(item["status"] == FAIL for item in domains.values()):
        overall = FAIL
    elif any(domains[name]["status"] == INSUFFICIENT for name in CORE_DOMAINS):
        overall = INSUFFICIENT
    else:
        overall = PASS
    return {
        "overall": overall,
        "coverage": coverage,
        "started_at": before.get("captured_at", ""),
        "ended_at": after.get("captured_at", ""),
        "samples": len(snapshots),
        "boot_count": int(log_summary.get("boot_count", 0) or 0),
        "domains": domains,
        "errors": errors,
    }


def render_markdown(report: dict) -> str:
    icons = {PASS: "🟢", FAIL: "🔴", INSUFFICIENT: "🟡"}
    labels = {
        "qq_gateway": "QQ接入", "local_services": "本地依赖",
        "sending": "发送事务", "memory": "记忆",
        "llm": "LLM", "llm_tools": "LLM工具", "dialog": "窗口对话", "voice": "语音输出",
        "proactive_decisions": "主动事件/决策",
        "audio_input": "语音输入", "singing": "唱歌",
        "role_switch": "角色切换",
        "vision": "识图", "sticker": "贴图", "knowledge_tools": "知识工具",
        "autonomy": "自治", "reminders": "提醒", "birthdays": "生日祝福",
        "image_shares": "定时图片",
        "album": "点赞",
        "health_check": "健康检查",
        "process_stability": "进程稳定性",
        "runtime_logs": "运行错误", "background_tasks": "后台任务",
        "observer_integrity": "观察器完整性",
    }
    lines = [
        "# 小糖糖生产运行验收报告", "",
        f"- 核心运行健康：{icons.get(report['overall'], '')} **{report['overall']}**",
        f"- 全功能样本覆盖：{icons.get(report.get('coverage'), '')} "
        f"**{report.get('coverage', INSUFFICIENT)}**",
        f"- 区间：{report.get('started_at') or '-'} → {report.get('ended_at') or '-'}",
        f"- 快照：{report.get('samples', 0)}；启动分区：{report.get('boot_count', 0)}", "",
        "| 功能域 | 状态 | 判定 | 证据 |", "|---|---|---|---|",
    ]
    for name, item in report.get("domains", {}).items():
        evidence = json.dumps(item.get("evidence", {}), ensure_ascii=False, separators=(",", ":"))
        lines.append(
            f"| {labels.get(name, name)} | {icons.get(item['status'], '')} {item['status']} | "
            f"{item['summary']} | `{evidence}` |"
        )
    if report.get("errors"):
        lines.extend(["", "## 观察器错误", ""])
        lines.extend(f"- `{error}`" for error in report["errors"])
    lines.extend([
        "", "## 判定说明", "",
        "- PASS：本区间有实际样本，且没有触发失败门槛。",
        "- FAIL：有可定位的失败/未确认/完整性证据。",
        "- INSUFFICIENT：运行时间或真实触发样本不足，不代表故障。",
        "- 报告只含汇总数值，不包含消息正文、QQ号、群号或原始 cid。",
    ])
    return "\n".join(lines) + "\n"


def _write_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_text(path: Path, content: str) -> None:
    """报告先写同目录临时文件，再原子替换，避免半份 JSON/Markdown。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    )
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def record_heartbeat(db_path: str | Path, *, at: float | None = None) -> bool:
    """把「观察器刚跑过」写进 kv，供进程内的缺席告警检查读取。

    2026-10-14 台账 T3.1：观察器此前**没有任何生产调用方**，它停了没人知道，
    而它的作用恰恰是发现别的东西停了——所以它自己也要有心跳。

    键名与 agent/health_check.OBSERVER_HEARTBEAT_KEY 必须一致（两边各自
    持有一份常量是为了不让只读工具反向依赖 agent 包；有测试钉住两者相等）。

    失败返回 False 而**不抛**：观察器的主职责是观测，心跳写不上不该让它崩。
    """
    try:
        import sqlite3 as _sq
        stamp = float(at if at is not None else time.time())
        con = _sq.connect(str(db_path), timeout=5.0)
        try:
            con.execute(
                "INSERT OR REPLACE INTO kv_store (key, value, updated) "
                "VALUES (?, ?, ?)",
                (OBSERVER_HEARTBEAT_KEY, str(stamp),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def _write_outputs(output_dir: Path, run_id: str, snapshots: list[dict], logs: dict) -> tuple[dict, Path, Path]:
    report = evaluate_observation(snapshots, logs)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"runtime-{run_id}.json"
    md_path = output_dir / f"runtime-{run_id}.md"
    _atomic_write_text(
        json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write_text(md_path, render_markdown(report))
    return report, json_path, md_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读监测小糖糖生产运行，输出脱敏 JSONL/JSON/Markdown")
    parser.add_argument("--duration-hours", type=float, default=2.0, help="观察时长；2小时冒烟，稳定后改12")
    parser.add_argument("--interval-seconds", type=float, default=300.0, help="快照间隔，默认5分钟")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "memory.db"))
    parser.add_argument("--log", default=str(log_path()))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"),
                        help="运行配置；用于判断已启用的本地服务是否必需")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "artifacts" / "production_monitor"))
    parser.add_argument("--from-start", action="store_true", help="包含启动命令前日志（调试用）")
    parser.add_argument("--once", action="store_true", help="立即采集一次，用于验证观察器本身")
    parser.add_argument(
        "--expected-restarts", type=int, default=None,
        help="显式重启恢复验收的计划重启次数；普通稳定性观测不要设置",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.expected_restarts is not None and args.expected_restarts < 0:
        print("🔴 --expected-restarts 必须是非负整数", file=sys.stderr)
        return 4
    if args.from_start and args.expected_restarts is not None:
        print("🔴 --from-start 不能用于显式重启恢复验收", file=sys.stderr)
        return 4
    run_id = (
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
        f"{os.getpid()}-{secrets.token_hex(2)}"
    )
    output_dir = Path(args.output_dir)
    samples_path = output_dir / f"runtime-{run_id}.jsonl"
    try:
        cursor = LogCursor(Path(args.log), start_at_end=not args.from_start)
    except Exception as exc:
        # 构造阶段也属于观察器自身错误；不能让异常越过 CLI，令调用方
        # 误以为是普通进程崩溃而得不到约定的 exit=4。
        print(
            f"🔴 观察器无法初始化日志游标: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 4
    if cursor.issues.get("initial_file_missing", 0):
        print(
            f"🔴 观察器日志文件不存在: {args.log}",
            file=sys.stderr,
        )
        return 4
    snapshots: list[dict] = []
    log_summary: dict = merge_log_summaries()
    # 正式模式从当前文件尾部开始追读；观察窗口边界内可能有一条在
    # 启动观察器前已开始、随后才完成的请求。允许 evaluator 将这类
    # 在对应收尾宽限（当前 210s）内的边界完成标为“边界样本”，
    # 避免把它误判为孤儿回合。
    # --from-start 是调试/取证模式，必须保持严格配对。
    log_summary["correlation_boundary_tolerant"] = not args.from_start
    log_summary["expected_restarts"] = args.expected_restarts
    log_summary["baseline_required"] = not args.from_start
    log_summary["initial_boot_id"] = cursor.initial_boot_id
    log_summary["initial_boot_at"] = cursor.initial_boot_at
    last_memory_id: int | None = None
    duration_seconds = 0.0 if args.once else max(0.0, args.duration_hours * 3600)
    interval = max(1.0, args.interval_seconds)
    deadline = time.monotonic() + duration_seconds

    print(f"🟢 生产观察已开始 run={run_id} duration={duration_seconds / 3600:.2f}h")
    try:
        while True:
            new_lines = cursor.read_new()
            interval_logs = parse_log_events(new_lines)
            log_summary = merge_log_summaries(log_summary, interval_logs)
            log_summary["correlation_boundary_tolerant"] = not args.from_start
            log_summary["expected_restarts"] = args.expected_restarts
            log_summary["baseline_required"] = not args.from_start
            log_summary["initial_boot_id"] = cursor.initial_boot_id
            log_summary["initial_boot_at"] = cursor.initial_boot_at
            snapshot = capture_snapshot(
                args.db,
                after_memory_id=last_memory_id,
                config_path=getattr(args, "config", None),
            )
            snapshots.append(snapshot)
            if snapshot.get("database", {}).get("ok"):
                memory_snapshot = snapshot["database"].get("memory", {})
                last_memory_id = int(
                    memory_snapshot.get(
                        "id_high_water", memory_snapshot.get("max_id", 0)
                    ) or 0
                )
            _write_jsonl(samples_path, {
                **snapshot,
                # 每个采样点只落盘本区间日志增量；累计计数保留为紧凑
                # totals，避免 12h 高流量时重复写入数万条延迟样本。
                # 区间日志也只写计数/直方图，不把最多 20k 个浮点延迟
                # 样本重复堆进 JSONL；完整配对状态仅留在进程内累计摘要。
                "log_summary": _public_log_summary(interval_logs, compact=True),
                "log_totals": _public_log_summary(log_summary, compact=True),
            })
            # 每个完整区间都更新一次可读报告；即使观察器被终端关闭或
            # 主机重启，最后一份 JSON/Markdown 仍可直接审阅，不必等到
            # finally 才生成。
            if len(snapshots) >= 2:
                try:
                    _write_outputs(output_dir, run_id, snapshots, log_summary)
                except Exception as exc:
                    print(
                        f"⚠️ 观察器检查点报告写入失败: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
            backlog = snapshot.get("database", {}).get("backlog", {})
            print(
                f"[{snapshot['captured_at']}] db={'ok' if snapshot['database'].get('ok') else 'error'} "
                f"backlog={backlog.get('backlog_messages', '-')} "
                f"napcat={'up' if snapshot['services']['napcat']['up'] else 'down'}"
            )
            if args.once or time.monotonic() >= deadline:
                break
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print("\n⚪ 收到中断，将已采集样本生成报告")
    except Exception as exc:
        snapshots.append({
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": {"ok": False}, "services": {},
            "errors": [f"observer:{type(exc).__name__}:{exc}"],
        })
    finally:
        try:
            log_summary = merge_log_summaries(
                log_summary, parse_log_events(cursor.read_new(final=True)),
            )
            log_summary["correlation_boundary_tolerant"] = not args.from_start
            log_summary["expected_restarts"] = args.expected_restarts
            log_summary["baseline_required"] = not args.from_start
            log_summary["initial_boot_id"] = cursor.initial_boot_id
            log_summary["initial_boot_at"] = cursor.initial_boot_at
            for name, count in cursor.issues.items():
                log_summary["events"][f"log_{name}"] = int(count)
        except Exception as exc:
            snapshots.append({
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "database": {"ok": False}, "services": {},
                "errors": [f"log_finalize:{type(exc).__name__}:{exc}"],
            })

    try:
        report, json_path, md_path = _write_outputs(
            output_dir, run_id, snapshots, log_summary,
        )
    except Exception as exc:
        print(
            f"🔴 观察器无法写出报告: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 4
    # T3.1：报告写出成功才记心跳——失败的心跳比没有心跳更坏（会掩盖缺席）。
    if record_heartbeat(args.db):
        print("💓 观察器心跳已写入")
    else:
        print("⚠️ 观察器心跳写入失败（不影响本次观测）", file=sys.stderr)
    print(f"📊 {report['overall']} | {md_path}")
    print(f"🧾 JSON | {json_path}")
    # 验收工具将业务 FAIL 固定为 2，样本不足为 3，观察器自身错误为 4；
    # Ctrl+C 仍使用已采集样本生成报告，并按报告状态返回上述代码。
    if report.get("errors"):
        return 4
    return {PASS: 0, FAIL: 2, INSUFFICIENT: 3}.get(report["overall"], 4)


if __name__ == "__main__":
    raise SystemExit(main())
