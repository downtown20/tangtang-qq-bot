"""ADR-001 的纯数据契约；不包含行为判断、发送或重试逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


ACTION_KINDS = frozenset({"text", "voice", "sticker", "image", "sing"})
ACTION_CHANNELS = frozenset({"group", "private"})
ACTION_STATUSES = frozenset({"confirmed", "uncertain", "failed", "draft"})
ACTION_SCHEMA_VERSION = 1
ACTION_IDENTITY_VERSION = 1


def _json_copy(value: Any) -> Any:
    """契约只接受 JSON 数据，并隔离调用方后续原地修改。"""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _nonempty_text(payload: Mapping[str, Any], key: str) -> bool:
    return isinstance(payload.get(key), str) and bool(payload[key].strip())


def _validate_payload(kind: str, payload: Mapping[str, Any], *,
                      schema_version: int = ACTION_SCHEMA_VERSION) -> None:
    """在动作入口验证 typed payload；actual 执行结果不走这套请求校验。"""
    if kind == "text":
        if not _nonempty_text(payload, "text"):
            raise ValueError("text payload requires non-empty text")
    elif kind == "voice":
        for key in ("text", "emotion", "pause"):
            if not _nonempty_text(payload, key):
                raise ValueError(f"voice payload requires non-empty {key}")
        speed = payload.get("speed")
        if (isinstance(speed, bool) or not isinstance(speed, (int, float))
                or not math.isfinite(float(speed)) or float(speed) <= 0):
            raise ValueError("voice payload requires positive finite speed")
    elif kind == "sticker":
        if not _nonempty_text(payload, "emotion"):
            raise ValueError("sticker payload requires non-empty emotion")
        count = payload.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
            raise ValueError("sticker payload count must be an integer from 1 to 20")
        if schema_version >= 2:
            if not _nonempty_text(payload, "asset_ref"):
                raise ValueError("v2 sticker payload requires non-empty asset_ref")
            if not _nonempty_text(payload, "role_id"):
                raise ValueError("v2 sticker payload requires non-empty role_id")
            if not _nonempty_text(payload, "library_id"):
                raise ValueError("v2 sticker payload requires non-empty library_id")
            asset_valid = payload.get("asset_valid")
            if not isinstance(asset_valid, bool):
                raise ValueError("v2 sticker payload requires boolean asset_valid")
            asset_sha256 = payload.get("asset_sha256")
            if not isinstance(asset_sha256, str):
                raise ValueError("v2 sticker payload requires string asset_sha256")
            if asset_sha256 and (
                    len(asset_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in asset_sha256.lower())
            ):
                raise ValueError("v2 sticker payload asset_sha256 must be lowercase hex")
            if asset_valid and len(asset_sha256) != 64:
                raise ValueError("valid v2 sticker asset requires sha256")
    elif kind == "image":
        if not (_nonempty_text(payload, "asset_ref") or _nonempty_text(payload, "url")):
            raise ValueError("image payload requires asset_ref or url")
        if schema_version >= 2:
            if not _nonempty_text(payload, "asset_ref"):
                raise ValueError("v2 image payload requires non-empty asset_ref")
            if not _nonempty_text(payload, "library_id"):
                raise ValueError("v2 image payload requires non-empty library_id")
            asset_valid = payload.get("asset_valid")
            if not isinstance(asset_valid, bool):
                raise ValueError("v2 image payload requires boolean asset_valid")
            asset_sha256 = payload.get("asset_sha256")
            if not isinstance(asset_sha256, str):
                raise ValueError("v2 image payload requires string asset_sha256")
            if asset_sha256 and (
                    len(asset_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in asset_sha256.lower())
            ):
                raise ValueError("v2 image payload asset_sha256 must be lowercase hex")
            if asset_valid and len(asset_sha256) != 64:
                raise ValueError("valid v2 image asset requires sha256")
    elif kind == "sing":
        if not (_nonempty_text(payload, "song_id") or _nonempty_text(payload, "title")):
            raise ValueError("sing payload requires song_id or title")


def _validate_trace_meta(schema_version: int, ordinal: int) -> None:
    if (isinstance(schema_version, bool) or not isinstance(schema_version, int)
            or schema_version < 1):
        raise ValueError("schema_version must be a positive integer")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise ValueError("ordinal must be a non-negative integer")


def _validate_identity_version(identity_version: int) -> None:
    if (isinstance(identity_version, bool) or not isinstance(identity_version, int)
            or identity_version != ACTION_IDENTITY_VERSION):
        raise ValueError("identity_version is frozen at 1")


@dataclass(frozen=True)
class ConversationRef:
    """动作对应的对话分区；transport target 不得代替 conversation actor。"""

    projection_kind: str = "none"
    conversation_user_id: str = ""
    group_id: str = ""
    source_chat_id: int | None = None
    self_memory_eligible: bool = False

    def __post_init__(self) -> None:
        kind = str(self.projection_kind or "").strip()
        user_id = str(self.conversation_user_id or "").strip()
        group_id = str(self.group_id or "").strip()
        source_chat_id = self.source_chat_id
        if kind not in {"conversation_reply", "none"}:
            raise ValueError(f"unsupported conversation projection kind: {kind}")
        if not isinstance(self.self_memory_eligible, bool):
            raise ValueError("self_memory_eligible must be a boolean")
        if source_chat_id is not None:
            if (isinstance(source_chat_id, bool) or not isinstance(source_chat_id, int)
                    or source_chat_id <= 0):
                raise ValueError("source_chat_id must be None or a positive integer")
        if kind == "none":
            if user_id or group_id or source_chat_id is not None or self.self_memory_eligible:
                raise ValueError("projection_kind=none cannot carry conversation ownership")
        elif not user_id:
            raise ValueError("conversation_reply requires conversation_user_id")
        object.__setattr__(self, "projection_kind", kind)
        object.__setattr__(self, "conversation_user_id", user_id)
        object.__setattr__(self, "group_id", group_id)

    @classmethod
    def from_value(cls, value: Any) -> "ConversationRef":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("conversation_ref must be ConversationRef or mapping")
        unknown = set(value) - {
            "projection_kind", "conversation_user_id", "group_id",
            "source_chat_id", "self_memory_eligible",
        }
        if unknown:
            raise ValueError(f"unknown conversation_ref fields: {sorted(unknown)}")
        return cls(
            projection_kind=value.get("projection_kind", "none"),
            conversation_user_id=value.get("conversation_user_id", ""),
            group_id=value.get("group_id", ""),
            source_chat_id=value.get("source_chat_id"),
            self_memory_eligible=value.get("self_memory_eligible", False),
        )

    def validate_for_action(self, channel: str, target: str,
                            scope_id: str = "") -> None:
        """校验对话归账身份与 transport channel/target/scope 完全一致。"""
        channel = str(channel or "")
        target = str(target or "")
        scope_id = str(scope_id or "")
        if channel == "group" and scope_id != target:
            raise ValueError("group action requires scope_id == target")
        if channel == "private" and scope_id != f"_private_{target}":
            raise ValueError("private action requires scope_id == _private_target")
        if self.projection_kind == "none":
            return
        if channel == "group":
            if not self.group_id or self.group_id != str(target):
                raise ValueError("group conversation_ref requires group_id == target")
        elif channel == "private":
            if self.group_id or self.conversation_user_id != str(target):
                raise ValueError(
                    "private conversation_ref requires user == target and empty group_id"
                )
        else:
            raise ValueError("conversation_reply requires an executable channel")

    def to_dict(self) -> dict:
        return {
            "projection_kind": self.projection_kind,
            "conversation_user_id": self.conversation_user_id,
            "group_id": self.group_id,
            "source_chat_id": self.source_chat_id,
            "self_memory_eligible": self.self_memory_eligible,
        }


def derive_action_id(*, source_id: str, kind: str, channel: str,
                     target: str, payload: Mapping[str, Any],
                     scope_id: str = "", ordinal: int = 0,
                     schema_version: int = ACTION_SCHEMA_VERSION,
                     identity_version: int = ACTION_IDENTITY_VERSION) -> str:
    """从来源与规范化参数生成稳定、无正文泄漏的动作标识。"""
    _validate_trace_meta(schema_version, ordinal)
    _validate_identity_version(identity_version)
    source_id = str(source_id or "").strip()
    if not source_id:
        raise ValueError("source_id is required")
    scope_id = str(scope_id or "").strip()
    kind = str(kind or "").strip()
    channel = str(channel or "").strip()
    target = str(target or "").strip()
    canonical = json.dumps({
        # v1 已部署 ID 的 canonical 字段名和值必须永久冻结；契约结构升级
        # 不得让同一个真实动作获得第二个 domain_action_id。
        "schema_version": identity_version,
        "source_id": source_id,
        "scope_id": scope_id,
        "kind": kind,
        "channel": channel,
        "target": target,
        "payload": _json_copy(dict(payload or {})),
        "ordinal": ordinal,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"act-{digest}"


@dataclass(frozen=True)
class ActionEnvelope:
    action_id: str
    kind: str
    channel: str
    target: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    review: bool = False
    schema_version: int = ACTION_SCHEMA_VERSION
    source_id: str = ""
    scope_id: str = ""
    ordinal: int = 0
    identity_version: int = ACTION_IDENTITY_VERSION
    conversation_ref: ConversationRef = field(default_factory=ConversationRef)

    def __post_init__(self) -> None:
        target = str(self.target or "").strip()
        source_id = str(self.source_id or "").strip()
        scope_id = str(self.scope_id or "").strip()
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported action kind: {self.kind}")
        if self.channel not in ACTION_CHANNELS:
            raise ValueError(f"unsupported action channel: {self.channel}")
        if not str(self.action_id or "").strip():
            raise ValueError("action_id is required")
        if not target:
            raise ValueError("target is required")
        _validate_trace_meta(self.schema_version, self.ordinal)
        _validate_identity_version(self.identity_version)
        copied = _json_copy(dict(self.payload or {}))
        _validate_payload(self.kind, copied, schema_version=self.schema_version)
        conversation_ref = ConversationRef.from_value(self.conversation_ref)
        if self.schema_version < 2 and conversation_ref.projection_kind != "none":
            raise ValueError("conversation_ref requires schema_version >= 2")
        if self.schema_version >= 2:
            if not source_id:
                raise ValueError("schema_version >= 2 requires source_id")
            conversation_ref.validate_for_action(
                self.channel, target, scope_id,
            )
            expected_action_id = derive_action_id(
                source_id=source_id,
                scope_id=scope_id,
                kind=self.kind,
                channel=self.channel,
                target=target,
                payload=copied,
                ordinal=self.ordinal,
                schema_version=self.schema_version,
                identity_version=self.identity_version,
            )
            if self.action_id != expected_action_id:
                raise ValueError("action_id does not match frozen source identity")
        object.__setattr__(self, "payload", MappingProxyType(copied))
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "conversation_ref", conversation_ref)

    def to_dict(self) -> dict:
        data = {
            "action_id": self.action_id,
            "kind": self.kind,
            "channel": self.channel,
            "target": self.target,
            "payload": _json_copy(dict(self.payload)),
            "review": bool(self.review),
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "scope_id": self.scope_id,
            "ordinal": self.ordinal,
        }
        if self.schema_version >= 2:
            data["identity_version"] = self.identity_version
            data["conversation_ref"] = self.conversation_ref.to_dict()
        return data


@dataclass(frozen=True)
class ActionReceipt:
    action_id: str
    kind: str
    channel: str
    target: str
    status: str
    message_ids: tuple[int, ...] = ()
    actual: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    schema_version: int = ACTION_SCHEMA_VERSION
    source_id: str = ""
    scope_id: str = ""
    ordinal: int = 0
    identity_version: int = ACTION_IDENTITY_VERSION
    identity_payload: Mapping[str, Any] = field(default_factory=dict)
    conversation_ref: ConversationRef = field(default_factory=ConversationRef)

    def __post_init__(self) -> None:
        target = str(self.target or "").strip()
        source_id = str(self.source_id or "").strip()
        scope_id = str(self.scope_id or "").strip()
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported action kind: {self.kind}")
        # 兼容旧执行入口：参数校验失败时 receipt 仍需原样回显非法 channel，
        # 让 LLM 得到 failed 事实；只有成功/不确定/草稿必须是可执行通道。
        if self.channel not in ACTION_CHANNELS and self.status != "failed":
            raise ValueError(f"unsupported action channel: {self.channel}")
        if self.status not in ACTION_STATUSES:
            raise ValueError(f"unsupported action status: {self.status}")
        _validate_trace_meta(self.schema_version, self.ordinal)
        _validate_identity_version(self.identity_version)
        normalized: list[int] = []
        for raw in self.message_ids:
            if isinstance(raw, bool):
                continue
            try:
                message_id = int(raw)
            except (TypeError, ValueError):
                continue
            if -(2**31) <= message_id <= 2**31 - 1 and message_id != 0 and message_id not in normalized:
                normalized.append(message_id)
        object.__setattr__(self, "message_ids", tuple(normalized))
        copied = _json_copy(dict(self.actual or {}))
        object.__setattr__(self, "actual", MappingProxyType(copied))
        identity_payload = _json_copy(dict(self.identity_payload or {}))
        object.__setattr__(self, "identity_payload", MappingProxyType(identity_payload))
        object.__setattr__(self, "error_code", str(self.error_code or "").strip())
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "scope_id", scope_id)
        conversation_ref = ConversationRef.from_value(self.conversation_ref)
        if self.schema_version < 2 and conversation_ref.projection_kind != "none":
            raise ValueError("conversation_ref requires schema_version >= 2")
        if self.schema_version >= 2:
            if not self.action_id:
                raise ValueError("action_id is required")
            if not source_id:
                raise ValueError("schema_version >= 2 requires source_id")
            if not identity_payload:
                raise ValueError("schema_version >= 2 requires identity_payload")
            conversation_ref.validate_for_action(
                self.channel, target, scope_id,
            )
            expected_action_id = derive_action_id(
                source_id=source_id, scope_id=scope_id, kind=self.kind,
                channel=self.channel, target=target, payload=identity_payload,
                ordinal=self.ordinal, schema_version=self.schema_version,
                identity_version=self.identity_version,
            )
            if self.action_id != expected_action_id:
                raise ValueError("receipt action_id does not match frozen source identity")
        object.__setattr__(self, "conversation_ref", conversation_ref)

    def to_dict(self) -> dict:
        data = {
            "action_id": self.action_id,
            "kind": self.kind,
            "channel": self.channel,
            "target": self.target,
            "status": self.status,
            "message_ids": list(self.message_ids),
            "actual": _json_copy(dict(self.actual)),
            "error_code": self.error_code,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "scope_id": self.scope_id,
            "ordinal": self.ordinal,
        }
        if self.schema_version >= 2:
            data["identity_version"] = self.identity_version
            data["identity_payload"] = _json_copy(dict(self.identity_payload))
            data["conversation_ref"] = self.conversation_ref.to_dict()
        return data

    def to_legacy_text_dict(self) -> dict:
        """保持 P0-D1 已公开给 LLM/命令行的文本 receipt 形状。"""
        if self.kind != "text":
            raise ValueError("legacy text receipt requires kind=text")
        actual = dict(self.actual)
        return {
            "requested": str(actual.get("requested") or ""),
            "actual": str(actual.get("text") or ""),
            "target": self.target,
            "channel": self.channel,
            "mode": str(actual.get("mode") or "verbatim"),
            "attribution": str(actual.get("attribution") or "none"),
            "message_id": self.message_ids[-1] if self.message_ids else 0,
            "status": self.status,
        }


def build_action_receipt_template(envelope: ActionEnvelope,
                                  actual: Mapping[str, Any]) -> dict:
    """冻结 outbox 终态归账所需静态事实，不复制重试状态或正文队列。"""
    if not isinstance(envelope, ActionEnvelope):
        raise TypeError("receipt template requires ActionEnvelope")
    template = {
        "schema_version": envelope.schema_version,
        "action_id": envelope.action_id,
        "kind": envelope.kind,
        "channel": envelope.channel,
        "target": envelope.target,
        "source_id": envelope.source_id,
        "scope_id": envelope.scope_id,
        "ordinal": envelope.ordinal,
        "actual": _json_copy(dict(actual or {})),
    }
    if envelope.schema_version >= 2:
        template["identity_version"] = envelope.identity_version
        template["identity_payload"] = _json_copy(dict(envelope.payload))
        template["conversation_ref"] = envelope.conversation_ref.to_dict()
    return template


def finalize_action_receipt_template(template: Mapping[str, Any], *,
                                     status: str,
                                     message_ids=(),
                                     error_code: str = "") -> dict:
    """将静态模板与 outbox 终局合成标准 ActionReceipt。"""
    if not isinstance(template, Mapping):
        raise TypeError("action receipt template must be a mapping")
    schema_version = int(template.get("schema_version", ACTION_SCHEMA_VERSION))
    identity_version = int(template.get(
        "identity_version", ACTION_IDENTITY_VERSION,
    ))
    if schema_version >= 2:
        identity_payload = template.get("identity_payload")
        if not isinstance(identity_payload, Mapping):
            raise ValueError("schema_version >= 2 requires identity_payload")
        expected_action_id = derive_action_id(
            source_id=str(template.get("source_id") or "").strip(),
            scope_id=str(template.get("scope_id") or "").strip(),
            kind=str(template.get("kind") or ""),
            channel=str(template.get("channel") or ""),
            target=str(template.get("target") or "").strip(),
            payload=dict(identity_payload),
            ordinal=int(template.get("ordinal", 0)),
            schema_version=schema_version,
            identity_version=identity_version,
        )
        if str(template.get("action_id") or "") != expected_action_id:
            raise ValueError("receipt action_id does not match frozen source identity")
    return ActionReceipt(
        action_id=str(template.get("action_id") or ""),
        kind=str(template.get("kind") or ""),
        channel=str(template.get("channel") or ""),
        target=str(template.get("target") or ""),
        status=str(status or ""),
        message_ids=tuple(message_ids or ()),
        actual=dict(template.get("actual") or {}),
        error_code=str(error_code or "")[:128],
        schema_version=schema_version,
        source_id=str(template.get("source_id") or ""),
        scope_id=str(template.get("scope_id") or ""),
        ordinal=int(template.get("ordinal", 0)),
        identity_version=identity_version,
        identity_payload=template.get("identity_payload"),
        conversation_ref=template.get("conversation_ref"),
    ).to_dict()
