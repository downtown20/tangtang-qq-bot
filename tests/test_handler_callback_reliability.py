"""消息回调的忙线释放与一次性问候发送契约。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from onebot.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


def test_busy_turn_guard_releases_owned_busy_state_after_send_exception():
    from agent.handler import _busy_turn_guard

    class Dummy:
        def __init__(self):
            self._busy = False
            self._busy_owner = None
            self._pending_reply = []
            self._safe_task = MagicMock()

        @_busy_turn_guard
        async def callback(self):
            self._busy = True
            self._busy_owner = asyncio.current_task()
            raise RuntimeError("response lost after POST")

    dummy = Dummy()
    try:
        _run(dummy.callback())
    except RuntimeError:
        pass

    assert dummy._busy is False
    assert dummy._busy_owner is None


def _welcome_handler(send):
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._group_blacklist = set()
    handler.config = {"behavior": {
        "welcome_new_members": True,
        "welcome_message": "欢迎 [QQ号]",
    }}
    handler.memory = SimpleNamespace(store=SimpleNamespace(
        get_group_info=lambda _gid: None,
    ))
    handler._fetch_welcome_context = AsyncMock(return_value="")
    handler.personality = SimpleNamespace(_cached_base="人格")
    handler._call_llm_light = AsyncMock(return_value="主欢迎语")
    handler._enrich_reply = lambda text, **_kwargs: text
    handler.napcat = SimpleNamespace(send_group_message=send)
    return handler


def test_welcome_send_exception_does_not_send_a_second_fallback():
    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    handler = _welcome_handler(send)

    _run(handler.handle_group_increase({"group_id": "g1", "user_id": "u1"}))

    send.assert_awaited_once_with("g1", "主欢迎语")


def test_welcome_generation_failure_sends_one_fallback():
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    handler = _welcome_handler(send)
    handler._call_llm_light = AsyncMock(side_effect=RuntimeError("llm down"))

    _run(handler.handle_group_increase({"group_id": "g1", "user_id": "u1"}))

    send.assert_awaited_once_with("g1", "欢迎 u1")


def _friend_handler(send):
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.owner_qq = "owner"
    handler.napcat = SimpleNamespace(
        accept_friend_request=AsyncMock(return_value=True),
        handle_friend_request=AsyncMock(),
        send_private_message=send,
    )
    handler._call_llm_light = AsyncMock(return_value="主问候")
    handler._enrich_reply = lambda text, **_kwargs: text
    return handler


def test_friend_greeting_send_exception_does_not_send_fallback_again():
    send = AsyncMock(side_effect=RuntimeError("response lost after POST"))
    handler = _friend_handler(send)

    _run(handler.handle_friend_request({
        "user_id": "u1", "comment": "你好", "flag": "f1",
    }))

    send.assert_awaited_once_with("u1", "主问候")


def test_friend_greeting_generation_failure_sends_one_fallback():
    send = AsyncMock(return_value=SendResult(True, True, message_id=2))
    handler = _friend_handler(send)
    handler._call_llm_light = AsyncMock(side_effect=RuntimeError("llm down"))

    _run(handler.handle_friend_request({
        "user_id": "u1", "comment": "你好", "flag": "f1",
    }))

    send.assert_awaited_once_with("u1", "你好呀～我是小糖糖，一只猫娘喵~")
