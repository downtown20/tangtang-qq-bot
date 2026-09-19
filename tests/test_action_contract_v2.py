"""ADR-003：v2 对话归属与冻结的 domain action identity。"""

from dataclasses import FrozenInstanceError

import pytest

from agent.action_contract import (
    ActionEnvelope,
    ConversationRef,
    build_action_receipt_template,
    derive_action_id,
    finalize_action_receipt_template,
)


VOICE = {
    "text": "你好",
    "emotion": "温柔",
    "speed": 1.0,
    "pause": "自然",
}


def _id(*, schema_version=2, identity_version=1):
    return derive_action_id(
        source_id="msg-501",
        scope_id="g1",
        kind="voice",
        channel="group",
        target="g1",
        payload=VOICE,
        ordinal=0,
        schema_version=schema_version,
        identity_version=identity_version,
    )


def test_v2_schema_keeps_v1_domain_identity_golden_hash():
    assert _id(schema_version=1, identity_version=1) == "act-871907d2efa3b089"
    assert _id(schema_version=2, identity_version=1) == "act-871907d2efa3b089"
    with pytest.raises(ValueError, match="frozen"):
        _id(schema_version=2, identity_version=2)


@pytest.mark.parametrize("channel,target,scope_id", [
    ("group", "g1", ""),
    ("group", "g1", "group:g1"),
    ("private", "u1", "u1"),
    ("private", "u1", "_private_u2"),
])
def test_v2_requires_canonical_scope_id(channel, target, scope_id):
    payload = VOICE
    with pytest.raises(ValueError, match="scope_id"):
        ActionEnvelope(
            action_id=derive_action_id(
                source_id="msg-1", scope_id=scope_id, kind="voice",
                channel=channel, target=target, payload=payload,
                schema_version=2, identity_version=1,
            ),
            kind="voice", channel=channel, target=target, payload=payload,
            schema_version=2, identity_version=1,
            source_id="msg-1", scope_id=scope_id,
        )


def test_v2_rejects_blank_or_noncanonical_source_identity():
    with pytest.raises(ValueError, match="source_id"):
        ActionEnvelope(
            action_id="act-invalid", kind="voice", channel="group", target="g1",
            payload=VOICE, schema_version=2, source_id="   ", scope_id="g1",
        )


def test_derive_action_id_rejects_blank_source_and_normalizes_whitespace():
    with pytest.raises(ValueError, match="source_id"):
        derive_action_id(
            source_id="  ", scope_id="g1", kind="voice", channel="group",
            target="g1", payload=VOICE,
        )
    assert derive_action_id(
        source_id=" msg-1 ", scope_id="g1", kind="voice", channel="group",
        target="g1", payload=VOICE,
    ) == derive_action_id(
        source_id="msg-1", scope_id="g1", kind="voice", channel="group",
        target="g1", payload=VOICE,
    )
    envelope = ActionEnvelope(
        action_id=derive_action_id(
            source_id="  msg-1  ", scope_id="g1", kind="voice",
            channel="group", target="g1", payload=VOICE,
            schema_version=2,
        ),
        kind="voice", channel="group", target="g1", payload=VOICE,
        schema_version=2, source_id="  msg-1  ", scope_id="g1",
    )
    assert envelope.source_id == "msg-1"


def test_conversation_ref_does_not_change_domain_action_identity():
    one = ActionEnvelope(
        action_id=_id(), kind="voice", channel="group", target="g1",
        payload=VOICE, schema_version=2, identity_version=1,
        source_id="msg-501", scope_id="g1",
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply",
            conversation_user_id="u1", group_id="g1",
        ),
    )
    two = ActionEnvelope(
        action_id=_id(), kind="voice", channel="group", target="g1",
        payload=VOICE, schema_version=2, identity_version=1,
        source_id="msg-501", scope_id="g1",
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply",
            conversation_user_id="u2", group_id="g1",
        ),
    )
    assert one.action_id == two.action_id


@pytest.mark.parametrize("channel,target,ref", [
    ("group", "g1", ConversationRef(
        projection_kind="conversation_reply",
        conversation_user_id="u1", group_id="g1", source_chat_id=7,
    )),
    ("private", "u1", ConversationRef(
        projection_kind="conversation_reply",
        conversation_user_id="u1",
    )),
    ("group", "g1", ConversationRef()),
])
def test_v2_conversation_ref_round_trips_through_receipt(channel, target, ref):
    envelope = ActionEnvelope(
        action_id=derive_action_id(
            source_id="msg-1",
            scope_id="g1" if channel == "group" else "_private_u1",
            kind="voice", channel=channel, target=target, payload=VOICE,
            schema_version=2, identity_version=1,
        ), kind="voice", channel=channel, target=target,
        payload=VOICE, schema_version=2, identity_version=1,
        source_id="msg-1", scope_id="g1" if channel == "group" else "_private_u1",
        conversation_ref=ref,
    )
    template = build_action_receipt_template(envelope, {
        "delivery_kind": "voice", "text": "你好",
    })
    receipt = finalize_action_receipt_template(
        template, status="confirmed", message_ids=(-3,),
    )

    assert receipt["identity_version"] == 1
    assert receipt["conversation_ref"] == ref.to_dict()


def test_v2_receipt_template_revalidates_action_identity():
    envelope = ActionEnvelope(
        action_id=derive_action_id(
            source_id="msg-1", scope_id="g1", kind="voice", channel="group",
            target="g1", payload=VOICE, schema_version=2,
        ),
        kind="voice", channel="group", target="g1", payload=VOICE,
        schema_version=2, source_id="msg-1", scope_id="g1",
    )
    template = build_action_receipt_template(envelope, {"text": "你好"})
    assert "identity_payload" in template
    with pytest.raises(ValueError, match="frozen source identity"):
        finalize_action_receipt_template(
            {**template, "action_id": "act-forged"}, status="confirmed",
        )
    with pytest.raises(ValueError, match="identity_payload"):
        finalize_action_receipt_template(
            {key: value for key, value in template.items()
             if key != "identity_payload"},
            status="confirmed",
        )


@pytest.mark.parametrize("channel,target,ref", [
    ("group", "g1", {
        "projection_kind": "conversation_reply", "conversation_user_id": "",
        "group_id": "g1",
    }),
    ("group", "g1", {
        "projection_kind": "conversation_reply", "conversation_user_id": "u1",
        "group_id": "g2",
    }),
    ("private", "u1", {
        "projection_kind": "conversation_reply", "conversation_user_id": "u2",
    }),
    ("private", "u1", {
        "projection_kind": "conversation_reply", "conversation_user_id": "u1",
        "group_id": "g1",
    }),
])
def test_v2_conversation_ref_rejects_cross_scope_mismatch(channel, target, ref):
    with pytest.raises(ValueError):
        ActionEnvelope(
            action_id="act-invalid", kind="voice", channel=channel, target=target,
            payload=VOICE, schema_version=2, identity_version=1,
            conversation_ref=ref,
        )


@pytest.mark.parametrize("source_chat_id", [0, -1, True, 1.5, "7"])
def test_conversation_ref_source_chat_id_is_strict_positive_integer(source_chat_id):
    with pytest.raises(ValueError):
        ConversationRef(
            projection_kind="conversation_reply",
            conversation_user_id="u1", group_id="g1",
            source_chat_id=source_chat_id,
        )


def test_conversation_ref_is_immutable_and_v1_shape_remains_exact():
    ref = ConversationRef()
    with pytest.raises(FrozenInstanceError):
        ref.group_id = "g1"

    envelope = ActionEnvelope(
        action_id="act-v1", kind="voice", channel="group", target="g1",
        payload=VOICE, schema_version=1,
    )
    data = envelope.to_dict()
    assert "conversation_ref" not in data
    assert "identity_version" not in data


def test_v1_rejects_hidden_v2_conversation_ref():
    with pytest.raises(ValueError):
        ActionEnvelope(
            action_id="act-v1", kind="voice", channel="group", target="g1",
            payload=VOICE, schema_version=1,
            conversation_ref=ConversationRef(
                projection_kind="conversation_reply",
                conversation_user_id="u1", group_id="g1",
            ),
        )
