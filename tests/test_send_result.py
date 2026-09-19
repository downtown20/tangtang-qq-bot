"""P2 发送结果语义：保留 bool 兼容，同时暴露送达证据。"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, Mock, patch

import httpx

from napcat.ws_client import (
    NapCatClient,
    SendResult,
    _classify_send_error,
    is_send_confirmed,
    _normalize_message_id,
)


def _run(coro):
    return asyncio.run(coro)


def _client(result):
    client = NapCatClient(testing_mode=True)
    client._qq_online = True

    async def fake_api(_action, _params):
        return {"status": "ok", "data": {"message_id": 1}}

    client._call_api = fake_api
    async def fake_api(_action, _params):
        return {"status": "ok", "data": {"message_id": 1}}
    client._call_api = fake_api
    client._call_api = AsyncMock(return_value=result)
    return client


def test_message_id_normalization_distinguishes_confirmed_and_unconfirmed():
    assert _normalize_message_id(None) == (0, False)
    assert _normalize_message_id(0) == (0, False)
    # OneBot 11 的消息 ID 是 int32，负数是合法取值；只有 0/缺失无证据。
    assert _normalize_message_id(-7) == (-7, True)
    assert _normalize_message_id(42) == (42, True)


def test_message_id_normalization_rejects_non_int32_and_bool_values():
    # OneBot 的 message_id 是有符号 int32；协议脏数据不能成为送达凭据。
    for raw in ("42", 42.0, True, False, 2**31, -(2**31) - 1):
        assert _normalize_message_id(raw) == (0, False)


def test_send_error_classification_only_retries_transport_failures():
    assert _classify_send_error({
        "status": "failed", "retcode": 1400, "msg": "bad params",
    }) == ("BOT_ERROR", False)
    assert _classify_send_error({
        "status": "failed", "retcode": 503,
        "_transport_error": "HTTP_5XX",
    }) == ("HTTP_5XX", True)
    assert _classify_send_error({
        "status": "failed", "_transport_error": "NETWORK",
    }) == ("NETWORK", True)
    assert _classify_send_error({
        "status": "failed", "retcode": 429, "_transport_error": "HTTP_4XX",
    }) == ("HTTP_4XX", False)


def test_call_api_preserves_http_failure_evidence_for_classification():
    class StubHttp:
        def __init__(self, *, response=None, error=None):
            self.response = response
            self.error = error

        async def post(self, *_args, **_kwargs):
            if self.error:
                raise self.error
            return self.response

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._http = StubHttp(response=types.SimpleNamespace(
            status_code=503, text="gateway unavailable",
        ))
        server_error = await client._call_api("send_group_msg", {})
        assert server_error["_transport_error"] == "NETWORK_UNCERTAIN"
        assert _classify_send_error(server_error) == ("NETWORK_UNCERTAIN", False)

        client._http = StubHttp(response=types.SimpleNamespace(
            status_code=400, text="bad request",
        ))
        client_error = await client._call_api("send_group_msg", {})
        assert client_error["_transport_error"] == "HTTP_4XX"
        assert _classify_send_error(client_error) == ("HTTP_4XX", False)

        client._http = StubHttp(error=httpx.ConnectError("connection refused"))
        network_error = await client._call_api("send_group_msg", {})
        assert network_error["_transport_error"] == "NETWORK"
        assert _classify_send_error(network_error) == ("NETWORK", True)

    _run(scenario())


def test_post_response_loss_or_invalid_200_body_is_uncertain():
    class StubResponse:
        status_code = 200
        text = "not-json"

        def json(self):
            raise ValueError("invalid json")

    class StubHttp:
        async def post(self, *_args, **_kwargs):
            return StubResponse()

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._http = StubHttp()

        result = await client._call_api("send_group_msg", {"message": "hello"})

        assert result["_transport_error"] == "NETWORK_UNCERTAIN"
        assert _classify_send_error(result) == ("NETWORK_UNCERTAIN", False)

    _run(scenario())


def test_send_http_5xx_is_uncertain_because_post_may_have_executed():
    class StubHttp:
        async def post(self, *_args, **_kwargs):
            return types.SimpleNamespace(status_code=503, text="gateway unavailable")

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._http = StubHttp()

        result = await client._call_api("send_group_msg", {"message": "hello"})

        assert result["_transport_error"] == "NETWORK_UNCERTAIN"
        assert _classify_send_error(result) == ("NETWORK_UNCERTAIN", False)

    _run(scenario())


def test_group_send_keeps_bool_compatibility_and_records_confirmed_result():
    client = _client({"status": "ok", "data": {"message_id": 42}})

    result = _run(client.send_group_message("100", "hello"))
    assert isinstance(result, SendResult)
    assert bool(result) is True
    assert result == SendResult(True, True, message_id=42)
    assert client._last_send_result == result
    assert client.last_sent_msg_id == 42


def test_zero_message_id_is_ok_but_not_confirmed():
    client = _client({"status": "ok", "data": {"message_id": 0}})

    assert bool(_run(client.send_group_message("100", "hello"))) is True
    assert client._last_send_result.ok is True
    assert client._last_send_result.delivered is False
    assert client._last_send_result.error == "MESSAGE_ID_UNCONFIRMED"


def test_ok_send_with_invalid_data_is_uncertain_not_an_exception():
    for data in (None, [], "bad"):
        client = _client({"status": "ok", "data": data})
        result = _run(client.send_group_message("100", "hello"))
        assert result.ok is True
        assert result.delivered is False
        assert result.uncertain is True
        assert result.error == "INVALID_SEND_RESPONSE"

        client = _client({"status": "ok", "data": data})
        result = _run(client.send_private_message("200", "hello"))
        assert result.ok is True
        assert result.delivered is False
        assert result.uncertain is True
        assert result.error == "INVALID_SEND_RESPONSE"


def test_negative_message_id_is_confirmed_onebot_int32():
    client = _client({"status": "ok", "data": {"message_id": -7}})

    result = _run(client.send_group_message("100", "hello"))

    assert result == SendResult(True, True, message_id=-7)
    assert is_send_confirmed(result) is True
    assert client.last_sent_msg_id == -7


def test_send_confirmed_requires_delivery_evidence_but_keeps_legacy_bool_support():
    assert is_send_confirmed(SendResult(True, True, message_id=42)) is True
    assert is_send_confirmed(SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")) is False
    assert is_send_confirmed(SendResult(False, False, error="BOT_ERROR")) is False
    # Third-party test doubles/old adapters still return bools.
    assert is_send_confirmed(True) is True
    assert is_send_confirmed(False) is False


def test_same_target_sends_are_serialized_in_submission_order():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        first_release = asyncio.Event()
        calls = []

        async def fake_call(_action, params):
            calls.append(params["message"])
            if params["message"] == "first":
                await first_release.wait()
            return {"status": "ok", "data": {"message_id": len(calls)}}

        client._call_api = fake_call
        first = asyncio.create_task(client.send_group_message("100", "first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(client.send_group_message("100", "second"))
        await asyncio.sleep(0)

        assert calls == ["first"]
        first_release.set()
        results = await asyncio.gather(first, second)
        assert calls == ["first", "second"]
        assert all(result.delivered for result in results)

    _run(scenario())


def test_long_group_send_requires_every_chunk_to_be_confirmed():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        client._call_api = AsyncMock(side_effect=[
            {"status": "ok", "data": {"message_id": 0}},
            {"status": "ok", "data": {"message_id": 123}},
        ])
        with patch("napcat.ws_client.asyncio.sleep", new=AsyncMock()):
            result = await client.send_group_message("100", "x" * 2001)
        assert result.ok is True
        assert result.delivered is False
        assert result.error == "MESSAGE_ID_UNCONFIRMED"
        assert result.chunk_ids == (123,)

    _run(scenario())


def test_long_group_partial_delivery_is_uncertain_not_replayable_failure():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        client._call_api = AsyncMock(side_effect=[
            {"status": "ok", "data": {"message_id": 123}},
            {"status": "failed", "retcode": 1400, "msg": "bad params"},
        ])
        with patch("napcat.ws_client.asyncio.sleep", new=AsyncMock()):
            result = await client.send_group_message("100", "x" * 2001)

        assert result.ok is False
        assert result.delivered is False
        assert result.chunk_ids == (123,)
        assert result.delivery_state == "uncertain"

    _run(scenario())


def test_long_group_second_chunk_exception_preserves_partial_uncertainty():
    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._qq_online = True
        client._call_api = AsyncMock(side_effect=[
            {"status": "ok", "data": {"message_id": 123}},
            RuntimeError("response lost after POST"),
        ])
        with patch("napcat.ws_client.asyncio.sleep", new=AsyncMock()):
            result = await client.send_group_message("100", "x" * 2001)
        assert result.ok is False
        assert result.delivered is False
        assert result.uncertain is True
        assert result.chunk_ids == (123,)
        assert "PARTIAL" in result.error

    _run(scenario())


def test_non_friend_failure_is_classified_without_retrying_other_channel():
    client = _client({"status": "failed", "retcode": 16, "msg": "不是好友"})

    assert bool(_run(client.send_private_message("200", "hello"))) is False
    assert client._last_send_result.error == "NO_FRIEND"
    client._call_api.assert_awaited_once()


def test_send_is_not_ready_before_qq_lifecycle_authentication():
    client = NapCatClient(testing_mode=False)
    client._qq_online = False
    client._ws_connected = True

    assert client.ready_to_send is False


def test_websocket_connection_does_not_mark_qq_online_by_itself():
    client = NapCatClient(testing_mode=False)
    client._ws_connected = True

    assert client.qq_online is False


def test_send_lock_tables_reclaim_idle_targets_in_testing_mode():
    import asyncio
    from napcat.ws_client import NapCatClient

    client = NapCatClient(testing_mode=True)
    client._qq_online = True

    async def fake_api(_action, _params):
        return {"status": "ok", "data": {"message_id": 1}}

    client._call_api = fake_api

    async def run():
        for index in range(1000):
            result = await client.send_private_message(str(index), f"消息{index}")
            assert result.delivery_state == "confirmed"

    asyncio.run(run())

    assert client._send_locks == {}
    assert client._send_lock_refs == {}
    assert client._send_lock_owners == {}


def test_lifecycle_online_callback_is_edge_triggered():
    async def scenario():
        client = NapCatClient(testing_mode=False)
        client._ws_connected = True
        online_calls = 0
        offline_calls = 0

        async def online():
            nonlocal online_calls
            online_calls += 1

        async def offline():
            nonlocal offline_calls
            offline_calls += 1

        client.on_qq_online = online
        client.on_qq_offline = offline
        await client._handle_meta({"meta_event_type": "lifecycle", "sub_type": "connect"})
        await client._handle_meta({"meta_event_type": "lifecycle", "sub_type": "enable"})
        assert online_calls == 1
        assert client.ready_to_send is True

        await client._handle_meta({"meta_event_type": "lifecycle", "sub_type": "disable"})
        await client._handle_meta({"meta_event_type": "lifecycle", "sub_type": "disable"})
        assert offline_calls == 1
        assert client.ready_to_send is False

        await client._handle_meta({"meta_event_type": "lifecycle", "sub_type": "enable"})
        assert online_calls == 2

    _run(scenario())


def test_every_qq_reconnect_triggers_outbox_replay_even_after_first_init():
    # main 在 Windows 启动时会重包 stdout；测试导入不应破坏 pytest 捕获器。
    with patch.object(sys, "platform", "linux"):
        from main import TangTang

    async def scenario():
        app = object.__new__(TangTang)
        app._qq_ready_initialized = True
        app.handler = types.SimpleNamespace(refresh_group_info=AsyncMock())
        app._notify_owner_updates = AsyncMock()
        replay = AsyncMock(return_value=0)
        app.napcat = types.SimpleNamespace(process_send_outbox=replay)

        await TangTang._on_qq_online(app)
        await asyncio.sleep(0)

        replay.assert_awaited_once_with()
        app.handler.refresh_group_info.assert_not_awaited()
        app._notify_owner_updates.assert_not_awaited()

    _run(scenario())


def test_first_authenticated_online_event_starts_catch_up_not_transport_connect():
    with patch.object(sys, "platform", "linux"):
        from main import TangTang

    async def scenario():
        catch_up_state = types.SimpleNamespace(
            record_online=Mock(),
            _load_state=Mock(return_value={}),
            _save_state=Mock(),
        )
        app = object.__new__(TangTang)
        app._qq_ready_initialized = False
        app.handler = types.SimpleNamespace(
            refresh_group_info=AsyncMock(),
            catch_up=catch_up_state,
        )
        app._notify_owner_updates = AsyncMock()
        app._catch_up_missed_messages = AsyncMock()
        app.napcat = types.SimpleNamespace(
            process_send_outbox=AsyncMock(return_value=0),
        )

        await TangTang._on_qq_online(app)
        await asyncio.sleep(0)

        catch_up_state.record_online.assert_called_once_with()
        app._catch_up_missed_messages.assert_awaited_once_with()

    _run(scenario())


def test_command_speak_reports_transport_failure_instead_of_optimistic_success():
    from agent.handler_commands import CommandRouter

    send = AsyncMock(return_value=False)
    router = CommandRouter(handler=types.SimpleNamespace(
        napcat=types.SimpleNamespace(send_group_message=send),
    ))

    result = _run(router._cmd_speak("owner", "100 hello"))

    assert "失败" in result
    send.assert_awaited_once_with("100", "hello")


def test_command_speak_exception_is_uncertain_and_discourages_replay():
    from agent.handler_commands import CommandRouter

    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    router = CommandRouter(handler=types.SimpleNamespace(
        napcat=types.SimpleNamespace(send_group_message=send),
    ))

    result = _run(router._cmd_speak("owner", "100 hello"))

    assert "未确认" in result and "请勿" in result
    assert "失败" not in result


def test_command_pm_reports_transport_failure_instead_of_optimistic_success():
    from agent.handler_commands import CommandRouter

    send = AsyncMock(return_value=False)
    router = CommandRouter(handler=types.SimpleNamespace(
        napcat=types.SimpleNamespace(send_private_message=send),
        _resolve_target_qq=lambda target: target,
    ))

    result = _run(router._cmd_pm("owner", "200 hello"))

    assert "失败" in result
    send.assert_awaited_once_with("200", "hello")


def test_command_pm_exception_is_uncertain_and_discourages_replay():
    from agent.handler_commands import CommandRouter

    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    router = CommandRouter(handler=types.SimpleNamespace(
        napcat=types.SimpleNamespace(send_private_message=send),
        _resolve_target_qq=lambda target: target,
    ))

    result = _run(router._cmd_pm("owner", "200 hello"))

    assert "未确认" in result and "请勿" in result
    assert "失败" not in result


def test_command_relay_exception_is_uncertain_and_discourages_replay():
    from agent.handler_commands import CommandRouter

    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    handler = types.SimpleNamespace(
        _resolve_target_qq=lambda target: target,
        memory=types.SimpleNamespace(
            store=types.SimpleNamespace(
                get_or_create_person=lambda *_args: {"nickname": "小蓝"},
                get_recent_dialogue=lambda *_args, **_kwargs: [],
            ),
            recall=lambda *_args, **_kwargs: [],
        ),
        self_state=types.SimpleNamespace(relationships={}),
        personality=types.SimpleNamespace(_cached_base="你是糖糖"),
        _call_llm=AsyncMock(return_value="最近还好吗？糖糖有点想你啦～"),
        _enrich_reply=lambda text: text,
        napcat=types.SimpleNamespace(send_private_message=send),
    )
    router = CommandRouter(handler=handler)

    result = _run(router._do_relay("200", "关心一下"))

    assert "未确认" in result and "请勿" in result
    assert "失败" not in result
    send.assert_awaited_once()


def test_commands_do_not_report_success_for_unconfirmed_send():
    from agent.handler_commands import CommandRouter

    result = SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")
    speak = AsyncMock(return_value=result)
    pm = AsyncMock(return_value=result)
    router = CommandRouter(handler=types.SimpleNamespace(
        napcat=types.SimpleNamespace(
            send_group_message=speak,
            send_private_message=pm,
        ),
        _resolve_target_qq=lambda target: target,
    ))

    speak_result = _run(router._cmd_speak("owner", "100 hello"))
    pm_result = _run(router._cmd_pm("owner", "200 hello"))

    assert "未确认" in speak_result
    assert "未确认" in pm_result
