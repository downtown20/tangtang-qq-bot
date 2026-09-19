"""ADR-002 B1：群/私语音的非 outbox 终局必须写入真实回执。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.action_contract import ActionEnvelope, ConversationRef, derive_action_id
from agent.handler import MessageHandler
from napcat.ws_client import NapCatClient, SendResult


def _envelope(scope_id: str = "g1", channel: str = "group") -> ActionEnvelope:
    target = scope_id if channel == "group" else "u1"
    return ActionEnvelope(
        action_id="act-voice-terminal",
        kind="voice",
        channel=channel,
        target=target,
        payload={
            "text": "你好呀",
            "emotion": "温柔",
            "speed": 0.9,
            "pause": "舒缓",
        },
        source_id=f"{scope_id}:501",
        scope_id=scope_id,
        ordinal=0,
    )


def _v2_envelope(scope_id: str = "g1", channel: str = "group") -> ActionEnvelope:
    target = scope_id if channel == "group" else "u1"
    user_id = "u1"
    payload = {
        "text": "你好呀", "emotion": "温柔", "speed": 0.9, "pause": "舒缓",
    }
    source_id = f"{scope_id}:501"
    action_id = derive_action_id(
        source_id=source_id, scope_id=scope_id, kind="voice", channel=channel,
        target=target, payload=payload, ordinal=0, schema_version=2,
        identity_version=1,
    )
    return ActionEnvelope(
        action_id=action_id,
        kind="voice",
        channel=channel,
        target=target,
        payload=payload,
        schema_version=2,
        identity_version=1,
        source_id=source_id,
        scope_id=scope_id,
        ordinal=0,
        conversation_ref=ConversationRef(
            projection_kind="conversation_reply",
            conversation_user_id=user_id,
            group_id=scope_id if channel == "group" else "",
            source_chat_id=7,
        ),
    )


def _handler(store, send, audio_files):
    handler = MessageHandler.__new__(MessageHandler)
    handler.memory = SimpleNamespace(store=store)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=audio_files),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = SimpleNamespace(
        send_group_message=send,
        send_private_message=send,
    )
    handler.mood = None
    handler._voice_emotion_history = {}
    handler._find_last_group = lambda _user_id: ""
    return handler


def _send(handler, envelope, *, target_type="group", target_id="g1"):
    return asyncio.run(handler._send_voice_reply(
        target_type,
        target_id,
        "[温柔]你好呀",
        speed=0.9,
        pause="舒缓",
        action_envelope=envelope,
    ))


def _only_receipt(store, scope_id):
    lease = store.lease_action_receipts(scope_id)
    assert len(lease["receipts"]) == 1
    return lease["receipts"][0]


def test_confirmed_voice_writes_confirmed_receipt_with_message_id(store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    handler = _handler(
        store,
        AsyncMock(return_value=SendResult(True, True, message_id=-501)),
        [audio],
    )

    assert _send(handler, _envelope()) is True

    receipt = _only_receipt(store, "g1")
    assert receipt["status"] == "confirmed", receipt
    assert receipt["message_ids"] == [-501]
    assert receipt["actual"] == {
        "delivery_kind": "voice",
        "voice_generated": True,
        "fallback_used": False,
        "emotion": "温柔",
        "speed": 0.9,
        "pause": "舒缓",
        "text": "你好呀",
    }


def test_direct_v2_voice_receipt_preserves_identity_and_conversation_ref(store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    handler = _handler(
        store,
        AsyncMock(return_value=SendResult(True, True, message_id=-502)),
        [audio],
    )

    assert _send(handler, _v2_envelope()) is True

    receipt = _only_receipt(store, "g1")
    assert receipt["schema_version"] == 2
    assert receipt["identity_version"] == 1
    assert receipt["conversation_ref"]["conversation_user_id"] == "u1"
    assert receipt["conversation_ref"]["source_chat_id"] == 7


def test_uncertain_voice_writes_uncertain_receipt_without_replay(store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(return_value=SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    ))
    handler = _handler(store, send, [audio])

    assert _send(handler, _envelope()) is False

    receipt = _only_receipt(store, "g1")
    assert receipt["status"] == "uncertain"
    assert receipt["error_code"] == "NETWORK_UNCERTAIN"
    send.assert_awaited_once()


def test_known_voice_failure_then_text_fallback_records_actual_delivery(store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(side_effect=[
        SendResult(False, False, error="BOT_ERROR", retryable=False),
        SendResult(True, True, message_id=77),
    ])
    handler = _handler(store, send, [audio])

    assert _send(handler, _envelope()) is True

    receipt = _only_receipt(store, "g1")
    assert receipt["status"] == "confirmed"
    assert receipt["message_ids"] == [77]
    assert receipt["actual"]["delivery_kind"] == "text"
    assert receipt["actual"]["text"] == "你好呀"
    assert receipt["actual"]["voice_generated"] is True
    assert receipt["actual"]["fallback_used"] is True
    assert receipt["actual"]["fallback_reason"] == "BOT_ERROR"
    assert [call.args[1] for call in send.await_args_list] == [
        "[CQ:record,file=test.wav]", "你好呀",
    ]


def test_retryable_voice_failure_is_owned_by_outbox_and_writes_no_receipt(store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(return_value=SendResult(
        False, False, error="OFFLINE", retryable=True,
    ))
    handler = _handler(store, send, [audio])

    assert _send(handler, _envelope()) is False

    assert store.lease_action_receipts("g1")["receipts"] == []
    send.assert_awaited_once()


def test_tts_failure_text_fallback_records_voice_not_generated(store):
    send = AsyncMock(return_value=SendResult(True, True, message_id=88))
    handler = _handler(store, send, [])

    assert _send(handler, _envelope()) is True

    receipt = _only_receipt(store, "g1")
    assert receipt["actual"]["delivery_kind"] == "text"
    assert receipt["actual"]["voice_generated"] is False
    assert receipt["actual"]["fallback_used"] is True


def test_tts_exception_uses_same_safe_text_fallback_and_receipt(store):
    send = AsyncMock(return_value=SendResult(True, True, message_id=89))
    handler = _handler(store, send, [])
    handler.voice.tts_streaming = AsyncMock(side_effect=RuntimeError("tts crashed"))

    assert _send(handler, _envelope()) is True

    receipt = _only_receipt(store, "g1")
    assert receipt["status"] == "confirmed"
    assert receipt["actual"]["delivery_kind"] == "text"
    assert receipt["actual"]["voice_generated"] is False


def test_unavailable_voice_fallback_response_loss_is_uncertain(store):
    send = AsyncMock(side_effect=RuntimeError("response lost"))
    handler = _handler(store, send, [])
    handler.voice.is_available = False

    assert _send(handler, _envelope()) is False

    receipt = _only_receipt(store, "g1")
    assert receipt["status"] == "uncertain"
    assert receipt["error_code"] == "VOICE_FALLBACK_ERROR"
    assert receipt["actual"]["delivery_kind"] == "text"
    assert receipt["actual"]["text"] == "你好呀"
    assert send.await_args.args[1] == "你好呀"


def test_retryable_voice_receipt_is_written_only_after_outbox_terminal(
        store, tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    client = NapCatClient(testing_mode=True)
    client._qq_online = False
    client.bind_outbox_store(store)
    handler = _handler(store, AsyncMock(), [audio])
    handler.napcat = client

    assert _send(handler, _envelope("100"), target_id="100") is False
    assert store.lease_action_receipts("100")["receipts"] == []
    job = store.list_due_send_outbox()[0]
    assert job["receipt_template"]

    client._qq_online = True
    client._call_api = AsyncMock(return_value={
        "status": "ok", "data": {"message_id": -901},
    })
    assert asyncio.run(client.process_send_outbox()) == 1

    receipt = _only_receipt(store, "100")
    assert receipt["status"] == "confirmed", receipt
    assert receipt["message_ids"] == [-901]
    assert receipt["actual"]["delivery_kind"] == "voice"
