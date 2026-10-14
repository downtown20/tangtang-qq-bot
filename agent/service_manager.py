"""
🔧 服务管理器 — 本地语音时自动启停 GPT-SoVITS / Ollama
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import sys
import time
import httpx
from pathlib import Path

logger = logging.getLogger("糖糖.ServiceMgr")

GPT_SOVITS_PORT = 9880
OLLAMA_PORT = 11434
# 2026-09-05：健康探测统一超时预算——无 CUDA 机器 CPU 首轮 /tts 实测 ~42.8s（笔记本取证），
# 15s 会把"能工作的慢服务"误判为不健康；GPU 机器同样受此预算约束（探测被取消不影响服务）。
GPT_SOVITS_HEALTH_TIMEOUT = 60.0
# 2026-08-15：从硬编码的本机绝对路径改为相对项目根——项目换路径/换机器时语音服务不再静默失效
GPT_SOVITS_DIR = Path(__file__).resolve().parent.parent / "gpt-sovits"


class ServiceManager:
    """管理外部本地服务的生命周期"""

    def __init__(self):
        self._gpt_sovits_proc: asyncio.subprocess.Process | None = None
        self._gpt_sovits_drain_task: asyncio.Task | None = None
        self._gpt_sovits_supervisor_task: asyncio.Task | None = None
        self._gpt_sovits_stop_requested = False
        # 一次 bot 运行内，已就绪子进程的自动恢复最多执行一次；否则每次
        # 子进程再次退出都会重新挂监护并形成无限重启风暴。
        self._gpt_sovits_recovery_attempted = False
        # 语音请求在服务已离线时还有一条恢复入口；它也必须有独立的
        # 一次性预算，不能被连续语音请求反复触发重启。
        self._gpt_sovits_failure_recovery_attempted = False
        # GPU 原生崩溃（例如 Windows 上 c10.dll 的 0xC0000005）不能靠
        # 重试同一 CUDA 进程恢复；本次 bot 运行最多切 CPU 一次，避免
        # 启动阶段语音永久失效或形成重启风暴。下次人工重启会重新尝试 GPU。
        self._gpt_sovits_cpu_fallback_attempted = False
        self._gpt_sovits_cpu_mode = False
        # 记录子进程生命周期，ready 后的 code=1 才能与启动失败/外部终止区分。
        self._gpt_sovits_started_monotonic: float | None = None
        # Keep a bounded tail so an already-ready child that exits later still
        # leaves actionable diagnostics instead of silently disappearing.
        # GPT-SoVITS 的 traceback 可能出现在进度条之后；20 行尾部会把真正
        # 的异常冲掉。保留有界的 200 行，避免诊断增强反过来无限占内存。
        self._gpt_sovits_stderr_tail: deque[str] = deque(maxlen=200)
        self._gpt_sovits_stdout_tail: deque[str] = deque(maxlen=200)
        # 进度条/请求日志可能在 traceback 后继续刷屏；独立保留少量
        # 高信号行，退出时优先输出，不让最后 24 行再次遮住根因。
        self._gpt_sovits_stderr_diagnostics: deque[str] = deque(maxlen=32)
        self._gpt_sovits_stdout_diagnostics: deque[str] = deque(maxlen=32)

    # ═══════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════

    async def start_for_full_mode(self):
        """启动本地 TTS 服务（GPT-SoVITS）"""
        self._gpt_sovits_stop_requested = False
        self._gpt_sovits_recovery_attempted = False
        self._gpt_sovits_failure_recovery_attempted = False
        self._gpt_sovits_cpu_fallback_attempted = False
        self._gpt_sovits_cpu_mode = False
        await self._ensure_gpt_sovits()

    async def restart_gpt_sovits(self):
        """GPT-SoVITS 挂了 → 杀掉旧进程，重新启动"""
        if self._gpt_sovits_stop_requested:
            logger.info("🔧 GPT-SoVITS 正在停止，忽略迟到的语音失败恢复回调")
            return
        if self._gpt_sovits_failure_recovery_attempted:
            logger.error(
                "🔧 GPT-SoVITS 语音失败自动恢复次数已用尽，本次不再重启；"
                "请在下一次 bot 手动重启后重置恢复窗口"
            )
            return
        self._gpt_sovits_failure_recovery_attempted = True
        logger.warning("🔧 GPT-SoVITS 进程异常，强制重启…")
        self._gpt_sovits_stop_requested = False
        await self._cancel_gpt_sovits_supervisor()
        # 取消持久 drain
        if self._gpt_sovits_drain_task:
            self._gpt_sovits_drain_task.cancel()
            self._gpt_sovits_drain_task = None
        if self._gpt_sovits_proc and self._gpt_sovits_proc.returncode is None:
            try:
                self._gpt_sovits_proc.kill()
                await asyncio.wait_for(self._gpt_sovits_proc.wait(), timeout=5)
            except Exception:
                pass
        self._gpt_sovits_proc = None
        await asyncio.sleep(2)  # 等端口释放
        await self._ensure_gpt_sovits()

    async def stop_all(self):
        """关闭所有由管理器启动的子进程"""
        self._gpt_sovits_stop_requested = True
        await self._cancel_gpt_sovits_supervisor()
        if self._gpt_sovits_drain_task:
            self._gpt_sovits_drain_task.cancel()
            self._gpt_sovits_drain_task = None
        if self._gpt_sovits_proc and self._gpt_sovits_proc.returncode is None:
            logger.info("🔧 正在关闭 GPT-SoVITS…")
            try:
                self._gpt_sovits_proc.terminate()
                await asyncio.wait_for(self._gpt_sovits_proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._gpt_sovits_proc.kill()
            except Exception:
                pass
            logger.info("🔧 GPT-SoVITS 已关闭")

    async def _cancel_gpt_sovits_supervisor(self):
        """取消子进程监护任务，并等待其退出，避免关机/手动重启竞态。"""
        task = self._gpt_sovits_supervisor_task
        self._gpt_sovits_supervisor_task = None
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _start_gpt_sovits_supervisor(self):
        """为本管理器创建的已就绪进程挂一次退出监护。"""
        task = self._gpt_sovits_supervisor_task
        if task is not None and not task.done():
            return
        if self._gpt_sovits_proc is None or self._gpt_sovits_stop_requested:
            return
        self._gpt_sovits_supervisor_task = asyncio.ensure_future(
            self._supervise_gpt_sovits(self._gpt_sovits_proc)
        )

    async def _supervise_gpt_sovits(self, proc):
        """就绪后子进程退出时自动尝试一次恢复，避免 TTS 静默失效。

        只监护本次启动且仍归属管理器的进程；正常关机/显式重启会先取消
        该任务，因此不会把用户的停止动作误判成崩溃并重新拉起服务。
        恢复失败后交给健康检查和下一次语音失败回调，不做无限重启风暴。
        """
        try:
            await proc.wait()
            if self._gpt_sovits_stop_requested or self._gpt_sovits_proc is not proc:
                return
            returncode = proc.returncode
            started_monotonic = getattr(
                proc, "_tangtang_started_monotonic", self._gpt_sovits_started_monotonic
            )
            lifetime = (
                f"{time.monotonic() - started_monotonic:.1f}s"
                if started_monotonic is not None
                else "unknown"
            )
            pid = getattr(proc, "pid", "unknown")
            self._gpt_sovits_proc = None
            self._gpt_sovits_supervisor_task = None
            if self._gpt_sovits_recovery_attempted:
                logger.error(
                    "🔧 GPT-SoVITS 就绪后再次退出 (code=%s) pid=%s lifetime=%s，本次运行自动恢复次数已用尽；"
                    "停止自动拉起，交给健康检查/下一次语音失败回调",
                    returncode, pid, lifetime,
                )
                return
            self._gpt_sovits_recovery_attempted = True
            logger.warning(
                "🔧 GPT-SoVITS 就绪后进程退出 (code=%s) pid=%s lifetime=%s，尝试一次自动恢复",
                returncode, pid, lifetime,
            )
            await asyncio.sleep(2)
            if self._gpt_sovits_stop_requested:
                return
            await self._ensure_gpt_sovits()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("🔧 GPT-SoVITS 退出恢复监护异常")
        finally:
            current = asyncio.current_task()
            if self._gpt_sovits_supervisor_task is current:
                self._gpt_sovits_supervisor_task = None

    # ═══════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════

    @staticmethod
    def _is_actionable_gpt_diagnostic(line: str) -> bool:
        """判断子进程输出是否像错误证据，而不是普通进度/请求日志。"""
        lowered = str(line).lower()
        return any(token in lowered for token in (
            "traceback", "exception", "runtimeerror", "valueerror", "typeerror",
            "fatal", "critical", "launcher non-zero", "access violation",
            "segmentation fault", "outofmemoryerror", "cuda error",
            "no kernel image", "killed", "no active exception",
            # start_api_patched.py 在 API 无异常返回时给出这一明确终止信号；
            # 它必须进入优先诊断队列，避免被后续进度/请求日志挤出尾部。
            "returned unexpectedly",
        ))

    @staticmethod
    async def _check_port(port: int) -> bool:
        """检查端口是否已在使用（服务是否已在运行）"""
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"http://127.0.0.1:{port}", timeout=2.0)
                return True  # 任何响应都说明服务在跑
        except Exception:
            return False

    async def _persistent_drain(self):
        """后台持续读取 stdout/stderr，避免管道塞满和崩溃诊断丢失。"""
        proc = self._gpt_sovits_proc
        if proc is None:
            return

        async def _drain_stream(stream, bucket, diagnostics):
            if stream is None:
                return
            while True:
                try:
                    # Windows 上 wait_for(readline()) 超时取消可能不会及时
                    # 解除底层 readuntil；下一轮读取同一管道会报
                    # ``readuntil() called while another coroutine is already
                    # waiting``。一个管道只保留一个读取协程，退出时统一取消。
                    line = await stream.readline()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("🔧 GPT-SoVITS 输出管道读取异常")
                    return
                if not line:
                    return
                decoded = line.decode(errors="replace").rstrip()
                if decoded:
                    bucket.append(decoded)
                    if self._is_actionable_gpt_diagnostic(decoded):
                        diagnostics.append(decoded)

        readers = [
            asyncio.create_task(_drain_stream(
                getattr(proc, "stderr", None), self._gpt_sovits_stderr_tail,
                self._gpt_sovits_stderr_diagnostics,
            )),
            asyncio.create_task(_drain_stream(
                getattr(proc, "stdout", None), self._gpt_sovits_stdout_tail,
                self._gpt_sovits_stdout_diagnostics,
            )),
        ]

        async def _cancel_readers():
            """统一收口管道读取任务，避免异常路径留下孤儿 reader。"""
            pending = [task for task in readers if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            wait_method = getattr(proc, "wait", None)
            if callable(wait_method):
                await wait_method()
            else:
                # 测试替身可能只通过 readline 回填 returncode；给 reader
                # 一个调度机会后再等待其退出状态。
                while proc.returncode is None:
                    await asyncio.sleep(0.05)
            if readers:
                _done, pending = await asyncio.wait(readers, timeout=0.5)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            if proc.returncode is not None:
                started_monotonic = getattr(
                    proc, "_tangtang_started_monotonic", self._gpt_sovits_started_monotonic
                )
                lifetime = (
                    f"{time.monotonic() - started_monotonic:.1f}s"
                    if started_monotonic is not None
                    else "unknown"
                )
                pid = getattr(proc, "pid", "unknown")
                stderr_tail = list(self._gpt_sovits_stderr_tail)[-24:]
                stdout_tail = list(self._gpt_sovits_stdout_tail)[-24:]
                prioritized = (
                    [f"stderr: {line}" for line in self._gpt_sovits_stderr_diagnostics]
                    + [f"stdout: {line}" for line in self._gpt_sovits_stdout_diagnostics]
                )
                recent = (
                    [f"stderr: {line}" for line in stderr_tail]
                    + [f"stdout: {line}" for line in stdout_tail]
                )
                seen = set()
                detail_lines = []
                for line in prioritized + recent:
                    if line not in seen:
                        seen.add(line)
                        detail_lines.append(line)
                # 优先保留错误证据，同时给正常尾部留出有限上下文；
                # 输出上限防止异常单行/进度刷屏放大主日志。
                detail_lines = detail_lines[:96]
                detail = "\n  ".join(detail_lines) if detail_lines else "无 stdout/stderr"
                logger.error(
                    f"🔧 GPT-SoVITS 就绪后进程意外退出 (code={proc.returncode}) pid={pid} lifetime={lifetime}，"
                    f"最后输出:\n  {detail}"
                )
        except asyncio.CancelledError:
            await _cancel_readers()
            raise
        except Exception:
            await _cancel_readers()
            logger.exception("🔧 GPT-SoVITS stdout/stderr 监控异常")

    async def _check_gpt_sovits_healthy(self) -> bool:
        """健康检查：正式 /tts 端点是否正常响应（HTTP 200 且音频体 >1000B）。

        失败日志分级：进程退出 / 端口无响应 / TTS 推理超时分开记录，避免
        把三类故障全部压成同一句静默 False（2026-09-05 M1c）。
        """
        try:
            # 健康检查必须与正式请求使用同一参考文本；错配 prompt 会让正常服务
            # 被误判为异常，随后进入无意义的杀进程/重启循环。
            from .voice import VoiceEngine
            prompt_lang, prompt_text = VoiceEngine._SPEAKER_PROMPT["normal"]
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"http://127.0.0.1:{GPT_SOVITS_PORT}/tts",
                    json={
                        "text": "测试", "text_lang": "zh",
                        "ref_audio_path": str(GPT_SOVITS_DIR / "speakers" / "normal" / "ref.wav"),
                        "prompt_lang": prompt_lang, "prompt_text": prompt_text,
                        "text_split_method": "cut0", "batch_size": 1,
                        "media_type": "wav", "streaming_mode": False,
                    },
                    timeout=GPT_SOVITS_HEALTH_TIMEOUT,
                )
                return r.status_code == 200 and len(r.content) > 1000
        except httpx.TimeoutException:
            # 服务在听但合成超预算（无 CUDA 首轮可达 ~43s，60s 预算仍超=真慢）
            logger.warning(f"🔧 GPT-SoVITS 健康探测超时（>{GPT_SOVITS_HEALTH_TIMEOUT:.0f}s）——TTS 推理过慢")
            return False
        except httpx.ConnectError:
            logger.warning("🔧 GPT-SoVITS 健康探测连接失败——端口无响应（进程可能未启动/已退出）")
            return False
        except Exception:
            logger.exception("🔧 GPT-SoVITS 健康探测异常")
            return False

    @staticmethod
    def _is_native_gpu_crash(returncode, stderr_lines) -> bool:
        """识别可安全切 CPU 的 GPU 原生崩溃，不把普通配置错误吞成兜底。"""
        try:
            code = int(returncode) & 0xFFFFFFFF
        except (TypeError, ValueError):
            code = None
        if code == 0xC0000005:  # Windows STATUS_ACCESS_VIOLATION
            return True
        return any(
            token in str(line).lower()
            for line in (stderr_lines or [])
            for token in (
                "c10.dll",
                "cuda error",
                "cuda out of memory",
                "outofmemoryerror",
                "no kernel image is available",
            )
        )

    @staticmethod
    def _gpt_sovits_child_env(force_cpu: bool):
        """为 GPT-SoVITS 构造诊断/隔离环境，不改变 GPU/CPU 选择。"""
        env = os.environ.copy()
        # 若模型线程触发 Python 可捕获的致命错误，faulthandler 会把线程栈
        # 写入现有 stderr 长尾；对正常推理没有行为改变。
        env["PYTHONFAULTHANDLER"] = "1"
        if not force_cpu:
            return env
        # 使用 -1 而不是空串：本机 PyTorch 实测空串仍报告
        # cuda.is_available()=True（但 device_count=0），TTS_Config 会误选 CUDA；
        # -1 才能让它可靠降为 CPU。无需改写 tts_infer.yaml，也不影响主 bot。
        env["CUDA_VISIBLE_DEVICES"] = "-1"
        return env

    async def _fallback_to_cpu_after_gpu_crash(
        self, returncode, stderr_lines, force_cpu: bool
    ) -> bool:
        """GPU 启动发生已确认的原生崩溃时，仅尝试一次 CPU 进程。"""
        if (
            force_cpu
            or self._gpt_sovits_cpu_fallback_attempted
            or not self._is_native_gpu_crash(returncode, stderr_lines)
        ):
            return False
        self._gpt_sovits_cpu_fallback_attempted = True
        # GPU→CPU 已经是本次运行的一次自动恢复；CPU 子进程若再次退出，
        # 交给健康检查/下一次语音失败回调，不再继续自动拉起。
        self._gpt_sovits_recovery_attempted = True
        self._gpt_sovits_cpu_mode = True
        logger.warning(
            "🔧 GPT-SoVITS GPU 进程发生原生崩溃 (code=%s)，本次运行切换 CPU 兜底；"
            "下次人工重启将重新尝试 GPU",
            returncode,
        )
        await asyncio.sleep(1)
        if self._gpt_sovits_stop_requested:
            return True
        await self._ensure_gpt_sovits(force_cpu=True)
        return True

    async def _ensure_gpt_sovits(self, *, force_cpu: bool | None = None):
        """确保 GPT-SoVITS API 在运行。已在运行就跳过，否则启动。"""
        if force_cpu is None:
            force_cpu = self._gpt_sovits_cpu_mode
        elif force_cpu:
            self._gpt_sovits_cpu_mode = True
        if await self._check_port(GPT_SOVITS_PORT):
            # 端口有响应——但必须验证是真的健康（可能是上次残留的孤儿进程）
            if await self._check_gpt_sovits_healthy():
                logger.info(f"🔧 GPT-SoVITS 已在运行 (端口 {GPT_SOVITS_PORT})")
                return
            else:
                logger.warning("🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…")
                # 杀掉不健康的旧进程
                if self._gpt_sovits_proc is None:
                    # 孤儿进程——用 netstat 找 PID 杀
                    try:
                        import subprocess
                        result = subprocess.run(
                            ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                        )
                        for line in result.stdout.split("\n"):
                            if f"127.0.0.1:{GPT_SOVITS_PORT}" in line and "LISTENING" in line:
                                pid = line.strip().split()[-1]
                                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                                logger.info(f"🔧 已终止孤儿 GPT-SoVITS (PID={pid})")
                                break
                    except Exception as e:
                        logger.warning(f"🔧 清理孤儿进程失败: {e}")
                await asyncio.sleep(2)  # 等端口释放

        if not GPT_SOVITS_DIR.exists():
            logger.warning(f"🔧 GPT-SoVITS 目录不存在: {GPT_SOVITS_DIR}，跳过语音")
            return

        mode = "CPU 兜底" if force_cpu else "GPU"
        logger.info(f"🔧 启动 GPT-SoVITS API (端口 {GPT_SOVITS_PORT}，{mode})…")
        try:
            self._gpt_sovits_proc = await asyncio.create_subprocess_exec(
                # 子进程 stdout 若使用块缓冲，原生退出/被系统终止时最近的
                # 模型阶段和请求上下文可能尚未写入管道；-u 让退出取证完整。
                sys.executable, "-u", "start_api_patched.py",
                cwd=str(GPT_SOVITS_DIR),
                # uvicorn/Python 的启动异常可能写到 stdout；stdout 与 stderr
                # 都必须保留并持续 drain，否则 native/模型退出只剩无法取证的 code=1。
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._gpt_sovits_child_env(force_cpu),
            )
            proc = self._gpt_sovits_proc
            self._gpt_sovits_started_monotonic = time.monotonic()
            # 将时间戳绑定到具体 Process，避免旧 drain 与新 supervisor 重启竞态时
            # 读取到下一代子进程的启动时间。
            try:
                proc._tangtang_started_monotonic = self._gpt_sovits_started_monotonic
            except Exception:
                pass
            self._gpt_sovits_stderr_tail.clear()
            self._gpt_sovits_stdout_tail.clear()
            self._gpt_sovits_stderr_diagnostics.clear()
            self._gpt_sovits_stdout_diagnostics.clear()
            # 启动阶段同时收集 stdout/stderr；两条管道都必须持续 drain，
            # 否则任一高流量输出都可能反过来阻塞 TTS 子进程。
            startup_stderr = []
            startup_stdout = []

            async def _collect_stream(stream, target):
                if stream is None:
                    return
                while True:
                    try:
                        # 启动阶段也不能反复超时取消 readline；取消整个
                        # 收集任务时再一次性关闭两个管道读取协程。
                        line = await stream.readline()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("🔧 GPT-SoVITS 启动输出读取异常")
                        return
                    if not line:
                        return
                    decoded = line.decode(errors="replace").rstrip()
                    if decoded:
                        target.append(decoded)

            async def _collect_output():
                await asyncio.gather(
                    _collect_stream(proc.stderr, startup_stderr),
                    _collect_stream(proc.stdout, startup_stdout),
                )

            drain_task = asyncio.ensure_future(_collect_output())
            # 等待服务就绪（最多 60 秒——模型加载需要时间）
            for i in range(60):
                await asyncio.sleep(1)
                if proc.returncode is not None:
                    drain_task.cancel()
                    await asyncio.gather(drain_task, return_exceptions=True)
                    output_lines = [
                        line for line in startup_stderr + startup_stdout if line.strip()
                    ]
                    if output_lines:
                        logger.error(
                            f"🔧 GPT-SoVITS 进程意外退出 (code={proc.returncode})，"
                            f"最后输出:\n  " + "\n  ".join(output_lines[-8:])
                        )
                    else:
                        logger.error(
                            f"🔧 GPT-SoVITS 进程意外退出 (code={proc.returncode})，无 stdout/stderr"
                        )
                    returncode = proc.returncode
                    self._gpt_sovits_proc = None
                    if await self._fallback_to_cpu_after_gpu_crash(
                        returncode, output_lines, force_cpu
                    ):
                        return
                    return
                if await self._check_port(GPT_SOVITS_PORT):
                    # 端口响应本身不是就绪证据：例如 GPT-SoVITS 启动中的
                    # HTTP 服务可能对根路径返回 404。必须再用正式 /tts
                    # 请求确认模型、参考音频和推理链路都可用。
                    if await self._check_gpt_sovits_healthy():
                        drain_task.cancel()
                        await asyncio.gather(drain_task, return_exceptions=True)
                        self._gpt_sovits_stderr_tail.extend(startup_stderr[-20:])
                        self._gpt_sovits_stdout_tail.extend(startup_stdout[-20:])
                        # 启动持久 drain 防止 stdout/stderr pipe 堆积
                        self._gpt_sovits_drain_task = asyncio.ensure_future(self._persistent_drain())
                        self._start_gpt_sovits_supervisor()
                        logger.info(f"🔧 GPT-SoVITS 已就绪 ({i + 1}s，{mode})")
                        return
            # 超时：杀掉进程
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
            logger.warning("🔧 GPT-SoVITS 启动超时 (60s)，语音可能不可用")
            output_lines = [
                line for line in startup_stderr + startup_stdout if line.strip()
            ]
            if output_lines:
                logger.warning("  stdout/stderr:\n  " + "\n  ".join(output_lines[-5:]))
            try:
                self._gpt_sovits_proc.kill()
                await asyncio.wait_for(self._gpt_sovits_proc.wait(), timeout=5)
            except Exception:
                pass
        except FileNotFoundError:
            logger.warning("🔧 找不到 Python 解释器，GPT-SoVITS 启动失败")
        except Exception as e:
            logger.warning(f"🔧 GPT-SoVITS 启动失败: {e}")
