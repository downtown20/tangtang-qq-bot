"""handler 的直发、语音与唱歌路径遵守 confirmed 提交边界。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from onebot.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


def test_pending_draft_is_retained_when_send_is_unconfirmed():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
    }
    handler.napcat = SimpleNamespace(send_group_message=AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    )))
    handler._save_state_kv = MagicMock(return_value=True)

    result = _run(handler._execute_natural_action(
        {"action": "send_pending"}, "owner", is_privileged=True,
    ))

    assert "未确认" in result
    assert handler._pending_pm["message"] == "草稿"
    assert handler._pending_pm["status"] == "uncertain"
    assert handler._save_state_kv.call_count == 2


def test_pending_draft_is_cleared_only_after_confirmed_send():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
    }
    handler.napcat = SimpleNamespace(send_group_message=AsyncMock(return_value=SendResult(
        True, True, message_id=42,
    )))
    handler._save_state_kv = MagicMock(return_value=True)

    result = _run(handler._execute_natural_action(
        {"action": "send_pending"}, "owner", is_privileged=True,
    ))

    assert "已发送" in result
    assert handler._pending_pm is None
    assert handler._save_state_kv.call_count == 2
    handler._save_state_kv.assert_called_with("state:pending_pm", None)


def test_pending_draft_does_not_post_when_sending_marker_cannot_persist():
    from agent.handler import MessageHandler

    send = AsyncMock(return_value=SendResult(True, True, message_id=42))
    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
    }
    handler.napcat = SimpleNamespace(send_group_message=send)
    handler._save_state_kv = MagicMock(return_value=False)

    result = _run(handler._execute_natural_action(
        {"action": "send_pending"}, "owner", is_privileged=True,
    ))

    assert "未发送" in result
    assert handler._pending_pm["status"] == "pending"
    send.assert_not_awaited()


def test_pending_draft_send_exception_is_frozen_as_uncertain():
    from agent.handler import MessageHandler

    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
    }
    handler.napcat = SimpleNamespace(send_group_message=send)
    handler._save_state_kv = MagicMock(return_value=True)

    result = _run(handler._execute_natural_action(
        {"action": "send_pending"}, "owner", is_privileged=True,
    ))

    assert "未确认" in result and "请勿" in result
    assert handler._pending_pm["status"] == "uncertain"
    send.assert_awaited_once()


def test_pending_draft_restart_quarantines_sending_state():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
        "status": "sending",
    }
    handler._save_state_kv = MagicMock(return_value=True)

    handler._recover_pending_draft()

    assert handler._pending_pm["status"] == "uncertain"
    handler._save_state_kv.assert_called_once()


def test_pending_draft_uncertain_state_refuses_manual_replay():
    from agent.handler import MessageHandler

    send = AsyncMock()
    handler = object.__new__(MessageHandler)
    handler._pending_pm = {
        "group_id": "g1", "message": "草稿", "user_id": "owner",
        "status": "uncertain",
    }
    handler.napcat = SimpleNamespace(send_group_message=send)

    result = _run(handler._execute_natural_action(
        {"action": "send_pending"}, "owner", is_privileged=True,
    ))

    assert "避免重复" in result
    send.assert_not_awaited()


def test_voice_reply_returns_false_for_unconfirmed_audio_send(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=[audio]),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = SimpleNamespace(send_group_message=AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    )))
    handler.mood = None
    handler._voice_emotion_history = {}

    sent = _run(handler._send_voice_reply("group", "g1", "你好呀"))

    assert sent is False


def test_voice_known_failure_falls_back_to_text_once(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(side_effect=[
        SendResult(False, False, error="BOT_ERROR", retryable=False),
        SendResult(True, True, message_id=43),
    ])
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=[audio]),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = SimpleNamespace(send_group_message=send)
    handler.mood = None
    handler._voice_emotion_history = {}

    sent = _run(handler._send_voice_reply("group", "g1", "你好呀"))

    assert sent is True
    assert [call.args[1] for call in send.await_args_list] == [
        "[CQ:record,file=test.wav]", "你好呀",
    ]


@pytest.mark.parametrize("target_type", ["group", "private"])
def test_voice_retryable_failure_queues_voice_without_text_fallback(
        tmp_path, target_type):
    """可重试语音已由 outbox 接管时，不能再排一条降级文字造成恢复后双发。"""
    from agent.handler import MessageHandler
    from agent.store import Store
    from onebot.ws_client import NapCatClient

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    store = Store(str(tmp_path / "memory.db"))
    client = NapCatClient(testing_mode=True)
    client.bind_outbox_store(store)
    assert client.ready_to_send is False

    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=[audio]),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = client
    handler.mood = None
    handler._voice_emotion_history = {}
    handler._find_last_group = lambda _user_id: ""

    result = _run(handler._send_voice_reply(
        target_type, "g1", "你好呀", raw_result=True,
    ))

    jobs = store.list_due_send_outbox(limit=10)
    assert result.retryable is True
    assert len(jobs) == 1
    assert jobs[0]["target_type"] == target_type
    assert jobs[0]["message"] == "[CQ:record,file=test.wav]"


def test_voice_uncertain_result_never_falls_back_to_duplicate_text(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(return_value=SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    ))
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=[audio]),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = SimpleNamespace(send_group_message=send)
    handler.mood = None
    handler._voice_emotion_history = {}

    sent = _run(handler._send_voice_reply("group", "g1", "你好呀"))

    assert sent is False
    send.assert_awaited_once_with("g1", "[CQ:record,file=test.wav]")


def test_private_voice_uses_allowed_recent_group_for_temp_session(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"audio")
    send = AsyncMock(return_value=SendResult(True, True, message_id=9))
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = SimpleNamespace(
        is_available=True,
        voice_description=lambda _emotion: "test",
        tts_streaming=AsyncMock(return_value=[audio]),
        to_cq=lambda _path: "[CQ:record,file=test.wav]",
    )
    handler.napcat = SimpleNamespace(send_private_message=send)
    handler._find_last_group = lambda _qq: "g1"
    handler.mood = None
    handler._voice_emotion_history = {}

    assert _run(handler._send_voice_reply("private", "u1", "你好呀")) is True
    send.assert_awaited_once_with(
        "u1", "[CQ:record,file=test.wav]", group_id="g1",
    )


def test_find_last_group_rejects_blacklisted_or_unallowed_scope():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(store=SimpleNamespace(
        find_last_group=lambda _qq: "g1",
    ))
    handler._allowed_groups = {"g1"}
    handler._group_blacklist = {"g1"}
    assert handler._find_last_group("u1") == ""

    handler._group_blacklist = set()
    handler._allowed_groups = {"g2"}
    assert handler._find_last_group("u1") == ""

    handler._allowed_groups = {"g1"}
    assert handler._find_last_group("u1") == "g1"


def test_song_section_returns_false_and_records_uncertain_state(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    handler = object.__new__(MessageHandler)
    handler.songs = SimpleNamespace(
        get_section_audio=lambda *_args: str(audio),
    )
    handler.napcat = SimpleNamespace(send_group_message=AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    )))

    sent = _run(handler._send_song_section(
        "group", "g1", {"title": "测试歌"}, "副歌",
    ))

    assert sent is False
    assert handler._last_song_send_state == "uncertain"


def test_song_send_exception_is_uncertain_and_does_not_fallback_to_tts(tmp_path):
    from agent.handler import MessageHandler

    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    handler = object.__new__(MessageHandler)
    handler.songs = SimpleNamespace(
        get_default_section=lambda _title: {"name": "副歌"},
        get_section_audio=lambda *_args: str(audio),
        get_section=lambda *_args: {"text": "副歌歌词"},
    )
    handler.napcat = SimpleNamespace(
        send_group_message=AsyncMock(side_effect=RuntimeError("response lost after POST")),
    )
    handler._send_voice_reply = AsyncMock(return_value=True)
    handler._persist_terminal_action_receipt = MagicMock(return_value=True)

    stats = _run(handler._send_singing_actions(
        "group", "g1", {"title": "测试歌"}, "[SING:副歌]副歌歌词",
        action_source_id="command_sing:group:g1:501",
    ))

    assert stats == {"attempted": 1, "confirmed": 0, "uncertain": 1, "failed": 0}
    assert handler._last_song_send_state == "uncertain"
    handler._send_voice_reply.assert_not_awaited()


def test_singing_failure_does_not_release_drive():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.songs = SimpleNamespace(
        get_default_section=lambda _title: {"name": "副歌"},
        get_section=lambda *_args: {"text": "副歌歌词"},
    )
    handler._send_song_section = AsyncMock(return_value=SendResult(
        False, False, error="SONG_AUDIO_MISSING",
    ))
    handler._send_voice_reply = AsyncMock(return_value=SendResult(
        False, False, error="VOICE_SEND_ERROR",
    ))
    handler._persist_terminal_action_receipt = MagicMock(return_value=True)
    release = MagicMock()
    handler.self_state = SimpleNamespace(
        drives=SimpleNamespace(release_by_action=release),
    )

    stats = _run(handler._send_singing_actions(
        "private", "u1", {"title": "测试歌"}, "歌词", "rvc",
        action_source_id="command_sing:private:u1:502",
    ))

    assert stats == {"attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1}
    release.assert_not_called()


def test_singing_success_releases_drive_once():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.songs = SimpleNamespace(
        get_default_section=lambda _title: {"name": "副歌"},
    )
    handler._send_song_section = AsyncMock(return_value=SendResult(
        True, True, message_id=503,
    ))
    handler._persist_terminal_action_receipt = MagicMock(return_value=True)
    release = MagicMock()
    handler.self_state = SimpleNamespace(
        drives=SimpleNamespace(release_by_action=release),
    )

    stats = _run(handler._send_singing_actions(
        "group", "g1", {"title": "测试歌"}, "歌词", "rvc",
        action_source_id="command_sing:group:g1:503",
    ))

    assert stats == {"attempted": 1, "confirmed": 1, "uncertain": 0, "failed": 0}
    release.assert_called_once_with("sang_or_played")


def test_command_sing_uses_platform_event_as_action_source():
    """同一平台命令事件必须生成稳定 source，供 sing child receipt 防重放。"""
    from agent.handler_commands import CommandRouter

    song = {"title": "测试歌"}
    handler = SimpleNamespace(
        songs=SimpleNamespace(
            search=lambda _query: song,
            list_songs=lambda: ["测试歌"],
        ),
    )
    router = CommandRouter(handler)
    router._sing_in_group = AsyncMock()
    router._spawn_background = MagicMock()

    result = _run(router._cmd_sing(
        "owner", "10006 测试歌", event_key="group:10006:504",
    ))

    assert result == "🎤 糖糖要在群10006唱《测试歌》啦~"
    router._sing_in_group.assert_called_once_with(
        "10006", song, action_source_id="command_sing:group:10006:504",
    )
    router._spawn_background.assert_called_once()
    router._spawn_background.call_args.args[0].close()


def test_command_sing_keeps_raw_marker_for_action_plan_after_cleaning():
    """`/唱歌` 的文字继续清洗，但 ActionPlan 必须收到清洗前 marker。"""
    from agent.handler_commands import CommandRouter

    raw_reply = "开唱啦[SING:副歌]副歌歌词"
    song = {"title": "测试歌"}
    sender = AsyncMock(return_value=SendResult(True, True, message_id=505))
    actions = AsyncMock(return_value={
        "attempted": 1, "confirmed": 1, "uncertain": 0, "failed": 0,
    })
    handler = SimpleNamespace(
        songs=SimpleNamespace(build_sing_prompt=lambda _song: "sing prompt"),
        personality=SimpleNamespace(build_system_prompt=lambda **_kwargs: "system"),
        _call_llm=AsyncMock(return_value=raw_reply),
        reply=SimpleNamespace(clean=lambda text: text.replace("[SING:副歌]", "")),
        napcat=SimpleNamespace(send_group_message=sender),
        _send_singing_actions=actions,
    )
    router = CommandRouter(handler)

    _run(router._sing_in_group(
        "g1", song, action_source_id="command_sing:group:g1:505",
    ))

    sender.assert_awaited_once_with("g1", "开唱啦副歌歌词")
    actions.assert_awaited_once_with(
        "group", "g1", song, raw_reply,
        action_source_id="command_sing:group:g1:505",
    )


def test_command_sing_does_not_append_audio_after_uncertain_text_delivery():
    """保持旧语义：歌词提交未确认时，不追加可能重复的音频。"""
    from agent.handler_commands import CommandRouter

    handler = SimpleNamespace(
        songs=SimpleNamespace(build_sing_prompt=lambda _song: "sing prompt"),
        personality=SimpleNamespace(build_system_prompt=lambda **_kwargs: "system"),
        _call_llm=AsyncMock(return_value="歌词[SING:副歌]"),
        reply=SimpleNamespace(clean=lambda text: text.replace("[SING:副歌]", "")),
        napcat=SimpleNamespace(send_group_message=AsyncMock(return_value=SendResult(
            True, False, error="MESSAGE_ID_UNCONFIRMED",
        ))),
        _send_singing_actions=AsyncMock(),
    )
    router = CommandRouter(handler)

    _run(router._sing_in_group(
        "g1", {"title": "测试歌"},
        action_source_id="command_sing:group:g1:506",
    ))

    handler._send_singing_actions.assert_not_awaited()


def test_singing_actions_parse_marker_before_cleaning_and_persist_child_receipt(tmp_path):
    """唱歌 marker 在清洗前冻结为 sing child，送达由 ActionExecutor receipt 结算。"""
    from agent.handler import MessageHandler

    audio = tmp_path / "chorus.wav"
    audio.write_bytes(b"audio")
    sender = AsyncMock(return_value=SendResult(True, True, message_id=777))
    handler = object.__new__(MessageHandler)
    handler.songs = SimpleNamespace(
        get_section_audio=lambda *_args: str(audio),
        get_default_section=lambda _title: {"name": "副歌"},
        get_section=lambda *_args: {"text": "副歌歌词"},
    )
    handler.napcat = SimpleNamespace(send_group_message=sender)
    handler._persist_terminal_action_receipt = MagicMock(return_value=True)
    handler.self_state = SimpleNamespace(
        drives=SimpleNamespace(release_by_action=MagicMock()),
    )

    stats = _run(handler._send_singing_actions(
        "group", "g1", {"title": "测试歌", "id": 9}, "唱这里[SING:副歌]", "rvc",
        action_source_id="g1:501", ordinal_start=4,
    ))

    assert stats == {"attempted": 1, "confirmed": 1, "uncertain": 0, "failed": 0}
    sender.assert_awaited_once()
    assert sender.await_args.args[1].startswith("[CQ:record,file=file:///")
    assert sender.await_args.kwargs["receipt_template"]["kind"] == "sing"
    envelope = handler._persist_terminal_action_receipt.call_args.args[0]
    assert envelope.kind == "sing"
    assert envelope.ordinal == 4


def test_natural_sing_reports_audio_failure_instead_of_success():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    song = {"title": "测试歌"}
    handler.songs = SimpleNamespace(search=lambda _name: song)
    handler._build_singing_system_prompt = MagicMock(return_value="prompt")
    handler._call_llm = AsyncMock(return_value="歌词")
    handler._clean_reply = lambda text: text
    handler.napcat = SimpleNamespace(send_private_message=AsyncMock(return_value=SendResult(
        True, True, message_id=8,
    )))
    handler.voice_enabled = True
    handler._is_voice_blocked = lambda _scope: False
    handler._send_singing_actions = AsyncMock(return_value={
        "attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1,
    })

    result = _run(handler._execute_natural_action(
        {"action": "sing", "song": "测试歌"}, "owner",
    ))

    assert "音频" in result
    assert not result.startswith("✅")
    assert handler._send_singing_actions.await_args.kwargs["action_source_id"].startswith(
        "natural_sing:"
    )


def test_control_tool_does_not_turn_empty_action_result_into_success():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.owner_qq = "owner"
    handler._allowed_groups = {"g1"}
    handler._is_group_owner = lambda _qq: False
    handler._execute_natural_action = AsyncMock(return_value=None)

    group_result = _run(handler._execute_tool(
        "group_say", {"group_id": "g1", "message": "测试"},
        "_private_owner", "owner", {},
    ))
    private_result = _run(handler._execute_tool(
        "send_private_message", {"qq": "123", "message": "测试"},
        "_private_owner", "owner", {},
    ))

    assert "失败" in group_result and "✅" not in group_result
    assert "失败" in private_result and "✅" not in private_result


def test_control_tool_send_exception_is_reported_as_uncertain_not_failed():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.owner_qq = "owner"
    handler._allowed_groups = {"g1"}
    handler._is_group_owner = lambda _qq: False
    handler._execute_natural_action = AsyncMock(
        side_effect=RuntimeError("response lost after POST"),
    )

    result = _run(handler._execute_tool(
        "group_say", {"group_id": "g1", "message": "测试"},
        "_private_owner", "owner", {},
    ))

    assert "未确认" in result
    assert "请勿重发" in result
    assert "未发送" not in result


def test_recall_is_bound_to_current_group_not_global_last_send():
    from agent.handler import MessageHandler
    from onebot.ws_client import NapCatClient

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        client._call_api = AsyncMock(side_effect=[
            {"status": "ok", "data": {"message_id": 101}},
            {"status": "ok", "data": {"message_id": 202}},
            {"status": "ok", "data": {}},
        ])
        await client.send_group_message("100", "A群消息")
        await client.send_group_message("200", "B群消息")

        handler = object.__new__(MessageHandler)
        handler.napcat = client
        handler.owner_qq = "owner"
        handler._allowed_groups = {"100", "200"}
        handler._get_admin_groups = lambda _qq: []

        result = await handler._execute_natural_action(
            {"action": "recall_msg", "group_id": "100"},
            "owner", is_privileged=True,
        )

        assert "已撤回" in result
        assert client._call_api.await_args_list[-1].args == (
            "delete_msg", {"message_id": 101},
        )
        assert client.get_last_sent_message_id("group", "100") == 0
        assert client.get_last_sent_message_id("group", "200") == 202

    _run(scenario())


def test_recall_execution_boundary_rejects_unprivileged_direct_call():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.napcat = SimpleNamespace(
        recall_message=AsyncMock(return_value=True),
        get_last_sent_message_id=lambda *_args: 101,
    )
    handler.owner_qq = "owner"
    handler._allowed_groups = {"100"}

    result = _run(handler._execute_natural_action(
        {"action": "recall_msg", "group_id": "100"},
        "ordinary-member", is_privileged=False,
    ))

    assert "只有主人和群主" in result
    handler.napcat.recall_message.assert_not_awaited()
