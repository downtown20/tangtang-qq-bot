"""入站消息的持久证据契约。

QQ 适配、批处理和 handler 只能从这里生成 ``event_key/raw/segments/time``，
避免同一事件在不同路径形成不同幂等语义。该模块不做行为判断，只规范化事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from .interaction_contract import InboundEvent


@dataclass(frozen=True)
class InboundEvidence:
    event_key: str
    raw_message: str
    segments: str
    timestamp: str


@dataclass(frozen=True)
class PersistInboundResult:
    chat_id: int | None
    duplicate: bool
    evidence: InboundEvidence


def _message_id(msg: dict) -> int:
    raw = msg.get("message_id")
    if isinstance(raw, bool):
        return 0
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return value if value else 0


def build_platform_event_key(message_type: str, msg: dict) -> str:
    """为 OneBot 原始事件和规范化 msg 生成相同的持久 inbox 键。"""
    kind = "group" if message_type == "group" else "private"
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    actor = str(msg.get("user_id") or sender.get("user_id") or "")
    scope = str(msg.get("group_id") if kind == "group" else actor or "")
    message_id = _message_id(msg)
    try:
        event_time = int(msg.get("time") or 0)
    except (TypeError, ValueError):
        event_time = 0
    if not scope or not message_id:
        return ""
    # message_id 保持末段，兼容现有 history query 的解析方式。
    return f"v2:{kind}:{scope}:{event_time}:{actor}:{message_id}"


def serialize_segments(msg: dict) -> str:
    """保留上游 typed segments；缺失时把 CQ 码拆为不可丢失的原子段。"""
    segments = msg.get("segments")
    if isinstance(segments, list):
        return json.dumps(segments, ensure_ascii=False)
    raw = str(msg.get("raw_message") or "")
    if not raw:
        return ""
    parts: list[dict[str, str]] = []
    pos = 0
    for cq in re.finditer(r"\[CQ:[^\]]*\]", raw):
        if cq.start() > pos:
            parts.append({"type": "text", "content": raw[pos:cq.start()]})
        parts.append({"type": "cq", "content": cq.group()})
        pos = cq.end()
    if pos < len(raw):
        parts.append({"type": "text", "content": raw[pos:]})
    return json.dumps(parts, ensure_ascii=False) if parts else ""


def build_inbound_evidence(message_type: str, msg: dict) -> InboundEvidence:
    """生成稳定证据；同一平台事件重放得到同一键，不同时间复用 ID 不冲突。"""
    try:
        event_time = int(msg.get("time") or 0)
    except (TypeError, ValueError):
        event_time = 0
    # event_time/actor 防止 OneBot int32 ID 在长期运行中复用时吞掉新消息。
    event_key = build_platform_event_key(message_type, msg)
    timestamp = ""
    if event_time:
        try:
            timestamp = datetime.fromtimestamp(event_time).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            timestamp = ""
    return InboundEvidence(
        event_key=event_key,
        raw_message=str(msg.get("raw_message") or ""),
        segments=serialize_segments(msg),
        timestamp=timestamp,
    )


def normalize_inbound_event(message_type: str, msg: dict) -> InboundEvent:
    """把平台 dict 适配为统一入站事实；不做业务路由或行为判断。"""
    kind = str(message_type or "").strip()
    if kind not in {"group", "private"}:
        raise ValueError("message_type must be group or private")
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    actor_id = str(msg.get("user_id") or sender.get("user_id") or "").strip()
    target = str(msg.get("group_id") or "").strip() if kind == "group" else actor_id
    if not actor_id or not target:
        raise ValueError("inbound event requires actor and scope target")
    evidence = build_inbound_evidence(kind, msg)
    raw = evidence.raw_message
    if evidence.event_key:
        event_id = evidence.event_key
    else:
        # 缺少平台 message_id 时仍需一个可观测的运行身份，但不把它当持久去重键。
        material = "|".join((kind, target, actor_id, str(msg.get("time") or 0), raw))
        event_id = "ephemeral:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return InboundEvent(
        event_id=event_id,
        event_key=evidence.event_key or event_id,
        channel=kind,
        scope_id=f"{kind}:{target}",
        actor_id=actor_id,
        raw_message=raw,
        segments=evidence.segments,
        message_id=_message_id(msg) or None,
        received_at=evidence.timestamp,
        source_id="onebot",
    )


def persist_inbound_message(memory, message_type: str, msg: dict,
                            message: str) -> PersistInboundResult:
    """逐条保存真实入站消息；非空事件键冲突只表示平台事件重放。"""
    event = normalize_inbound_event(message_type, msg)
    evidence = InboundEvidence(
        event_key=event.event_key if event.event_key.startswith("v2:") else "",
        raw_message=event.raw_message,
        segments=event.segments,
        timestamp=event.received_at,
    )
    group_id = event.scope_id.split(":", 1)[1] if event.channel == "group" else ""
    chat_id = memory.log_chat(
        event.actor_id,
        str(message or ""),
        group_id=group_id,
        is_bot=False,
        timestamp=evidence.timestamp,
        raw_message=evidence.raw_message,
        segments=evidence.segments,
        event_key=evidence.event_key,
    )
    return PersistInboundResult(
        chat_id=chat_id,
        duplicate=bool(evidence.event_key and chat_id is None),
        evidence=evidence,
    )


def inbound_message_already_persisted(memory, message_type: str,
                                      msg: dict) -> bool:
    """在任何业务副作用前识别跨重启或 inbox 降级窗口中的旧重投。"""
    event_key = build_platform_event_key(message_type, msg)
    return bool(event_key and memory.store.has_chat_event(event_key))
