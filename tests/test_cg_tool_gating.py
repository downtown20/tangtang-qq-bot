"""亲密模式 CG 工具只投影给当前已激活用户。"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.handler import MessageHandler


CG_ACTIVE_STATUS = "❤️ 亲密模式已激活——CG 贴图能力可用，氛围到位时可以发"


def _make_handler(active_users=None):
    handler = object.__new__(MessageHandler)
    handler._sed_active = dict(active_users or {})
    handler.cg_stickers = SimpleNamespace(has_stickers=lambda: True)
    handler.voice_enabled = False
    handler.memory = None
    handler._call_llm = AsyncMock(return_value="好的")
    return handler


def _call_for(handler, current_user):
    with patch("agent.skills.list_skills", return_value=[]):
        asyncio.run(handler._call_llm_with_skills(
            "system", "user", current_user=current_user,
        ))
    call = handler._call_llm.await_args
    system_prompt = call.args[0]
    tool_names = {
        tool["function"]["name"] for tool in (call.kwargs["tools"] or [])
    }
    return system_prompt, tool_names


def test_active_user_gets_cg_tool_and_one_line_status():
    handler = _make_handler({"user-a": time.time() + 3600})

    system_prompt, tool_names = _call_for(handler, "user-a")

    assert "send_cg_sticker" in tool_names
    assert CG_ACTIVE_STATUS in system_prompt


def test_cg_tool_description_names_content_without_misplaced_warning():
    handler = _make_handler({"user-a": time.time() + 3600})

    with patch("agent.skills.list_skills", return_value=[]):
        asyncio.run(handler._call_llm_with_skills(
            "system", "user", current_user="user-a",
        ))
    tools = handler._call_llm.await_args.kwargs["tools"] or []
    description = next(
        tool["function"]["description"]
        for tool in tools
        if tool["function"]["name"] == "send_cg_sticker"
    )

    assert "卧室亲密场景" in description
    assert "害羞" in description
    assert "日常闲聊" not in description
    assert "尴尬比错过更糟" not in description


def test_inactive_user_gets_neither_cg_tool_nor_status():
    handler = _make_handler()

    system_prompt, tool_names = _call_for(handler, "user-a")

    assert "send_cg_sticker" not in tool_names
    assert CG_ACTIVE_STATUS not in system_prompt


def test_expired_ttl_hides_cg_tool_and_is_lazily_cleaned():
    handler = _make_handler({"user-a": time.time() - 1})

    system_prompt, tool_names = _call_for(handler, "user-a")

    assert "send_cg_sticker" not in tool_names
    assert CG_ACTIVE_STATUS not in system_prompt
    assert "user-a" not in handler._sed_active


def test_ttl_equal_to_now_is_already_expired():
    handler = _make_handler({"user-a": 100.0})

    with patch("time.time", return_value=100.0):
        assert handler._is_sed_active("user-a") is False

    assert "user-a" not in handler._sed_active


def test_empty_current_user_fails_closed():
    handler = _make_handler({"user-a": time.time() + 3600})

    system_prompt, tool_names = _call_for(handler, "")

    assert "send_cg_sticker" not in tool_names
    assert CG_ACTIVE_STATUS not in system_prompt


def test_group_turn_uses_current_speaker_for_cg_gate():
    handler = _make_handler({"user-a": time.time() + 3600})

    system_prompt, tool_names = _call_for(handler, "user-b")

    assert "send_cg_sticker" not in tool_names
    assert CG_ACTIVE_STATUS not in system_prompt


def test_cg_gate_does_not_replace_existing_send_chain_contract():
    source = (
        Path(__file__).resolve().parent.parent / "agent" / "handler.py"
    ).read_text(encoding="utf-8")

    assert source.count("await self._send_cg_actions(") == 2
    assert 'if name == "send_cg_sticker":' in source
    assert 'turn_actions["cg"] = True' in source
