"""GPT-SoVITS 启动器 jieba 回退 + 健康检查超时常量回归（M1，2026-09-05）。

背景：笔记本换机因 jieba_fast 缺失（Windows 无 MSVC 时无预编译 wheel）语音服务起不来；
台式机基线为 jieba_fast 原生。start_api_patched.py 现在：原生优先 → 缺失回退纯 jieba
（映射 sys.modules）→ 两者皆缺抛明确 RuntimeError。健康检查超时改为统一常量
GPT_SOVITS_HEALTH_TIMEOUT=60s（无 CUDA CPU 首轮 /tts 实测 ~42.8s，15s 误判）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "gpt_launcher_patched", BASE / "gpt-sovits" / "start_api_patched.py")
LAUNCHER = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LAUNCHER)  # 顶层仅 _ensure_jieba_fast()，不启动服务

SERVICE_MANAGER = (BASE / "agent" / "service_manager.py").read_text(encoding="utf-8")


def _restore_sys_modules():
    """fallback 测试会注入 sys.modules 映射——逐测清理，防污染后续用例。"""
    for key in ("jieba_fast", "jieba_fast.posseg"):
        if key in sys.modules and key not in getattr(sys.modules[key], "__name__", ""):
            del sys.modules[key]


@pytest.fixture(autouse=True)
def _clean_modules():
    yield
    _restore_sys_modules()


def test_launcher_has_three_branch_function():
    """启动器暴露可测的 _ensure_jieba_fast（返回 native/fallback，双缺抛错）。"""
    assert callable(LAUNCHER._ensure_jieba_fast)


def test_launcher_native_when_jieba_fast_installed():
    """当前环境（台式机基线）应为原生 jieba_fast 路径。"""
    assert LAUNCHER._mode == "native"


def test_launcher_fallback_when_jieba_fast_missing(monkeypatch):
    """jieba_fast 缺失（无 MSVC 机器）→ 回退纯 Python jieba，映射 sys.modules。"""
    import importlib.util as iu
    real_find = iu.find_spec

    def fake_find(name, *a, **kw):
        return None if name == "jieba_fast" else real_find(name, *a, **kw)

    monkeypatch.setattr(iu, "find_spec", fake_find)
    assert LAUNCHER._ensure_jieba_fast() == "fallback"
    assert "jieba_fast" in sys.modules          # chinese.py 的硬导入能找到模块
    assert "jieba_fast.posseg" in sys.modules   # `import jieba_fast.posseg as psg` 可用


def test_launcher_raises_when_both_missing(monkeypatch):
    """jieba 与 jieba_fast 都缺 → RuntimeError 且信息含安装指引。"""
    import importlib.util as iu
    real_find = iu.find_spec

    def fake_find(name, *a, **kw):
        return None if name in ("jieba_fast", "jieba") else real_find(name, *a, **kw)

    monkeypatch.setattr(iu, "find_spec", fake_find)
    with pytest.raises(RuntimeError, match="jieba"):
        LAUNCHER._ensure_jieba_fast()


def test_health_check_uses_shared_timeout_constant():
    """健康探测必须引用统一常量（改回 15s 硬编码 = 回归）。"""
    assert "GPT_SOVITS_HEALTH_TIMEOUT = 60.0" in SERVICE_MANAGER
    assert "timeout=GPT_SOVITS_HEALTH_TIMEOUT" in SERVICE_MANAGER
    assert "timeout=15.0" not in SERVICE_MANAGER.replace("timeout=15.0 # 旧", "")


def test_health_check_error_logs_classified():
    """超时/连接失败/其他异常分开记录，不再吞成同一静默 False。"""
    assert "httpx.TimeoutException" in SERVICE_MANAGER
    assert "httpx.ConnectError" in SERVICE_MANAGER
    assert "健康探测超时" in SERVICE_MANAGER
