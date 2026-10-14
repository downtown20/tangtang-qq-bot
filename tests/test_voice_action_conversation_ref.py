"""ADR-003：voice producer 显式保存对话 actor，不从发送 target 猜测。"""

from agent.handler import _build_voice_action_envelope


def _actions():
    return {
        "action_source_id": "g1:501",
        "voice_emotion": "温柔",
        "voice_speed": 1.0,
        "voice_pause": "自然",
    }


def test_group_voice_action_binds_user_group_and_source_chat():
    envelope = _build_voice_action_envelope(
        channel="group", target="g1", scope_id="g1",
        requested_text="你好", turn_actions=_actions(),
        conversation_user_id="u1", group_id="g1", source_chat_id=7,
    )
    data = envelope.to_dict()
    assert data["schema_version"] == 2
    assert data["identity_version"] == 1
    assert data["conversation_ref"] == {
        "projection_kind": "conversation_reply",
        "conversation_user_id": "u1",
        "group_id": "g1",
        "source_chat_id": 7,
        "self_memory_eligible": False,
    }


def test_private_voice_action_binds_target_user_without_group():
    envelope = _build_voice_action_envelope(
        channel="private", target="u1", scope_id="_private_u1",
        requested_text="你好", turn_actions={
            **_actions(), "action_source_id": "_private_u1:501",
        }, conversation_user_id="u1", source_chat_id=8,
    )
    ref = envelope.to_dict()["conversation_ref"]
    assert ref["conversation_user_id"] == "u1"
    assert ref["group_id"] == ""
    assert ref["source_chat_id"] == 8


def test_missing_actor_or_batched_source_never_guesses_conversation_owner():
    envelope = _build_voice_action_envelope(
        channel="group", target="g1", scope_id="g1",
        requested_text="你好", turn_actions=_actions(),
        conversation_user_id="", group_id="g1", source_chat_id=0,
    )
    assert envelope.to_dict()["conversation_ref"] == {
        "projection_kind": "none",
        "conversation_user_id": "",
        "group_id": "",
        "source_chat_id": None,
        "self_memory_eligible": False,
    }
