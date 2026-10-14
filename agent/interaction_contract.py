"""P0-1 统一交互事实契约。

本模块只描述跨模块传递的事实，不负责是否回复、是否重试或发送网络请求。
入站事件、LLM 决策回合和主动事件先在这里形成不可变边界，后续业务可以逐步
把现有 ``dict`` 接线迁移到这些对象，而不改变当前线上行为。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


_DECISIONS = frozenset({"reply", "skip", "fade", "action"})
_RUN_STATUSES = frozenset({"running", "completed", "failed"})
_CHANNELS = frozenset({"group", "private"})


def _freeze(value: Any) -> Any:
    """递归冻结 JSON 值，防止调用方在持久化/执行间隙原地改写事实。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} is required")
    return result


def _scope(channel: str, scope_id: str, target_or_actor: str) -> None:
    expected_prefix = f"{channel}:"
    if not scope_id.startswith(expected_prefix) or not scope_id[len(expected_prefix):]:
        raise ValueError("scope must be a non-empty channel-prefixed value")
    if channel == "private" and scope_id != f"private:{target_or_actor}":
        raise ValueError("private scope must bind the actor/target")


def classify_decision_outcome(
    *, reply: str, responded: bool, tool_calls: tuple[str, ...] = (),
) -> str:
    """把已发生的回合结果归档为事实，不替 LLM 预判行为。

    ``responded`` 来自工具/执行结果；没有正文但确有工具动作时记录 ``action``，
    没有任何可观察结果则返回空串，交由调用方记录技术失败。
    """
    if not responded:
        return "skip"
    if str(reply or "").strip():
        return "reply"
    if tuple(tool_calls or ()):
        return "action"
    return ""


def build_proactive_event(
    *,
    event_id: str,
    source: str,
    channel: str,
    target: str,
    payload: Mapping[str, Any] | None = None,
    created_at: str = "",
    idempotency_key: str = "",
) -> "ProactiveEvent":
    """用统一默认值构造主动入口事实。

    主动事件的幂等键若未显式提供，回退到稳定 ``event_id``；该函数只负责
    规范化和校验，不执行网络请求，也不把事件标记为已发送。
    """
    normalized_event_id = _text(event_id, "event_id")
    normalized_channel = _text(channel, "channel")
    normalized_target = _text(target, "target")
    return ProactiveEvent(
        event_id=normalized_event_id,
        source=source,
        scope_id=f"{normalized_channel}:{normalized_target}",
        channel=normalized_channel,
        target=normalized_target,
        payload=payload or {},
        created_at=created_at,
        idempotency_key=str(idempotency_key or "").strip() or normalized_event_id,
    )


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """规范化的入站事实；原始正文和 typed segments 都不可被批处理覆盖。"""

    event_id: str
    event_key: str
    channel: str
    scope_id: str
    actor_id: str
    raw_message: str = ""
    segments: str = ""
    message_id: int | None = None
    received_at: str = ""
    source_id: str = "onebot"

    def __post_init__(self) -> None:
        event_id = _text(self.event_id, "event_id")
        event_key = _text(self.event_key, "event_key")
        channel = _text(self.channel, "channel")
        scope_id = _text(self.scope_id, "scope_id")
        actor_id = _text(self.actor_id, "actor_id")
        source_id = _text(self.source_id, "source_id")
        if channel not in _CHANNELS:
            raise ValueError("channel must be group or private")
        _scope(channel, scope_id, actor_id)
        message_id = self.message_id
        if message_id is not None and (
                isinstance(message_id, bool) or not isinstance(message_id, int)
                or message_id == 0):
            raise ValueError("message_id must be None or a non-zero integer")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "raw_message", str(self.raw_message or ""))
        object.__setattr__(self, "segments", str(self.segments or ""))
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "received_at", str(self.received_at or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_key": self.event_key,
            "channel": self.channel,
            "scope_id": self.scope_id,
            "actor_id": self.actor_id,
            "raw_message": self.raw_message,
            "segments": self.segments,
            "message_id": self.message_id,
            "received_at": self.received_at,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class ChatContext:
    """一次 LLM 回合可见的结构化上下文边界。

    ``history_messages``、``window_state`` 和其他集合都只保存来源材料；本对象
    不判断是否回复，也不把摘要或记忆提升为事实。
    """

    scope_id: str
    channel: str
    actor_id: str
    event_key: str
    current_message: str
    source_event_keys: tuple[str, ...] = ()
    history_messages: tuple[Mapping[str, Any], ...] = ()
    trusted_memory_ids: tuple[int, ...] = ()
    media_refs: tuple[str, ...] = ()
    window_state: Mapping[str, Any] = MappingProxyType({})
    received_at: str = ""

    def __post_init__(self) -> None:
        channel = _text(self.channel, "channel")
        if channel not in _CHANNELS:
            raise ValueError("channel must be group or private")
        scope_id = _text(self.scope_id, "scope_id")
        actor_id = _text(self.actor_id, "actor_id")
        event_key = _text(self.event_key, "event_key")
        current_message = str(self.current_message or "")
        _scope(channel, scope_id, actor_id)

        event_keys = tuple(str(item).strip() for item in (self.source_event_keys or (event_key,)))
        if not event_keys or any(not item for item in event_keys):
            raise ValueError("source_event_keys must contain non-empty keys")
        if event_key not in event_keys:
            raise ValueError("event_key must be included in source_event_keys")

        memories = tuple(self.trusted_memory_ids or ())
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0
               for item in memories):
            raise ValueError("trusted_memory_ids must contain positive integers")
        refs = tuple(str(item).strip() for item in (self.media_refs or ()))
        if any(not item for item in refs):
            raise ValueError("media_refs must contain non-empty references")
        history = tuple(self.history_messages or ())
        if any(not isinstance(item, Mapping) for item in history):
            raise TypeError("history_messages must contain mappings")
        window_state = self.window_state or {}
        if not isinstance(window_state, Mapping):
            raise TypeError("window_state must be a mapping")

        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "current_message", current_message)
        object.__setattr__(self, "source_event_keys", event_keys)
        object.__setattr__(self, "history_messages", tuple(_freeze(item) for item in history))
        object.__setattr__(self, "trusted_memory_ids", memories)
        object.__setattr__(self, "media_refs", refs)
        object.__setattr__(self, "window_state", _freeze(window_state))
        object.__setattr__(self, "received_at", str(self.received_at or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "channel": self.channel,
            "actor_id": self.actor_id,
            "event_key": self.event_key,
            "current_message": self.current_message,
            "source_event_keys": list(self.source_event_keys),
            "history_messages": [_thaw(item) for item in self.history_messages],
            "trusted_memory_ids": list(self.trusted_memory_ids),
            "media_refs": list(self.media_refs),
            "window_state": _thaw(self.window_state),
            "received_at": self.received_at,
        }


@dataclass(frozen=True, slots=True)
class DecisionRun:
    """LLM 决策回合事实；完成后不可再次被同一对象改写。"""

    run_id: str
    event_key: str
    scope_id: str
    correlation_id: str
    model: str = ""
    status: str = "running"
    decision: str = ""
    tool_calls: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        run_id = _text(self.run_id, "run_id")
        event_key = _text(self.event_key, "event_key")
        scope_id = _text(self.scope_id, "scope_id")
        correlation_id = _text(self.correlation_id, "correlation_id")
        status = _text(self.status, "status")
        if status not in _RUN_STATUSES:
            raise ValueError("status must be running, completed or failed")
        decision = str(self.decision or "").strip()
        if status == "running" and decision:
            raise ValueError("running decision run cannot have a terminal decision")
        if status == "completed" and decision not in _DECISIONS:
            raise ValueError("terminal decision must be reply, skip, fade or action")
        if status == "failed" and decision:
            raise ValueError("failed decision run cannot claim a behavior decision")
        tool_calls = tuple(str(item).strip() for item in (self.tool_calls or ()))
        if any(not item for item in tool_calls):
            raise ValueError("tool_calls must contain non-empty names")
        if status == "failed" and not str(self.error_code or "").strip():
            raise ValueError("failed decision run requires error_code")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "correlation_id", correlation_id)
        object.__setattr__(self, "model", str(self.model or "").strip())
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(self, "started_at", str(self.started_at or "").strip())
        object.__setattr__(self, "finished_at", str(self.finished_at or "").strip())
        object.__setattr__(self, "error_code", str(self.error_code or "").strip())

    @classmethod
    def start(cls, *, run_id: str, event_key: str, scope_id: str,
              correlation_id: str, model: str = "", started_at: str = "") -> "DecisionRun":
        return cls(
            run_id=run_id, event_key=event_key, scope_id=scope_id,
            correlation_id=correlation_id, model=model,
            started_at=started_at or datetime.now(timezone.utc).isoformat(),
        )

    def finish(self, *, decision: str,
               tool_calls: tuple[str, ...] = ()) -> "DecisionRun":
        if self.status != "running":
            raise ValueError("terminal decision run cannot be finished again")
        normalized_decision = str(decision or "").strip()
        if "skip_response" in tuple(tool_calls or ()) and normalized_decision != "skip":
            raise ValueError("decision conflicts with skip_response tool call")
        return replace(
            self, status="completed", decision=normalized_decision,
            tool_calls=tuple(tool_calls),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    def fail(self, *, error_code: str) -> "DecisionRun":
        if self.status != "running":
            raise ValueError("terminal decision run cannot be failed again")
        return replace(
            self, status="failed", decision="",
            error_code=str(error_code or "").strip(),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "event_key": self.event_key,
            "scope_id": self.scope_id,
            "correlation_id": self.correlation_id,
            "model": self.model,
            "status": self.status,
            "decision": self.decision,
            "tool_calls": list(self.tool_calls),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class ProactiveEvent:
    """定时/自治/提醒等主动入口的持久事实，不代表已发送。"""

    event_id: str
    source: str
    scope_id: str
    channel: str
    target: str
    payload: Mapping[str, Any] = MappingProxyType({})
    created_at: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        event_id = _text(self.event_id, "event_id")
        source = _text(self.source, "source")
        channel = _text(self.channel, "channel")
        target = _text(self.target, "target")
        scope_id = _text(self.scope_id, "scope_id")
        if channel not in _CHANNELS:
            raise ValueError("channel must be group or private")
        expected = f"{channel}:{target}"
        if scope_id != expected:
            raise ValueError("scope and target must identify the same proactive destination")
        payload = self.payload or {}
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "payload", _freeze(payload))
        object.__setattr__(self, "created_at", str(self.created_at or "").strip())
        object.__setattr__(self, "idempotency_key", str(self.idempotency_key or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "scope_id": self.scope_id,
            "channel": self.channel,
            "target": self.target,
            "payload": _thaw(self.payload),
            "created_at": self.created_at,
            "idempotency_key": self.idempotency_key,
        }
