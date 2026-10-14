"""角色专属贴图库的隔离、热重载和选图契约。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.handler import MessageHandler
from agent.reply_pipeline import ReplyPipeline
from agent.sticker import StickerManager


def _write_metadata(directory: Path, entries: dict) -> None:
    (directory / "metadata.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


def test_reload_picks_up_new_metadata_and_jfif(tmp_path):
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    (sticker_dir / "old.jpg").write_bytes(b"old")
    _write_metadata(sticker_dir, {
        "old.jpg": {"emotions": ["开心"], "desc": "开心", "source": "test"},
    })
    manager = StickerManager(str(sticker_dir))

    (sticker_dir / "new.jfif").write_bytes(b"new")
    _write_metadata(sticker_dir, {
        "old.jpg": {"emotions": ["开心"], "desc": "开心", "source": "test"},
        "new.jfif": {"emotions": ["惊讶"], "desc": "惊讶", "source": "test"},
    })

    manager.reload()

    assert manager.count == 2
    assert len(manager.metadata) == 2
    assert "new.jfif" in manager.get_by_emotion("惊讶")


def test_role_switch_reloads_files_added_after_startup(tmp_path):
    default_dir = tmp_path / "stickers"
    michele_dir = tmp_path / "stickers_michele"
    default_dir.mkdir()
    michele_dir.mkdir()
    _write_metadata(default_dir, {})
    _write_metadata(michele_dir, {})
    default = StickerManager(str(default_dir))
    michele = StickerManager(str(michele_dir))

    (michele_dir / "smile.jpg").write_bytes(b"michele")
    _write_metadata(michele_dir, {
        "smile.jpg": {"emotions": ["开心"], "desc": "开心", "source": "test"},
    })

    handler = MessageHandler.__new__(MessageHandler)
    handler.stickers = default
    handler.reply = SimpleNamespace(stickers=default)
    handler._role_stickers = {"default": default, "michele": michele}
    handler._current_sticker_role = "default"

    handler._set_sticker_role("michele")

    assert handler.stickers is michele
    assert handler.reply.stickers is michele
    assert handler._current_sticker_role == "michele"
    assert handler.stickers.count == 1
    assert "stickers_michele" in handler.stickers.get_by_emotion("开心")


def test_role_directories_never_fallback_to_default_manager():
    source = (
        Path(__file__).resolve().parent.parent / "agent" / "handler.py"
    ).read_text(encoding="utf-8")

    assert '"murasame": StickerManager("./stickers_murasame"),' in source
    assert '"michele": StickerManager("./stickers_michele"),' in source
    assert 'if Path("./stickers_michele").exists() else self.stickers' not in source
    assert 'if Path("./stickers_murasame").exists() else self.stickers' not in source


def test_sticker_tool_describes_the_current_role_library(tmp_path):
    michele_dir = tmp_path / "stickers_michele"
    michele_dir.mkdir()
    (michele_dir / "smile.jpg").write_bytes(b"michele")
    _write_metadata(michele_dir, {
        "smile.jpg": {"emotions": ["开心"], "desc": "开心", "source": "test"},
    })
    handler = MessageHandler.__new__(MessageHandler)
    handler.stickers = StickerManager(str(michele_dir))
    handler._current_sticker_role = "michele"

    tools = handler._build_memory_tools("1")
    send_stickers = next(
        tool["function"] for tool in tools
        if tool["function"]["name"] == "send_stickers"
    )

    assert "当前角色 michele" in send_stickers["description"]
    assert "1张" in send_stickers["description"]
    assert "开心" in send_stickers["description"]


def test_native_tool_and_legacy_tag_share_the_same_matcher():
    stickers = MagicMock()
    stickers.sticker_dir = Path("stickers_michele")
    stickers.match_by_emotion_text.return_value = [
        "[CQ:image,file=file:///stickers_michele/happy.jpg]"
    ]

    handler = MessageHandler.__new__(MessageHandler)
    handler.stickers = stickers
    handler.embed_engine = None
    handler._current_sticker_role = "michele"
    actions = {"stickers": []}
    asyncio.run(handler._execute_tool(
        "send_stickers", {"emotion": "开心", "count": 1}, turn_actions=actions
    ))

    pipeline = ReplyPipeline(None, stickers, None, {}, [], "0")
    pipeline.resolve_sticker_tags("[贴图:开心]")

    assert stickers.match_by_emotion_text.call_count == 2
    assert actions["stickers"] == [
        "[CQ:image,file=file:///stickers_michele/happy.jpg]"
    ]
