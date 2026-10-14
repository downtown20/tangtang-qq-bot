"""ADR-004 ActionExecutor 的顺序、幂等和失败隔离契约。"""

import asyncio

from agent.action_contract import ActionEnvelope, ActionReceipt, derive_action_id
from agent.action_executor import ActionExecutor


def _child(*, source="msg-1", ordinal=0, kind="sticker", payload=None):
    base_payload = {
        "emotion": "开心", "count": 1, "asset_ref": "asset-a",
        "asset_sha256": "0" * 64, "asset_valid": True,
        "role_id": "default", "library_id": "stickers-v1",
    }
    if kind == "sticker":
        payload = {**base_payload, **(payload or {})}
    else:
        payload = payload or {
            "text": "你好", "emotion": "温柔", "speed": 1.0, "pause": "自然",
        }
    action_id = derive_action_id(
        source_id=source, scope_id="g1", kind=kind, channel="group",
        target="g1", payload=payload, ordinal=ordinal, schema_version=2,
    )
    return ActionEnvelope(
        action_id=action_id, kind=kind, channel="group", target="g1",
        payload=payload, schema_version=2, source_id=source, scope_id="g1",
        ordinal=ordinal,
    )


def _run(coro):
    return asyncio.run(coro)


def _receipt(child, status="confirmed", error_code=""):
    return ActionReceipt(
        action_id=child.action_id, kind=child.kind, channel=child.channel,
        target=child.target, status=status, schema_version=2,
        source_id=child.source_id, scope_id=child.scope_id,
        ordinal=child.ordinal, identity_payload=child.payload,
        error_code=error_code,
    )


def _plan(children):
    from agent.action_plan import ActionPlan
    return ActionPlan.create(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children,
    )


def test_executor_runs_children_in_ordinal_order_and_aggregates_confirmed():
    children = (_child(ordinal=0), _child(ordinal=1, payload={"emotion": "温柔", "count": 1}))
    plan = _plan(children)
    seen = []

    async def dispatch(child):
        seen.append(child.ordinal)
        return _receipt(child)

    result = _run(ActionExecutor(dispatch).execute(plan))
    assert seen == [0, 1]
    assert result.status == "confirmed"
    assert [r.action_id for r in result.receipts] == [c.action_id for c in children]


def test_executor_does_not_resend_confirmed_or_uncertain_children():
    children = (_child(ordinal=0), _child(ordinal=1, payload={"emotion": "温柔", "count": 1}))
    plan = _plan(children)
    prior = {
        children[0].action_id: _receipt(children[0], "confirmed"),
        children[1].action_id: _receipt(children[1], "uncertain", "SEND_RESULT_LOST"),
    }
    called = []

    async def dispatch(child):
        called.append(child.action_id)
        return _receipt(child)

    result = _run(ActionExecutor(dispatch).execute(plan, prior_receipts=prior))
    assert called == []
    assert result.status == "uncertain"


def test_executor_isolates_child_exception_and_continues_later_media():
    children = (_child(ordinal=0), _child(ordinal=1, payload={"emotion": "温柔", "count": 1}))
    plan = _plan(children)
    called = []

    async def dispatch(child):
        called.append(child.ordinal)
        if child.ordinal == 0:
            raise RuntimeError("response lost after POST")
        return _receipt(child)

    result = _run(ActionExecutor(dispatch).execute(plan))
    assert called == [0, 1]
    assert result.status == "uncertain"
    assert result.receipts[0].status == "uncertain"
    assert result.receipts[0].error_code == "EXECUTOR_EXCEPTION"
    assert result.receipts[1].status == "confirmed"


def test_executor_rejects_malformed_dispatch_receipt_fail_closed():
    child = _child()
    plan = _plan((child,))

    async def dispatch(_child):
        return {"action_id": "forged", "status": "confirmed"}

    result = _run(ActionExecutor(dispatch).execute(plan))
    assert result.status == "uncertain"
    assert result.receipts[0].error_code == "INVALID_RECEIPT"


def test_executor_rejects_sparse_v2_prior_receipt_without_resending():
    child = _child()
    plan = _plan((child,))
    called = []

    async def dispatch(_child):
        called.append(_child.action_id)
        return _receipt(_child)

    result = _run(ActionExecutor(dispatch).execute(
        plan,
        prior_receipts={child.action_id: {
            "action_id": child.action_id,
            "status": "confirmed",
        }},
    ))
    assert called == []
    assert result.receipts[0].status == "uncertain"
    assert result.receipts[0].error_code == "INVALID_PRIOR_RECEIPT"
