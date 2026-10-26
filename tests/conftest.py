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
def isolate_runtime_state_files(tmp_path, monkeypatch):
    """所有运行时状态文件的默认落点一律改到 tmp_path——禁止测试读写生产 状态/。

    为什么从「只隔离 scheduler」扩到全家（2026-10-15 事故）：
    这些落点是**模块常量 / 类属性**，import 时就由 state_path() 算死了，
    所以「哪个测试写生产状态」只取决于该测试有没有自己 monkeypatch——
    漏一个就写生产。实测事故：tests/test_reflection_io.py 走真实
    engine.maybe_reflect()，把生产 状态/.reflection_state.json 写成
    「2026-10-15 13:05」，而 agent/reflection.py:120 的 20 小时门是拿它
    算「该不该反思」的——生产反思因此被推迟一整天（日志实证：最后一次
    真实反思停在 2026-10-12 22:32）。

    改成 autouse 全量隔离之后：忘记隔离的测试最多写坏自己的 tmp_path，
    不可能再碰生产。阳性对照见 tests/test_runtime_state_isolation.py。
    """
    from agent import (
        catch_up as catch_up_module,
        conversation_tracker as tracker_module,
        daily_report as daily_report_module,
        reflection as reflection_module,
        scheduler as scheduler_module,
        self_state as self_state_module,
    )

    monkeypatch.setattr(
        scheduler_module, "TASKS_FILE", str(tmp_path / ".scheduled_tasks.json")
    )
    monkeypatch.setattr(
        reflection_module.ReflectionEngine, "STATE_FILE",
        str(tmp_path / ".reflection_state.json"),
    )
    monkeypatch.setattr(
        self_state_module.TangTangSelf, "STATE_FILE",
        str(tmp_path / ".tangtang_self.json"),
    )
    monkeypatch.setattr(
        tracker_module.ConversationTracker, "STATE_FILE",
        str(tmp_path / ".conversation_state.json"),
    )
    monkeypatch.setattr(
        catch_up_module.CatchUpManager, "STATE_FILE",
        str(tmp_path / ".catch_up_state.json"),
    )
    monkeypatch.setattr(
        catch_up_module.CatchUpManager, "SENT_FILE",
        str(tmp_path / ".catch_up_sent.json"),
    )
    monkeypatch.setattr(
        daily_report_module, "STATE_FILE",
        str(tmp_path / ".daily_report_state.json"),
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
