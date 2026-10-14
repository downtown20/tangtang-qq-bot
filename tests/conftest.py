"""
pytest fixtures for 小糖糖 tests.
"""

import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock

# Add project root to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolate_scheduler_storage(tmp_path, monkeypatch):
    """定时任务测试一律使用临时文件，禁止读写生产调度状态。"""
    import agent.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module, "TASKS_FILE", str(tmp_path / ".scheduled_tasks.json")
    )


@pytest.fixture
def store():
    """内存数据库 Store，每次测试独立"""
    from agent.store import Store
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    s = Store(path)
    yield s
    # Windows 上 SQLite 可能还有文件锁，忽略清理错误
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def mock_napcat():
    """Mock SnowLuma / OneBot 客户端"""
    napcat = AsyncMock()
    napcat.send_group_message = AsyncMock(return_value=True)
    napcat.send_private_message = AsyncMock(return_value=True)
    return napcat


@pytest.fixture
def mock_stickers():
    """Mock StickerManager"""
    from unittest.mock import MagicMock
    stickers = MagicMock()
    stickers.has_stickers = MagicMock(return_value=True)
    stickers.get_by_emotions = MagicMock(return_value=None)
    stickers.get_by_keyword = MagicMock(return_value=None)
    stickers.match_by_emotion_text = MagicMock(return_value=[])
    stickers.random_sticker = MagicMock(return_value=None)
    stickers.random_safe_sticker = MagicMock(return_value=None)
    return stickers


@pytest.fixture
def reply_pipeline(mock_napcat, mock_stickers, store):
    """ReplyPipeline with mock dependencies"""
    from agent.reply_pipeline import ReplyPipeline
    return ReplyPipeline(
        napcat=mock_napcat,
        stickers=mock_stickers,
        store=store,
        short_term={},
        bot_nicknames=["糖糖", "小糖糖"],
        bot_qq="10000",
    )
