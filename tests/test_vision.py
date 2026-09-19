"""识图回归测试（2026-08-16 事故）——file_id 拿不到图走 URL 兜底时，
img_path 未赋值引用崩溃（UnboundLocalError → 糖糖说「图没传过来」）。
兜底路径必须能正常出描述。"""
import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _make_handler(monkeypatch, api_result):
    from agent.handler import MessageHandler
    h = object.__new__(MessageHandler)
    h.vision_enabled = True
    h.napcat = MagicMock()
    h.napcat._call_api = AsyncMock(return_value=api_result)
    h.llm = MagicMock()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.content = b"PNG bytes"
    h.llm.get = AsyncMock(return_value=fake_resp)

    class FakeRouter:
        provider = "fake"

        def describe(self, img_bytes, prompt=""):
            return "一只猫"

        def describe_gif(self, path, prompt=""):
            return "gif"

    import agent.vision_router as vr
    monkeypatch.setattr(vr, "get_vision_router", lambda: FakeRouter())
    return h


def test_url_fallback_when_get_image_fails(monkeypatch):
    """get_image 失败 → URL 兜底 → 正常出描述（此前 UnboundLocalError）"""
    h = _make_handler(monkeypatch, {"status": "failed"})
    desc = asyncio.run(h._call_vision("http://x/img.png", file_id="D420"))
    assert desc == "一只猫"


def test_url_fallback_when_file_missing(monkeypatch):
    """get_image 返回的本地文件不存在 → URL 兜底 → 正常出描述"""
    h = _make_handler(monkeypatch,
                      {"status": "ok", "data": {"file": "Z:/不存在的路径.png"}})
    desc = asyncio.run(h._call_vision("http://x/img.png", file_id="D420"))
    assert desc == "一只猫"


def test_no_file_id_uses_url_directly(monkeypatch):
    """无 file_id（纯 URL 路径）→ 正常出描述"""
    h = _make_handler(monkeypatch, {"status": "ok", "data": {"file": ""}})
    desc = asyncio.run(h._call_vision("http://x/img.png"))
    assert desc == "一只猫"


def test_vision_disabled_returns_none(monkeypatch):
    h = _make_handler(monkeypatch, {"status": "ok", "data": {"file": ""}})
    h.vision_enabled = False
    assert asyncio.run(h._call_vision("http://x/img.png")) is None


def test_local_image_read_does_not_block_event_loop(tmp_path, monkeypatch):
    """NapCat 返回本地图片时，读取图片 bytes 不能阻塞事件循环。"""
    image = tmp_path / "image.png"
    image.write_bytes(b"PNG bytes")
    h = _make_handler(monkeypatch, {
        "status": "ok",
        "data": {"file": str(image)},
    })
    h.llm.get = AsyncMock(side_effect=AssertionError("should use local image"))
    main_thread = threading.get_ident()
    reads = []
    original_read = Path.read_bytes

    def slow_read(path_obj):
        reads.append(threading.get_ident())
        time.sleep(0.05)
        return original_read(path_obj)

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            desc = await h._call_vision("http://x/img.png", file_id="D420")
        finally:
            stopped = True
            await task
        return ticks, desc

    with patch.object(Path, "read_bytes", slow_read):
        ticks, desc = asyncio.run(scenario())

    assert desc == "一只猫"
    assert ticks > 0
    assert reads and all(thread_id != main_thread for thread_id in reads)


class TestVisionPromptGuards:
    """2026-08-16：表情包误读现场（流口水→呕吐）后的 prompt 防护"""

    def test_default_prompt_no_emotion_assertion(self):
        from agent.vision_router import _DEFAULT_PROMPT
        assert "感觉" not in _DEFAULT_PROMPT
        assert "不要推断" in _DEFAULT_PROMPT

    def test_emoji_prompt_exists_and_stricter(self):
        from agent.vision_router import EMOJI_PROMPT
        assert "表情包" in EMOJI_PROMPT
        assert "不要断言发送者" in EMOJI_PROMPT

    def test_emoji_branch_in_vision_call(self):
        """_call_vision 的表情包分流在场（防删闸门——事故修复点）"""
        src = open(r"d:/qq-小糖糖/agent/handler.py", encoding="utf-8").read()
        assert "EMOJI_PROMPT" in src
        assert '"Emoji" in (img_path or "")' in src
