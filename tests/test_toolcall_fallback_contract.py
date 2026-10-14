"""tool_calls 兜底契约（2026-09-21）。

## 背景

某些 OpenAI 兼容网关（NewAPI 面板实测）在 **`stream=false`** 时返回
`finish_reason: "tool_calls"` 却把 `message.tool_calls` 整个吞掉——模型自己都在
`reasoning_content` 里写着"我该调 get_time"，但载荷没了。同一个请求加
`stream: true` 就正常（5 个模型逐个对照，非流式全丢、流式全有）。

`_post_chat_with_toolcall_fallback` 就是兜这个：声称调了工具却没给载荷 →
改用流式重发一次。

## 本测试最要紧的一条

**「对官方零影响」不能靠嘴说。** 官方端点自己也有个间歇性丢工具调用的已知故障
（GitHub issue #1244，约 11%），但它的签名**是另一种**：`finish_reason: "stop"`
+ 调用被当纯文本写进 `content`。

所以判据必须**只认 `finish_reason == "tool_calls"`**。`test_official_signature_...`
那几条就是用机器钉死这件事——判据一旦被改宽，官方端点上的每一次"模型正常决定
不调工具"都会被误判成"网关吞了载荷"，然后无谓地多发一次请求。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# 极简替身：只需要 self.llm.post 和 self._stream_deepseek 两个接口
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeLLM:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append(json)
        return self.resp

    @property
    def n_calls(self):
        return len(self.calls)


class _FakeHandler:
    def __init__(self, resp, stream_msg=None, stream_raises=None):
        self.llm = _FakeLLM(resp)
        self._stream_msg = stream_msg
        self._stream_raises = stream_raises
        self.stream_calls = []

    async def _stream_deepseek(self, body, base, api_key):
        self.stream_calls.append(dict(body))
        if self._stream_raises:
            raise self._stream_raises
        return "", self._stream_msg, 1.0


def _payload(finish, message):
    return {"choices": [{"finish_reason": finish, "message": message}]}


def _call(handler):
    from agent.handler import _post_chat_with_toolcall_fallback
    return asyncio.run(_post_chat_with_toolcall_fallback(
        handler.llm, handler._stream_deepseek,
        {"model": "m", "stream": False}, "https://h", "k"))


TC = [{"id": "call_1", "type": "function",
       "function": {"name": "get_time", "arguments": "{}"}}]


# ---------------------------------------------------------------------------
# 1. 正常情况：不许有多余请求
# ---------------------------------------------------------------------------

def test_normal_tool_call_does_not_resend():
    h = _FakeHandler(_Resp(200, _payload("tool_calls", {"tool_calls": TC})))
    msg, finish, status = _call(h)
    assert msg["tool_calls"] == TC
    assert finish == "tool_calls" and status == 200
    assert h.llm.n_calls == 1 and not h.stream_calls, "正常返回不该触发重发"


def test_plain_text_reply_does_not_resend():
    h = _FakeHandler(_Resp(200, _payload("stop", {"content": "在的"})))
    msg, finish, _ = _call(h)
    assert msg["content"] == "在的" and finish == "stop"
    assert h.llm.n_calls == 1 and not h.stream_calls


# ---------------------------------------------------------------------------
# 2. 要兜的那个形状：说调了 + 没载荷
# ---------------------------------------------------------------------------

def test_swallowed_toolcall_triggers_stream_retry():
    h = _FakeHandler(
        _Resp(200, _payload("tool_calls", {"content": "", "role": "assistant"})),
        stream_msg={"tool_calls": TC, "content": None},
    )
    msg, finish, status = _call(h)
    assert msg["tool_calls"] == TC, "应该拿到流式重发的结果"
    assert finish == "tool_calls" and status == 200
    assert len(h.stream_calls) == 1
    assert h.stream_calls[0]["stream"] is True, "重发必须开流式——这是关键"


def test_retry_body_is_a_copy_not_the_original():
    """重发用的 body 必须是副本——不能把调用方的 body 改成 stream=True。"""
    h = _FakeHandler(
        _Resp(200, _payload("tool_calls", {})),
        stream_msg={"tool_calls": TC},
    )
    original = {"model": "m", "stream": False}
    from agent.handler import _post_chat_with_toolcall_fallback
    asyncio.run(_post_chat_with_toolcall_fallback(
        h.llm, h._stream_deepseek, original, "https://h", "k"))
    assert original["stream"] is False, "原 body 被就地改了"


def test_stream_retry_also_empty_falls_back_to_original():
    h = _FakeHandler(
        _Resp(200, _payload("tool_calls", {"content": "半截"})),
        stream_msg={"content": "还是没载荷"},
    )
    msg, finish, _ = _call(h)
    assert msg["content"] == "半截"          # 沿用原响应，不是流式那份
    assert finish == "tool_calls"


def test_stream_retry_raises_does_not_change_outcome():
    h = _FakeHandler(
        _Resp(200, _payload("tool_calls", {"content": "原样"})),
        stream_raises=RuntimeError("boom"),
    )
    msg, finish, status = _call(h)
    assert msg["content"] == "原样" and status == 200


# ---------------------------------------------------------------------------
# 3. 【最要紧】官方的故障签名不许触发这条兜底
# ---------------------------------------------------------------------------

def test_official_signature_stop_with_no_tool_calls_does_not_resend():
    """官方那个已知故障（issue #1244）是 finish_reason=stop + 调用写进正文。
    判据若把这种也当成"被吞了"，官方端点上每次正常不调工具都会白重发一次。"""
    h = _FakeHandler(_Resp(200, _payload(
        "stop", {"content": "让我查一下。get_time{}"})))
    msg, finish, _ = _call(h)
    assert finish == "stop"
    assert h.llm.n_calls == 1 and not h.stream_calls, \
        "官方签名（stop + 无载荷）绝不能触发重发"


@pytest.mark.parametrize("finish", ["stop", "length", "content_filter",
                                    "insufficient_system_resource", "aborted"])
def test_other_finish_reasons_never_trigger(finish):
    h = _FakeHandler(_Resp(200, _payload(finish, {"content": "x"})))
    _call(h)
    assert not h.stream_calls, f"finish_reason={finish} 不该触发重发"


# ---------------------------------------------------------------------------
# 4. 错误码原样传回（不吞状态码——那是排障的唯一线索）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [400, 401, 429, 500, 503])
def test_error_status_passes_through(code):
    h = _FakeHandler(_Resp(code, {}))
    msg, finish, status = _call(h)
    assert msg is None and finish is None and status == code
    assert not h.stream_calls, "出错时不该重发"
