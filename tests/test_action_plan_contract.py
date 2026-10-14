"""ADR-004 ActionPlan：父计划只编排子动作，不伪造送达。"""

import pytest

from agent.action_contract import ActionEnvelope, ActionReceipt, derive_action_id
from agent.action_plan import ActionPlan, derive_plan_id


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


def test_plan_id_is_stable_for_same_frozen_children():
    children = (_child(ordinal=0), _child(ordinal=1, payload={"emotion": "温柔", "count": 1}))
    one = ActionPlan.create(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children, role_id="default", library_id="stickers-v1",
        created_at="2026-08-28T10:00:00+08:00",
    )
    two = ActionPlan.create(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children, role_id="default", library_id="stickers-v1",
        created_at="2026-08-28T10:00:00+08:00",
    )
    assert one.plan_id == two.plan_id
    assert one.to_dict()["children"][1]["ordinal"] == 1


def test_plan_rejects_cross_scope_child_and_duplicate_ordinals():
    child = _child()
    with pytest.raises(ValueError, match="scope"):
        ActionPlan.create(
            source_id="msg-1", scope_id="g2", channel="group", target="g2",
            children=(child,),
        )
    second = _child(ordinal=0, payload={"emotion": "温柔", "count": 1})
    with pytest.raises(ValueError, match="ordinal"):
        ActionPlan.create(
            source_id="msg-1", scope_id="g1", channel="group", target="g1",
            children=(child, second),
        )


def test_parent_aggregation_never_promotes_partial_or_missing_children():
    children = (_child(ordinal=0), _child(ordinal=1, payload={"emotion": "温柔", "count": 1}))
    plan = ActionPlan.create(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children,
    )
    receipts = {
        children[0].action_id: ActionReceipt(
            action_id=children[0].action_id, kind="sticker", channel="group",
            target="g1", status="confirmed", schema_version=2,
            source_id="msg-1", scope_id="g1", identity_payload=children[0].payload,
        ),
    }
    assert plan.aggregate_status(receipts) == "pending"
    receipts[children[1].action_id] = ActionReceipt(
        action_id=children[1].action_id, kind="sticker", channel="group",
        target="g1", status="uncertain", schema_version=2,
        source_id="msg-1", scope_id="g1", ordinal=children[1].ordinal,
        identity_payload=children[1].payload,
    )
    assert plan.aggregate_status(receipts) == "uncertain"


def test_derive_plan_id_changes_when_frozen_media_version_changes():
    children = (_child(),)
    first = derive_plan_id(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children, role_id="default", library_id="stickers-v1",
    )
    second = derive_plan_id(
        source_id="msg-1", scope_id="g1", channel="group", target="g1",
        children=children, role_id="default", library_id="stickers-v2",
    )
    assert first != second
