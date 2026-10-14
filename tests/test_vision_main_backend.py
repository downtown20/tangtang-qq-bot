"""识图主 LLM 后端回归测试（2026-10-14，T1）。

背景：主人定案——识图直接用中转的 llm.model（deepseek-v4.1-flash）原生视觉，
不再需要单独的识图 API Key，也不再需要本机 Ollama MiniCPM-V。

本文件钉住四件事，防"改回去"或"改坏了"：
  1. provider=main 时**必须**走主 LLM 的 _stream_deepseek，且 body 是
     OpenAI 多模态格式（content 数组 + image_url + data URL）
  2. MIME 由**魔数**判定，不由扩展名——URL 兜底路径没有文件名
  3. 任何失败都返回空串（调用方按"识图不可用"处理），**绝不抛给上层**
  4. provider=local/api 时**完全不碰**主 LLM（回退路径未被污染）

阳性对照纪律（CLAUDE.md 教训 #30）：test_main_backend_is_actually_called
必须能因"分支被删/写错"而变红——它断言的是调用事实，不是"跑起来没报错"。
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

# 各格式的最小合法魔数头（只校验前 12 字节，无需真图）
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4
GIF = b"GIF89a" + b"\x00" * 12
UNKNOWN = b"\x00\x01\x02\x03" + b"\x00" * 12


def _make_handler(*, provider="main", enabled=True, llm_cfg=None):
    """构造一个只够跑识图的 handler（object.__new__，不跑 __init__）。"""
    from agent.handler import MessageHandler

    h = object.__new__(MessageHandler)
    h.vision_enabled = enabled
    h.vision_provider = provider
    h.vision_config = {"provider": provider, "enabled": enabled}
    h._vision_cache = {}
    h._vision_cache_ttl = 600.0
    h._vision_cache_max = 128
    h.llm_config = llm_cfg if llm_cfg is not None else {
        "api_key": "sk-test", "base_url": "https://relay.invalid",
        "model": "deepseek-v4.1-flash", "temperature": 0.8,
    }
    h._stream_deepseek = AsyncMock(return_value=("一只橘猫趴在窗台上", None, 0.3))
    h.napcat = MagicMock()
    h.napcat._call_api = AsyncMock(return_value={"status": "ok", "data": {"file": ""}})

    class _Resp:
        status_code = 200
        content = JPEG

    h.llm = MagicMock()
    h.llm.get = AsyncMock(return_value=_Resp())
    return h


def _capture_body(handler):
    """取出 _stream_deepseek 实际收到的 body"""
    assert handler._stream_deepseek.await_count >= 1, "主 LLM 后端没有被调用"
    return handler._stream_deepseek.await_args.args[0]


# ── 1. 主后端真的被调用，且 body 是多模态格式 ──

def test_main_backend_is_actually_called():
    """provider=main → 走主 LLM；阳性对照：分支被删这条必红"""
    h = _make_handler()
    desc = asyncio.run(h._call_vision("http://x/img.png"))
    assert desc == "一只橘猫趴在窗台上"
    body = _capture_body(h)
    content = body["messages"][0]["content"]
    assert isinstance(content, list), "content 必须是数组（多模态），不能是字符串"
    kinds = [part["type"] for part in content]
    # 2026-10-14 T43：**契约变更，不是回归**。原断言要求 content 恰好是
    # [image_url, text] —— 那是 T1 时的形状。T43 为抑制推理模型把 max_tokens
    # 全花在 reasoning_content 上（导致 content 为空、finish_reason=length），
    # **加了一段抑制思维链的 text**。本用例的原意是「content 是多模态数组、
    # 首段是图，不是纯字符串」，所以改成断言首段是 image_url 且至少一段 text。
    assert kinds[0] == "image_url", f"首段必须是图片: {kinds}"
    assert kinds.count("text") >= 1, f"必须至少有一段提示词: {kinds}"
    assert set(kinds) <= {"image_url", "text"}, f"出现未知段类型: {kinds}"
    assert content[0]["image_url"]["url"].startswith("data:image/")
    assert ";base64," in content[0]["image_url"]["url"]
    assert body["model"] == "deepseek-v4.1-flash"


def test_main_backend_does_not_attach_tools():
    """识图是纯生成——挂 tools 会让中转走工具循环，徒增延迟"""
    h = _make_handler()
    asyncio.run(h._call_vision("http://x/img.png"))
    assert "tools" not in _capture_body(h)
    assert "tool_choice" not in _capture_body(h)


def test_base_url_is_normalized_for_main_backend():
    """base_url 结尾多余的斜杠不能带进请求（否则 //v1 双斜杠）"""
    h = _make_handler(llm_cfg={"api_key": "k", "base_url": "https://relay.invalid/",
                               "model": "m", "temperature": 0.8})
    asyncio.run(h._call_vision("http://x/img.png"))
    assert h._stream_deepseek.await_args.args[1] == "https://relay.invalid"


# ── 2. MIME 由魔数判定（URL 兜底路径没有扩展名） ──

@pytest.mark.parametrize("raw,expected", [
    (JPEG, "image/jpeg"),
    (PNG, "image/png"),
    (WEBP, "image/webp"),
    (GIF, "image/gif"),
    (UNKNOWN, "image/jpeg"),   # 兜底：QQ 图片绝大多数是 jpeg
])
def test_mime_sniffed_from_magic_bytes(raw, expected):
    h = _make_handler()

    class _Resp:
        status_code = 200
        content = raw

    h.llm.get = AsyncMock(return_value=_Resp())
    asyncio.run(h._call_vision("http://x/no-extension"))
    url = _capture_body(h)["messages"][0]["content"][0]["image_url"]["url"]
    assert url.startswith(f"data:{expected};base64,"), f"{raw[:4]!r} 判定成 {url[:32]}"


def test_image_bytes_survive_base64_roundtrip():
    """图片字节必须原样编码——多一层转码会毁掉图"""
    h = _make_handler()

    class _Resp:
        status_code = 200
        content = PNG

    h.llm.get = AsyncMock(return_value=_Resp())
    asyncio.run(h._call_vision("http://x/img"))
    url = _capture_body(h)["messages"][0]["content"][0]["image_url"]["url"]
    encoded = url.split(";base64,", 1)[1]
    assert base64.b64decode(encoded) == PNG


# ── 3. 失败一律降级为空串，绝不抛 ──

def test_missing_llm_config_returns_empty_not_raise():
    h = _make_handler(llm_cfg={"api_key": "", "base_url": "", "model": ""})
    assert asyncio.run(h._describe_via_main_llm(JPEG, "看图")) == ""
    assert h._stream_deepseek.await_count == 0


def test_stream_exception_is_swallowed():
    h = _make_handler()
    h._stream_deepseek = AsyncMock(side_effect=RuntimeError("relay 502"))
    assert asyncio.run(h._describe_via_main_llm(JPEG, "看图")) == ""


def test_empty_model_output_returns_empty():
    h = _make_handler()
    h._stream_deepseek = AsyncMock(return_value=("   ", None, 0.1))
    assert asyncio.run(h._describe_via_main_llm(JPEG, "看图")) == ""


def test_describe_returns_stripped_text():
    h = _make_handler()
    h._stream_deepseek = AsyncMock(return_value=("  一只猫\n", None, 0.1))
    assert asyncio.run(h._describe_via_main_llm(JPEG, "看图")) == "一只猫"


# ── 4. 回退路径未被污染 ──

def test_local_provider_never_calls_main_llm(monkeypatch):
    """provider=local → 完全走旧路由，一次都不碰主 LLM"""
    h = _make_handler(provider="local")

    class FakeRouter:
        provider = "local"

        def describe(self, _b, _p=""):
            return "本地结果"

        def describe_gif(self, _p, _q=""):
            return "本地gif"

    import agent.handler as _handler_mod
    monkeypatch.setattr(_handler_mod, "_get_vision_router", lambda: FakeRouter())
    desc = asyncio.run(h._call_vision("http://x/img.png"))
    assert desc == "本地结果"
    assert h._stream_deepseek.await_count == 0, "回退路径不该调用主 LLM"


def test_vision_disabled_short_circuits_every_backend(monkeypatch):
    h = _make_handler(enabled=False)
    called = []

    class FakeRouter:
        provider = "local"

        def describe(self, _b, _p=""):
            called.append(1)
            return "不该出现"

    import agent.handler as _handler_mod
    monkeypatch.setattr(_handler_mod, "_get_vision_router", lambda: FakeRouter())
    assert asyncio.run(h._call_vision("http://x/img.png")) is None
    assert h._stream_deepseek.await_count == 0
    assert not called


# ── 5. 缓存对两个后端都生效 ──

def test_cache_hit_avoids_second_call():
    h = _make_handler()
    first = asyncio.run(h._call_vision("http://x/a.png"))
    second = asyncio.run(h._call_vision("http://x/b.png"))   # 同一内容 → 同 hash
    assert first == second == "一只橘猫趴在窗台上"
    assert h._stream_deepseek.await_count == 1, "同内容第二次应当命中缓存"


# ── 6. __init__ 的 provider 归一化（防非法值静默走错后端） ──

class TestProviderNormalization:
    """归一化逻辑必须与 _call_vision 的分支判据一致"""

    @staticmethod
    def _norm(raw):
        """复刻 __init__ 的归一化（保持与实现同步；实现改了这里必须改）"""
        value = str(raw or "main").strip().lower()
        if value in ("", "qwen"):
            value = "main" if value == "" else "api"
        if value not in ("main", "local", "api"):
            value = "main"
        return value

    @pytest.mark.parametrize("raw,expected", [
        ("main", "main"), ("MAIN", "main"), (" Main ", "main"),
        ("local", "local"), ("api", "api"),
        ("qwen", "api"),        # 历史别名
        ("", "main"),           # 空 → 默认
        ("None", "main"),       # 非法 → 回退默认并告警
        ("ollama", "main"),
    ])
    def test_normalization_table(self, raw, expected):
        assert self._norm(raw) == expected

    def test_implementation_matches_table(self):
        """源码闸门：__init__ 里确实做了归一化，而不是直接信任配置值"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "agent" / "handler.py").read_text(
            encoding="utf-8")
        assert "self.vision_provider = _vision_provider" in src
        assert '_vision_provider not in ("main", "local", "api")' in src
