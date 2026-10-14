"""工具循环内的二次 LLM 调用不得死锁。"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler


def _run(coro):
    return asyncio.run(coro)


def test_same_task_can_reenter_llm_without_deadlock():
    """外层 LLM 工具执行期间，同一任务可同步调用一次内层 LLM。"""
    fake = types.SimpleNamespace(
        _llm_lock=asyncio.Lock(),
        _llm_busy=False,
        llm_config={"provider": "deepseek"},
    )

    async def call_deepseek(system_prompt, _user_message, *_args, **_kwargs):
        if system_prompt == "outer":
            return await MessageHandler._call_llm(fake, "inner", "rewrite")
        return "inner-ok"

    fake._call_deepseek = call_deepseek

    result = _run(asyncio.wait_for(
        MessageHandler._call_llm(fake, "outer", "run tool"), timeout=0.2,
    ))

    assert result == "inner-ok"
    assert fake._llm_lock.locked() is False
    assert fake._llm_busy is False


def test_long_group_say_tool_completes_once_inside_outer_llm():
    """真实 group_say 长文本路径会二次改写；它必须完成且只发送一次。"""
    sent = AsyncMock(return_value=True)
    fake = types.SimpleNamespace(
        _llm_lock=asyncio.Lock(),
        _llm_busy=False,
        llm_config={"provider": "deepseek"},
        owner_qq="owner",
        _allowed_groups={"group-1"},
        personality=types.SimpleNamespace(
            build_system_prompt=lambda *_args, **_kwargs: "group-rewrite",
        ),
        napcat=types.SimpleNamespace(send_group_message=sent),
        _get_active_members=lambda _group_id: "",
        _get_admin_groups=lambda _owner_id: [],
        _enrich_reply=lambda reply, **_kwargs: reply,
        _last_group_say=None,
    )
    fake._execute_natural_action = types.MethodType(
        MessageHandler._execute_natural_action, fake,
    )

    async def call_with_skills(system_prompt, user_message, **_kwargs):
        reply = await MessageHandler._call_llm(fake, system_prompt, user_message)
        return reply, {}

    fake._call_llm_with_skills = call_with_skills

    async def call_deepseek(system_prompt, _user_message, *_args, **_kwargs):
        if system_prompt == "outer":
            return await MessageHandler._execute_tool(
                fake,
                "group_say",
                {"group_id": "group-1", "message": "这是一段需要改写后发送的长消息。" * 8},
                "_private_owner",
                "owner",
                {},
            )
        return "改写完成的群消息"

    fake._call_deepseek = call_deepseek

    result = _run(asyncio.wait_for(
        MessageHandler._call_llm(fake, "outer", "invoke group_say"), timeout=0.3,
    ))

    assert "已在群group-1发言" in result
    sent.assert_awaited_once_with("group-1", "改写完成的群消息")
    assert fake._llm_lock.locked() is False

