"""ADR-001：动作信封与真实回执的纯数据契约。"""

import pytest


def test_action_id_is_stable_for_canonical_payload_order():
    from agent.action_contract import derive_action_id

    first = derive_action_id(
        source_id="group:g1:501", kind="sticker", channel="group",
        target="g1", payload={"emotion": "开心", "count": 2},
    )
    second = derive_action_id(
        source_id="group:g1:501", kind="sticker", channel="group",
        target="g1", payload={"count": 2, "emotion": "开心"},
    )

    assert first == second
    assert first.startswith("act-") and len(first) == 20


def test_action_id_separates_scope_identity_and_ordinal():
    from agent.action_contract import derive_action_id

    base = {
        "source_id": "msg-501", "scope_id": "group:g1",
        "kind": "sticker", "channel": "group", "target": "g1",
        "payload": {"emotion": "开心", "count": 1},
    }

    first = derive_action_id(**base, ordinal=0)
    assert derive_action_id(**base, ordinal=0) == first
    assert derive_action_id(**base, ordinal=1) != first
    assert derive_action_id(**{**base, "scope_id": "group:g2"}) != first
    assert derive_action_id(**base, schema_version=2, identity_version=1) == first
    with pytest.raises(ValueError, match="frozen"):
        derive_action_id(**base, schema_version=2, identity_version=2)


def test_envelope_copies_payload_and_validates_kind():
    from agent.action_contract import ActionEnvelope

    payload = {"text": "你好", "emotion": "开心", "speed": 1.0, "pause": "自然"}
    envelope = ActionEnvelope(
        action_id="act-1", kind="voice", channel="private",
        target="u1", payload=payload,
    )
    payload["text"] = "被外部改写"

    assert envelope.to_dict()["payload"]["text"] == "你好"
    with pytest.raises(ValueError):
        ActionEnvelope(
            action_id="act-2", kind="unknown", channel="private",
            target="u1", payload={},
        )


def test_envelope_serializes_required_trace_metadata():
    from agent.action_contract import ActionEnvelope

    envelope = ActionEnvelope(
        action_id="act-1", kind="sticker", channel="group", target="g1",
        payload={"emotion": "开心", "count": 2},
        schema_version=1, source_id="msg-501", scope_id="group:g1", ordinal=3,
    )

    data = envelope.to_dict()
    assert data["schema_version"] == 1
    assert data["source_id"] == "msg-501"
    assert data["scope_id"] == "group:g1"
    assert data["ordinal"] == 3


def test_v2_sticker_payload_requires_frozen_asset_identity():
    from agent.action_contract import ActionEnvelope, derive_action_id

    payload = {"emotion": "开心", "count": 1}
    action_id = derive_action_id(
        source_id="msg-501", scope_id="g1", kind="sticker", channel="group",
        target="g1", payload=payload, schema_version=2,
    )
    with pytest.raises(ValueError, match="asset_ref"):
        ActionEnvelope(
            action_id=action_id, kind="sticker", channel="group", target="g1",
            payload=payload, schema_version=2, source_id="msg-501", scope_id="g1",
        )

    valid = {
        **payload,
        "asset_ref": "happy/a.png",
        "asset_sha256": "a" * 64,
        "asset_valid": True,
        "role_id": "default",
        "library_id": "stickers-v1",
    }
    valid_id = derive_action_id(
        source_id="msg-501", scope_id="g1", kind="sticker", channel="group",
        target="g1", payload=valid, schema_version=2,
    )
    envelope = ActionEnvelope(
        action_id=valid_id, kind="sticker", channel="group", target="g1",
        payload=valid, schema_version=2, source_id="msg-501", scope_id="g1",
    )
    assert envelope.payload["asset_sha256"] == "a" * 64


def test_v2_image_payload_requires_frozen_asset_identity():
    """图片 child 和贴图一样必须冻结库内资产，不能只靠临时 CQ 路径。"""
    from agent.action_contract import ActionEnvelope, derive_action_id

    payload = {"asset_ref": "draw.png"}
    action_id = derive_action_id(
        source_id="msg-502", scope_id="g1", kind="image", channel="group",
        target="g1", payload=payload, schema_version=2,
    )
    with pytest.raises(ValueError, match="library_id"):
        ActionEnvelope(
            action_id=action_id, kind="image", channel="group", target="g1",
            payload=payload, schema_version=2, source_id="msg-502", scope_id="g1",
        )

    frozen = {
        "asset_ref": "draw.png",
        "asset_sha256": "b" * 64,
        "asset_valid": True,
        "library_id": "generated_images",
    }
    frozen_id = derive_action_id(
        source_id="msg-502", scope_id="g1", kind="image", channel="group",
        target="g1", payload=frozen, schema_version=2,
    )
    envelope = ActionEnvelope(
        action_id=frozen_id, kind="image", channel="group", target="g1",
        payload=frozen, schema_version=2, source_id="msg-502", scope_id="g1",
    )
    assert envelope.payload["asset_sha256"] == "b" * 64


@pytest.mark.parametrize("kind,payload", [
    ("text", {"text": "你好", "mode": "verbatim", "attribution": "none"}),
    ("voice", {"text": "你好", "emotion": "开心", "speed": 1.0, "pause": "自然"}),
    ("sticker", {"emotion": "开心", "count": 2}),
    ("image", {"asset_ref": "asset:1", "caption": "看这个"}),
    ("image", {"url": "https://example.invalid/a.png"}),
    ("sing", {"song_id": "song-1", "title": "测试曲"}),
    ("sing", {"title": "测试曲"}),
])
def test_envelope_accepts_typed_payloads(kind, payload):
    from agent.action_contract import ActionEnvelope

    envelope = ActionEnvelope(
        action_id="act-typed", kind=kind, channel="private",
        target="u1", payload=payload,
    )
    assert envelope.to_dict()["payload"] == payload


@pytest.mark.parametrize("kind,payload", [
    ("text", {}),
    ("voice", {"text": "你好", "emotion": "开心", "speed": 1.0}),
    ("voice", {"text": "你好", "emotion": "开心", "speed": "快", "pause": "自然"}),
    ("sticker", {"emotion": "", "count": 1}),
    ("sticker", {"emotion": "开心", "count": 0}),
    ("image", {"caption": "没有资源"}),
    ("sing", {"sections": ["chorus"]}),
])
def test_envelope_rejects_invalid_typed_payloads(kind, payload):
    from agent.action_contract import ActionEnvelope

    with pytest.raises(ValueError):
        ActionEnvelope(
            action_id="act-invalid", kind=kind, channel="private",
            target="u1", payload=payload,
        )


def test_receipt_normalizes_message_ids_and_status():
    from agent.action_contract import ActionReceipt

    receipt = ActionReceipt(
        action_id="act-1", kind="sticker", channel="group", target="g1",
        status="confirmed", message_ids=(0, 7, 7, -1, 8, True, 2**31),
        actual={"files": ["a.png", "b.png"]},
        source_id="msg-501", scope_id="group:g1", ordinal=2,
    )

    assert receipt.to_dict() == {
        "action_id": "act-1", "kind": "sticker", "channel": "group",
        "target": "g1", "status": "confirmed", "message_ids": [7, -1, 8],
        "actual": {"files": ["a.png", "b.png"]}, "error_code": "",
        "schema_version": 1, "source_id": "msg-501",
        "scope_id": "group:g1", "ordinal": 2,
    }
    with pytest.raises(ValueError):
        ActionReceipt(
            action_id="act-2", kind="voice", channel="private", target="u1",
            status="accepted",
        )


def test_legacy_text_receipt_shape_remains_exact():
    from agent.send_actions import build_receipt

    import json

    data = json.loads(build_receipt(
        requested="a", actual="b", channel="group", target="g1",
        mode="verbatim", attribution="none", status="confirmed",
        message_id=3,
    ))
    assert data == {
        "requested": "a", "actual": "b", "target": "g1",
        "channel": "group", "mode": "verbatim", "attribution": "none",
        "message_id": 3, "status": "confirmed",
    }
