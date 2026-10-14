"""ServiceManager 启动就绪判定回归测试。"""

import asyncio
import logging

from agent import service_manager
from agent.service_manager import ServiceManager


def test_gpt_launcher_normal_return_is_actionable_diagnostic():
    """包装器的正常返回终止信号必须优先保留。"""
    assert ServiceManager._is_actionable_gpt_diagnostic(
        "GPT-SoVITS launcher API returned unexpectedly without an exception"
    )
    assert not ServiceManager._is_actionable_gpt_diagnostic(
        "INFO: 127.0.0.1:9880 - POST /tts 200 OK"
    )


class _FakeStderr:
    async def readline(self):
        return b""


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = _FakeStderr()
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class _ExitingStderr:
    def __init__(self, process):
        self._process = process
        self._read = False

    async def readline(self):
        if not self._read:
            self._read = True
            return b"fatal: model worker stopped\\n"
        self._process.returncode = 17
        return b""


class _ExitingProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = _ExitingStderr(self)


class _EofStderr:
    async def readline(self):
        return b""


class _EofProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = _EofStderr()

    async def wait(self):
        self.returncode = 23
        return self.returncode


class _StdoutOnlyProcess:
    """模拟 uvicorn 把致命诊断写到 stdout 后退出。"""

    def __init__(self):
        self.returncode = None
        self.stderr = _FakeStderr()
        self.stdout = self
        self._read = False

    async def readline(self):
        if not self._read:
            self._read = True
            return b"fatal: stdout model worker stopped\n"
        self.returncode = 29
        return b""


class _ManyLinesStream:
    def __init__(self, prefix, count):
        self._lines = [f"{prefix}-{i}\n".encode() for i in range(count)]

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _ManyLinesProcess:
    def __init__(self):
        self.returncode = None
        self.stderr = _ManyLinesStream("traceback", 30)
        self.stdout = _ManyLinesStream("progress", 30)

    async def wait(self):
        self.returncode = 31
        return self.returncode


class _BuriedDiagnosticStream:
    def __init__(self):
        self._lines = [
            b"Traceback (most recent call last):\n",
            b"  File 'api_v2.py', line 601, in <module>\n",
            b"RuntimeError: model worker stopped\n",
        ] + [f"progress-{i}\n".encode() for i in range(80)]

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _BuriedDiagnosticProcess:
    """模拟 traceback 后被高流量进度条冲掉的 ready-child。"""

    def __init__(self):
        self.returncode = None
        self.stderr = _BuriedDiagnosticStream()
        self.stdout = _FakeStderr()

    async def wait(self):
        self.returncode = 37
        return self.returncode


class _SupervisedProcess:
    def __init__(self, returncode=17):
        self.returncode = None
        self._returncode = returncode

    async def wait(self):
        self.returncode = self._returncode
        return self.returncode


class _BlockingStream:
    def __init__(self):
        self.cancelled = False
        self._release = asyncio.Event()

    async def readline(self):
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""


class _WaitFailingProcess:
    returncode = None

    def __init__(self):
        self.stderr = _BlockingStream()
        self.stdout = _BlockingStream()

    async def wait(self):
        await asyncio.sleep(0)
        raise RuntimeError("wait failed")


def test_startup_does_not_report_ready_from_port_only(monkeypatch, caplog, tmp_path):
    """端口有响应但 /tts 不健康时，启动不得记录“已就绪”。"""
    manager = ServiceManager()
    process = _FakeProcess()
    health_calls = 0

    async def fake_sleep(_seconds):
        return None

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    port_calls = 0

    async def fake_check_port(_port):
        nonlocal port_calls
        port_calls += 1
        return port_calls > 1

    async def fake_check_health():
        nonlocal health_calls
        health_calls += 1
        return False

    monkeypatch.setattr(service_manager, "GPT_SOVITS_DIR", tmp_path)
    monkeypatch.setattr(service_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(service_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(manager, "_check_port", fake_check_port)
    monkeypatch.setattr(manager, "_check_gpt_sovits_healthy", fake_check_health)

    with caplog.at_level(logging.INFO, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._ensure_gpt_sovits())

    assert health_calls >= 1
    assert process.killed is True
    assert manager._gpt_sovits_drain_task is None
    assert not any("GPT-SoVITS 已就绪" in record.message for record in caplog.records)
    assert any("GPT-SoVITS 启动超时" in record.message for record in caplog.records)


def test_gpt_sovits_launch_is_unbuffered(monkeypatch, tmp_path):
    """子进程 stdout 必须即时刷新，原生退出时才不会丢失诊断。"""
    manager = ServiceManager()
    process = _FakeProcess()
    captured = {}

    async def fake_sleep(_seconds):
        return None

    async def fake_create_subprocess_exec(*args, **_kwargs):
        captured["args"] = args
        return process

    port_calls = 0

    async def fake_check_port(_port):
        nonlocal port_calls
        port_calls += 1
        return port_calls > 1

    async def fake_check_health():
        return True

    monkeypatch.setattr(service_manager, "GPT_SOVITS_DIR", tmp_path)
    monkeypatch.setattr(service_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        service_manager.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(manager, "_check_port", fake_check_port)
    monkeypatch.setattr(manager, "_check_gpt_sovits_healthy", fake_check_health)

    asyncio.run(manager._ensure_gpt_sovits())

    assert captured["args"][1:3] == ("-u", "start_api_patched.py")


def test_persistent_drain_reports_ready_child_exit_and_stderr(caplog):
    """就绪后的 GPT-SoVITS 子进程退出不得静默，必须保留退出码和 stderr 尾部。"""
    manager = ServiceManager()
    process = _ExitingProcess()
    process.pid = 4321
    manager._gpt_sovits_proc = process
    manager._gpt_sovits_started_monotonic = service_manager.time.monotonic() - 2.0

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    assert any("就绪后进程意外退出 (code=17)" in record.message for record in caplog.records)
    assert any("fatal: model worker stopped" in record.message for record in caplog.records)
    assert any("pid=4321" in record.message and "lifetime=" in record.message for record in caplog.records)


def test_persistent_drain_resolves_exit_code_after_stderr_eof(caplog):
    """stderr 先 EOF、returncode 后回填时也必须识别子进程已退出。"""
    manager = ServiceManager()
    manager._gpt_sovits_proc = _EofProcess()

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    assert any("就绪后进程意外退出 (code=23)" in record.message for record in caplog.records)


def test_persistent_drain_uses_process_specific_start_time(monkeypatch, caplog):
    """旧 drain 与新一代子进程并存时，生命周期必须绑定旧 Process。"""
    manager = ServiceManager()
    process = _EofProcess()
    process.pid = 4322
    process._tangtang_started_monotonic = 4.0
    manager._gpt_sovits_proc = process
    manager._gpt_sovits_started_monotonic = 1.0
    monkeypatch.setattr(service_manager.time, "monotonic", lambda: 10.0)

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    assert any("pid=4322" in record.message and "lifetime=6.0s" in record.message
               for record in caplog.records)


def test_persistent_drain_keeps_stdout_diagnostics(caplog):
    """stdout 中的 Python/uvicorn 异常也必须进入退出取证。"""
    manager = ServiceManager()
    manager._gpt_sovits_proc = _StdoutOnlyProcess()

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    assert any("就绪后进程意外退出 (code=29)" in record.message for record in caplog.records)
    assert any("fatal: stdout model worker stopped" in record.message
               for record in caplog.records)


def test_persistent_drain_keeps_labeled_long_tail(caplog):
    """进度条很多时仍应保留 traceback 尾部，并区分 stdout/stderr。"""
    manager = ServiceManager()
    manager._gpt_sovits_proc = _ManyLinesProcess()

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    message = "\n".join(record.message for record in caplog.records)
    assert "stderr: traceback-29" in message
    assert "stdout: progress-29" in message


def test_persistent_drain_keeps_buried_error_diagnostics(caplog):
    """traceback 被进度条淹没时仍须保留完整错误证据。"""
    manager = ServiceManager()
    manager._gpt_sovits_proc = _BuriedDiagnosticProcess()

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    message = "\n".join(record.message for record in caplog.records)
    assert "stderr: Traceback (most recent call last):" in message
    assert "stderr: RuntimeError: model worker stopped" in message


def test_persistent_drain_uses_one_reader_per_pipe(monkeypatch):
    """持续 drain 不得用超时反复取消同一个 StreamReader 的 readline。"""
    manager = ServiceManager()
    manager._gpt_sovits_proc = _StdoutOnlyProcess()

    def fail_wait_for(*_args, **_kwargs):
        raise AssertionError("pipe reader must not be wrapped in wait_for")

    monkeypatch.setattr(service_manager.asyncio, "wait_for", fail_wait_for)
    asyncio.run(manager._persistent_drain())


def test_persistent_drain_cleans_readers_when_wait_fails(caplog):
    """子进程 wait 异常时也必须取消两个管道 reader，不能遗留后台任务。"""
    manager = ServiceManager()
    process = _WaitFailingProcess()
    manager._gpt_sovits_proc = process

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._persistent_drain())

    assert process.stderr.cancelled is True
    assert process.stdout.cancelled is True
    assert any("stdout/stderr 监控异常" in record.message for record in caplog.records)


def test_supervisor_attempts_one_recovery_after_ready_child_exit(monkeypatch, caplog):
    """就绪子进程退出时，监护器自动恢复一次且不形成重启风暴。"""
    manager = ServiceManager()
    process = _SupervisedProcess()
    manager._gpt_sovits_proc = process
    recoveries = 0

    async def fake_sleep(_seconds):
        return None

    async def fake_ensure():
        nonlocal recoveries
        recoveries += 1

    monkeypatch.setattr(service_manager.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)

    with caplog.at_level(logging.WARNING, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._supervise_gpt_sovits(process))

    assert recoveries == 1
    assert manager._gpt_sovits_proc is None
    assert any("尝试一次自动恢复" in record.message for record in caplog.records)


def test_supervisor_does_not_recover_after_explicit_stop(monkeypatch):
    """显式停止先置位关机标记，不能被退出监护重新拉起。"""
    manager = ServiceManager()
    process = _SupervisedProcess()
    manager._gpt_sovits_proc = process
    manager._gpt_sovits_stop_requested = True
    recoveries = 0

    async def fake_ensure():
        nonlocal recoveries
        recoveries += 1

    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)
    asyncio.run(manager._supervise_gpt_sovits(process))

    assert recoveries == 0
    assert manager._gpt_sovits_proc is process


def test_supervisor_stops_after_one_recovery(monkeypatch, caplog):
    """同一 bot 运行内第二次就绪退出不得再次自动拉起。"""
    manager = ServiceManager()
    manager._gpt_sovits_recovery_attempted = True
    process = _SupervisedProcess(returncode=19)
    manager._gpt_sovits_proc = process
    recoveries = 0

    async def fake_ensure():
        nonlocal recoveries
        recoveries += 1

    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)
    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager._supervise_gpt_sovits(process))

    assert recoveries == 0
    assert manager._gpt_sovits_proc is None
    assert any("自动恢复次数已用尽" in record.message for record in caplog.records)


def test_voice_failure_restart_is_bounded_to_one_attempt(monkeypatch, caplog):
    """语音失败回调不能绕过恢复预算形成反复重启。"""
    manager = ServiceManager()
    calls = 0

    async def fake_ensure(**_kwargs):
        nonlocal calls
        calls += 1

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)
    monkeypatch.setattr(service_manager.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.ERROR, logger="糖糖.ServiceMgr"):
        asyncio.run(manager.restart_gpt_sovits())
        asyncio.run(manager.restart_gpt_sovits())

    assert calls == 1
    assert any("语音失败自动恢复次数已用尽" in record.message
               for record in caplog.records)


def test_voice_failure_restart_does_not_race_explicit_stop(monkeypatch):
    """关机期间迟到的语音失败回调不得重新拉起 GPT。"""
    manager = ServiceManager()
    manager._gpt_sovits_stop_requested = True
    calls = 0

    async def fake_ensure(**_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)

    asyncio.run(manager.restart_gpt_sovits())

    assert calls == 0
    assert manager._gpt_sovits_stop_requested is True


def test_native_gpu_crash_is_restricted_to_known_signals():
    """只有明确 GPU 崩溃/OOM 才允许进入 CPU 兜底，普通退出不应被吞掉。"""
    assert ServiceManager._is_native_gpu_crash(3221225477, []) is True
    assert ServiceManager._is_native_gpu_crash(1, ["CUDA error: device lost"]) is True
    assert ServiceManager._is_native_gpu_crash(
        1, ["torch.OutOfMemoryError: CUDA out of memory"]
    ) is True
    assert ServiceManager._is_native_gpu_crash(1, ["missing model file"]) is False
    assert ServiceManager._is_native_gpu_crash(None, []) is False


def test_cpu_fallback_is_one_shot_and_hides_cuda(monkeypatch):
    """GPU 原生崩溃只切 CPU 一次，子进程不可见 CUDA 但父进程环境不变。"""
    manager = ServiceManager()
    calls = []

    async def fake_ensure(**kwargs):
        calls.append(kwargs)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(manager, "_ensure_gpt_sovits", fake_ensure)
    monkeypatch.setattr(service_manager.asyncio, "sleep", fake_sleep)

    assert asyncio.run(
        manager._fallback_to_cpu_after_gpu_crash(3221225477, ["c10.dll"], False)
    ) is True
    assert manager._gpt_sovits_cpu_mode is True
    assert manager._gpt_sovits_recovery_attempted is True
    assert calls == [{"force_cpu": True}]

    # 第二次不能再次拉起任何进程。
    assert asyncio.run(
        manager._fallback_to_cpu_after_gpu_crash(3221225477, ["c10.dll"], False)
    ) is False
    assert calls == [{"force_cpu": True}]

    gpu_env = ServiceManager._gpt_sovits_child_env(False)
    assert gpu_env["PYTHONFAULTHANDLER"] == "1"
    cpu_env = ServiceManager._gpt_sovits_child_env(True)
    assert cpu_env["PYTHONFAULTHANDLER"] == "1"
    assert cpu_env["CUDA_VISIBLE_DEVICES"] == "-1"
