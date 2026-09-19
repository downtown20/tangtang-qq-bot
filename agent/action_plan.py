"""ADR-004：回合动作计划的纯数据契约。

`ActionPlan` 只负责冻结一个逻辑回合和有序的子动作，不执行网络调用，
也不把父计划状态冒充成平台送达事实。实际执行和 receipt 归账由后续
`ActionExecutor` 负责；这样可以先建立稳定边界，再逐条迁移旧发送路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
import hashlib
import json

from .action_contract import ActionEnvelope, ActionReceipt


PLAN_SCHEMA_VERSION = 1
PLAN_STATUSES = frozenset({
    "draft", "pending", "confirmed", "uncertain", "failed", "partial",
})


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _text(value: Any) -> str:
    return str(value or "").strip()


def derive_plan_id(*, source_id: str, scope_id: str, channel: str,
                   target: str, children: Sequence[ActionEnvelope],
                   role_id: str = "", library_id: str = "",
                   schema_version: int = PLAN_SCHEMA_VERSION) -> str:
    """由来源、作用域、冻结版本和子动作 ID 派生稳定父计划 ID。"""
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("plan schema_version must be a positive integer")
    source = _text(source_id)
    scope = _text(scope_id)
    channel = _text(channel)
    target = _text(target)
    if not source:
        raise ValueError("plan source_id is required")
    if not scope:
        raise ValueError("plan scope_id is required")
    if not channel or not target:
        raise ValueError("plan channel and target are required")
    child_ids = []
    for child in children:
        if not isinstance(child, ActionEnvelope):
            raise TypeError("plan children must be ActionEnvelope instances")
        child_ids.append({"ordinal": child.ordinal, "action_id": child.action_id})
    canonical = json.dumps({
        "schema_version": schema_version,
        "source_id": source,
        "scope_id": scope,
        "channel": channel,
        "target": target,
        "role_id": _text(role_id),
        "library_id": _text(library_id),
        "children": child_ids,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "plan-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ActionPlan:
    """不可变的父计划；子动作的送达必须逐个由 receipt 证明。"""

    plan_id: str
    source_id: str
    scope_id: str
    channel: str
    target: str
    children: tuple[ActionEnvelope, ...] = ()
    created_at: str = ""
    role_id: str = ""
    library_id: str = ""
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        plan_id = _text(self.plan_id)
        source_id = _text(self.source_id)
        scope_id = _text(self.scope_id)
        channel = _text(self.channel)
        target = _text(self.target)
        if not plan_id:
            raise ValueError("plan_id is required")
        if not source_id:
            raise ValueError("plan source_id is required")
        if not scope_id:
            raise ValueError("plan scope_id is required")
        if channel not in {"group", "private"}:
            raise ValueError("plan channel must be group or private")
        if not target:
            raise ValueError("plan target is required")
        if (isinstance(self.schema_version, bool)
                or not isinstance(self.schema_version, int)
                or self.schema_version < 1):
            raise ValueError("plan schema_version must be a positive integer")

        children = tuple(self.children or ())
        ordinals: list[int] = []
        for child in children:
            if not isinstance(child, ActionEnvelope):
                raise TypeError("plan children must be ActionEnvelope instances")
            if child.channel != channel or child.target != target or child.scope_id != scope_id:
                raise ValueError("child channel, target and scope must match plan")
            if child.source_id != source_id:
                raise ValueError("child source_id must match plan")
            if child.ordinal in ordinals:
                raise ValueError("child ordinal must be unique")
            ordinals.append(child.ordinal)
        if ordinals != sorted(ordinals):
            raise ValueError("children must be ordered by ordinal")

        # The ID is deliberately checked against the frozen child list.  A caller
        # cannot silently swap an asset or role after persistence.
        expected = derive_plan_id(
            source_id=source_id, scope_id=scope_id, channel=channel,
            target=target, children=children, role_id=self.role_id,
            library_id=self.library_id, schema_version=self.schema_version,
        )
        if plan_id != expected:
            raise ValueError("plan_id does not match frozen plan identity")

        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "created_at", _text(self.created_at))
        object.__setattr__(self, "role_id", _text(self.role_id))
        object.__setattr__(self, "library_id", _text(self.library_id))

    @classmethod
    def create(cls, *, source_id: str, scope_id: str, channel: str,
               target: str, children: Sequence[ActionEnvelope] = (),
               created_at: str = "", role_id: str = "",
               library_id: str = "", schema_version: int = PLAN_SCHEMA_VERSION,
               plan_id: str | None = None) -> "ActionPlan":
        frozen_children = tuple(children or ())
        derived = derive_plan_id(
            source_id=source_id, scope_id=scope_id, channel=channel,
            target=target, children=frozen_children, role_id=role_id,
            library_id=library_id, schema_version=schema_version,
        )
        if plan_id is not None and _text(plan_id) != derived:
            raise ValueError("plan_id does not match frozen plan identity")
        return cls(
            plan_id=derived, source_id=source_id, scope_id=scope_id,
            channel=channel, target=target, children=frozen_children,
            created_at=created_at, role_id=role_id, library_id=library_id,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "source_id": self.source_id,
            "scope_id": self.scope_id,
            "channel": self.channel,
            "target": self.target,
            "created_at": self.created_at,
            "role_id": self.role_id,
            "library_id": self.library_id,
            "schema_version": self.schema_version,
            "children": [child.to_dict() for child in self.children],
        }

    def aggregate_status(self, receipts: Mapping[str, ActionReceipt | Mapping[str, Any]] | None) -> str:
        """根据子 receipt 聚合父状态；缺子 receipt 永远不是 confirmed。"""
        if not self.children:
            return "draft"
        statuses: list[str] = []
        receipt_map = receipts or {}
        for child in self.children:
            raw = receipt_map.get(child.action_id)
            if isinstance(raw, ActionReceipt):
                status = raw.status
            elif isinstance(raw, Mapping):
                status = _text(raw.get("status"))
            else:
                status = "pending"
            statuses.append(status if status in {"confirmed", "uncertain", "failed"} else "pending")
        if all(status == "confirmed" for status in statuses):
            return "confirmed"
        if any(status == "uncertain" for status in statuses):
            return "uncertain"
        if any(status == "failed" for status in statuses):
            return "partial" if any(status == "confirmed" for status in statuses) else "failed"
        return "pending"
