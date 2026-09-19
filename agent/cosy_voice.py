"""
CosyVoice 3 语音引擎 🎤
通过 HTTP 调用本地 TTS 服务器（Python 3.10 + GPU），
糖糖主进程（Python 3.14）本模块做轻量客户端。
"""
from __future__ import annotations

import io
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

from .paths import find_python310

logger = logging.getLogger("糖糖.CosyVoice")

TTS_SERVER_URL = "http://127.0.0.1:9267"
TTS_SCRIPT = Path(__file__).parent.parent / "cosyvoice3" / "tts_server.py"
# 2026-08-15：Python 3.10 解释器不再硬编码 Administrator 路径——多候选自动发现
TTS_PYTHON: Optional[Path] = find_python310()


class CosyVoiceEngine:
    """CosyVoice 3 客户端——通本地 HTTP 调用 GPU TTS 服务"""

    def __init__(self):
        self._http = None

    @property
    def http(self):
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    @property
    def available(self) -> bool:
        """Checks if there's a GPU available (driver level)"""
        try:
            import torch
            return torch.cuda.is_available()
        except Exception:
            return False

    # ---- 服务管理 ----

    def start_server(self) -> bool:
        """后台启动 CosyVoice 3 TTS 服务器（Python 3.10, GPU）"""
        if not TTS_SCRIPT.exists():
            logger.error(f"❌ TTS server script not found: {TTS_SCRIPT}")
            return False
        if TTS_PYTHON is None:
            logger.error("❌ 未找到 Python 3.10 解释器——CosyVoice 不可用（可设 PYTHON310 环境变量指定）")
            return False
        try:
            subprocess.Popen(
                [str(TTS_PYTHON), str(TTS_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(TTS_SCRIPT.parent),
            )
            logger.info("🎤 CosyVoice 3 TTS 服务已启动（后台）")
            return True
        except Exception as e:
            logger.error(f"❌ 启动 TTS 服务失败: {e}")
            return False

    async def health_check(self) -> bool:
        """检查 TTS 服务是否在线"""
        try:
            resp = await self.http.get(f"{TTS_SERVER_URL}/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    # ---- 核心 TTS ----

    async def synthesize(self, text: str, speaker_id: str = "default",
                         speed: float = 1.0, emotion: str = "") -> Optional[tuple[bytes, int]]:
        """合成语音，返回 (wav_bytes, sample_rate) 或 None"""
        if not text or len(text) < 2:
            return None
        try:
            resp = await self.http.post(
                f"{TTS_SERVER_URL}/tts",
                json={"text": text, "speaker": speaker_id, "speed": speed},
            )
            if resp.status_code == 200:
                wav = resp.content
                return (wav, 24000) if wav else None
            logger.warning(f"TTS 服务返回 {resp.status_code}: {resp.text[:100]}")
            return None
        except Exception as e:
            logger.warning(f"TTS 调用失败: {e}")
            return None

    # ---- 音色注册 ----

    async def register_speaker(self, name: str, ref_audio: str, ref_text: str = "") -> bool:
        """注册新音色。ref_audio 是参考音频路径（3-10 秒 WAV）"""
        try:
            resp = await self.http.post(
                f"{TTS_SERVER_URL}/register",
                json={"speaker": name, "ref_wav": ref_audio, "ref_text": ref_text},
            )
            ok = resp.status_code == 200
            if ok:
                logger.info(f"✅ 注册音色: {name}")
            return ok
        except Exception as e:
            logger.error(f"注册音色失败: {e}")
            return False

    async def close(self):
        """关闭 HTTP 客户端，释放连接资源"""
        if self._http:
            await self._http.aclose()
            self._http = None


# 单例
_engine: Optional[CosyVoiceEngine] = None


def get_cosy_engine() -> CosyVoiceEngine:
    global _engine
    if _engine is None:
        _engine = CosyVoiceEngine()
    return _engine


async def close_cosy_engine():
    """关闭 CosyVoice 引擎单例，释放资源"""
    global _engine
    if _engine:
        await _engine.close()
        _engine = None
