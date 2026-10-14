"""贴图文件名情绪 fallback + 情绪词表单一源（2026-09-05）。

背景：标注工具认「情绪词开头的文件名=已处理」，但 sticker.py 语义索引只认 metadata——
用户按规范命名（开心_撒娇_xxx.jpg）放新图不跑标注也能被选到（fallback）。
词表单一事实源在 agent/sticker.py，标注工具引用它——两端漂移由本测试钉住。
"""

import importlib.util
import sys
from pathlib import Path

from agent.sticker import STICKER_EMOTION_PREFIXES, StickerManager

BASE = Path(__file__).resolve().parent.parent


def test_filename_fallback_extracts_emotion_prefixes():
    """按规范命名的图 → 从文件名提取情绪段。"""
    assert StickerManager._filename_emotion_text("开心_撒娇_abc123.jpg") == "开心，撒娇"
    assert StickerManager._filename_emotion_text("难过_委屈_xyz.png") == "难过，委屈"


def test_filename_fallback_rejects_unlabeled_names():
    """收集/哈希/随意中文名 → 不产生 fallback 标签。"""
    assert StickerManager._filename_emotion_text("collected_12345678.jpg") == ""
    assert StickerManager._filename_emotion_text("00cb074db3852571892ead63193686df1609643189.jpg") == ""
    assert StickerManager._filename_emotion_text("普通描述图_x.jpg") == ""
    assert StickerManager._filename_emotion_text("表情包.png") == ""


def test_emotion_wordlist_single_source_with_label_tool():
    """标注工具与 sticker.py 共用同一词表对象（防漂移）。"""
    spec = importlib.util.spec_from_file_location(
        "label_tool_contract", BASE / "tools" / "标注贴图情绪.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.EMOTION_PREFIXES is STICKER_EMOTION_PREFIXES
    assert len(STICKER_EMOTION_PREFIXES) >= 200
