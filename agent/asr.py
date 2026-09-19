"""
小糖糖的语音识别模块 🎤
QQ 语音消息 → 下载 → 转码 → ASR → 文本

借鉴 EchoBot 的 ASR 架构：
- SenseVoice (sherpa-onnx) 作为主引擎，离线免费
- ffmpeg 做音频格式转换
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from .async_io import run_bounded_blocking

logger = logging.getLogger("糖糖.ASR")

# SenseVoice 模型（sherpa-onnx），自动下载
_SENSE_VOICE_MODEL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
_SENSE_VOICE_URL = (
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{_SENSE_VOICE_MODEL}.tar.bz2"
)

# 模型存放目录
_MODEL_DIR = Path(__file__).parent.parent / "asr_models"


def _extract_tar_safely(tar, target: Path) -> None:
    """安全解压：拒绝绝对路径、`..` 穿越、符号链接与设备文件。

    ⚠ Python 3.10 的 `extractall()` **没有 `filter` 参数**（3.12 才加），
    默认完全信任压缩包内容。模型是从 GitHub Releases 拉的 tar.bz2——
    处于中间人环境或上游 Release 被替换时，一个含 `../../启动/x.bat`
    或符号链接条目的小包就能一路写到目标目录之外。
    2026-09-19 独立安全审计把这条列为唯一带本地代码执行潜力的面。
    """
    base = Path(target).resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"压缩包含非法条目（链接/设备文件）：{member.name}")
        dest = (base / member.name).resolve()
        if base != dest and base not in dest.parents:
            raise ValueError(f"压缩包含路径穿越条目：{member.name}")
    tar.extractall(target)


def _find_model_file(model_dir: Path) -> Path | None:
    """在模型目录中定位 onnx 模型文件。

    2026-08-15 修复：旧代码硬编码 model.onnx，但 2025-09-09 int8 版
    实际文件名是 model.int8.onnx——目录存在却永远「找不到模型」，
    每次启动重复下载 233MB，加载识别器时报 FileNotFoundError。
    按优先级匹配：model.onnx → model.int8.onnx → 任意 .onnx。
    """
    if not model_dir.exists():
        return None
    for name in ("model.onnx", "model.int8.onnx"):
        candidate = model_dir / name
        if candidate.exists():
            return candidate
    onnx_files = sorted(model_dir.glob("*.onnx"))
    return onnx_files[0] if onnx_files else None


class VoiceRecognizer:
    """语音识别器"""

    def __init__(self):
        self._recognizer = None
        self._model_dir: Path | None = None
        self._model_file: Path | None = None  # 实际 onnx 文件（model.onnx 或 model.int8.onnx）
        self._available: bool | None = None  # None=未检测, True=可用, False=不可用

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = self._check_dependencies()
        return self._available

    def _check_dependencies(self) -> bool:
        """检查 sherpa-onnx 是否已安装"""
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except ImportError:
            logger.warning("⚠️ sherpa-onnx 未安装，语音识别不可用。安装: pip install sherpa-onnx")
            return False

    async def _ensure_model(self) -> bool:
        """确保 SenseVoice 模型已下载（异步，不阻塞事件循环）"""
        model_path = _MODEL_DIR / _SENSE_VOICE_MODEL
        model_file = _find_model_file(model_path)
        if model_file:
            self._model_dir = model_path
            self._model_file = model_file
            return True

        # 自动下载（在后台线程执行，避免阻塞事件循环）
        logger.info(f"📥 正在下载语音识别模型 ({_SENSE_VOICE_MODEL})...")
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        import tarfile
        from urllib.request import urlopen

        def _download_and_extract():
            tmp_file = _MODEL_DIR / f"{_SENSE_VOICE_MODEL}.tar.bz2"
            if not tmp_file.exists():
                logger.info(f"📥 下载中: {_SENSE_VOICE_URL}")
                resp = urlopen(_SENSE_VOICE_URL, timeout=600)
                tmp_file.write_bytes(resp.read())
            logger.info(f"📦 解压模型...")
            with tarfile.open(tmp_file, "r:bz2") as tar:
                _extract_tar_safely(tar, _MODEL_DIR)
            tmp_file.unlink(missing_ok=True)
            return _find_model_file(model_path) is not None

        try:
            ok = await run_bounded_blocking(
                "asr.download_and_extract_model",
                _download_and_extract,
                logger=logger,
                log_prefix="🎤 ASR 模型下载/解压较慢",
            )
            if ok:
                self._model_dir = model_path
                self._model_file = _find_model_file(model_path)
                logger.info(f"✅ 语音识别模型就绪: {model_path}")
                return True
        except Exception as e:
            logger.error(f"❌ 模型下载失败: {e}")
        return False

    def _load_recognizer(self):
        """加载 SenseVoice 识别器"""
        import sherpa_onnx

        model_file = str(self._model_file)
        tokens_file = str(self._model_dir / "tokens.txt")

        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model_file,
            tokens=tokens_file,
            use_itn=True,  # 反文本归一化（数字/日期等）
            num_threads=2,
            provider="cpu",
        )

    async def init(self) -> bool:
        """初始化识别器（下载模型 + 加载）。返回是否成功。"""
        if not self.available:
            return False
        if self._recognizer is not None:
            return True
        if not await self._ensure_model():
            return False
        try:
            await run_bounded_blocking(
                "asr.load_recognizer",
                self._load_recognizer,
                logger=logger,
                log_prefix="🎤 ASR 模型加载较慢",
            )
            logger.info("🎤 语音识别引擎已就绪")
            return True
        except Exception as e:
            logger.error(f"❌ 加载语音识别模型失败: {e}")
            self._available = False
            return False

    async def transcribe(self, audio_path: str) -> str:
        """识别音频文件，返回文本。自动处理格式转换。"""
        if not self.available:
            return ""

        # 确保模型已加载
        if self._recognizer is None:
            if not await self.init():
                return ""

        # 转换为 16kHz mono WAV（异步）
        wav_path = await _convert_to_wav(audio_path)
        if not wav_path:
            return ""

        try:
            import sherpa_onnx

            # 读取 WAV 样本（标准库 wave/struct 是同步 I/O，移出事件循环）
            sample_rate, samples = await run_bounded_blocking(
                "asr.wav_sample_read",
                _read_wav,
                wav_path,
                logger=logger,
                log_prefix="🎤 ASR WAV 样本读取较慢",
            )
            if sample_rate != 16000:
                samples = await run_bounded_blocking(
                    "asr.wav_resample",
                    _resample,
                    samples,
                    sample_rate,
                    16000,
                    logger=logger,
                    log_prefix="🎤 ASR 音频重采样较慢",
                )
                sample_rate = 16000

            # 创建流并识别
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate, samples)
            self._recognizer.decode_stream(stream)

            result = stream.result
            text = result.text.strip() if result and result.text else ""

            if text:
                logger.info(f"🎤 ASR → {text[:80]}")

            return text
        except Exception as e:
            logger.error(f"❌ 语音识别失败: {e}")
            return ""
        finally:
            # 清理临时 WAV
            if wav_path != audio_path:
                await run_bounded_blocking(
                    "asr.wav_cleanup",
                    Path(wav_path).unlink,
                    missing_ok=True,
                    logger=logger,
                    log_prefix="🎤 ASR 临时 WAV 清理较慢",
                )


# ---- 全局单例 ----
_recognizer: VoiceRecognizer | None = None


def get_recognizer() -> VoiceRecognizer:
    global _recognizer
    if _recognizer is None:
        _recognizer = VoiceRecognizer()
    return _recognizer


# ---- 音频工具 ----

async def _convert_to_wav(audio_path: str) -> str | None:
    """将音频转为 16kHz mono WAV（异步，不阻塞事件循环）"""
    p = Path(audio_path)
    # 输入存在性检查和 WAV 快速路径都可能触发磁盘 I/O；统一移到有界
    # blocking worker，避免已是 WAV 的语音反而绕过异步边界。
    def _inspect_input() -> tuple[bool, str | None]:
        if not p.exists():
            return False, None
        if p.suffix.lower() != ".wav":
            return True, None
        try:
            sr, _ = _read_wav(str(p))
        except Exception:
            return True, None
        return True, str(p) if sr == 16000 else None

    input_exists, ready_wav = await run_bounded_blocking(
        "asr.input_inspect",
        _inspect_input,
        logger=logger,
        log_prefix="🎤 ASR 输入音频检查较慢",
    )
    if not input_exists:
        return None
    if ready_wav:
        return ready_wav

    # QQ 语音实际是 SILK v3（文件名带 .amr 但内容是 silk，2026-08-24 取证：
    # get_record 返回字节头 b'\x02#!SILK_V3'）——标准 ffmpeg 无 silk 解码器，
    # 转码必失败。先按魔数检测走 pilk 解码。
    def _read_header() -> bytes:
        with open(p, "rb") as f:
            return f.read(10)

    try:
        head = await run_bounded_blocking(
            "asr.input_header_read",
            _read_header,
            logger=logger,
            log_prefix="🎤 ASR 音频头读取较慢",
        )
    except OSError:
        head = b""
    if b"#!SILK_V3" in head:
        try:
            import pilk
        except ImportError:
            logger.warning(
                "⚠️ 检测到 SILK v3 语音但 pilk 未安装（pip install pilk），"
                "QQ 语音识别不可用"
            )
            return None
        import tempfile
        fd, output = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            await run_bounded_blocking(
                "asr.silk_to_wav",
                pilk.silk_to_wav,
                str(p),
                output,
                16000,
                logger=logger,
                log_prefix="🎤 SILK 解码较慢",
            )
            return output
        except Exception as e:
            logger.warning(f"⚠️ SILK 解码失败: {e}")
            Path(output).unlink(missing_ok=True)
            return None

    # 尝试用 ffmpeg 转码
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning(
            "⚠️ ffmpeg 未安装，无法转换语音格式。\n"
            "   安装方法（任选一种）：\n"
            "   1. winget install Gyan.FFmpeg  （推荐，命令行直接装）\n"
            "   2. 下载 ffmpeg.exe 放到 C:\\Windows\\System32\\\n"
            "   3. 运行 python 安装ffmpeg.py"
        )
        return None

    import tempfile as _tmp
    fd, output = _tmp.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", audio_path, "-ar", "16000", "-ac", "1",
            "-sample_fmt", "s16", output,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            return output
        else:
            logger.warning(f"⚠️ ffmpeg 转码失败: {audio_path}")
            return None
    except asyncio.TimeoutError:
        logger.warning(f"⚠️ ffmpeg 转码超时: {audio_path}")
        return None


def _read_wav(wav_path: str) -> tuple[int, list[float]]:
    """读取 WAV 文件，返回 (sample_rate, samples)"""
    import wave
    import struct

    with wave.open(wav_path, "rb") as wf:
        # 标准库 wave 无下划线 API（getframerate 而非 get_framerate）——
        # 2026-08-24 修复：旧写法 NameError 级错误，ASR 路径从未跑通
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()

        raw = wf.readframes(n_frames)

    # 解码为 float samples
    if sample_width == 2:
        fmt = f"<{n_frames * n_channels}h"
    elif sample_width == 4:
        fmt = f"<{n_frames * n_channels}i"
    else:
        raise ValueError(f"不支持的采样位深: {sample_width}")

    data = struct.unpack(fmt, raw)
    max_val = float(1 << (sample_width * 8 - 1))

    # 转单声道
    if n_channels == 1:
        samples = [s / max_val for s in data]
    else:
        samples = [
            sum(data[i * n_channels:(i + 1) * n_channels]) / (n_channels * max_val)
            for i in range(n_frames)
        ]

    return sample_rate, samples


def _resample(samples: list[float], src_rate: int, dst_rate: int) -> list[float]:
    """简单线性重采样"""
    if src_rate == dst_rate:
        return samples
    ratio = src_rate / dst_rate
    new_len = int(len(samples) / ratio)
    result = []
    for i in range(new_len):
        src_idx = i * ratio
        src_i = int(src_idx)
        frac = src_idx - src_i
        if src_i + 1 < len(samples):
            result.append(samples[src_i] * (1 - frac) + samples[src_i + 1] * frac)
        else:
            result.append(samples[src_i])
    return result
