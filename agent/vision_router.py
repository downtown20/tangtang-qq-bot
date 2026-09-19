"""
统一识图路由 👁

控制台「识图方式」(llm.vision.provider) 选择的模型优先，另一后端自动兜底：
    provider = local      → 本地 MiniCPM-V (Ollama) 优先，失败切云端千问 VL
    provider = api / qwen → 云端千问 VL 优先，失败切本地

设计：
    - 单点入口 describe()/describe_gif()——贴图标注工具、糖糖运行时识图都用它
    - 主后端不可用（Ollama 没启动 / API Key 缺失 / 调用失败）时静默降级，
      返回空字符串 = 彻底不可用（调用方自行处理）
"""
from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

import httpx

from .vision_local import get_vision_engine

logger = logging.getLogger("糖糖.VisionRouter")

# 2026-08-16 现场：流口水表情包被本地 VLM 认成「呕吐，恶心，想吐」→ LLM 当成
# 用户反应说「你嫌我肉麻」。聊天图片常被夸张/戏谑使用——只描述画面，不断言情绪。
_DEFAULT_PROMPT = ("请用中文描述这张图片的画面内容：主体、动作、风格、颜色。50字以内。"
                   "只描述画面本身，不要推断图中人或发送者的情绪、态度或意图。")
# QQ 系统表情包专用：比默认更严——表情包语义几乎总是夸张/戏谑的
EMOJI_PROMPT = ("这是一张QQ聊天表情包：只描述画面元素（主体/动作/风格），50字以内。"
                "不要断言发送者的情绪、态度或意图——表情包常被夸张戏谑使用。")
_DEFAULT_TAG_PROMPT = (
    "用空格分隔的情绪词描述这张图，如：开心 撒娇。最多3个词，只输出词。"
)


def _load_vision_cfg() -> dict:
    try:
        import yaml
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            # 主程序已加载 .env；独立标注工具没有时也不应因可选依赖崩溃。
            pass
        cfg = yaml.safe_load(
            Path(__file__).resolve().parent.parent.joinpath("config.yaml")
            .read_text(encoding="utf-8"))
        vision = (cfg.get("llm", {}) or {}).get("vision", {}) or {}
        # VisionRouter 独立读取 config.yaml，不能直接复用 main 的已解析副本；
        # 否则 `${QWEN_KEY}` 会作为字面量发送，稳定得到 401 云端错误。
        env_pattern = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
        resolved = {}
        for key, value in vision.items():
            if isinstance(value, str):
                match = env_pattern.match(value)
                if match:
                    value = os.environ.get(match.group(1), value)
            resolved[key] = value
        return resolved
    except Exception:
        return {}


class VisionRouter:
    """统一识图路由：控制台选的 provider 优先，另一后端兜底"""

    def __init__(self):
        self._cfg = _load_vision_cfg()
        self._local = None

    @property
    def provider(self) -> str:
        return str(self._cfg.get("provider", "local")).lower()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    @property
    def _local_engine(self):
        if self._local is None:
            self._local = get_vision_engine()
        return self._local

    # ── 两个后端 ──

    def _describe_local(self, image_bytes: bytes, prompt: str) -> str:
        try:
            if not self._local_engine.available:
                return ""
            return self._local_engine.describe(image_bytes, prompt) or ""
        except Exception as e:
            logger.warning(f"本地识图失败: {e}")
            return ""

    def _describe_api(self, image_bytes: bytes, prompt: str) -> str:
        try:
            key = str(self._cfg.get("api_key", "") or "")
            url = str(self._cfg.get("base_url", "") or "")
            model = str(self._cfg.get("model", "qwen-vl-plus") or "qwen-vl-plus")
            if not key or not url:
                return ""
            img_b64 = base64.b64encode(image_bytes).decode()
            body = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 300,
            }
            with httpx.Client(timeout=60.0) as c:
                r = c.post(
                    f"{url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=body)
                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"].strip()
                logger.warning(f"云端识图 HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"云端识图失败: {e}")
        return ""

    # ── 统一入口 ──

    def describe(self, image_bytes: bytes, prompt: str = "") -> str:
        """静态图：主后端优先，失败自动切兜底后端"""
        if not self.enabled:
            return ""
        prompt = prompt or _DEFAULT_PROMPT
        if self.provider in ("api", "qwen"):
            text = self._describe_api(image_bytes, prompt)
            if text:
                return text
            return self._describe_local(image_bytes, prompt)
        # local / 默认
        text = self._describe_local(image_bytes, prompt)
        if text:
            return text
        return self._describe_api(image_bytes, prompt)

    def describe_gif(self, filepath: Path, prompt: str = "") -> str:
        """动图：本地 4 帧感知优先（API 对动图支持不稳，作为兜底）"""
        if not self.enabled:
            return ""
        prompt = prompt or _DEFAULT_PROMPT
        if self.provider in ("api", "qwen"):
            try:
                text = self._describe_api(filepath.read_bytes(), prompt)
                if text:
                    return text
            except Exception:
                pass
            return self._local_engine.describe_gif(str(filepath), prompt)
        text = self._local_engine.describe_gif(str(filepath), prompt)
        if text:
            return text
        try:
            return self._describe_api(filepath.read_bytes(), prompt)
        except Exception:
            return ""


_router: VisionRouter | None = None


def get_vision_router() -> VisionRouter:
    """全局单例"""
    global _router
    if _router is None:
        _router = VisionRouter()
    return _router
