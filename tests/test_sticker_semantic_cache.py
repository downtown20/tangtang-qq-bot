"""贴图语义矩阵的持久化、增量和冷缓存降级边界。"""

import asyncio
import numpy as np
import threading
import time
from types import SimpleNamespace

from agent.handler import MessageHandler
from agent.sticker import StickerManager, classify_emotions


class FakeEmbed:
    ready = True
    model_id = "fake-bge-v1"

    def __init__(self):
        self.encode_calls = 0
        self.batch_calls = 0

    def encode(self, text):
        self.encode_calls += 1
        # 查询向量和所有缓存向量保持同一维度，便于断言只发生一次查询编码。
        return np.asarray(
            [1.0, 0.0] if "开心" in text else [0.0, 1.0],
            dtype=np.float32,
        )

    def encode_batch(self, texts):
        self.batch_calls += 1
        return [
            np.asarray(
                [1.0, 0.0] if "开心" in text else [0.0, 1.0],
                dtype=np.float32,
            )
            for text in texts
        ]


def _make_library(tmp_path, desc="开心地笑"):
    (tmp_path / "happy.jpg").write_bytes(b"happy")
    (tmp_path / "calm.jpg").write_bytes(b"calm")
    (tmp_path / "metadata.json").write_text(
        '{"happy.jpg":{"emotions":["开心"],"emotion_desc":"%s"},'
        '"calm.jpg":{"emotions":["温柔"],"emotion_desc":"温柔地陪伴"}}' % desc,
        encoding="utf-8",
    )


def test_semantic_cache_warm_is_batched_and_reused_after_restart(tmp_path):
    _make_library(tmp_path)
    embed = FakeEmbed()
    manager = StickerManager(str(tmp_path))

    assert manager.warm_semantic_cache(embed) is True
    assert embed.batch_calls == 1
    assert embed.encode_calls == 0
    first = manager.search_by_emotion_semantic("开心", embed)
    assert first and "happy.jpg" in first[0]
    assert embed.encode_calls == 1  # 只有查询向量，不能再逐图 encode

    restarted = StickerManager(str(tmp_path))
    embed_after_restart = FakeEmbed()
    assert restarted.search_by_emotion_semantic("开心", embed_after_restart)
    assert embed_after_restart.encode_calls == 1
    assert embed_after_restart.batch_calls == 0
    assert restarted.semantic_cache_stats()["cache_hits"] == 1


def test_semantic_cache_reencodes_only_changed_metadata(tmp_path):
    _make_library(tmp_path)
    embed = FakeEmbed()
    manager = StickerManager(str(tmp_path))
    assert manager.warm_semantic_cache(embed) is True

    _make_library(tmp_path, desc="惊讶地睁大眼睛")
    manager.reload()
    assert manager.warm_semantic_cache(embed) is True
    assert embed.batch_calls == 2
    assert manager.semantic_cache_stats()["warm_encoded"] == 3


def test_cold_semantic_cache_falls_back_without_per_sticker_encoding(tmp_path):
    _make_library(tmp_path)
    embed = FakeEmbed()
    manager = StickerManager(str(tmp_path))

    result = manager.match_by_emotion_text("开心", embed_engine=embed)
    assert result and "happy.jpg" in result[0]
    assert embed.encode_calls == 1  # 冷缓存只编码查询，随后走标签降级
    assert embed.batch_calls == 0
    assert manager.semantic_cache_stats()["cache_misses"] == 1


def test_handler_warms_role_libraries_off_event_loop_in_stable_order():
    calls = []
    main_thread = threading.get_ident()

    class Manager:
        def __init__(self, role):
            self.role = role

        def warm_semantic_cache(self, _embed):
            calls.append((self.role, threading.get_ident()))
            time.sleep(0.03)
            return True

    async def run():
        handler = MessageHandler.__new__(MessageHandler)
        handler._role_stickers = {
            "default": Manager("default"),
            "murasame": Manager("murasame"),
            "michele": Manager("michele"),
        }
        handler.embed_engine = SimpleNamespace(ready=True)
        await handler._warm_sticker_embeddings()

    asyncio.run(run())
    assert [role for role, _thread in calls] == ["default", "murasame", "michele"]
    assert all(thread != main_thread for _role, thread in calls)


def test_emotion_fallback_respects_complete_words_and_negation():
    assert classify_emotions("不开心") == []
    assert classify_emotions("不要生气") == []
    assert classify_emotions("别想你") == []
    assert classify_emotions("不是色色") == []
    assert "开心" in classify_emotions("今天真的开心")
    assert "生气" in classify_emotions("我有点不爽")
    assert classify_emotions("emotional") == []
