"""异常退出时只清理本进程拥有的 GPT-SoVITS 子进程。"""

import asyncio
import sys
import types
from unittest.mock import Mock

import pytest


def _load_tangtang(monkeypatch):
    # main.py wraps pytest's capture streams on Windows at import time; load it
    # under a neutral platform in this isolated unit test.
    monkeypatch.setattr(sys, "platform", "linux")
    from main import TangTang
    return TangTang


def _tangtang_with_proc(tangtang_cls, proc):
    app = object.__new__(tangtang_cls)
    app.handler = types.SimpleNamespace(
        _service_mgr=types.SimpleNamespace(_gpt_sovits_proc=proc),
    )
    return app


def test_atexit_does_not_kill_foreign_tts_port(monkeypatch):
    tangtang_cls = _load_tangtang(monkeypatch)
    _tangtang_with_proc(tangtang_cls, None)._atexit_cleanup()


def test_atexit_kills_only_owned_live_tts_process(monkeypatch):
    tangtang_cls = _load_tangtang(monkeypatch)
    proc = Mock(pid=4321, returncode=None)

    _tangtang_with_proc(tangtang_cls, proc)._atexit_cleanup()

    proc.kill.assert_called_once_with()


def test_main_cleans_failed_start_before_reraising(monkeypatch):
    """3001 绑定失败时，已构造的实例也必须走统一关闭流程。"""
    _load_tangtang(monkeypatch)
    import main as main_module

    instances = []

    class FailingTangTang:
        def __init__(self):
            self.stop_called = False
            instances.append(self)

        async def start(self):
            raise OSError("port already in use")

        async def stop(self):
            self.stop_called = True

    monkeypatch.setattr(main_module, "TangTang", FailingTangTang)

    with pytest.raises(OSError, match="port already in use"):
        asyncio.run(main_module.main())

    assert len(instances) == 1
    assert instances[0].stop_called is True


def test_ws_port_preflight_rejects_manual_second_start(monkeypatch):
    """旧实例仍持有 3001 时，第二次手动启动应在 Handler 前 fail-closed。"""
    _load_tangtang(monkeypatch)
    import main as main_module

    class FakeSocket:
        def settimeout(self, _seconds):
            pass

        def connect_ex(self, _address):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(main_module.socket, "socket", lambda *_args: FakeSocket())

    with pytest.raises(RuntimeError, match="3001.*已被占用"):
        main_module._assert_ws_port_available("127.0.0.1", 3001)


def test_tangtang_preflight_runs_before_handler_construction(monkeypatch, tmp_path):
    """端口冲突时不得先构造 Handler（模型/后台副作用）。"""
    _load_tangtang(monkeypatch)
    import main as main_module

    config = tmp_path / "config.yaml"
    config.write_text(
        """
bot:
  qq_id: '1'
  owner_qq: '2'
napcat:
  access_token: ''
behavior:
  active_interjection: false
tasks: {}
llm:
  provider: deepseek
  model: test
""",
        encoding="utf-8",
    )

    class FakeNapCat:
        def __init__(self, **_kwargs):
            self.ws_host = "127.0.0.1"
            self.ws_port = 3001

    handler_calls = []
    monkeypatch.setattr(main_module, "NapCatClient", FakeNapCat)
    monkeypatch.setattr(
        main_module,
        "_assert_ws_port_available",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("occupied")),
    )
    monkeypatch.setattr(
        main_module,
        "MessageHandler",
        lambda *_args, **_kwargs: handler_calls.append(True),
    )

    with pytest.raises(RuntimeError, match="occupied"):
        main_module.TangTang(str(config))

    assert handler_calls == []
