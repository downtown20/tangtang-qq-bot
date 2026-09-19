"""ADR-004：有序、可恢复的子动作执行编排器。

该模块不负责选择动作，也不调用具体平台 API。调用方注入一个单 child
dispatch 函数；执行器只做三件事：按 ordinal 编排、校验回执身份、隔离
单 child 异常。这样确认/不确定动作不会被重复发送，后续 child 仍能继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .action_contract import ActionReceipt
from .action_plan import ActionPlan


Dispatch = Callable[[Any], Awaitable[ActionReceipt | Mapping[str, Any]]]
_TERMINAL_NO_RETRY = frozenset({"confirmed", "uncertain", "failed"})


@dataclass(frozen=True)
class ActionExecutionResult:
    """一次计划执行的只读结果；父状态由 child receipts 推导。"""

    plan: ActionPlan
    receipts: tuple[ActionReceipt, ...]
    status: str


class ActionExecutor:
    """按计划顺序执行 child，并对每个 child 建立独立 receipt。"""

    def __init__(self, dispatch: Dispatch):
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        self._dispatch = dispatch

    @staticmethod
    def _coerce_receipt(child, value: ActionReceipt | Mapping[str, Any]) -> ActionReceipt:
        if isinstance(value, ActionReceipt):
            receipt = value
        elif isinstance(value, Mapping):
            # 不接受任意“成功字典”：完整重建会再次执行 v2 identity 校验。
            try:
                if child.schema_version >= 2:
                    required = {
                        "action_id", "kind", "channel", "target", "status",
                        "message_ids", "actual", "error_code", "schema_version",
                        "source_id", "scope_id", "ordinal", "identity_version",
                        "identity_payload", "conversation_ref",
                    }
                    missing = required - set(value)
                    if missing:
                        raise ValueError(
                            "v2 prior receipt missing fields: "
                            + ",".join(sorted(missing))
                        )
                receipt = ActionReceipt(
                    action_id=value.get("action_id", ""),
                    kind=value.get("kind", child.kind),
                    channel=value.get("channel", child.channel),
                    target=value.get("target", child.target),
                    status=value.get("status", ""),
                    message_ids=tuple(value.get("message_ids") or ()),
                    actual=value.get("actual") or {},
                    error_code=value.get("error_code", ""),
                    schema_version=value.get("schema_version", child.schema_version),
                    source_id=value.get("source_id", child.source_id),
                    scope_id=value.get("scope_id", child.scope_id),
                    ordinal=value.get("ordinal", child.ordinal),
                    identity_version=value.get("identity_version", child.identity_version),
                    identity_payload=value.get("identity_payload", dict(child.payload)),
                    conversation_ref=value.get("conversation_ref", child.conversation_ref),
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError("invalid child receipt") from exc
        else:
            raise ValueError("invalid child receipt")

        # Receipt identity must match the exact child; a valid receipt for a
        # different action is still forged in this plan and is fail-closed.
        if (receipt.action_id != child.action_id
                or receipt.kind != child.kind
                or receipt.channel != child.channel
                or receipt.target != child.target
                or receipt.source_id != child.source_id
                or receipt.scope_id != child.scope_id
                or receipt.ordinal != child.ordinal
                or receipt.schema_version != child.schema_version
                or receipt.identity_version != child.identity_version
                or dict(receipt.identity_payload) != dict(child.payload)
                or receipt.conversation_ref != child.conversation_ref):
            raise ValueError("child receipt identity mismatch")
        return receipt

    @staticmethod
    def _synthetic_receipt(child, *, error_code: str, detail: str = "") -> ActionReceipt:
        actual = {"executor": "action_executor"}
        if detail:
            actual["error_detail"] = detail[:256]
        return ActionReceipt(
            action_id=child.action_id,
            kind=child.kind,
            channel=child.channel,
            target=child.target,
            status="uncertain",
            actual=actual,
            error_code=error_code,
            schema_version=child.schema_version,
            source_id=child.source_id,
            scope_id=child.scope_id,
            ordinal=child.ordinal,
            identity_version=child.identity_version,
            identity_payload=child.payload,
            conversation_ref=child.conversation_ref,
        )

    async def execute(self, plan: ActionPlan,
                      prior_receipts: Mapping[str, ActionReceipt | Mapping[str, Any]] | None = None
                      ) -> ActionExecutionResult:
        if not isinstance(plan, ActionPlan):
            raise TypeError("execute requires ActionPlan")
        prior = prior_receipts or {}
        receipts: list[ActionReceipt] = []
        for child in plan.children:
            previous = prior.get(child.action_id)
            if previous is not None:
                try:
                    prior_receipt = self._coerce_receipt(child, previous)
                except (TypeError, ValueError):
                    prior_receipt = self._synthetic_receipt(
                        child, error_code="INVALID_PRIOR_RECEIPT",
                    )
                # Existing terminal facts are authoritative.  In particular,
                # uncertain must never be silently retried by this executor.
                if prior_receipt.status in _TERMINAL_NO_RETRY:
                    receipts.append(prior_receipt)
                    continue

            try:
                raw = await self._dispatch(child)
            except Exception as exc:  # per-child isolation is intentional
                receipt = self._synthetic_receipt(
                    child, error_code="EXECUTOR_EXCEPTION", detail=str(exc),
                )
            else:
                try:
                    receipt = self._coerce_receipt(child, raw)
                except (TypeError, ValueError) as exc:
                    receipt = self._synthetic_receipt(
                        child, error_code="INVALID_RECEIPT", detail=str(exc),
                    )
            receipts.append(receipt)

        status = plan.aggregate_status({receipt.action_id: receipt for receipt in receipts})
        return ActionExecutionResult(plan=plan, receipts=tuple(receipts), status=status)
