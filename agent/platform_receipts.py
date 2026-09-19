"""受信平台回执契约（只做验证，不改变发送状态）。

OneBot 的入站 event_key 只服务于入站去重，不能拿来关联糖糖自己的出站动作。
本模块为未来 WS API echo / 适配器回调定义独立的、默认 fail-closed 的信封：

* ``transport_request_id`` 必须在发送前持久化，不能用 message_id 反推动作；
* ``event_key`` 使用 ``out:v1:`` 命名空间，与入站 ``v2:`` 永不相撞；
* receipt 必须带精确的 outbox、self、channel、target、正文哈希和合法 message_id；
* ``message_sent``/synthetic 事件默认不可自动确认，除非显式开启 canary 选项。

这里不调用 Store，也不负责改变 outbox 状态；适配器完成验证后才可把 evidence
交给 Store 的私有 capability 入口，避免原始平台帧越权写入永久事实。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any


PLATFORM_RECEIPT_SCHEMA_VERSION = 1
PLATFORM_RECEIPT_EVENT_PREFIX = "out:v1:"
PLATFORM_RECEIPT_SOURCES = frozenset({
    "onebot_ws_api",
    "onebot_message_sent",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PlatformReceiptError(ValueError):
    """平台回执不满足契约；调用方必须保持原状态，不得重发。"""


def _normalize_message_ids(values: Any) -> tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)) or values is None:
        return ()
    try:
        items = list(values)
    except TypeError:
        return ()
    result: list[int] = []
    for raw in items:
        if (isinstance(raw, bool) or not isinstance(raw, int)
                or raw == 0 or raw < -(2**31) or raw > 2**31 - 1):
            return ()
        if raw in result:
            return ()
        result.append(raw)
    return tuple(result)


def build_outgoing_receipt_event_key(*, adapter: str,
                                     transport_request_id: str) -> str:
    """生成不泄漏正文/目标的稳定出站事件键。"""
    adapter = str(adapter or "").strip()
    request_id = str(transport_request_id or "").strip()
    if not adapter or not request_id:
        raise PlatformReceiptError(
            "outgoing receipt event key requires adapter and transport_request_id"
        )
    canonical = json.dumps({
        "schema_version": PLATFORM_RECEIPT_SCHEMA_VERSION,
        "adapter": adapter,
        "transport_request_id": request_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{PLATFORM_RECEIPT_EVENT_PREFIX}{digest}"


@dataclass(frozen=True)
class PlatformReceipt:
    """已通过结构校验的出站回执；仍需适配器/Store 做身份比对。"""

    event_key: str
    source: str
    adapter: str
    transport_request_id: str
    outbox_id: str
    self_id: str
    channel: str
    target_id: str
    payload_sha256: str
    message_ids: tuple[int, ...]
    observed_at: float
    synthetic: bool = False
    schema_version: int = PLATFORM_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLATFORM_RECEIPT_SCHEMA_VERSION:
            raise PlatformReceiptError("unsupported platform receipt schema_version")
        if self.source not in PLATFORM_RECEIPT_SOURCES:
            raise PlatformReceiptError("unsupported platform receipt source")
        for field_name in (
            "event_key", "adapter", "transport_request_id", "outbox_id",
            "self_id", "target_id",
        ):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name).strip():
                raise PlatformReceiptError(f"platform receipt requires {field_name}")
        if self.channel not in {"group", "private"}:
            raise PlatformReceiptError("platform receipt channel must be group/private")
        if (not isinstance(self.payload_sha256, str)
                or not _SHA256_RE.fullmatch(self.payload_sha256)):
            raise PlatformReceiptError("platform receipt requires lowercase payload_sha256")
        if not isinstance(self.synthetic, bool):
            raise PlatformReceiptError("platform receipt synthetic must be boolean")
        if (isinstance(self.observed_at, bool)
                or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(float(self.observed_at))
                or float(self.observed_at) < 0):
            raise PlatformReceiptError("platform receipt observed_at must be finite")
        normalized = _normalize_message_ids(self.message_ids)
        if not normalized or normalized != tuple(self.message_ids):
            raise PlatformReceiptError("platform receipt requires valid unique message_ids")
        expected_key = build_outgoing_receipt_event_key(
            adapter=self.adapter,
            transport_request_id=self.transport_request_id,
        )
        if self.event_key != expected_key:
            raise PlatformReceiptError("platform receipt event_key mismatch")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlatformReceipt":
        if not isinstance(value, Mapping):
            raise PlatformReceiptError("platform receipt must be a mapping")
        allowed = {
            "event_key", "source", "adapter", "transport_request_id", "outbox_id",
            "self_id", "channel", "target_id", "payload_sha256", "message_ids",
            "observed_at", "synthetic", "schema_version",
        }
        unknown = set(value) - allowed
        if unknown:
            raise PlatformReceiptError(
                f"unknown platform receipt fields: {sorted(unknown)}"
            )
        try:
            return cls(
                event_key=str(value.get("event_key") or ""),
                source=str(value.get("source") or ""),
                adapter=str(value.get("adapter") or ""),
                transport_request_id=str(value.get("transport_request_id") or ""),
                outbox_id=str(value.get("outbox_id") or ""),
                self_id=str(value.get("self_id") or ""),
                channel=str(value.get("channel") or ""),
                target_id=str(value.get("target_id") or ""),
                payload_sha256=str(value.get("payload_sha256") or ""),
                message_ids=_normalize_message_ids(value.get("message_ids")),
                observed_at=value.get("observed_at", 0),
                synthetic=value.get("synthetic", False),
                schema_version=value.get(
                    "schema_version", PLATFORM_RECEIPT_SCHEMA_VERSION,
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, PlatformReceiptError):
                raise
            raise PlatformReceiptError(str(exc)) from exc

    def validate_context(self, *, expected_self_id: str, now: float | None = None,
                         max_age_seconds: float = 300.0,
                         allow_synthetic: bool = False) -> "PlatformReceipt":
        """验证运行时上下文；失败时调用方必须 fail-closed。"""
        if str(expected_self_id or "").strip() != self.self_id:
            raise PlatformReceiptError("platform receipt self_id mismatch")
        if (isinstance(max_age_seconds, bool)
                or not isinstance(max_age_seconds, (int, float))
                or not math.isfinite(float(max_age_seconds))
                or float(max_age_seconds) < 0):
            raise PlatformReceiptError("max_age_seconds must be finite and non-negative")
        if self.synthetic and not allow_synthetic:
            raise PlatformReceiptError(
                "synthetic message_sent receipt is not auto-confirmable"
            )
        current = time.time() if now is None else float(now)
        if not math.isfinite(current) or abs(current - float(self.observed_at)) > float(max_age_seconds):
            raise PlatformReceiptError("platform receipt is outside the allowed time window")
        return self

    def to_evidence(self) -> dict[str, Any]:
        """转换为 Store evidence；message_ids 单独作为事实参数传递。"""
        return {
            "event_key": self.event_key,
            "source": self.source,
            "adapter": self.adapter,
            "transport_request_id": self.transport_request_id,
            "outbox_id": self.outbox_id,
            "self_id": self.self_id,
            "channel": self.channel,
            "target_id": self.target_id,
            "payload_sha256": self.payload_sha256,
            "observed_at": float(self.observed_at),
            "synthetic": self.synthetic,
            "schema_version": self.schema_version,
        }


def validate_platform_receipt(value: Mapping[str, Any], *,
                              expected_self_id: str, now: float | None = None,
                              max_age_seconds: float = 300.0,
                              allow_synthetic: bool = False) -> PlatformReceipt:
    """解析并验证平台回执的便捷入口。"""
    receipt = PlatformReceipt.from_mapping(value)
    return receipt.validate_context(
        expected_self_id=expected_self_id,
        now=now,
        max_age_seconds=max_age_seconds,
        allow_synthetic=allow_synthetic,
    )
