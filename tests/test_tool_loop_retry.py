"""
收尾轮修复测试（2026-08-15）

现场 bug：群友 @糖糖「你之前叫我什么？」→ LLM 连查 3 轮记忆工具后
最后一轮只回空内容 → _call_deepseek 返回 "" → 清洗后回复为空，
群聊静默无回复（用户看到的就是「@了没反应」）。

修复：工具循环结束仍无文字 → 收尾轮（tool_choice=none 禁止再调工具）追问一次。
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler

CFG = {"api_key": "k", "model": "m", "base_url": "http://fake",
       "max_tokens": 100, "temperature": 0.9}

TC = lambda i=1: [{"id": f"call_{i}", "type": "function",
                   "function": {"name": "search_facts",
                                "arguments": '{"subject_qq":"123","query":"称呼"}'}}]


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeLLM:
    """记录 post 次数、按队列返回响应——模拟 DeepSeek API"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def post(self, *a, **k):
        self.calls += 1
        return FakeResp(self.responses.pop(0))


def _make_fake(llm, stream_return, monkeypatch):
    fake = types.SimpleNamespace(llm=llm)
    fake._stream_deepseek = AsyncMock(return_value=stream_return)
    fake._execute_tool = AsyncMock(return_value="工具结果")
    # 循环内 from .skills import get_method_type——按调用时属性取，可 monkeypatch
    import agent.skills as skills_mod
    monkeypatch.setattr(skills_mod, "get_method_type", lambda name: "info")
    return fake


def _msg(content=None, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


class TestToolLoopRetry:
    @staticmethod
    def _voice_tool_fake(monkeypatch, content=""):
        voice_tc = [{
            "id": "call_voice", "type": "function",
            "function": {
                "name": "send_voice",
                "arguments": '{"text":"哥哥～只说这一句","emotion":"撒娇","speed":1.0,"pause":"自然"}',
            },
        }]
        llm = FakeLLM([])
        fake = _make_fake(llm, (content, _msg(content or None, voice_tc), 0.1), monkeypatch)

        async def _execute(name, args, scope, user, turn_actions):
            turn_actions["voice"] = True
            turn_actions["voice_text"] = args["text"]
            return "好的，这条回复会用语音发送"

        fake._execute_tool = _execute
        return fake, llm

    def test_send_voice_text_is_terminal_without_followup(self, monkeypatch):
        """send_voice 已包含最终正文，不再请求 LLM 补一段未发送的文字。"""
        fake, llm = self._voice_tool_fake(monkeypatch)
        actions = {"respond": True}

        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user",
            tools=[{"function": {"name": "send_voice"}}],
            config=CFG, turn_actions=actions,
        ))

        assert result == "哥哥～只说这一句"
        assert llm.calls == 0

    def test_send_voice_text_is_the_recorded_reply(self, monkeypatch):
        """即使模型同时返回文字，真实回复也必须与实际播出的语音正文一致。"""
        fake, llm = self._voice_tool_fake(monkeypatch, content="这段文字没有发送")
        actions = {"respond": True}

        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user",
            tools=[{"function": {"name": "send_voice"}}],
            config=CFG, turn_actions=actions,
        ))

        assert result == "哥哥～只说这一句"
        assert llm.calls == 0

    def test_skip_response_does_not_trigger_forced_final_round(self, monkeypatch):
        """LLM 明确决定沉默后，系统不能再用收尾轮强迫它生成文字。"""
        no_reply_tc = [{
            "id": "call_skip", "type": "function",
            "function": {"name": "skip_response", "arguments": '{"reason":"无需接话"}'},
        }]
        llm = FakeLLM([])
        fake = _make_fake(llm, ("", _msg(None, no_reply_tc), 0.1), monkeypatch)
        actions = {"respond": True}

        async def _execute(name, args, scope, user, turn_actions):
            turn_actions["respond"] = False
            return "好的，这一轮不回复"

        fake._execute_tool = _execute
        import agent.skills as skills_mod
        monkeypatch.setattr(skills_mod, "get_method_type", lambda name: "behavior")

        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user",
            tools=[{"function": {"name": "skip_response"}}],
            config=CFG, turn_actions=actions))

        assert result == ""
        assert actions["respond"] is False
        assert llm.calls == 0

    def test_skip_response_does_not_truncate_media_tool_calls(self, monkeypatch):
        """skip_response 只抑制文本；同批媒体工具调用仍全部执行。"""
        calls = []
        tool_calls = [
            {
                "id": "call_skip", "type": "function",
                "function": {"name": "skip_response", "arguments": '{"reason":"无需接话"}'},
            },
            {
                "id": "call_sticker", "type": "function",
                "function": {"name": "send_stickers", "arguments": '{"emotion":"开心"}'},
            },
        ]
        llm = FakeLLM([])
        fake = _make_fake(llm, ("", _msg(None, tool_calls), 0.1), monkeypatch)

        async def _execute(name, args, scope, user, turn_actions):
            calls.append(name)
            if name == "skip_response":
                turn_actions["respond"] = False
            else:
                turn_actions.setdefault("sticker_intents", []).append({"emotion": "开心"})
            return "工具结果"

        fake._execute_tool = _execute
        monkeypatch.setattr(
            "agent.skills.get_method_type",
            lambda name: "behavior",
        )
        actions = {"respond": True}
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user",
            tools=[
                {"function": {"name": "skip_response"}},
                {"function": {"name": "send_stickers"}},
            ], config=CFG, turn_actions=actions,
        ))
        assert result == ""
        assert actions["respond"] is False
        assert calls == ["skip_response", "send_stickers"]
        assert llm.calls == 0

    def test_empty_after_tools_triggers_final_round(self, monkeypatch):
        """工具循环 3 轮全空 → 收尾轮补出文字"""
        llm = FakeLLM([
            {"choices": [{"message": _msg("", TC(2))}]},   # 轮1 后
            {"choices": [{"message": _msg("", TC(3))}]},   # 轮2 后
            {"choices": [{"message": _msg("")}]},          # 轮3 后（无内容无工具）
            {"choices": [{"message": _msg("想起来了，叫你狠人哥")}]},  # 收尾轮
        ])
        fake = _make_fake(llm, ("", _msg(None, TC(1)), 0.1), monkeypatch)
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", tools=[{"function": {"name": "x"}}], config=CFG))
        assert result == "想起来了，叫你狠人哥"
        assert llm.calls == 4  # 3 轮工具循环 + 1 收尾轮

    def test_final_round_also_empty_returns_empty(self, monkeypatch):
        """收尾轮也空 → 返回空串（上层照旧跳过发送，但不再静默——有告警日志）"""
        llm = FakeLLM([
            {"choices": [{"message": _msg("", TC(2))}]},
            {"choices": [{"message": _msg("")}]},
            {"choices": [{"message": _msg("")}]},
        ])
        fake = _make_fake(llm, ("", _msg(None, TC(1)), 0.1), monkeypatch)
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", tools=[{"function": {"name": "x"}}], config=CFG))
        assert result == ""

    def test_normal_content_no_extra_call(self, monkeypatch):
        """正常有文字回复 → 不触发收尾轮（零额外开销）"""
        llm = FakeLLM([])
        fake = _make_fake(llm, ("直接回复", _msg("直接回复"), 0.1), monkeypatch)
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", tools=[{"function": {"name": "x"}}], config=CFG))
        assert result == "直接回复"
        assert llm.calls == 0

    def test_no_tools_empty_content_no_retry(self, monkeypatch):
        """无工具路径（记忆提取等后台任务）空内容 → 不追加收尾轮（不浪费 API 调用）"""
        llm = FakeLLM([])
        fake = _make_fake(llm, ("", None, 0.1), monkeypatch)
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", config=CFG))
        assert result == ""
        assert llm.calls == 0
