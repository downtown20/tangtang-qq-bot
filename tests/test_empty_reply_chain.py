"""空回复链路回归测试（2026-08-17 调查 + 2026-08-18 Codex 审查修复）

现场：日志 120 次「LLM 工具循环+收尾轮后仍无文字回复」警告。取证结论：
- 117/120 是「流式完成 0字 + 25~40s」——deepseek-v4 是推理模型，思考把
  max_tokens 烧满 → finish_reason=length → 内容为空
- 0 次发生在带工具调用的主回复路径；绝大多数是后台轻量调用（事实簇提取
  24 次、语义提取/反思等其余），空内容没有收尾轮保护且游标不推进 → 同批次反复重试
- 另有 1 例：流式降级非流式分支直接 return，空内容绕过收尾轮 → 主回复静默丢弃

修复：
1. 纯 JSON 提取任务（语义提取/事实簇）关思考（THINKING_OFF）——实测输出
   相同、快 12 倍、绝无预算耗尽
2. 轻量调用空内容重试（与异常重试统一为最多两次 attempt）
3. 降级分支不再早退——收尾轮保护覆盖降级空内容
4. 流式完成日志带 finish_reason 与思考字数

Codex 审查（2026-08-18 Request Changes → 全修）新增回归：
- I1 降级必须替换而非追加（半截流式片段不得与降级完整回复拼接）
- I2 降级响应带的 tool_calls 不得被公共路径重置吞掉
- I3 finish_reason 从 choices[0] 层级读取（旧代码读 delta 永远取不到）
"""
import asyncio
import ast
import logging
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler, THINKING_OFF

CFG = {"api_key": "k", "model": "m", "base_url": "http://fake",
       "max_tokens": 100, "temperature": 0.9}


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeStreamCM:
    """可编程 SSE 流：逐行产出 lines；产出第 raise_after 行后抛异常（模拟流中断）"""

    def __init__(self, lines, raise_after=None):
        self._lines = list(lines)
        self._raise_after = raise_after
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def aiter_lines(self):
        async def _gen():
            for i, ln in enumerate(self._lines):
                yield ln
                if self._raise_after is not None and i + 1 == self._raise_after:
                    raise RuntimeError("模拟流中断")
        return _gen()


class FakeLLM:
    """post 按队列返回；stream 返回 stream_cm（None 则抛异常触发降级）"""

    def __init__(self, responses, stream_cm=None):
        self.responses = list(responses)
        self.calls = 0
        self.stream_cm = stream_cm

    async def post(self, *a, **k):
        self.calls += 1
        return FakeResp(self.responses.pop(0))

    def stream(self, *a, **k):
        if self.stream_cm is None:
            raise RuntimeError("模拟流式故障")
        return self.stream_cm


def _light_handler():
    h = object.__new__(MessageHandler)
    h._llm_lock = asyncio.Lock()
    h._llm_busy = False
    h.llm_config = {"provider": "deepseek"}
    h.metrics = MagicMock()
    return h


def _stream_real(fake):
    """绑定真实 _stream_deepseek——降级分支测试必须走真实现"""
    fake._stream_deepseek = types.MethodType(MessageHandler._stream_deepseek, fake)
    return fake


def _msg(content=None, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


TC = lambda: [{"id": "c1", "type": "function",
               "function": {"name": "x", "arguments": "{}"}}]

SSE = lambda delta, fr=None: (
    'data: {"id":"1","choices":[{"index":0,"delta":'
    + str(delta).replace("'", '"')
    + ',"finish_reason":' + (fr if fr else "null") + "}]}"
)


class TestLightEmptyRetry:
    def test_empty_budget_exhaustion_is_classified(self, monkeypatch):
        """轻量推理耗尽预算与普通空文本分开计数，且仍只重试一次。"""
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        h = _light_handler()
        async def fake_call(s, u, extra_body=None):
            h._last_llm_diagnostic = {
                "finish_reason": "length", "reasoning_chars": 5643,
            }
            return ""
        h._call_deepseek = fake_call

        out = asyncio.run(h._call_llm_light("s", "u"))

        assert out == ""
        names = [call.args[0] for call in h.metrics.incr.call_args_list]
        assert "light_budget_exhausted" in names

    def test_transport_failure_is_classified(self, monkeypatch):
        """传输异常不会被吞成无来源的空回复指标。"""
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        h = _light_handler()
        async def fake_call(s, u, extra_body=None):
            raise RuntimeError("gateway down")
        h._call_deepseek = fake_call

        out = asyncio.run(h._call_llm_light("s", "u"))

        assert out == ""
        names = [call.args[0] for call in h.metrics.incr.call_args_list]
        assert "light_transport_error" in names

    def test_plain_empty_is_not_reported_as_budget_exhaustion(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        h = _light_handler()
        async def fake_call(s, u, extra_body=None):
            h._last_llm_diagnostic = {"finish_reason": "stop", "reasoning_chars": 0}
            return ""
        h._call_deepseek = fake_call

        asyncio.run(h._call_llm_light("s", "u"))

        names = [call.args[0] for call in h.metrics.incr.call_args_list]
        assert "light_empty_response" in names
        assert "light_budget_exhausted" not in names

    def test_empty_result_retried_once(self, monkeypatch):
        """轻量调用返回空内容 → 重试一次（推理耗尽预算的兜底）"""
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        h = _light_handler()
        calls = []
        async def fake_call(s, u, extra_body=None):
            calls.append(extra_body)
            return "" if len(calls) == 1 else "结果"
        h._call_deepseek = fake_call
        out = asyncio.run(h._call_llm_light("s", "u"))
        assert out == "结果" and len(calls) == 2

    def test_retry_backoff_does_not_hold_global_llm_lock(self, monkeypatch):
        """后台重试退避期间必须让出全局锁，不能阻塞前台回合。"""
        h = _light_handler()
        calls = []
        lock_states = []

        async def fake_call(s, u, extra_body=None):
            calls.append(1)
            return "" if len(calls) == 1 else "结果"

        async def fake_sleep(_seconds):
            lock_states.append(h._llm_lock.locked())

        h._call_deepseek = fake_call
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        out = asyncio.run(h._call_llm_light("s", "u"))

        assert out == "结果"
        assert lock_states == [False]

    def test_nonempty_result_no_retry(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        h = _light_handler()
        calls = []
        async def fake_call(s, u, extra_body=None):
            calls.append(1)
            return "直接结果"
        h._call_deepseek = fake_call
        out = asyncio.run(h._call_llm_light("s", "u"))
        assert out == "直接结果" and len(calls) == 1

    def test_max_two_attempts_empty_then_exception(self, monkeypatch):
        """Codex I4：空→异常 不再发第三次请求（旧嵌套写法会发三次）；
        且只 sleep 一次——最后失败不持锁空等 1.5 秒"""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        h = _light_handler()
        calls = []
        async def fake_call(s, u, extra_body=None):
            calls.append(1)
            if len(calls) == 1:
                return ""
            raise RuntimeError("第二次炸了")
        h._call_deepseek = fake_call
        out = asyncio.run(h._call_llm_light("s", "u"))
        assert out == "" and len(calls) == 2
        assert sleep_mock.await_count == 1

    def test_max_two_attempts_exception_then_empty(self, monkeypatch):
        """Codex I4：异常→空 也走完重试路径，最多两次（旧写法漏掉空重试）；
        且只 sleep 一次"""
        sleep_mock = AsyncMock()
        monkeypatch.setattr(asyncio, "sleep", sleep_mock)
        h = _light_handler()
        calls = []
        async def fake_call(s, u, extra_body=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("第一次炸了")
            return ""
        h._call_deepseek = fake_call
        out = asyncio.run(h._call_llm_light("s", "u"))
        assert out == "" and len(calls) == 2
        assert sleep_mock.await_count == 1

    def test_extra_body_passed_through(self):
        """extra_body（THINKING_OFF）透传到 deepseek 调用"""
        h = _light_handler()
        seen = []
        async def fake_call(s, u, extra_body=None):
            seen.append(extra_body)
            return "结果"
        h._call_deepseek = fake_call
        out = asyncio.run(h._call_llm_light("s", "u", extra_body=THINKING_OFF))
        assert out == "结果" and seen == [THINKING_OFF]


class TestExtraBodyInRequest:
    def test_extra_body_reaches_request_body(self):
        """_call_deepseek 把 extra_body 合进请求体（思考关闭参数真正发出）"""
        h = object.__new__(MessageHandler)
        captured = {}
        async def fake_stream(body, base, api_key):
            captured.update(body)
            return "OK", {"role": "assistant", "content": "OK"}, 0.1
        h._stream_deepseek = fake_stream
        out = asyncio.run(
            MessageHandler._call_deepseek(h, "s", "u", config=CFG, extra_body=THINKING_OFF))
        assert out == "OK"
        assert captured["thinking"] == {"type": "disabled"}


class TestFallbackFallsThroughToWrapUp:
    @pytest.mark.parametrize("respond", [True, None])
    def test_empty_tool_response_warns_without_deliberate_skip(self, respond, caplog):
        """真实空回复仍发警告；只有明确 respond=False 才抑制假红。"""
        h = types.SimpleNamespace()
        h._stream_deepseek = AsyncMock(return_value=("", None, 0.1))
        h.llm = MagicMock()
        actions = {"respond": respond}
        with caplog.at_level(logging.WARNING, logger="糖糖.Handler"):
            result = asyncio.run(MessageHandler._call_deepseek(
                h, "sys", "user", tools=[{"function": {"name": "x"}}],
                config=CFG, turn_actions=actions))
        assert result == ""
        assert "仍无文字回复——本次回复将被丢弃" in caplog.text

    def test_deliberate_skip_does_not_warn_on_empty_tool_response(self, caplog):
        """skip_response 已落账的故意沉默不得产生空回复假红警告。"""
        h = types.SimpleNamespace()
        h._stream_deepseek = AsyncMock(return_value=("", None, 0.1))
        h.llm = MagicMock()
        with caplog.at_level(logging.WARNING, logger="糖糖.Handler"):
            result = asyncio.run(MessageHandler._call_deepseek(
                h, "sys", "user", tools=[{"function": {"name": "x"}}],
                config=CFG, turn_actions={"respond": False}))
        assert result == ""
        assert caplog.text == ""

    def test_stream_failure_empty_fallback_still_wraps_up(self, monkeypatch):
        """流式故障降级非流式返回空内容 → 收尾轮仍生效（旧代码降级后直接
        return，空内容绕过收尾轮 → 主回复静默丢弃）"""
        llm = FakeLLM([
            {"choices": [{"message": _msg("")}]},                    # 降级非流式：空
            {"choices": [{"message": _msg("补上了")}]},              # 收尾轮
        ])
        fake = _stream_real(types.SimpleNamespace(llm=llm))
        fake._execute_tool = AsyncMock(return_value="工具结果")
        import agent.skills as skills_mod
        monkeypatch.setattr(skills_mod, "get_method_type", lambda name: "info")
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", tools=[{"function": {"name": "x"}}], config=CFG))
        assert result == "补上了"
        assert llm.calls == 2  # 降级非流式 + 收尾轮

    def test_partial_stream_then_failure_replaces_not_appends(self):
        """Codex I1：流出半截正文后中断 → 降级完整回复替换半截，
        不得拼出「半截+完整回复」；残留工具调用流也被清掉"""
        lines = [
            SSE({"role": "assistant", "content": "半截"}),
            SSE({"content": None,
                 "tool_calls": [{"index": 0, "id": "c9", "type": "function",
                                 "function": {"name": "x", "arguments": "{"}}]}),
        ]
        llm = FakeLLM(
            [{"choices": [{"message": _msg("完整回复")}]}],
            stream_cm=FakeStreamCM(lines, raise_after=2),
        )
        fake = _stream_real(types.SimpleNamespace(llm=llm))
        content, msg, _ = asyncio.run(MessageHandler._stream_deepseek(
            fake, {"messages": []}, "http://x", "k"))
        assert content == "完整回复"          # 不是 "半截完整回复"
        assert msg.get("content") == "完整回复"
        assert not msg.get("tool_calls")     # 半截工具调用流被清掉
        assert llm.calls == 1                # 只走了降级这一次 post

    def test_fallback_tool_calls_survive_and_execute(self, monkeypatch):
        """Codex I2：降级响应带 tool_calls → 工具循环照常执行
        （旧代码公共路径 msg=None 重置把 tool_calls 吞掉）"""
        llm = FakeLLM([
            {"choices": [{"message": _msg("", TC())}]},   # 降级：要求调工具
            {"choices": [{"message": _msg("查到了")}]},    # 工具结果后的续写
        ])
        fake = _stream_real(types.SimpleNamespace(llm=llm))
        fake._execute_tool = AsyncMock(return_value="工具结果")
        import agent.skills as skills_mod
        monkeypatch.setattr(skills_mod, "get_method_type", lambda name: "info")
        result = asyncio.run(MessageHandler._call_deepseek(
            fake, "sys", "user", tools=[{"function": {"name": "x"}}], config=CFG))
        assert result == "查到了"
        assert fake._execute_tool.await_count == 1   # 降级要求的工具被执行了
        assert llm.calls == 2


class TestFinishReasonDiagnostics:
    def test_finish_reason_read_from_choice_level(self, caplog):
        """Codex I3：标准 SSE 的 finish_reason 在 choices[0] 层级——
        运行时断言日志出现 stop（旧代码读 delta 层级永远显示 '?'）"""
        lines = [
            SSE({"role": "assistant", "content": "你好"}),
            SSE({"content": "呀"}),
            SSE({}, fr='"stop"'),
            "data: [DONE]",
        ]
        llm = FakeLLM([], stream_cm=FakeStreamCM(lines))
        fake = _stream_real(types.SimpleNamespace(llm=llm))
        with caplog.at_level(logging.INFO, logger="糖糖.Handler"):
            content, msg, _ = asyncio.run(MessageHandler._stream_deepseek(
                fake, {"messages": []}, "http://x", "k"))
        assert content == "你好呀"
        assert re.search(r"流式完成: 3字 \([^)]*stop, 思考0字\)", caplog.text)


class TestThinkingOffWiring:
    # 每个提取方法必须把 THINKING_OFF lambda 接在【目标调用的 llm_call 参数】上
    EXPECTED = {
        "_do_extract_memories": "_process_extraction_batch",
        "_extract_fact_clusters_task": "extract_fact_clusters",
    }

    @staticmethod
    def _llm_call_lambda_wired(method_node):
        """方法体内是否存在 目标调用(..., llm_call=<调用 self._call_llm_light
        且带 extra_body=THINKING_OFF 的 lambda>)——精确到参数接线，
        未使用的散落 lambda / 别的函数接线都不算（Codex 复审 M1）"""
        target = TestThinkingOffWiring.EXPECTED[method_node.name]
        for call in ast.walk(method_node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute) and func.attr == target):
                continue
            llm_kw = next((kw for kw in call.keywords if kw.arg == "llm_call"), None)
            if llm_kw is None or not isinstance(llm_kw.value, ast.Lambda):
                return False
            body = llm_kw.value.body
            if not (isinstance(body, ast.Call)
                    and isinstance(body.func, ast.Attribute)
                    and body.func.attr == "_call_llm_light"):
                return False
            # Codex 终审 Minor：接收者必须就是 self——impostor._call_llm_light(...)
            # 之类的接线不得放行
            if not (isinstance(body.func.value, ast.Name)
                    and body.func.value.id == "self"):
                return False
            return any(kw.arg == "extra_body"
                       and isinstance(kw.value, ast.Name)
                       and kw.value.id == "THINKING_OFF"
                       for kw in body.keywords)
        return False

    def test_extraction_sites_use_thinking_off(self):
        """机械闸门：两个纯 JSON 提取方法的 llm_call 参数必须是挂
        THINKING_OFF 的 lambda——接线被撤/换成裸方法调用都会红灯"""
        src_path = Path(__file__).parents[1] / "agent" / "handler.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        methods = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in self.EXPECTED:
                methods[node.name] = node
        assert set(methods) == set(self.EXPECTED)
        for name, node in methods.items():
            assert self._llm_call_lambda_wired(node), f"{name} 的 llm_call 接线缺失 THINKING_OFF"

    def test_thinking_off_definition(self):
        src_path = Path(__file__).parents[1] / "agent" / "handler.py"
        src = src_path.read_text(encoding="utf-8")
        assert '"thinking": {"type": "disabled"}' in src
