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
# 2026-10-26：Qt 运维控制台从根目录移入 运维/——模块 import 路径随之改变。
# （只影响测试里的 importlib/import；生产入口走 start.py 的 TARGETS。）
sys.path.insert(0, str(Path(__file__).parent.parent / "运维"))


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


@pytest.fixture(autouse=True)
def _isolate_global_deps_config(tmp_path, monkeypatch):
    """把全局依赖配置指到临时目录（2026-11-14）。

    tools/依赖位置.py 的 get_deps_root() 有第四级回退：
    %LOCALAPPDATA%/tangtang/依赖位置.json。不隔离的话，本机真实那份会渗进
    所有测试，「默认位置」的断言会随机器状态随机变红（教训 #45）。

    用环境变量覆盖（模块内 _global_config_path() 每次现算）——不改模块常量：
    测试各自 importlib 加载同一个文件，patch 常量管不到另一份实例。
    """
    monkeypatch.setenv(
        "TANGTANG_GLOBAL_CONFIG",
        str(tmp_path / "_no_global" / "依赖位置.json"),
    )
    yield


# ── 2026-11-14（第二版）：这里原有一个屏蔽 jieba_fast 的 meta_path 钩子，已删除 ──
#
# 首版钩子在 `find_spec` 里 raise ImportError，想把坏包当「未安装」。实测两个问题：
#   1) `importlib.util.find_spec()` 是**探测**语义，不是导入语义。抛异常不等于
#      「视为不存在」，而是把探测本身炸掉——`gpt-sovits/start_api_patched.py:23`
#      正是用它判断装没装，于是 tests/test_gpt_launcher*.py 在**收集期**就 ERROR。
#   2) 前提本身不成立：Python 3.10 的 site-packages 里只有纯 `jieba 0.42.1`，
#      `find_spec('jieba_fast')` 本来就返回 None。项目「jieba_fast → 纯 jieba」
#      的回退路径**已经在被真实地走到**，不需要再伪造。
#
# 真正的形态就是「没装 jieba-fast」，让 find_spec 自然返回 None 即可。
