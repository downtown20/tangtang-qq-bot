"""工具目录分层：核心 schema 常驻，低频能力由 LLM 自主发现后加载。"""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock


def _tool(name, *, deferred=""):
    tool = {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    if deferred:
        tool["_deferred_category"] = deferred
    return tool


def test_group_tool_catalog_respects_channel_and_resource_availability(tmp_path):
    from agent.handler import MessageHandler
    from agent.sticker import StickerManager

    handler = object.__new__(MessageHandler)
    handler.stickers = StickerManager(str(tmp_path))
    handler._current_sticker_role = "default"

    tools = handler._build_memory_tools(
        "user-1", group_id="group-1", has_image=False,
    )
    by_name = {tool["function"]["name"]: tool for tool in tools}

    assert "search_relations" not in by_name
    assert "analyze_image" not in by_name
    assert "_deferred_category" not in by_name["search_facts"]
    assert by_name["get_recent_messages"]["_deferred_category"] == "history"
    assert by_name["get_group_activity"]["_deferred_category"] == "group"


def test_memory_catalog_reduces_initial_schema_tokens(tmp_path):
    from agent.context_builder import estimate_tokens
    from agent.handler import MessageHandler
    from agent.sticker import StickerManager

    handler = object.__new__(MessageHandler)
    handler.stickers = StickerManager(str(tmp_path))
    handler._current_sticker_role = "default"
    tools = handler._build_memory_tools("user-1", has_image=False)
    full_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False))
    initial = [tool for tool in tools if not tool.get("_deferred_category")]
    initial_tokens = estimate_tokens(json.dumps(initial, ensure_ascii=False))

    assert initial_tokens < full_tokens * 0.7


def test_discover_capabilities_loads_deferred_schema_for_next_tool_round():
    from agent.handler import MessageHandler

    initial_names = []
    posted_bodies = []

    async def stream(body, _base, _key):
        initial_names.extend(t["function"]["name"] for t in body["tools"])
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "discover-1",
                "type": "function",
                "function": {
                    "name": "discover_capabilities",
                    "arguments": '{"category":"history"}',
                },
            }],
        }
        return "", msg, 0.1

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {
                "role": "assistant", "content": "已查到记录", "tool_calls": None,
            }}]}

    class LLM:
        async def post(self, _url, **kwargs):
            posted_bodies.append(copy.deepcopy(kwargs["json"]))
            return Response()

    handler = object.__new__(MessageHandler)
    handler.llm = LLM()
    handler._stream_deepseek = AsyncMock(side_effect=stream)
    handler._execute_tool = AsyncMock(side_effect=AssertionError(
        "discover_capabilities 不应进入业务工具执行器"
    ))
    tools = [
        _tool("discover_capabilities"),
        _tool("get_recent_messages", deferred="history"),
    ]

    result = asyncio.run(handler._call_deepseek(
        "system", "user", tools=tools,
        config={
            "api_key": "test", "model": "test", "base_url": "http://test",
            "max_tokens": 128, "temperature": 0,
        },
    ))

    assert initial_names == ["discover_capabilities"]
    assert result == "已查到记录"
    posted_names = [
        tool["function"]["name"] for tool in posted_bodies[0]["tools"]
    ]
    assert posted_names == ["discover_capabilities", "get_recent_messages"]
    assert all("_deferred_category" not in tool for tool in posted_bodies[0]["tools"])


def test_group_memory_evidence_contract_is_injected_once():
    source = open("agent/handler.py", encoding="utf-8").read()
    line = 'backgrounds.append(("证据纪律", _protocols.MEMORY_EVIDENCE_CONTRACT))'

    assert source.count(line) == 1
