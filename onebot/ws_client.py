"""
SnowLuma / OneBot 11 反向 WebSocket 适配器
我们的程序作为 WebSocket 服务端，SnowLuma 主动连过来推送消息
API 调用通过 HTTP 发送到 SnowLuma（兼容所有 OneBot 11 实现）
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
import websockets
import httpx

from agent.telemetry import correlation_scope, new_correlation_id
from agent.inbound_event import build_platform_event_key, normalize_inbound_event
from agent.async_io import run_bounded_blocking, run_bounded_store_io

logger = logging.getLogger("糖糖.SnowLuma")

EVENT_SCOPE_QUEUE_MAX = 128
EVENT_TOTAL_INFLIGHT_MAX = 4096
EVENT_HANDLER_CONCURRENCY = 8
EVENT_ACTIVE_SCOPE_MAX = 2048
EVENT_DEDUP_TTL_SECONDS = 300.0
EVENT_DEDUP_MAX = 20_000
EVENT_SCOPE_IDLE_SECONDS = 60.0


@dataclass(frozen=True)
class SendResult:
    """发送尝试的可观测结果；bool 兼容旧调用点的成功判断。"""

    ok: bool
    delivered: bool
    message_id: int = 0
    chunk_ids: tuple[int, ...] = ()
    error: str = ""
    retcode: int | str | None = None
    retryable: bool = False
    uncertain: bool = False

    def __bool__(self) -> bool:
        # 兼容旧调用点：bool 只表示 API 接受了请求（ok），不代表已有
        # message_id 证据。需要提交业务状态时必须调用 is_send_confirmed()。
        return self.ok

    @property
    def delivery_state(self) -> str:
        """返回 confirmed / uncertain / failed，避免把 ok 当作已送达。"""
        if self.delivered:
            return "confirmed"
        if self.ok or self.uncertain:
            return "uncertain"
        return "failed"


def is_send_confirmed(result) -> bool:
    """判断发送是否有明确的送达证据。

    ``SendResult.ok`` 仅代表网关接受请求；只有 ``delivered``（由非零
    OneBot int32 message_id 推导）才允许提交「已发送」状态。旧版适配器返回
    bool 时没有更细证据，继续按 bool 兼容。
    """
    delivered = getattr(result, "delivered", None)
    if delivered is not None:
        return bool(delivered)
    return bool(result)


def send_delivery_state(result) -> str:
    """返回统一的发送状态：confirmed / uncertain / failed。"""
    if isinstance(result, SendResult):
        return result.delivery_state
    delivered = getattr(result, "delivered", None)
    if delivered is not None:
        return "confirmed" if bool(delivered) else ("uncertain" if bool(result) else "failed")
    return "confirmed" if bool(result) else "failed"


def _normalize_message_id(raw) -> tuple[int, bool]:
    """返回 (规范化 ID, 是否可确认送达)；OneBot int32 负 ID 合法。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0, False
    if raw < -(2**31) or raw > 2**31 - 1 or raw == 0:
        return 0, False
    return raw, True


def _classify_send_error(result: dict) -> tuple[str, bool]:
    """统一 transport/OneBot 错误分类；重试只交给未来 outbox。"""
    retcode = result.get("retcode")
    text = str(result.get("msg", result.get("wording", ""))).lower()
    if retcode == 16 or str(retcode) == "16" or "好友" in text:
        return "NO_FRIEND", False
    transport_error = result.get("_transport_error")
    if transport_error == "NETWORK_UNCERTAIN":
        return "NETWORK_UNCERTAIN", False
    if transport_error in {"NETWORK", "HTTP_5XX"}:
        return str(transport_error), True
    if transport_error in {"HTTP_4XX", "CLIENT_ERROR"}:
        return str(transport_error), False
    # 含 OneBot retcode 的 failed 是业务层拒绝，不得按网络失败重放。
    if retcode is not None:
        return "BOT_ERROR", False
    # 兼容旧测试替身/第三方适配器的无细节失败。
    if result.get("status") == "failed":
        return "NETWORK_OR_GATEWAY", True
    return "BOT_ERROR", False


@dataclass(frozen=True)
class _InboundEvent:
    """进入处理层后不可换路由的 OneBot 事件信封。"""

    handler_name: str
    data: dict
    received_at: float
    event_id: str
    durable_key: str = ""


def _split_message_chunks(message: str, limit: int = 2000) -> list[str]:
    """按 OneBot 长度上限分段，CQ 码作为不可拆分的原子片段。"""
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""
    for token in re.split(r'(\[CQ:[^\]]*\])', message):
        if not token:
            continue
        if token.startswith("[CQ:") and token.endswith("]"):
            if current and len(current) + len(token) > limit:
                chunks.append(current)
                current = ""
            current += token
            continue

        remaining = token
        while remaining:
            room = limit - len(current)
            if room <= 0:
                chunks.append(current)
                current = ""
                room = limit
            if len(remaining) <= room:
                current += remaining
                break

            window = remaining[:room]
            breaks = [window.rfind(ch) + 1 for ch in "\n。！？!?；;"]
            natural = max(breaks)
            cut = natural if natural >= max(1, room // 2) else room
            current += remaining[:cut]
            chunks.append(current)
            current = ""
            remaining = remaining[cut:]

    if current:
        chunks.append(current)
    return chunks


class NapCatClient:
    """SnowLuma QQ 网关 —— 反向WebSocket + HTTP API 模式（兼容 OneBot 11）"""

    def __init__(self, ws_host: str = "127.0.0.1", ws_port: int = 3001,
                 http_url: str = "http://127.0.0.1:3000", access_token: str = "",
                 self_id: str = "", testing_mode: bool = False,
                 poll_friends: bool = False, persist_cb=None):
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.http_url = http_url
        self.access_token = access_token
        self.self_id = self_id
        self._poll_friends = poll_friends  # 好友申请轮询开关（默认关，省资源）
        self.testing_mode = testing_mode
        self.server = None
        self.client_ws = None  # SnowLuma 连过来的那个 ws

        # 消息回调
        self.on_group_message: Optional[Callable] = None
        self.on_private_message: Optional[Callable] = None
        self.on_poke: Optional[Callable] = None  # 戳一戳回调
        self.on_friend_request: Optional[Callable] = None  # 好友申请回调
        self.on_group_invite: Optional[Callable] = None    # 群邀请回调
        self.on_group_increase: Optional[Callable] = None  # 新人入群回调
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None
        self.on_qq_offline: Optional[Callable] = None     # QQ 掉线（WebSocket 仍连着）
        self.on_qq_online: Optional[Callable] = None      # QQ 恢复上线

        self._running = False
        self._http = None
        self._poll_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._outbox_task: Optional[asyncio.Task] = None
        self._qq_online: bool = False   # QQ 实际登录状态
        self._ws_connected: bool = False  # SnowLuma 传输层连接状态
        self._no_friend_until: dict[str, float] = {}  # 非好友冷却表（2026-08-15）
        self._persist_cb = persist_cb  # (dict) -> None——非好友冷却落盘（2026-08-16 持久化规矩）
        self._heartbeat_fail_count: int = 0
        self._offline_since: float = 0.0  # QQ 离线时间戳（由 meta_event lifecycle:disable 设置）
        self._last_send_result: SendResult | None = None
        self.last_sent_msg_id: int = 0  # 旧撤回路径兼容；详细结果见 _last_send_result
        # 按目标会话保存最近一条已确认消息；撤回不得使用跨群全局 ID。
        self._last_sent_msg_ids: dict[str, int] = {}
        self._outbox_store = None
        self._send_locks: dict[str, asyncio.Lock] = {}
        # 锁表引用计数：等待者也占一个引用，最后一个离开且锁已释放时
        # 才回收 scope，避免按目标数量单调增长，同时不破坏 FIFO。
        self._send_lock_refs: dict[str, int] = {}
        self._send_lock_owners: dict[str, asyncio.Task] = {}
        self._metrics = None
        self._runtime_counters: dict[str, int] = {}
        self._runtime_last_api_latency_ms = 0.0
        self._runtime_max_event_lag_ms = 0.0

        # 入站事件边界：同一会话严格 FIFO，不同会话受全局预算约束并发。
        # 所有 worker 都登记，close 时必须取消并等待，不能让回调在 HTTP
        # 客户端关闭后继续写库或发消息。
        self._event_queues: dict[str, asyncio.Queue] = {}
        self._event_workers: dict[str, asyncio.Task] = {}
        self._event_tasks: set[asyncio.Task] = set()
        self._event_concurrency = asyncio.Semaphore(EVENT_HANDLER_CONCURRENCY)
        self._event_inflight = 0
        self._seen_event_ids: dict[str, float] = {}
        self._seen_event_expiry = deque()
        self._accept_events = True
        self._close_lock = asyncio.Lock()
        self._closed = False

    def bind_metrics(self, metrics) -> None:
        """绑定现有内存指标缓冲；网关不直接写 SQLite。"""
        self._metrics = metrics

    def _record_runtime_metric(self, name: str, delta: int = 1) -> None:
        self._runtime_counters[name] = self._runtime_counters.get(name, 0) + delta
        if self._metrics is not None:
            self._metrics.incr(f"gateway_{name}", delta)

    def _record_send_result(self, result: SendResult) -> None:
        self._record_runtime_metric("send_attempts")
        if result.delivered:
            self._record_runtime_metric("send_confirmed")
        elif result.ok or result.uncertain:
            self._record_runtime_metric("send_uncertain")
        else:
            self._record_runtime_metric("send_failed")

    @property
    def runtime_snapshot(self) -> dict:
        """返回不含用户标识的 O(1) 运行快照，供健康检查和控制台使用。"""
        return {
            "ws_connected": bool(self._ws_connected),
            "qq_online": bool(self._qq_online),
            "ready": bool(self.ready_to_send),
            "event_inflight": int(self._event_inflight),
            "active_scopes": len(self._event_workers),
            "heartbeat_failures": int(self._heartbeat_fail_count),
            "api_last_latency_ms": round(self._runtime_last_api_latency_ms, 1),
            "event_max_lag_ms": round(self._runtime_max_event_lag_ms, 1),
            **self._runtime_counters,
        }

    def bind_outbox_store(self, store):
        """绑定持久 store：恢复发送 outbox 与入站 inbox，不改变公开 API。"""
        self._outbox_store = store
        recovered = store.recover_send_outbox_after_restart()
        if recovered:
            logger.warning(f"📥 {recovered} 条崩溃时在途发送已标记 uncertain，不自动重放")
        inbound = store.recover_inbound_events_after_restart()
        if inbound.get("received"):
            logger.warning(
                f"📨 {inbound['received']} 条尚未开始回调的入站事件已恢复待处理"
            )
        if inbound.get("uncertain"):
            logger.warning(
                f"📨 {inbound['uncertain']} 条回调中断事件已标记 uncertain，不盲目重放"
            )

    def _enqueue_retryable_send(self, target_type: str, target_id: str,
                                message: str, result: SendResult,
                                group_id: str = "",
                                receipt_template: dict | None = None,
                                allow_enqueue: bool = True):
        if not self._outbox_store or not allow_enqueue:
            return
        if not result.retryable:
            return
        try:
            action_id = self._outbox_store.enqueue_send_outbox(
                target_type, target_id, message, group_id=group_id,
                error=result.error, receipt_template=receipt_template,
            )
            logger.warning(
                f"📥 发送已进入 outbox: action_id={action_id} "
                f"target={target_type}:{target_id}"
            )
        except Exception as e:
            logger.error(f"📥 发送 outbox 写入失败: {e}")

    async def _enqueue_retryable_send_async(
        self, target_type: str, target_id: str, message: str,
        result: SendResult, group_id: str = "",
        receipt_template: dict | None = None, allow_enqueue: bool = True,
    ):
        """异步发送路径的 outbox 写入，避免同步 SQLite 冻结事件循环。"""
        if not self._outbox_store or not allow_enqueue or not result.retryable:
            return
        try:
            action_id = await self._run_outbox_store_io(
                "enqueue_send_outbox", self._outbox_store.enqueue_send_outbox,
                target_type, target_id, message, group_id=group_id,
                error=result.error, receipt_template=receipt_template,
            )
            logger.warning(
                f"📥 发送已进入 outbox: action_id={action_id} "
                f"target={target_type}:{target_id}"
            )
        except Exception as e:
            logger.error(f"📥 发送 outbox 写入失败: {e}")

    async def _run_outbox_store_io(self, operation: str, func, *args, **kwargs):
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="📤 outbox Store 调用较慢", **kwargs,
        )

    @staticmethod
    def _validate_sticker_outbox_asset(job: dict) -> str:
        """重放前校验 v2 贴图仍指向同一文件内容；返回空串表示通过。"""
        try:
            template = json.loads(str(job.get("receipt_template") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(template, dict) or str(template.get("kind") or "") != "sticker":
            return ""
        try:
            schema_version = int(template.get("schema_version", 1))
        except (TypeError, ValueError, OverflowError):
            return "STICKER_ASSET_IDENTITY_MISSING"
        if schema_version < 2:
            return ""
        payload = template.get("identity_payload")
        if not isinstance(payload, dict):
            return "STICKER_ASSET_IDENTITY_MISSING"
        expected = str(payload.get("asset_sha256") or "").lower()
        if not expected or len(expected) != 64:
            return "STICKER_ASSET_HASH_MISSING"
        message = str(job.get("message") or "")
        match = re.search(r"\[CQ:image,[^\]]*?file=([^,\]]+)", message, flags=re.IGNORECASE)
        if not match:
            return "STICKER_ASSET_PATH_MISSING"
        file_value = match.group(1).strip()
        if file_value.startswith("file:///"):
            file_value = file_value[8:]
        elif file_value.startswith("file://"):
            file_value = file_value[7:]
        path = Path(file_value)
        try:
            if not path.is_file():
                return "STICKER_ASSET_MISSING"
            actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
        except OSError:
            return "STICKER_ASSET_UNREADABLE"
        return "" if actual == expected else "STICKER_ASSET_HASH_MISMATCH"

    @staticmethod
    def _validate_voice_outbox_asset(job: dict) -> str:
        """重放前校验统一媒体计划冻结的语音文件；旧回执保持兼容。"""
        try:
            template = json.loads(str(job.get("receipt_template") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not isinstance(template, dict) or str(template.get("kind") or "") != "voice":
            return ""
        actual = template.get("actual")
        if not isinstance(actual, dict) or actual.get("asset_frozen") is not True:
            # 历史即时语音回执没有冻结资产字段，不能因新增校验改变其重试语义。
            return ""
        expected = str(actual.get("asset_sha256") or "").lower()
        if not expected or len(expected) != 64:
            return "VOICE_ASSET_HASH_MISSING"
        message = str(job.get("message") or "")
        match = re.search(r"\[CQ:record,[^\]]*?file=([^,\]]+)", message,
                          flags=re.IGNORECASE)
        if not match:
            return "VOICE_ASSET_PATH_MISSING"
        file_value = match.group(1).strip()
        if file_value.startswith("file:///"):
            file_value = file_value[8:]
        elif file_value.startswith("file://"):
            file_value = file_value[7:]
        path = Path(file_value)
        try:
            if not path.is_file():
                return "VOICE_ASSET_MISSING"
            digest = hashlib.sha256(path.read_bytes()).hexdigest().lower()
        except OSError:
            return "VOICE_ASSET_UNREADABLE"
        return "" if digest == expected else "VOICE_ASSET_HASH_MISMATCH"

    def process_confirmed_projection_repairs(self, limit: int = 5) -> int:
        """仅修复 known-confirmed 的本地 SQLite 归账，不调用 QQ。

        ``confirmed_unaccounted`` 是平台已经返回 message_id 后，SQLite 投影
        未完成的终态。后台可以安全重试数据库投影；冲突态刻意不自动重试，
        需人工先审查 immutable 快照，避免日志刷屏或掩盖数据损坏。
        """
        store = self._outbox_store
        if not store:
            return 0
        list_repairs = getattr(store, "list_confirmed_projection_repairs", None)
        repair = getattr(store, "repair_confirmed_projection", None)
        if not callable(list_repairs) or not callable(repair):
            return 0
        processed = 0
        try:
            jobs = list_repairs(limit=max(1, min(int(limit), 100)))
        except Exception:
            logger.exception("known-confirmed 修复队列读取失败")
            return 0
        for job in jobs or ():
            outbox_id = str(job.get("action_id") or "").strip()
            if not outbox_id:
                continue
            try:
                result = repair(outbox_id)
                processed += 1
                if result == "confirmed":
                    logger.info(
                        "known-confirmed DB-only 修复完成: outbox_id=%s", outbox_id,
                    )
                elif result not in {"confirmed_duplicate", "missing"}:
                    logger.warning(
                        "known-confirmed DB-only 修复未完成: outbox_id=%s state=%s",
                        outbox_id, result,
                    )
            except Exception:
                # 修复本身失败不应结束 outbox worker；下一轮继续尝试，且从不
                # 回到 process_send_outbox/QQ 发送路径。
                logger.exception(
                    "known-confirmed DB-only 修复异常: outbox_id=%s", outbox_id,
                )
        return processed

    async def process_send_outbox(self, limit: int = 20) -> int:
        """在线后重试有限的网络失败；uncertain/dead 永不盲重放。"""
        if (not getattr(self, "_outbox_claims_enabled", True)
                or not self.ready_to_send or not self._outbox_store):
            return 0
        processed = 0
        jobs = await self._run_outbox_store_io(
            "list_due_send_outbox", self._outbox_store.list_due_send_outbox,
            limit=limit,
        )
        for job in jobs:
            claimed = await self._run_outbox_store_io(
                "claim_send_outbox", self._outbox_store.claim_send_outbox,
                job["action_id"],
            )
            if not claimed:
                continue
            processed += 1
            self._outbox_replay = True
            try:
                asset_error = await run_bounded_blocking(
                    "outbox.validate_sticker_asset",
                    self._validate_sticker_outbox_asset,
                    claimed,
                    logger=logger,
                    log_prefix="📤 outbox 媒体校验较慢",
                )
                if not asset_error:
                    asset_error = await run_bounded_blocking(
                        "outbox.validate_voice_asset",
                        self._validate_voice_outbox_asset,
                        claimed,
                        logger=logger,
                        log_prefix="📤 outbox 媒体校验较慢",
                    )
                if asset_error:
                    try:
                        _asset_kind = str(json.loads(
                            str(claimed.get("receipt_template") or "{}")
                        ).get("kind") or "")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        _asset_kind = ""
                    try:
                        await self._run_outbox_store_io(
                            "settle_send_outbox", self._outbox_store.settle_send_outbox,
                            claimed["action_id"], "failed",
                            error_code=asset_error, max_attempts=1,
                        )
                    except Exception:
                        logger.exception(
                            "媒体资产校验失败且终态无法落库: outbox_id=%s kind=%s",
                            claimed["action_id"], _asset_kind,
                        )
                    logger.error(
                        "outbox 媒体资产校验失败，禁止重放: outbox_id=%s error=%s",
                        claimed["action_id"], asset_error,
                    )
                    continue
                try:
                    if claimed["target_type"] == "group":
                        send_result = await self.send_group_message(
                            claimed["target_id"], claimed["message"],
                            _allow_outbox_enqueue=False,
                        )
                    else:
                        send_result = await self.send_private_message(
                            claimed["target_id"], claimed["message"],
                            group_id=claimed.get("group_id", ""),
                            _allow_outbox_enqueue=False,
                        )
                except Exception as exc:
                    # 只有 QQ 调用本身丢失结果才是不确定；本地 settle 异常绝不能
                    # 走到这里把已知 confirmed 改写为 uncertain。
                    try:
                        await self._run_outbox_store_io(
                            "settle_send_outbox", self._outbox_store.settle_send_outbox,
                            claimed["action_id"], "uncertain",
                            error_code="SEND_RESULT_LOST", error_detail=str(exc),
                        )
                    except Exception:
                        logger.exception(
                            "outbox 未知发送结果落库也失败: outbox_id=%s",
                            claimed["action_id"],
                        )
                    continue
                # 兼容旧适配器直接返回 bool/类 bool 对象；绝不能读取共享的
                # _last_send_result，否则上一次请求的 uncertain 会污染本次结果。
                result = send_result
                state = send_delivery_state(result) if result is not None else "failed"
                message_ids = list(getattr(result, "chunk_ids", ()) or ())
                message_id = getattr(result, "message_id", 0)
                if message_id and message_id not in message_ids:
                    message_ids.append(message_id)
                # task-linked outbox 的 confirmed 必须有 OneBot message_id
                # 证据。旧 adapter 的裸 bool 只表示“请求被接受”，不能把
                # 它写成永久成功事实；普通 legacy（未关联 task）保持兼容。
                if claimed.get("task_attempt_id") is not None and (
                        isinstance(result, bool)
                        or (state == "confirmed" and not message_ids)):
                    state = "uncertain"
                try:
                    if state == "confirmed":
                        settled = await self._run_outbox_store_io(
                            "settle_send_outbox", self._outbox_store.settle_send_outbox,
                            claimed["action_id"], "confirmed",
                            message_ids=message_ids,
                        )
                        if settled in {
                                "confirmed_unaccounted", "confirmed_conflict"}:
                            logger.error(
                                "outbox 已送达但本地归账待审查: "
                                "outbox_id=%s state=%s",
                                claimed["action_id"], settled,
                            )
                    elif state == "uncertain":
                        await self._run_outbox_store_io(
                            "settle_send_outbox", self._outbox_store.settle_send_outbox,
                            claimed["action_id"], "uncertain",
                            message_ids=message_ids,
                            error_code=(getattr(result, "error", "")
                                        or "MESSAGE_ID_UNCONFIRMED"),
                        )
                    else:
                        await self._run_outbox_store_io(
                            "settle_send_outbox", self._outbox_store.settle_send_outbox,
                            claimed["action_id"], "failed",
                            error_code=(
                                getattr(result, "error", "SEND_FAILED")
                                if result is not None else "SEND_FAILED"
                            ),
                            max_attempts=(
                                3 if getattr(result, "retryable", False) else 1
                            ),
                            retry_delay_seconds=30,
                        )
                except Exception as exc:
                    if state == "confirmed":
                        try:
                            await self._run_outbox_store_io(
                                "mark_send_outbox_confirmed_unaccounted",
                                self._outbox_store.mark_send_outbox_confirmed_unaccounted,
                                claimed["action_id"], message_ids=message_ids,
                                error=f"SETTLE_LOCAL_ERROR:{type(exc).__name__}",
                            )
                        except Exception:
                            logger.exception(
                                "known-confirmed 归账与终态冻结均失败: outbox_id=%s",
                                claimed["action_id"],
                            )
                        logger.exception(
                            "known-confirmed 本地 settle 异常，禁止再次发送: "
                            "outbox_id=%s",
                            claimed["action_id"],
                        )
                    else:
                        logger.exception(
                            "outbox 本地 settle 异常: outbox_id=%s state=%s",
                            claimed["action_id"], state,
                        )
            finally:
                self._outbox_replay = False
        return processed

    async def _outbox_loop(self):
        """持续在线消化可重试任务；只处理 pending，不触发 uncertain/dead 重放。"""
        while self._running and not self._closed:
            await asyncio.sleep(15)
            # DB-only repair 不依赖 QQ 在线；先处理已确认但本地未归账的
            # 终态，再决定是否执行 pending 的外部发送重试。
            try:
                await self._run_outbox_store_io(
                    "repair_confirmed_projection_batch",
                    self.process_confirmed_projection_repairs,
                    limit=5,
                )
            except Exception as exc:
                # 即使 repair 实现或适配器异常，也不能让发送 worker 永久退出；
                # 该轮留待下一次循环继续，且不会回退到 QQ 重发。
                logger.warning(f"known-confirmed 修复轮次异常，下轮继续: {exc}")
            if self.ready_to_send:
                try:
                    await self.process_send_outbox(limit=5)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"outbox 后台单轮异常，下轮继续: {exc}")

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    @property
    def qq_online(self) -> bool:
        """QQ 实际登录状态（由 meta_event + 心跳共同维护）"""
        return self._qq_online

    @property
    def ready_to_send(self) -> bool:
        """只有 WS 与 QQ 登录态都确认后才视为可发送。"""
        return bool(
            self._qq_online
            and (self._ws_connected or self.testing_mode)
            and not self._closed
        )

    # ---- 启动WebSocket服务端 ----

    async def start_server(self):
        """启动 WebSocket 服务器，等待 SnowLuma 连接"""
        self._running = True
        self._accept_events = True
        # 「见过连上」与「已经提醒过没连上」——心跳循环靠它们区分
        # 「掉线了」和「压根还没接上」。见 _heartbeat_loop 里的条件2。
        self._ever_connected = False
        self._never_connected_warned = False
        logger.info(f"🔗 启动反向WS服务 ws://{self.ws_host}:{self.ws_port}，等待 SnowLuma 连接...")

        async def handler(websocket):
            # 2026-08-15 整体审查安全 M3：只接受本机连接——协议无鉴权，
            # ws_host 一旦被配成 0.0.0.0 就会把事件入口暴露给局域网
            remote = websocket.remote_address
            _peer = remote[0] if remote else ""
            if _peer not in ("127.0.0.1", "::1", "localhost"):
                logger.warning(f"🚫 拒绝非本机 WebSocket 连接: {remote}")
                await websocket.close(code=1008, reason="localhost only")
                return
            self.client_ws = websocket
            self._ws_connected = True
            self._ever_connected = True
            logger.info(f"✅ SnowLuma 已连接！来自 {remote}")
            if self.on_connected:
                await self._safe_callback(self.on_connected)

            try:
                async for raw in websocket:
                    logger.debug(f"📨 [RAW] {raw[:300]}")
                    try:
                        data = json.loads(raw)
                        # SnowLuma / OneBot 可能把事件包装在数组里（messagePostFormat: array）
                        if isinstance(data, list):
                            for item in data:
                                await self._dispatch(item)
                        else:
                            await self._dispatch(data)
                    except json.JSONDecodeError:
                        logger.info(f"📨 [SKIP] 非JSON: {raw[:100]}")
                    except Exception as e:
                        logger.error(f"📨 [ERROR] 事件分发异常: {e}")
            except websockets.ConnectionClosed:
                logger.warning("⚠️ SnowLuma 断开了连接")
            finally:
                # async for 在干净关闭时会正常结束而不抛 ConnectionClosed。
                # 仅当前连接负责清理，避免旧连接晚关闭误报新连接下线。
                if self.client_ws is websocket:
                    self.client_ws = None
                    self._ws_connected = False
                    logger.warning("⚠️ SnowLuma WebSocket 已断开")
                    if self.on_disconnected:
                        await self._safe_callback(self.on_disconnected)

        try:
            self.server = await websockets.serve(
                handler, self.ws_host, self.ws_port
            )
        except OSError as e:
            # 绑定失败时不留下“正在运行且接受事件”的半初始化状态。
            # 这既让调用方可以安全清理/重试，也避免重复启动在同一进程内
            # 把后续事件误认为已接收（服务实际并未建立）。
            self._running = False
            self._accept_events = False
            self.server = None
            if "10048" in str(e) or "address already in use" in str(e).lower():
                logger.error(f"❌ 端口 {self.ws_port} 被占用，旧糖糖进程可能还在运行")
                logger.error("💡 请先关闭旧的糖糖进程，或在任务管理器中结束 python.exe，再重新启动")
            raise

        # 后台任务在 serve 成功后才启动——serve 失败时不残留
        if self._poll_friends:
            self._poll_task = asyncio.create_task(self._poll_friend_requests())
        else:
            self._poll_task = None
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        if self._outbox_store:
            self._outbox_task = asyncio.create_task(
                self._outbox_loop(), name="send_outbox_loop",
            )

        logger.info(f"🎧 反向WS服务已启动，等待 SnowLuma 连接...")

    async def _dispatch(self, data: dict):
        """按会话入队；同 scope 保序，不让回调阻塞 WebSocket 收包。"""
        if not self._accept_events:
            return

        self._record_runtime_metric("events_ingress")

        route = self._event_route(data)
        if route is None:
            return
        scope_key, handler_name = route

        now = time.monotonic()
        self._prune_seen_events(now)
        dedup_key = self._event_dedup_key(data, scope_key)
        if dedup_key and self._seen_event_ids.get(dedup_key, 0.0) > now:
            logger.info(f"♻️ 忽略重复事件: {dedup_key}")
            return

        queue = self._event_queues.get(scope_key)
        if self._event_inflight >= EVENT_TOTAL_INFLIGHT_MAX:
            self._record_runtime_metric("events_dropped_global")
            logger.error(
                f"🚫 入站事件全局队列已满，拒绝事件: scope={scope_key}, "
                f"total={self._event_inflight}"
            )
            return
        if queue is not None and queue.full():
            self._record_runtime_metric("events_dropped_scope_queue")
            logger.error(
                f"🚫 入站会话队列已满，拒绝事件: scope={scope_key}, "
                f"scope_depth={queue.qsize()}"
            )
            return
        if queue is None and len(self._event_workers) >= EVENT_ACTIVE_SCOPE_MAX:
            self._record_runtime_metric("events_dropped_scopes")
            logger.error(
                f"🚫 入站活跃会话已达上限，拒绝新会话事件: "
                f"scope={scope_key}, active={len(self._event_workers)}"
            )
            return

        durable_key = ""
        if data.get("post_type") == "message":
            durable_key = build_platform_event_key(
                str(data.get("message_type") or ""), data,
            )
        if durable_key and self._outbox_store is not None:
            try:
                status = await run_bounded_store_io(
                    "register_inbound_event",
                    self._outbox_store.register_inbound_event,
                    durable_key, str(data.get("message_type") or "message"),
                    logger=logger,
                    log_prefix="📨 inbox Store 调用较慢",
                )
            except Exception as e:
                self._record_runtime_metric("inbox_errors")
                logger.error(f"📨 入站事件登记失败，降级为进程内去重: {e}")
            else:
                if status != "received":
                    self._record_runtime_metric("events_deduped_persistent")
                    logger.info(f"♻️ 忽略持久 inbox 重复事件: status={status}")
                    return

        if queue is None:
            queue = asyncio.Queue(maxsize=EVENT_SCOPE_QUEUE_MAX)
            self._event_queues[scope_key] = queue
            worker = asyncio.create_task(
                self._event_worker(scope_key, queue),
                name=f"napcat_event:{scope_key}",
            )
            self._event_workers[scope_key] = worker
            self._event_tasks.add(worker)
            worker.add_done_callback(self._event_tasks.discard)

        queue.put_nowait(_InboundEvent(
            handler_name=handler_name,
            data=dict(data),
            received_at=now,
            event_id=new_correlation_id("evt"),
            durable_key=durable_key,
        ))
        self._event_inflight += 1
        if dedup_key:
            expires_at = now + EVENT_DEDUP_TTL_SECONDS
            while len(self._seen_event_ids) >= EVENT_DEDUP_MAX and self._seen_event_expiry:
                old_expiry, old_key = self._seen_event_expiry.popleft()
                if self._seen_event_ids.get(old_key) == old_expiry:
                    self._seen_event_ids.pop(old_key, None)
            self._seen_event_ids[dedup_key] = expires_at
            self._seen_event_expiry.append((expires_at, dedup_key))

    def _event_route(self, data: dict) -> tuple[str, str] | None:
        """返回 (会话 FIFO key, handler 方法名)。"""
        post_type = data.get("post_type", "")
        message_type = data.get("message_type", "")
        sender = data.get("sender", {}) or {}
        group_id = str(data.get("group_id") or "")
        user_id = str(data.get("user_id") or sender.get("user_id") or "")

        if post_type == "message" and message_type == "group":
            return f"group:{group_id}", "_handle_group"
        if post_type == "message" and message_type == "private":
            return f"private:{user_id}", "_handle_private"
        if post_type == "notice":
            scope = f"group:{group_id}" if group_id else f"private:{user_id}"
            return scope, "_handle_notice"
        if post_type == "request":
            scope = f"group:{group_id}" if group_id else f"private:{user_id}"
            return scope, "_handle_request"
        if post_type == "meta_event":
            return "gateway", "_handle_meta"
        return None

    @staticmethod
    def _event_dedup_key(
        data: dict, scope_key: str,
    ) -> str | None:
        """进程内与持久 inbox 共用同一 v2 键；0/缺失不参与去重。"""
        if data.get("post_type") != "message":
            return None
        key = build_platform_event_key(
            str(data.get("message_type") or ""), data,
        )
        return key or None

    def _prune_seen_events(self, now: float):
        while self._seen_event_expiry and self._seen_event_expiry[0][0] <= now:
            expires_at, key = self._seen_event_expiry.popleft()
            if self._seen_event_ids.get(key) == expires_at:
                self._seen_event_ids.pop(key, None)

    async def _event_worker(self, scope_key: str, queue: asyncio.Queue):
        """单 scope 单消费者；空闲后回收，处理时受全局并发预算约束。"""
        try:
            while self._accept_events:
                try:
                    envelope = await asyncio.wait_for(
                        queue.get(), timeout=EVENT_SCOPE_IDLE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if queue.empty():
                        return
                    continue

                try:
                    async with self._event_concurrency:
                        durable_key = envelope.durable_key
                        inbox_store = self._outbox_store
                        if durable_key and inbox_store is not None:
                            claim_and_execute = getattr(
                                inbox_store,
                                "claim_and_mark_inbound_event_executing",
                                None,
                            )
                            if callable(claim_and_execute):
                                claim_started = time.perf_counter()
                                claimed = await run_bounded_store_io(
                                    "claim_and_mark_inbound_event_executing",
                                    claim_and_execute, durable_key,
                                    logger=logger,
                                    log_prefix="📨 inbox Store 调用较慢",
                                )
                                claim_elapsed_ms = (time.perf_counter() - claim_started) * 1000
                                self._record_runtime_metric("inbox_claim_latency_samples")
                                if claim_elapsed_ms <= 5:
                                    bucket = "le_5ms"
                                elif claim_elapsed_ms <= 10:
                                    bucket = "le_10ms"
                                elif claim_elapsed_ms <= 25:
                                    bucket = "le_25ms"
                                elif claim_elapsed_ms <= 50:
                                    bucket = "le_50ms"
                                elif claim_elapsed_ms <= 100:
                                    bucket = "le_100ms"
                                else:
                                    bucket = "gt_100ms"
                                self._record_runtime_metric(
                                    f"inbox_claim_latency_{bucket}"
                                )
                                executing = bool(claimed)
                            else:
                                claimed = await run_bounded_store_io(
                                    "claim_inbound_event",
                                    inbox_store.claim_inbound_event, durable_key,
                                    logger=logger,
                                    log_prefix="📨 inbox Store 调用较慢",
                                )
                                executing = False
                            if not claimed:
                                self._record_runtime_metric("events_deduped_persistent")
                                logger.info("♻️ 入站事件已被其它 worker 领取或完成")
                                continue
                            if not executing:
                                executing = await run_bounded_store_io(
                                    "mark_inbound_event_executing",
                                    inbox_store.mark_inbound_event_executing, durable_key,
                                    logger=logger,
                                    log_prefix="📨 inbox Store 调用较慢",
                                )
                            if not executing:
                                self._record_runtime_metric("inbox_errors")
                                logger.error("📨 入站事件无法进入 executing，保留待重启恢复")
                                continue
                        lag_ms = max(0.0, (time.monotonic() - envelope.received_at) * 1000)
                        self._runtime_max_event_lag_ms = max(
                            self._runtime_max_event_lag_ms, lag_ms,
                        )
                        handler = getattr(self, envelope.handler_name)
                        with correlation_scope(envelope.event_id):
                            handled = await handler(envelope.data)
                        if handled is False:
                            raise RuntimeError("event callback failed")
                        if durable_key and inbox_store is not None:
                            completed = await run_bounded_store_io(
                                "complete_inbound_event",
                                inbox_store.complete_inbound_event, durable_key,
                                logger=logger,
                                log_prefix="📨 inbox Store 调用较慢",
                            )
                            if not completed:
                                raise RuntimeError("inbound completion not persisted")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if envelope.durable_key and self._outbox_store is not None:
                        try:
                            await run_bounded_store_io(
                                "fail_inbound_event",
                                self._outbox_store.fail_inbound_event,
                                envelope.durable_key, type(exc).__name__,
                                logger=logger,
                                log_prefix="📨 inbox Store 调用较慢",
                            )
                        except Exception:
                            self._record_runtime_metric("inbox_errors")
                            logger.exception("📨 入站失败状态持久化异常")
                    self._record_runtime_metric("callback_errors")
                    logger.exception(
                        f"📨 事件处理失败: scope={scope_key}, "
                        f"handler={envelope.handler_name}"
                    )
                finally:
                    queue.task_done()
                    self._event_inflight = max(0, self._event_inflight - 1)
        finally:
            current = asyncio.current_task()
            if (self._event_workers.get(scope_key) is current
                    and self._event_queues.get(scope_key) is queue):
                self._event_workers.pop(scope_key, None)
                self._event_queues.pop(scope_key, None)

    # ---- 群消息 ----

    async def _handle_group(self, data: dict):
        sender = data.get("sender", {})
        await self._resolve_forwards(data.get("message", ""))
        # 2026-08-16 Codex M1：结构化 mentions——不依赖 raw CQ 反推身份
        mentions = []
        _msg_arr = data.get("message", "")
        if isinstance(_msg_arr, list):
            for seg in _msg_arr:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    _d = seg.get("data", {}) or {}
                    _qq = str(_d.get("qq", ""))
                    if _qq:
                        mentions.append({
                            "qq": _qq,
                            "name": str(_d.get("name") or _d.get("nickname") or "").strip(),
                        })
        msg = {
            "type": "group",
            "group_id": str(data.get("group_id", "")),
            "user_id": str(sender.get("user_id", data.get("user_id", ""))),
            "nickname": sender.get("nickname", sender.get("card", "未知")),
            "card": sender.get("card", ""),          # 群名片（可能在群里改了名字）
            "title": sender.get("title", ""),        # 群头衔
            "role": sender.get("role", "member"),    # owner/admin/member
            "mentions": mentions,                    # 2026-08-16 Codex M1：结构化 @ 身份
            "message": self._extract_text(data.get("message", "")),
            "raw_message": str(data.get("raw_message", "")),
            "message_id": data.get("message_id", 0),
            "time": data.get("time", 0),
            "is_at_bot": self._check_at_bot(data.get("message", ""), str(self.self_id)),
        }
        # P0-1b：网关是入站事实的唯一规范化边界；保留旧 dict 字段供现有
        # handler/批处理兼容，契约对象本身不参与路由或行为判断。
        msg["_inbound_event"] = normalize_inbound_event("group", msg)
        logger.info(f"💬 [群:{msg['group_id']}] {msg['nickname']}: {msg['message'][:80]}")
        if self.on_group_message:
            return await self._safe_callback(self.on_group_message, msg)
        return True

    async def _handle_private(self, data: dict):
        sender = data.get("sender", {})
        await self._resolve_forwards(data.get("message", ""))
        msg = {
            "type": "private",
            "user_id": str(sender.get("user_id", data.get("user_id", ""))),
            "nickname": sender.get("nickname", "未知"),
            "message": self._extract_text(data.get("message", "")),
            "raw_message": str(data.get("raw_message", "")),
            "message_id": data.get("message_id", 0),
            "time": data.get("time", 0),
        }
        # 与群聊相同：私聊作用域在边界处绑定 actor，避免后续 dict 改写串到他人。
        msg["_inbound_event"] = normalize_inbound_event("private", msg)
        # 私聊日志：不记录非主人的消息内容
        if self.on_private_message:
            return await self._safe_callback(self.on_private_message, msg)
        return True

    async def _handle_notice(self, data: dict):
        notice_type = data.get("notice_type", "")
        sub_type = data.get("sub_type", "")

        if notice_type == "group_increase":
            group_id = str(data.get("group_id", ""))
            user_id = str(data.get("user_id", ""))
            logger.info(f"👋 新成员 {user_id} 加入群 {group_id}")
            if self.on_group_increase and group_id and user_id:
                await self._safe_callback(self.on_group_increase, {
                    "group_id": group_id,
                    "user_id": user_id,
                })

        elif notice_type == "notify" and sub_type == "poke":
            # 戳一戳
            target_id = str(data.get("target_id", ""))
            if target_id == str(self.self_id):
                user_id = str(data.get("user_id", ""))
                group_id = str(data.get("group_id", "0"))
                logger.info(f"👆 被 {user_id} 戳了一下！（群:{group_id}）")
                if self.on_poke:
                    await self._safe_callback(self.on_poke, {
                        "user_id": user_id,
                        "group_id": group_id,
                    })

    async def _handle_request(self, data: dict):
        """处理好友申请 / 群邀请"""
        logger.info(f"📨 [REQUEST-EVENT] {json.dumps(data, ensure_ascii=False)}")
        request_type = data.get("request_type", "")
        if request_type == "friend":
            user_id = str(data.get("user_id", ""))
            comment = data.get("comment", "")
            flag = data.get("flag", "")
            logger.info(f"📩 好友申请: {user_id} — 验证消息: {comment[:60]}")

            if self.on_friend_request:
                await self._safe_callback(self.on_friend_request, {
                    "user_id": user_id,
                    "comment": comment,
                    "flag": flag,
                })
            else:
                logger.warning("⚠️ on_friend_request 回调未设置！好友申请将被忽略")

        elif request_type == "group":
            sub_type = data.get("sub_type", "")
            if sub_type == "invite":
                group_id = str(data.get("group_id", ""))
                user_id = str(data.get("user_id", ""))
                flag = data.get("flag", "")
                logger.info(f"📩 群邀请: {user_id} 邀请糖糖加入群{group_id}")

                if self.on_group_invite:
                    await self._safe_callback(self.on_group_invite, {
                        "group_id": group_id,
                        "user_id": user_id,
                        "flag": flag,
                        "sub_type": sub_type,
                    })
                else:
                    logger.warning(f"⚠️ on_group_invite 回调未设置！群邀请 {group_id} 将被忽略")
            else:
                logger.info(f"📨 [REQUEST] 未处理的群请求子类型: {sub_type}")
        else:
            logger.info(f"📨 [REQUEST] 未处理的请求类型: {request_type}")

    async def _handle_meta(self, data: dict):
        """处理元事件：lifecycle（QQ上线/下线）、heartbeat"""
        meta_type = data.get("meta_event_type", "")
        if meta_type == "lifecycle":
            sub_type = data.get("sub_type", "")
            if sub_type == "connect":
                logger.info("🟢 QQ 已登录（lifecycle: connect）")
                was_online = self._qq_online
                self._qq_online = True
                self._heartbeat_fail_count = 0
                self._offline_since = 0.0
                if not was_online and self.on_qq_online:
                    await self._safe_callback(self.on_qq_online)
            elif sub_type == "disable":
                logger.warning("🔴 QQ 已掉线（lifecycle: disable）")
                was_online = self._qq_online
                self._qq_online = False
                if not self._offline_since:
                    self._offline_since = asyncio.get_event_loop().time()
                if was_online and self.on_qq_offline:
                    await self._safe_callback(self.on_qq_offline)
            elif sub_type == "enable":
                logger.info("🟢 QQ 已恢复（lifecycle: enable）")
                was_online = self._qq_online
                self._qq_online = True
                self._heartbeat_fail_count = 0
                self._offline_since = 0.0
                if not was_online and self.on_qq_online:
                    await self._safe_callback(self.on_qq_online)
        elif meta_type == "heartbeat":
            if not self._qq_online:
                self._qq_online = True
                self._heartbeat_fail_count = 0
                self._offline_since = 0.0
                if self.on_qq_online:
                    await self._safe_callback(self.on_qq_online)

    async def _heartbeat_loop(self):
        """双路检测 + 自动重连：
        1. meta_event（主）：SnowLuma WS 推送 QQ 上下线 → 记录 offline_since 时间戳
        2. get_login_info（辅）：HTTP 心跳兜底，连续失败也触发重启
        3. 任一条件满足 → 调 set_restart 自动重启"""
        await asyncio.sleep(10)
        while self._running:
            now = asyncio.get_event_loop().time()

            # ── HTTP 心跳 ──
            try:
                result = await self._call_api("get_login_info", {})
                if result.get("status") == "ok" and result.get("data", {}).get("user_id"):
                    if not self._qq_online and self._heartbeat_fail_count >= 3:
                        logger.info("🟢 HTTP 心跳恢复，QQ 已在线")
                        self._qq_online = True
                        self._offline_since = 0.0
                        if self.on_qq_online:
                            await self._safe_callback(self.on_qq_online)
                    self._heartbeat_fail_count = 0
                else:
                    self._heartbeat_fail_count += 1
            except Exception:
                self._heartbeat_fail_count += 1

            # HTTP 连续 3 次失败 → 标记离线
            if self._heartbeat_fail_count == 3 and self._qq_online:
                logger.warning("🔴 HTTP 心跳连续失败，QQ 可能已离线")
                self._qq_online = False
                if not self._offline_since:
                    self._offline_since = now
                if self.on_qq_offline:
                    await self._safe_callback(self.on_qq_offline)

            # ── 触发重启条件 ──
            should_restart = False
            reason = ""

            # 条件1：meta_event 记录的离线时间超过 3 分钟
            meta_offline = (now - self._offline_since) if self._offline_since else 0
            if not self._qq_online and meta_offline > 180:
                should_restart = True
                reason = f"meta_event 离线 {meta_offline:.0f}s"

            # 条件2：HTTP 心跳连续失败 10 次（5 分钟）
            #
            # ⚠ 2026-09-20：**从没连上过时不许走重启链。**
            #   新手的典型状态是「SnowLuma 面板里的 WS 客户端还没配」——那种情况下
            #   糖糖的 HTTP 心跳当然一直失败，于是每 5 分钟刷一次
            #   「🔄 尝试重启 SnowLuma」，把真病因（**压根还没接上**）讲成一桩"掉线事故"，
            #   还让控制台去做一次重启——而控制台那边杀的是不存在的 SnowLuma.exe。
            #   现在只提醒一次，然后把计数归零，等用户去把连接配好。
            if self._heartbeat_fail_count >= 10 and not self._ever_connected:
                if not self._never_connected_warned:
                    self._never_connected_warned = True
                    logger.warning(
                        f"⚠️ 糖糖还没等到 SnowLuma 连过来（HTTP 心跳连续失败 "
                        f"{self._heartbeat_fail_count} 次）。这不是掉线，是**还没接上**——"
                        f"最可能是 SnowLuma 网页面板里没建「WS 客户端」，"
                        f"或者它的目标 URL 还是默认的 ws://127.0.0.1:8080/ws"
                        f"（要改成 ws://127.0.0.1:3001）。"
                        f"在控制台点「连接自检」会告诉你缺哪一步。")
                self._heartbeat_fail_count = 0
            elif self._heartbeat_fail_count >= 10:
                should_restart = True
                reason = f"HTTP 心跳失败 {self._heartbeat_fail_count} 次"

            if should_restart:
                logger.warning(f"🔄 {reason}，尝试重启 SnowLuma...")
                restarted = await self._restart_napcat()
                if restarted:
                    logger.info("⏳ 等待 SnowLuma 完全启动（25s）...")
                    await asyncio.sleep(25)
                    self._heartbeat_fail_count = 0
                    self._offline_since = 0.0
                    # 只有重启成功才标记在线
                else:
                    logger.error("❌ SnowLuma 重启失败，请手动重启")
                    self._qq_online = False  # 保持离线状态

            await asyncio.sleep(30)

    async def _restart_napcat(self) -> bool:
        """通知控制台重启 SnowLuma。
        通过写标记文件 → 控制台定时检测 → 控制台执行重启。
        控制台已有成熟的启停逻辑，比从 main.py 杀进程可靠得多。"""
        signal_file = Path(__file__).parent.parent / ".trigger_restart_snowluma"
        try:
            await run_bounded_blocking(
                "napcat.restart_signal_write",
                signal_file.write_text,
                str(int(asyncio.get_event_loop().time())),
                encoding="utf-8",
                logger=logger,
                log_prefix="🔄 SnowLuma 重启信号写入较慢",
            )
            logger.info("📝 已发送 SnowLuma 重启信号 → 等待控制台处理...")
            return True
        except Exception as e:
            logger.error(f"写重启信号文件失败: {e}")
            return False

    async def _poll_friend_requests(self):
        """HTTP 轮询待处理的好友申请 — 弥补 OneBot 不推送 request 事件的问题"""
        # 等 WebSocket 连上后再开始轮询
        await asyncio.sleep(5)
        seen_flags = set()  # 已处理的 flag，避免重复

        while self._running:
            try:
                # 调用 OneBot11 API 获取未处理的好友申请
                result = await self._call_api("get_friend_add_request", {})
                if result.get("status") == "ok":
                    requests_data = result.get("data", [])
                    if requests_data:
                        logger.info(f"📩 [轮询] 发现 {len(requests_data)} 条待处理好友申请")
                        for req in requests_data:
                            flag = req.get("flag", "")
                            if flag in seen_flags:
                                continue
                            seen_flags.add(flag)

                            user_id = str(req.get("user_id", ""))
                            comment = req.get("comment", "")
                            logger.info(f"📩 [轮询] 好友申请: {user_id} — {comment[:60]}")

                            if self.on_friend_request:
                                await self._safe_callback(self.on_friend_request, {
                                    "user_id": user_id,
                                    "comment": comment,
                                    "flag": flag,
                                })
                    # 定期清理 seen_flags 防止无限增长
                    if len(seen_flags) > 1000:
                        seen_flags.clear()
            except Exception as e:
                logger.debug(f"📩 [轮询] 好友申请查询异常: {e}")

            await asyncio.sleep(15)  # 每15秒查一次

    async def handle_friend_request(self, flag: str, approve: bool, remark: str = ""):
        """同意/拒绝好友申请"""
        params = {
            "flag": flag,
            "approve": approve,
        }
        if remark:
            params["remark"] = remark
        logger.info(f"🔧 调用 set_friend_add_request: flag={flag[:40]}... approve={approve}")
        result = await self._call_api("set_friend_add_request", params)
        logger.info(f"🔧 set_friend_add_request 返回: {result}")
        return result.get("status") == "ok"

    # ---- 发送消息 (HTTP API) ----

    async def recall_message(self, message_id: int) -> bool:
        """撤回消息"""
        result = await self._call_api("delete_msg", {"message_id": message_id})
        return result.get("status") == "ok"

    def get_last_sent_message_id(self, target_type: str, target_id: str) -> int:
        """读取指定会话最近一条已确认消息的 ID。"""
        return int(self._last_sent_msg_ids.get(f"{target_type}:{target_id}", 0))

    def clear_last_sent_message_id(self, target_type: str, target_id: str,
                                   message_id: int) -> None:
        """条件清除会话消息 ID，避免并发发送覆盖后的误清除。"""
        key = f"{target_type}:{target_id}"
        if self._last_sent_msg_ids.get(key) == message_id:
            self._last_sent_msg_ids.pop(key, None)

    # ---- 群管理 (HTTP API) ----

    async def set_group_ban(self, group_id: str, user_id: str, duration_sec: int = 60) -> bool:
        """禁言群成员。duration_sec=0 表示解禁"""
        result = await self._call_api("set_group_ban", {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "duration": duration_sec,
        })
        return result.get("status") == "ok"

    async def set_group_kick(self, group_id: str, user_id: str, reject_add: bool = False) -> bool:
        """踢出群成员"""
        result = await self._call_api("set_group_kick", {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "reject_add_request": reject_add,
        })
        return result.get("status") == "ok"

    async def set_group_card(self, group_id: str, user_id: str, card: str) -> bool:
        """修改群名片"""
        result = await self._call_api("set_group_card", {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "card": card,
        })
        return result.get("status") == "ok"

    async def set_group_special_title(self, group_id: str, user_id: str, title: str) -> bool:
        """设置群头衔（需群主权限）"""
        result = await self._call_api("set_group_special_title", {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "special_title": title,
        })
        return result.get("status") == "ok"

    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> bool:
        """全员禁言"""
        result = await self._call_api("set_group_whole_ban", {
            "group_id": int(group_id),
            "enable": enable,
        })
        return result.get("status") == "ok"

    async def _call_api(self, action: str, params: dict) -> dict:
        """通过 HTTP 调用 OneBot API（兼容 SnowLuma / NapCat / LLOneBot 等）"""
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            # SnowLuma 要求在请求体里也传 access_token（OneBot 11 标准）
            params = {**params, "access_token": self.access_token}

        started = time.perf_counter()
        api_result = None
        try:
            resp = await self.http.post(
                f"{self.http_url}/{action}",
                headers=headers,
                json=params,
            )
            if resp.status_code == 200:
                try:
                    parsed = resp.json()
                except (TypeError, ValueError) as e:
                    # POST 已到达网关却丢失了可验证响应。服务端可能已经执行，
                    # 不能把它当作确定失败后自动重放。
                    logger.error(f"API响应无法解析: {action} -> {type(e).__name__}")
                    api_result = {
                        "status": "failed",
                        "msg": "invalid OneBot response",
                        "_transport_error": "NETWORK_UNCERTAIN",
                    }
                    return api_result
                if not isinstance(parsed, dict) or parsed.get("status") not in {"ok", "failed"}:
                    logger.error(f"API响应协议损坏: {action} -> {type(parsed).__name__}")
                    api_result = {
                        "status": "failed",
                        "msg": "invalid OneBot response shape",
                        "_transport_error": "NETWORK_UNCERTAIN",
                    }
                    return api_result
                api_result = parsed
                return api_result
            else:
                logger.error(f"API调用失败: {action} -> {resp.status_code} {resp.text[:200]}")
                # send_* 是非幂等 POST。5xx 可能发生在服务端执行之后，
                # 对发送动作必须冻结为 uncertain；读取类 API 仍可安全重试。
                if resp.status_code >= 500 and action in {
                    "send_group_msg", "send_private_msg", "send_msg",
                }:
                    error_type = "NETWORK_UNCERTAIN"
                else:
                    error_type = "HTTP_5XX" if resp.status_code >= 500 else "HTTP_4XX"
                api_result = {
                    "status": "failed",
                    "retcode": resp.status_code,
                    "msg": resp.text[:200],
                    "_transport_error": error_type,
                }
                return api_result
        except Exception as e:
            # 连接失败在启动/重启期间是正常现象，用 warning 而非 error
            if "connection" in str(e).lower() or "all connection attempts" in str(e).lower():
                logger.debug(f"API 暂时不可用: {action} -> {e}")
            else:
                logger.error(f"API调用异常: {action} -> {e}")
            # POST 写出后才丢失响应（read/write timeout、协议中断）时，
            # 服务端可能已经执行请求；它不是可安全重放的普通网络失败。
            ambiguous = isinstance(e, (
                httpx.ReadError, httpx.WriteError,
                httpx.ReadTimeout, httpx.WriteTimeout,
                httpx.RemoteProtocolError,
            ))
            error_type = (
                "NETWORK_UNCERTAIN" if ambiguous
                else "NETWORK"
                if isinstance(e, (httpx.NetworkError, httpx.TimeoutException))
                else "CLIENT_ERROR"
            )
            api_result = {
                "status": "failed",
                "msg": str(e)[:200],
                "_transport_error": error_type,
            }
            return api_result
        finally:
            self._runtime_last_api_latency_ms = (time.perf_counter() - started) * 1000
            self._record_runtime_metric("api_calls")
            if not api_result or api_result.get("status") != "ok":
                self._record_runtime_metric("api_failures")

    async def send_group_message(self, group_id: str, message: str,
                                 receipt_template: dict | None = None, *,
                                 _allow_outbox_enqueue: bool = True) -> SendResult:
        # 同一目标严格按提交顺序发送；长消息分块在同一 task 内可重入。
        send_key = f"group:{group_id}"
        current_task = asyncio.current_task()
        if self._send_lock_owners.get(send_key) is not current_task:
            lock = self._send_locks.setdefault(send_key, asyncio.Lock())
            self._send_lock_refs[send_key] = self._send_lock_refs.get(send_key, 0) + 1
            try:
                async with lock:
                    self._send_lock_owners[send_key] = current_task
                    try:
                        result = await self.send_group_message(
                            group_id, message, receipt_template=receipt_template,
                            _allow_outbox_enqueue=_allow_outbox_enqueue,
                        )
                        await self._enqueue_retryable_send_async(
                            "group", str(group_id), message, result,
                            receipt_template=receipt_template,
                            allow_enqueue=_allow_outbox_enqueue,
                        )
                        self._record_send_result(result)
                        return result
                    finally:
                        self._send_lock_owners.pop(send_key, None)
            finally:
                refs = self._send_lock_refs.get(send_key, 1) - 1
                if refs <= 0:
                    self._send_lock_refs.pop(send_key, None)
                    if (not lock.locked()
                            and self._send_locks.get(send_key) is lock):
                        self._send_locks.pop(send_key, None)
                else:
                    self._send_lock_refs[send_key] = refs
        # 注：测试模式的发送拦截已移至 handler 层（精确控制：主人放行，其他人跳过 LLM）
        # QQ消息长度限制：过长的消息分条发
        if not self.ready_to_send:
            logger.warning(f"🔴 QQ离线，跳过群{group_id}消息发送")
            retryable = not self._closed
            result = SendResult(False, False, error="OFFLINE", retryable=retryable)
            self._last_send_result = result
            return result
        if len(message) > 2000:
            chunks = _split_message_chunks(message)
            logger.warning(f"📏 消息过长({len(message)}字)，分为{len(chunks)}条发送")
            chunk_ids = []
            all_delivered = True
            for index, chunk in enumerate(chunks):
                try:
                    chunk_result = await self.send_group_message(
                        group_id, chunk, _allow_outbox_enqueue=False,
                    )
                except Exception as exc:
                    # 子请求已进入 POST 后才抛异常时，当前块可能已投递；若前面
                    # 已有确认块，还必须保留其 message_id，禁止上层整条重放。
                    result = SendResult(
                        False, False,
                        message_id=chunk_ids[-1] if chunk_ids else 0,
                        chunk_ids=tuple(chunk_ids),
                        error=(
                            "PARTIAL_DELIVERY:NETWORK_UNCERTAIN"
                            if chunk_ids else "NETWORK_UNCERTAIN"
                        ),
                        retryable=False,
                        uncertain=True,
                    )
                    self._last_send_result = result
                    logger.exception(
                        "群%s长消息第%d/%d块响应丢失: %s",
                        group_id, index + 1, len(chunks), exc,
                    )
                    return result
                if not chunk_result.ok:
                    previous = chunk_result
                    partial_or_ambiguous = index > 0 or previous.delivery_state == "uncertain"
                    result = SendResult(
                        False, False,
                        message_id=chunk_ids[-1] if chunk_ids else 0,
                        chunk_ids=tuple(chunk_ids), error=previous.error,
                        retcode=previous.retcode,
                        retryable=previous.retryable and not partial_or_ambiguous,
                        uncertain=partial_or_ambiguous,
                    )
                    self._last_send_result = result
                    return result
                if not chunk_result.delivered:
                    # API 接受但本块没有非零 message_id：继续完成其它块，
                    # 但整条长消息最终只能是 uncertain，不能被已有块 ID
                    # 误判为 confirmed。
                    all_delivered = False
                if chunk_result.message_id:
                    chunk_ids.append(chunk_result.message_id)
                if index < len(chunks) - 1:
                    await asyncio.sleep(0.5)
            result = SendResult(
                True, all_delivered,
                message_id=chunk_ids[-1] if chunk_ids else 0,
                chunk_ids=tuple(chunk_ids),
                error="" if all_delivered else "MESSAGE_ID_UNCONFIRMED",
            )
            self._last_send_result = result
            return result
        result = await self._call_api("send_group_msg", {
            "group_id": int(group_id),
            "message": message,
        })
        ok = result.get("status") == "ok"
        if ok:
            data = result.get("data")
            if not isinstance(data, dict):
                send_result = SendResult(
                    True, False, error="INVALID_SEND_RESPONSE",
                    retcode=result.get("retcode"), uncertain=True,
                )
                self._last_send_result = send_result
                logger.error(
                    "⚠️ 群%s发送响应结构异常，投递状态未知: %r", group_id, result,
                )
                return send_result
            mid = data.get("message_id", 0)
            message_id, delivered = _normalize_message_id(mid)
            send_result = SendResult(
                True, delivered, message_id=message_id,
                error="" if delivered else "MESSAGE_ID_UNCONFIRMED",
                retcode=result.get("retcode"),
            )
            if delivered:
                self.last_sent_msg_id = message_id
                self._last_sent_msg_ids[f"group:{group_id}"] = message_id
                logger.info(f"📤 群消息已发送 → 群{group_id} (msg_id={mid})")
            elif mid == 0:
                logger.warning(f"⚠️ 群{group_id}发送可疑: msg_id=0（消息可能未到达QQ）| 内容: {message[:60]!r}")
            else:
                logger.info(f"📤 群消息已发送 → 群{group_id} (msg_id={mid})")
        else:
            retcode = result.get("retcode", "?")
            err_msg = str(result.get("msg", result.get("wording", "")))[:150]
            error, retryable = _classify_send_error(result)
            send_result = SendResult(
                False, False, error=error, retcode=retcode,
                retryable=retryable,
                uncertain=error == "NETWORK_UNCERTAIN",
            )
            self._last_send_result = send_result
            logger.error(f"❌ 群{group_id}发送失败: retcode={retcode} msg={err_msg} | 完整响应: {result}")
            return send_result
        self._last_send_result = send_result
        return send_result

    # ---- 私聊 ----

    async def send_private_message(self, user_id: str, message: str,
                                   group_id: str = "",
                                   receipt_template: dict | None = None, *,
                                   _allow_outbox_enqueue: bool = True) -> SendResult:
        """发送私聊消息。优先用 send_private_msg，失败时尝试群临时会话。"""
        send_key = f"private:{user_id}"
        current_task = asyncio.current_task()
        if self._send_lock_owners.get(send_key) is not current_task:
            lock = self._send_locks.setdefault(send_key, asyncio.Lock())
            self._send_lock_refs[send_key] = self._send_lock_refs.get(send_key, 0) + 1
            try:
                async with lock:
                    self._send_lock_owners[send_key] = current_task
                    try:
                        result = await self.send_private_message(
                            user_id, message, group_id,
                            receipt_template=receipt_template,
                            _allow_outbox_enqueue=_allow_outbox_enqueue,
                        )
                        await self._enqueue_retryable_send_async(
                            "private", str(user_id), message, result,
                            group_id=str(group_id or ""),
                            receipt_template=receipt_template,
                            allow_enqueue=_allow_outbox_enqueue,
                        )
                        self._record_send_result(result)
                        return result
                    finally:
                        self._send_lock_owners.pop(send_key, None)
            finally:
                refs = self._send_lock_refs.get(send_key, 1) - 1
                if refs <= 0:
                    self._send_lock_refs.pop(send_key, None)
                    if (not lock.locked()
                            and self._send_locks.get(send_key) is lock):
                        self._send_locks.pop(send_key, None)
                else:
                    self._send_lock_refs[send_key] = refs
        if not self.ready_to_send:
            logger.warning(f"🔴 QQ离线，跳过私聊{user_id}消息发送")
            retryable = not self._closed
            send_result = SendResult(
                False, False, error="OFFLINE", retryable=retryable,
            )
            self._last_send_result = send_result
            return send_result
        result = await self._call_api("send_private_msg", {
            "user_id": int(user_id),
            "message": message,
        })
        if result.get("status") == "ok":
            data = result.get("data")
            if not isinstance(data, dict):
                send_result = SendResult(
                    True, False, error="INVALID_SEND_RESPONSE",
                    retcode=result.get("retcode"), uncertain=True,
                )
                self._last_send_result = send_result
                logger.error(
                    "⚠️ 私聊%s发送响应结构异常，投递状态未知: %r", user_id, result,
                )
                return send_result
            mid = data.get("message_id", 0)
            message_id, delivered = _normalize_message_id(mid)
            send_result = SendResult(
                True, delivered, message_id=message_id,
                error="" if delivered else "MESSAGE_ID_UNCONFIRMED",
                retcode=result.get("retcode"),
            )
            if delivered:
                self._last_sent_msg_ids[f"private:{user_id}"] = message_id
                logger.info(f"📤 私聊已发送 → {user_id} (msg_id={mid})")
            elif mid == 0:
                # message_id=0：API 返回 ok 但消息可能未真正投递
                logger.warning(f"⚠️ 私聊发送可疑 → {user_id} msg_id=0（消息可能未到达QQ）| 内容: {message[:60]!r}")
            else:
                logger.info(f"📤 私聊已发送 → {user_id} (msg_id={mid})")
            self._last_send_result = send_result
            return send_result

        # ── 失败判定（2026-08-15 整体审查）：必须在兜底之前——
        # 旧代码兜底用 send_msg 的返回覆盖了 result，冷却读到的是兜底的错误，
        # 非好友冷却永远不生效；且任何失败都换通道重发=顶着风控再冲一次。
        _retcode = result.get("retcode")
        _err_raw = str(result.get("msg", result.get("wording", "")))[:150]
        # 只有明确非好友（retcode=16 或文案含「好友」）才走临时会话；
        # 风控/拉黑/网关抖动等其他失败直接返回 False，不重发（封号风险）
        _is_no_friend = (
            _retcode == 16 or str(_retcode) == "16"
            or "好友" in _err_raw or "result=16" in str(result)
        )
        if _is_no_friend:
            # 冷却提前设置——兜底成功与否，自治插话都不该再选这个对象
            self._no_friend_until[str(user_id)] = time.time() + 6 * 3600
            if self._persist_cb:
                try:
                    self._persist_cb(dict(self._no_friend_until))
                except Exception:
                    pass
            logger.info(f"🚫 非好友冷却 6h → {user_id}（自治插话期间跳过）")
            if group_id:
                logger.info(f"📩 非好友，尝试群临时会话 → {user_id} (群{group_id})")
                fallback = await self._call_api("send_msg", {
                    "message_type": "private",
                    "user_id": int(user_id),
                    "group_id": int(group_id),
                    "message": message,
                })
                if fallback.get("status") == "ok":
                    data = fallback.get("data")
                    if not isinstance(data, dict):
                        send_result = SendResult(
                            True, False, error="INVALID_SEND_RESPONSE",
                            retcode=fallback.get("retcode"), uncertain=True,
                        )
                        self._last_send_result = send_result
                        logger.error(
                            "⚠️ 群临时会话%s响应结构异常，投递状态未知: %r",
                            user_id, fallback,
                        )
                        return send_result
                    mid = data.get("message_id", 0)
                    message_id, delivered = _normalize_message_id(mid)
                    send_result = SendResult(
                        True, delivered, message_id=message_id,
                        error="" if delivered else "MESSAGE_ID_UNCONFIRMED",
                        retcode=fallback.get("retcode"),
                    )
                    if delivered:
                        self._last_sent_msg_ids[f"private:{user_id}"] = message_id
                        logger.info(
                            f"📤 群临时会话已发送 → {user_id} (msg_id={mid})"
                        )
                    else:
                        logger.warning(
                            f"⚠️ 群临时会话发送可疑 → {user_id} msg_id=0（消息可能未到达QQ）"
                        )
                    self._last_send_result = send_result
                    return send_result
                result = fallback  # 仅用于下方日志

        # 发送失败：记录完整响应用于排查
        retcode = result.get("retcode", "?")
        err_msg = str(result.get("msg", result.get("wording", "")))[:150]
        error, retryable = _classify_send_error(result)
        send_result = SendResult(
            False, False, error=error, retcode=retcode,
            retryable=retryable,
            uncertain=error == "NETWORK_UNCERTAIN",
        )
        self._last_send_result = send_result
        logger.error(f"❌ 私信{user_id}发送失败: retcode={retcode} msg={err_msg} | 完整响应: {result}")
        return send_result

    # ---- 好友/群请求处理 ----

    async def accept_friend_request(self, flag: str, remark: str = "") -> bool:
        """同意好友申请"""
        result = await self._call_api("set_friend_add_request", {
            "flag": flag,
            "approve": True,
            "remark": remark,
        })
        ok = result.get("status") == "ok"
        if ok:
            logger.info(f"✅ 已同意好友申请 (flag={flag[:20]}...)")
        else:
            logger.warning(f"❌ 同意好友申请失败: {result}")
        return ok

    async def delete_friend(self, user_id: str) -> bool:
        """删除好友"""
        try:
            result = await self._call_api("delete_friend", {"user_id": int(user_id)})
            if result.get("status") == "ok":
                logger.info(f"🗑 已删除好友: {user_id}")
                return True
            logger.warning(f"❌ 删除好友失败 ({user_id}): {result}")
            return False
        except Exception as e:
            logger.warning(f"❌ 删除好友异常 ({user_id}): {e}")
            return False

    async def accept_group_invite(self, flag: str) -> bool:
        """同意群邀请"""
        result = await self._call_api("set_group_add_request", {
            "flag": flag,
            "sub_type": "invite",
            "approve": True,
        })
        ok = result.get("status") == "ok"
        if ok:
            logger.info(f"✅ 已同意群邀请 (flag={flag[:20]}...)")
        else:
            logger.warning(f"❌ 同意群邀请失败: {result}")
        return ok

    # ---- 工具方法 ----

    async def _resolve_forwards(self, message):
        """解析合并转发消息，通过 API 获取实际内容并注入到消息数据中。
        递归处理——包括回复（reply）段中引用的消息里的转发。"""
        if not isinstance(message, list):
            return
        for seg in message:
            if not isinstance(seg, dict):
                continue

            seg_type = seg.get("type", "")
            data = seg.get("data", {})

            if seg_type == "forward":
                fid = data.get("id", "")
                if not fid:
                    continue
                # 已有内联内容就不重复请求
                if data.get("content", ""):
                    continue
                try:
                    result = await self._call_api("get_forward_msg", {"message_id": fid})
                    if result.get("status") == "ok":
                        msgs = result.get("data", {}).get("messages", [])
                        if not msgs:
                            continue
                        lines = []
                        for m in msgs:
                            sender = (m.get("sender", {}) or {}).get("nickname", "")
                            content = self._extract_text(m.get("content", m.get("message", "")))
                            if content.strip():
                                line = f"{sender}: {content}" if sender else content
                                lines.append(line[:200])
                        if lines:
                            combined = "\n".join(lines)
                            seg["data"]["content"] = combined[:800]
                except Exception:
                    pass

            elif seg_type == "reply":
                # 🆕 递归处理引用消息中的合并转发
                quoted_msg = data.get("message") or data.get("content")
                if isinstance(quoted_msg, list):
                    await self._resolve_forwards(quoted_msg)

    def _extract_text(self, message) -> str:
        if isinstance(message, str):
            return message
        if isinstance(message, list):
            texts = []
            for seg in message:
                if isinstance(seg, dict):
                    seg_type = seg.get("type", "")
                    data = seg.get("data", {})

                    if seg_type == "text":
                        texts.append(data.get("text", ""))

                    elif seg_type == "image":
                        # 图片：附上摘要信息
                        summary = data.get("summary", "")
                        sub_type = data.get("sub_type", "")
                        if sub_type == "1":
                            texts.append("[表情]")
                        elif summary:
                            # summary 通常是 "[图片]" 或表情名
                            texts.append(f"[图片:{summary[:20]}]")
                        else:
                            texts.append("[图片]")

                    elif seg_type == "reply":
                        # 回复/引用消息：提取被引用的内容
                        # OneBot 各实现字段名不一致，逐个尝试
                        quoted = (
                            data.get("text", "") or
                            data.get("content", "") or
                            data.get("message", "") or
                            data.get("title", "")
                        )
                        if isinstance(quoted, list):
                            # 嵌套消息数组
                            quoted = self._extract_text(quoted)
                        if isinstance(quoted, dict):
                            quoted = quoted.get("text", "") or quoted.get("content", "")
                        if quoted and str(quoted).strip():
                            texts.append(f"[引用内容：{str(quoted)[:120]}]")
                        else:
                            texts.append("[回复了上面的消息]")

                    elif seg_type == "forward":
                        # 合并转发/聊天记录——内容已由 _resolve_forwards 提前拉取注入
                        content = data.get("content", "")
                        if isinstance(content, list):
                            nested = self._extract_text(content)
                            if nested:
                                texts.append(f"[转发聊天记录]\n{nested[:600]}")
                            else:
                                texts.append("[转发聊天记录]")
                        elif content and isinstance(content, str) and len(content) > 5:
                            texts.append(f"[转发聊天记录]\n{content[:600]}")
                        else:
                            texts.append("[转发聊天记录]")

                    elif seg_type == "file":
                        name = data.get("name", "")
                        fid = data.get("file_id", data.get("id", ""))
                        texts.append(f"[文件:{name[:30]}|file_id={fid}]" if name else f"[文件|file_id={fid}]")

                    elif seg_type == "at":
                        # 2026-08-16：@ 段此前被静默跳过——LLM 看到的消息里 @ 对象
                        # 消失（现场：「你认识@一个包子 是什么时候」→「你认识 是
                        # 什么时候」→ LLM 答错人）。name 优先（QQ 显示名），
                        # qq 兜底（handler 侧解析 @QQxxxx 为昵称）
                        qq = data.get("qq", "")
                        name = (data.get("name") or data.get("nickname") or "").strip()
                        if name:
                            texts.append(f"@{name}")
                        elif qq:
                            texts.append(f"@QQ{qq}")

                    elif seg_type == "video":
                        texts.append("[视频]")

                    elif seg_type == "record":
                        texts.append("[语音]")

                    # 其他类型静默跳过
                elif isinstance(seg, str):
                    texts.append(seg)
            return "".join(texts)
        return str(message)

    def _check_at_bot(self, message, self_id: str) -> bool:
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    if seg.get("data", {}).get("qq") == str(self_id):
                        return True
        if isinstance(message, str):
            return f"[CQ:at,qq={self_id}]" in message
        return False

    async def _safe_callback(self, callback, *args):
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(*args)
            else:
                callback(*args)
            return True
        except Exception as e:
            logger.error(f"回调执行出错：{e}")
            return False

    async def close(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._running = False
            self._accept_events = False
            self._ws_connected = False

            server = self.server
            self.server = None
            if server:
                server.close()

            current = asyncio.current_task()
            background_tasks = [
                task for task in (
                    self._poll_task, self._heartbeat_task, self._outbox_task,
                )
                if task is not None and task is not current
            ]
            event_tasks = [
                task for task in self._event_tasks if task is not current
            ]
            for task in background_tasks + event_tasks:
                task.cancel()
            if background_tasks or event_tasks:
                await asyncio.gather(
                    *background_tasks, *event_tasks, return_exceptions=True,
                )
            self._poll_task = None
            self._heartbeat_task = None
            self._outbox_task = None
            self._event_tasks.clear()
            self._event_workers.clear()
            self._event_queues.clear()
            self._event_inflight = 0
            self._seen_event_ids.clear()
            self._seen_event_expiry.clear()

            if server:
                await server.wait_closed()
            http = self._http
            self._http = None
            if http:
                await http.aclose()

    # ---- 消息查询 ----

    async def get_msg(self, message_id: int) -> dict | None:
        """获取单条消息的详细内容"""
        result = await self._call_api("get_msg", {"message_id": message_id})
        if result.get("status") == "ok":
            data = result.get("data", {})
            # 2026-08-15 实测：NapCat 私聊消息的 user_id/nickname 在 data 顶层（无 sender 字典），
            # 群聊消息包在 sender 里——两种形状都读，否则发送者永远是空串。
            sender = data.get("sender", {}) or {}
            return {
                "message_id": data.get("message_id", message_id),
                "group_id": str(data.get("group_id") or ""),
                "sender_qq": str(sender.get("user_id") or data.get("user_id") or ""),
                "sender_nickname": str(sender.get("nickname") or data.get("nickname") or ""),
                "sender_card": str(sender.get("card") or data.get("card") or ""),
                "content": self._extract_text(data.get("message", data.get("content", ""))),
                "time": data.get("time", 0),
            }
        return None

    async def get_essence_msg_list(self, group_id: str) -> list[dict]:
        """获取群精华消息列表"""
        result = await self._call_api("get_essence_msg_list", {"group_id": int(group_id)})
        if result.get("status") == "ok":
            messages = result.get("data", [])
            summaries = []
            for m in messages[:10]:
                # SnowLuma 字段: sender_nick / sender_time / msg_content[{text}]
                nick = str(m.get("sender_nick", ""))
                content_parts = []
                for seg in (m.get("msg_content") or []):
                    if isinstance(seg, dict):
                        content_parts.append(seg.get("text", ""))
                content = " ".join(content_parts)
                summaries.append({
                    "sender_nickname": nick,
                    "sender_card": nick,
                    "content": content[:200],
                    "time": m.get("sender_time", 0),
                })
            return summaries
        return []

    async def get_group_notice(self, group_id: str) -> list[str]:
        """获取群公告列表（全部）"""
        try:
            result = await self._call_api("_get_group_notice", {"group_id": int(group_id)})
            if result.get("status") == "ok":
                data = result.get("data", [])
                notices = []
                for item in (data if isinstance(data, list) else [data]):
                    if isinstance(item, dict):
                        # OneBot 公告格式: {"message": {"text": "..."}, ...}
                        msg = item.get("message", {})
                        text = msg.get("text", "") if isinstance(msg, dict) else str(msg)
                        if text:
                            notices.append(text[:500])
                    elif isinstance(item, str) and item:
                        notices.append(item[:500])
                return notices
        except Exception:
            pass
        return []

    # ---- 群信息 ----

    async def get_group_list(self) -> list[dict]:
        """获取所有群列表"""
        result = await self._call_api("get_group_list", {})
        if result.get("status") == "ok":
            return result.get("data", [])
        return []

    async def get_group_info(self, group_id: str) -> dict | None:
        """获取群详细信息：群名、人数"""
        result = await self._call_api("get_group_info", {"group_id": int(group_id)})
        if result.get("status") == "ok":
            data = result.get("data", {})
            return {
                "group_name": str(data.get("group_name", "")),
                "member_count": int(data.get("member_count", 0)),
                "max_members": int(data.get("max_member_count", data.get("max_members", 0))),
            }
        return None

    # ---- 群成员信息 ----

    async def get_group_member_info(self, group_id: str, user_id: str) -> dict | None:
        """获取群成员详细信息：群名片、头衔、入群时间、最后发言"""
        result = await self._call_api("get_group_member_info", {
            "group_id": int(group_id),
            "user_id": int(user_id),
        })
        if result.get("status") == "ok":
            data = result.get("data", {})
            return {
                "nickname": str(data.get("nickname", "")),
                "card": str(data.get("card", "")),
                "role": str(data.get("role", "member")),
                "title": str(data.get("title", "")),
                "join_time": int(data.get("join_time", 0)),
                "last_sent_time": int(data.get("last_sent_time", 0)),
            }
        return None

    # ---- 陌生人信息 ----

    async def get_stranger_info(self, user_id: str) -> dict | None:
        """获取陌生人信息（非好友也可以查）。
        返回 {nickname, sex, age, qid} 或 None。"""
        result = await self._call_api("get_stranger_info", {"user_id": int(user_id)})
        if result.get("status") == "ok":
            data = result.get("data", {})
            return {
                "nickname": str(data.get("nickname", "")),
                "sex": str(data.get("sex", "")),
                "age": int(data.get("age", 0)),
                "qid": str(data.get("qid", "")),  # QQ 自定义 ID（类似微信号）
            }
        return None

    # ---- 文件消息 ----

    async def download_file(self, file_id: str, save_dir: str = "./temp_files",
                             original_name: str = "") -> str | None:
        """下载群文件/私聊文件，保存到本地。返回文件路径或 None。"""
        import os, shutil
        from urllib.parse import unquote
        os.makedirs(save_dir, exist_ok=True)

        result = await self._call_api("get_file", {"file_id": file_id})
        if result.get("status") != "ok":
            logger.error(f"❌ 获取文件信息失败: {result}")
            return None

        data = result.get("data", {})
        # 优先用 QQ 消息里的原始文件名，其次 API 返回的 file_name/name
        file_name = original_name or data.get("file_name", "") or data.get("name", "") or f"file_{file_id}"
        file_name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
        if not file_name or file_name == file_id:
            file_name = f"file_{file_id[:8]}"
        file_url = data.get("url", "")
        base64_data = data.get("base64", "")

        # OneBot 有时返回相对路径，需要补上 base URL
        if file_url and not file_url.startswith(("http://", "https://")):
            file_url = self.http_url.rstrip("/") + "/" + file_url.lstrip("/")

        save_path = os.path.join(save_dir, file_name)

        try:
            # 1) base64 直接写盘
            if base64_data:
                await run_bounded_blocking(
                    "napcat.download_file_base64_write",
                    Path(save_path).write_bytes,
                    base64.b64decode(base64_data),
                    logger=logger,
                    log_prefix="📁 文件 base64 落盘较慢",
                )
                logger.info(f"📁 文件已保存(base64): {save_path}")
                return save_path

            # 2) 尝试 HTTP 下载
            if file_url:
                # OneBot 返回的 url 可能是「本机 HTTP 前缀 + 绝对路径」的混搭格式
                resp = await self.http.get(file_url, timeout=60.0)
                if resp.status_code == 200:
                    await run_bounded_blocking(
                        "napcat.download_file_http_write",
                        Path(save_path).write_bytes,
                        resp.content,
                        logger=logger,
                        log_prefix="📁 文件 HTTP 落盘较慢",
                    )
                    logger.info(f"📁 文件已保存(HTTP): {save_path} ({len(resp.content)} bytes)")
                    return save_path

                # HTTP 失败 → 尝试把 URL 中的本地路径提取出来直接读磁盘
                # 格式形如 http://127.0.0.1:3000/<盘符>:/<用户目录>/.../xxx.docx
                # （不写具体用户名——这行曾经带着操作者的真名一起发布出去）
                raw_path = file_url
                if "/" in raw_path:
                    path_part = raw_path.split("/", 3)[-1] if raw_path.startswith("http") else raw_path
                    decoded = unquote(path_part)
                    if os.path.exists(decoded):
                        # 用真实文件名（从本地路径提取），覆盖 UUID 假名
                        real_name = decoded.replace("\\", "/").rsplit("/", 1)[-1]
                        real_path = os.path.join(save_dir, real_name)
                        await run_bounded_blocking(
                            "napcat.download_file_local_copy",
                            shutil.copy,
                            decoded,
                            real_path,
                            logger=logger,
                            log_prefix="📁 文件本地复制较慢",
                        )
                        logger.info(f"📁 文件已复制(本地): {real_path}")
                        return real_path

            # 3) data.file 兜底
            local_path = data.get("file", "")
            if local_path and Path(local_path).exists():
                real_name = local_path.replace("\\", "/").rsplit("/", 1)[-1]
                real_path = os.path.join(save_dir, real_name) if real_name != file_name else save_path
                await run_bounded_blocking(
                    "napcat.download_file_data_copy",
                    shutil.copy,
                    local_path,
                    real_path,
                    logger=logger,
                    log_prefix="📁 文件 data.file 复制较慢",
                )
                logger.info(f"📁 文件已复制(data.file): {real_path}")
                return real_path

        except Exception as e:
            logger.error(f"❌ 下载文件异常: {e}")
        return None

    # ---- 语音消息 ----

    async def download_record(self, file_id: str, save_path: str) -> bool:
        """下载语音消息文件（AMR/SILK 格式），保存到本地"""
        result = await self._call_api("get_record", {"file": file_id})
        if result.get("status") != "ok":
            logger.error(f"❌ 下载语音失败: {result}")
            return False
        file_url = result.get("data", {}).get("file", "")
        if not file_url:
            # OneBot 可能直接返回 base64 或文件路径
            base64_data = result.get("data", {}).get("data", "")
            if base64_data:
                await run_bounded_blocking(
                    "napcat.download_record_base64_write",
                    Path(save_path).write_bytes,
                    base64.b64decode(base64_data),
                    logger=logger,
                    log_prefix="🎤 语音 base64 落盘较慢",
                )
                return True
            return False
        try:
            resp = await self.http.get(file_url, timeout=30.0)
            if resp.status_code == 200:
                await run_bounded_blocking(
                    "napcat.download_record_http_write",
                    Path(save_path).write_bytes,
                    resp.content,
                    logger=logger,
                    log_prefix="🎤 语音 HTTP 落盘较慢",
                )
                return True
        except Exception as e:
            logger.error(f"❌ 下载语音文件异常: {e}")
        return False
