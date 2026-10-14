"""SnowLuma 发送层的长消息回归测试。"""

import asyncio

from onebot import ws_client
from onebot.ws_client import NapCatClient, _split_message_chunks


def test_split_long_message_preserves_text_and_cq_code():
    cq = "[CQ:image,file=file:///tmp/picture.png]"
    message = "甲" * 1990 + cq + "乙" * 2510

    chunks = _split_message_chunks(message)

    assert "".join(chunks) == message
    assert all(len(chunk) <= 2000 for chunk in chunks)
    assert sum(cq in chunk for chunk in chunks) == 1


def test_send_long_group_message_uses_all_chunks(monkeypatch):
    client = NapCatClient()
    client._qq_online = True
    client._ws_connected = True
    sent = []

    async def fake_call(action, params):
        sent.append(params["message"])
        return {"status": "ok", "data": {"message_id": len(sent)}}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(client, "_call_api", fake_call)
    monkeypatch.setattr(ws_client.asyncio, "sleep", no_sleep)
    message = "长消息" * 1500

    assert bool(asyncio.run(client.send_group_message("123", message))) is True
    assert len(sent) >= 2
    assert "".join(sent) == message


def test_clean_websocket_close_runs_disconnect_callback(monkeypatch):
    captured = {}

    async def fake_serve(handler, *_args, **_kwargs):
        captured["handler"] = handler
        return object()

    class CleanlyClosedWebSocket:
        remote_address = ("127.0.0.1", 12345)

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def scenario():
        client = NapCatClient()
        events = []

        async def on_connected():
            events.append("connected")

        async def on_disconnected():
            events.append("disconnected")

        client.on_connected = on_connected
        client.on_disconnected = on_disconnected
        monkeypatch.setattr(ws_client.websockets, "serve", fake_serve)
        await client.start_server()
        await captured["handler"](CleanlyClosedWebSocket())
        client._running = False
        client._heartbeat_task.cancel()
        try:
            await client._heartbeat_task
        except asyncio.CancelledError:
            pass
        return events, client.client_ws

    events, current_socket = asyncio.run(scenario())
    assert events == ["connected", "disconnected"]
    assert current_socket is None


def test_start_server_failure_resets_runtime_gate(monkeypatch):
    """端口绑定失败后不能留下一个看似运行、实际无 server 的客户端。"""

    async def fail_serve(*_args, **_kwargs):
        raise OSError(10048, "address already in use")

    async def scenario():
        client = NapCatClient()
        monkeypatch.setattr(ws_client.websockets, "serve", fail_serve)
        try:
            await client.start_server()
        except OSError:
            pass
        return client._running, client._accept_events, client.server

    running, accepting, server = asyncio.run(scenario())
    assert running is False
    assert accepting is False
    assert server is None
