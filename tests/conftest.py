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
def _isolate_global_deps_config(tmp_path, monkeypatch):
    """把全局依赖配置指到临时目录（2026-11-14）。

    tools/依赖位置.py 的 get_deps_root() 有第四级回退：
    %LOCALAPPDATA%/tangtang/依赖位置.json。不隔离的话，本机真实配置会渗进
    所有测试，「默认位置」的断言会随机器状态随机变红（教训 #45）。
    用环境变量覆盖（模块内 _global_config_path() 每次现算），不改模块常量——
    测试各自 importlib 加载同一个文件，patch 常量管不到另一份实例。
    """
    monkeypatch.setenv("TANGTANG_GLOBAL_CONFIG", str(tmp_path / "_no_global" / "依赖位置.json"))
    yield
