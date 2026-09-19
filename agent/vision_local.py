"""
MiniCPM-V 本地识图引擎 🖼
通过 Ollama API 调用，零成本、零延迟、零审查
"""
from __future__ import annotations
import logging, base64
from typing import Optional

import httpx

logger = logging.getLogger("糖糖.Vision")

OLLAMA_URL = "http://127.0.0.1:11434"
# Ollama 的默认上下文窗口会按模型上限预留 KV cache，RTX 5070 与 BGE/
# GPT-SoVITS 同机时容易直接 cudaMalloc OOM。识图描述限制在 50 字以内，
# 4k 上下文足够且已用同机实测验证可用。
MODEL = "minicpm-v:latest"
NUM_CTX = 4096


class MiniCPMVision:
    """MiniCPM-V via Ollama"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = httpx.Client(timeout=90.0)
        return self._client

    @property
    def available(self) -> bool:
        try:
            r = self.client.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                model_base = MODEL.split(":", 1)[0]
                return any(
                    name == MODEL or name.split(":", 1)[0] == model_base
                    for name in models
                )
        except Exception:
            pass
        return False

    def describe(self, image_bytes: bytes, prompt: str = "") -> str:
        """识图：输入图片 bytes，返回中文描述"""
        img_b64 = base64.b64encode(image_bytes).decode()
        if not prompt:
            prompt = "请用中文描述这张图片：主体、风格、颜色、感觉。50字以内。"

        try:
            r = self.client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "images": [img_b64],
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "num_predict": 160,
                        "num_ctx": NUM_CTX,
                    },
                },
                timeout=90.0,
            )
            if r.status_code == 200:
                desc = r.json().get("response", "").strip()
                return desc
            logger.warning("Ollama 识图 HTTP %s: %s", r.status_code, r.text[:240])
        except Exception as e:
            logger.warning(f"Ollama 识图失败: {e}")
        return ""

    def describe_gif(self, filepath: str, prompt: str = "") -> str:
        """识动图：提取 4 帧合并为多图请求，一次 Ollama 调用感知情绪变化。
        filepath: GIF 文件路径。返回中文情绪描述。"""
        import io
        from PIL import Image as PILImage

        try:
            gif = PILImage.open(filepath)
            n = getattr(gif, 'n_frames', 1)

            # 均匀取 4 帧
            frames_b64 = []
            for idx in [0, max(1, n // 3), max(1, 2 * n // 3), max(1, n - 1)]:
                gif.seek(min(idx, n - 1))
                frame = gif.copy().convert('RGB')
                buf = io.BytesIO()
                frame.save(buf, format='JPEG', quality=75)
                frames_b64.append(base64.b64encode(buf.getvalue()).decode())

            if not prompt:
                prompt = (
                    "这是一张动图的4帧画面（按时间顺序）。"
                    "描述情绪变化过程，然后用逗号分隔的情绪词概括。50字以内。"
                )

            r = self.client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "images": frames_b64,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "num_predict": 120,
                        "num_ctx": NUM_CTX,
                    },
                },
                timeout=90.0,
            )
            if r.status_code == 200:
                return r.json().get("response", "").strip()
            logger.warning("Ollama 识动图 HTTP %s: %s", r.status_code, r.text[:240])
        except Exception as e:
            logger.warning(f"Ollama 识动图失败 ({filepath}): {e}")
        return ""


_engine: Optional[MiniCPMVision] = None


def get_vision_engine() -> MiniCPMVision:
    global _engine
    if _engine is None:
        _engine = MiniCPMVision()
    return _engine
