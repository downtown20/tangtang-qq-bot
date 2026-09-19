"""贴图标注工具的增量修复契约。"""

import asyncio
import json
import runpy
import sys
from pathlib import Path


TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "标注贴图情绪.py"


def _load_tool():
    return runpy.run_path(str(TOOL_PATH))


class _FailIfCalledRouter:
    provider = "test"

    def describe(self, *args, **kwargs):
        raise AssertionError("已有有效标签的图片不应再次调用视觉模型")

    def describe_gif(self, *args, **kwargs):
        raise AssertionError("已有有效标签的 GIF 不应再次调用视觉模型")


class _StaticRouter:
    provider = "test"

    def __init__(self, result):
        self.result = result

    def describe(self, *args, **kwargs):
        return self.result

    def describe_gif(self, *args, **kwargs):
        return self.result


def test_existing_nonprefixed_metadata_is_not_reprocessed(tmp_path):
    ns = _load_tool()
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    (sticker_dir / "hash_name.jpg").write_bytes(b"image")
    original = {
        "hash_name.jpg": {
            "emotions": ["开心"], "desc": "开心", "source": "existing"
        }
    }
    (sticker_dir / "metadata.json").write_text(
        json.dumps(original, ensure_ascii=False), encoding="utf-8"
    )
    ns["label_dir"].__globals__["get_vision_router"] = lambda: _FailIfCalledRouter()

    result = asyncio.run(ns["label_dir"]("测试", sticker_dir))

    saved = json.loads((sticker_dir / "metadata.json").read_text(encoding="utf-8"))
    assert result == (0, 1, 0)
    assert saved == original
    assert (sticker_dir / "hash_name.jpg").is_file()


def test_missing_optional_sticker_directory_is_skipped(tmp_path):
    ns = _load_tool()
    missing_dir = tmp_path / "stickers_michele"
    ns["label_dir"].__globals__["get_vision_router"] = lambda: _FailIfCalledRouter()

    assert asyncio.run(ns["label_dir"]("米雪儿", missing_dir)) == (0, 0, 0)
    assert not missing_dir.exists()


def test_prefixed_filename_repairs_missing_metadata_without_vision(tmp_path):
    ns = _load_tool()
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    filename = "开心_撒娇_abcd.png"
    (sticker_dir / filename).write_bytes(b"image")
    ns["label_dir"].__globals__["get_vision_router"] = lambda: _FailIfCalledRouter()

    result = asyncio.run(ns["label_dir"]("测试", sticker_dir))

    saved = json.loads((sticker_dir / "metadata.json").read_text(encoding="utf-8"))
    assert result == (1, 0, 0)
    assert saved[filename]["emotions"] == ["开心", "撒娇"]
    assert (sticker_dir / filename).is_file()


def test_jfif_is_part_of_the_labeling_pipeline(tmp_path):
    ns = _load_tool()
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    filename = "开心_abcd.jfif"
    (sticker_dir / filename).write_bytes(b"image")
    ns["label_dir"].__globals__["get_vision_router"] = lambda: _FailIfCalledRouter()

    result = asyncio.run(ns["label_dir"]("测试", sticker_dir))

    saved = json.loads((sticker_dir / "metadata.json").read_text(encoding="utf-8"))
    assert result == (1, 0, 0)
    assert saved[filename]["emotions"] == ["开心"]


def test_main_keeps_label_and_path_when_filtering_targets(monkeypatch):
    ns = _load_tool()
    calls = []

    async def fake_label_dir(label, directory):
        calls.append((label, directory.name))
        return 0, 0, 0

    ns["main"].__globals__["label_dir"] = fake_label_dir
    ns["main"].__globals__["get_vision_router"] = lambda: type(
        "Router", (), {"provider": "test"}
    )()
    monkeypatch.setattr(sys, "argv", [str(TOOL_PATH), "stickers_michele"])

    assert asyncio.run(ns["main"]()) == 0
    assert calls == [("米雪儿(michele)", "stickers_michele")]


def test_model_emotions_are_deduplicated_and_stripped_of_punctuation(tmp_path):
    ns = _load_tool()
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    (sticker_dir / "raw.jpg").write_bytes(b"image")
    ns["label_dir"].__globals__["get_vision_router"] = lambda: _StaticRouter(
        "惊讶，惊恐 惊讶。"
    )

    result = asyncio.run(ns["label_dir"]("测试", sticker_dir))

    saved = json.loads((sticker_dir / "metadata.json").read_text(encoding="utf-8"))
    entry = next(iter(saved.values()))
    assert result == (1, 0, 0)
    assert entry["emotions"] == ["惊讶", "惊恐"]


def test_animated_webp_uses_multiframe_vision_path(tmp_path):
    from PIL import Image

    ns = _load_tool()
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    filepath = sticker_dir / "animated.webp"
    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
    frames[0].save(filepath, save_all=True, append_images=frames[1:], format="WEBP")

    class Router(_StaticRouter):
        def describe(self, *args, **kwargs):
            raise AssertionError("动画 WebP 不应作为静态图片直接提交")

    ns["label_dir"].__globals__["get_vision_router"] = lambda: Router("开心")

    result = asyncio.run(ns["label_dir"]("测试", sticker_dir))

    assert result == (1, 0, 0)
