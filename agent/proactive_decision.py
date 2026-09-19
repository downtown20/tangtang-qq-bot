"""主动事件的 LLM 决策回合适配。

主动事件先由来源状态机取得租约，再把 LLM 的终态决策作为独立事实落库。
本模块只归档已经发生的结果，不替 LLM 预判是否应该说话。
"""

from __future__ import annotations

from typing import Any

from .interaction_contract import (
    DecisionRun,
    ProactiveEvent,
    classify_decision_outcome,
)
from .telemetry import current_correlation_id, new_correlation_id


def start_proactive_decision(
        event: ProactiveEvent, *, model: str = "",
        correlation_id: str = "",
) -> DecisionRun:
    """为一个已取得执行租约的主动事件创建 running 决策事实。"""
    if not isinstance(event, ProactiveEvent):
        raise TypeError("start_proactive_decision requires ProactiveEvent")
    correlation_id = str(correlation_id or current_correlation_id()).strip()
    if not correlation_id or correlation_id == "-":
        correlation_id = new_correlation_id("cid")
    return DecisionRun.start(
        run_id=new_correlation_id("run"),
        # ProactiveEvent 没有额外的 event_key；event_id 是不可变来源键，
        # 也由 Store 在绑定时强制核对。
        event_key=event.event_id,
        scope_id=event.scope_id,
        correlation_id=correlation_id,
        model=str(model or "").strip(),
    )


def _persist(store: Any, run: DecisionRun) -> None:
    recorder = getattr(store, "record_decision_run", None)
    if callable(recorder):
        recorder(run)


def finalize_proactive_decision(
        store: Any, event: ProactiveEvent, lease_token: str,
        run: DecisionRun, *, reply: str = "", responded: bool = True,
        tool_calls: tuple[str, ...] = (), error_code: str = "",
) -> tuple[DecisionRun, bool]:
    """把主动 LLM 回合收口并在有租约时绑定到事件。

    返回 ``(终态回合, 可继续执行)``。没有持久 Store/租约时保持旧离线调用
    兼容；有租约而无法绑定已完成回合时返回 ``False``，调用方必须拒绝外部副作用。
    """
    if not isinstance(event, ProactiveEvent):
        raise TypeError("finalize_proactive_decision requires ProactiveEvent")
    if not isinstance(run, DecisionRun):
        raise TypeError("finalize_proactive_decision requires DecisionRun")
    if run.status != "running":
        raise ValueError("PROACTIVE_DECISION_RUN_NOT_RUNNING")

    if error_code:
        terminal = run.fail(error_code=str(error_code))
    else:
        decision = classify_decision_outcome(
            reply=reply, responded=responded, tool_calls=tuple(tool_calls or ()),
        )
        terminal = (
            run.finish(decision=decision, tool_calls=tuple(tool_calls or ()))
            if decision else run.fail(error_code="empty_response")
        )

    # Store 可能为 None（旧离线测试）；若存在则先落终态，再写事件引用。
    _persist(store, terminal)
    if terminal.status != "completed":
        # 持久化主动事件一旦已有租约，失败回合不能继续产生外部副作用。
        # 无 Store/租约时保留旧离线调用的兼容行为。
        return terminal, not (store is not None and str(lease_token or "").strip())
    if store is None or not str(lease_token or "").strip():
        return terminal, True

    binder = getattr(store, "mark_proactive_event_decided", None)
    if not callable(binder):
        return terminal, False
    bound = bool(binder(event.event_id, str(lease_token), terminal.run_id))
    return terminal, bound
