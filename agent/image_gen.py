"""
🎨 AI 画图 — 通义万相 (DashScope) 图片生成

复用千问 VL 的 API Key（同属阿里云 DashScope），无需额外配置。
注册为 LLM 可调用技能，群友说"画一个XX"时自动触发。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from .async_io import run_bounded_blocking

logger = logging.getLogger("糖糖.ImageGen")

# DashScope 图片生成端点
DASHSCOPE_IMAGE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"

# 注册为技能（导入时自动注册）
def _register():
    from .skills import register_skill

    @register_skill(
        "generate_image",
        "根据文字描述生成AI图片。每次调用有API成本——在闲聊中随意生成是对资源的不尊重。但当群友真的想看到一幅画、一个场景、一个想法变成图像时，这是独一无二的视觉礼物。",
        {"prompt": "画图描述（中文或英文），要详细具体，包含风格、色调、画面元素等"},
    )
    async def _generate_image_skill(prompt: str = "") -> str:
        if not prompt:
            return "请告诉糖糖你想画什么喵～比如'画一只戴帽子的猫'"
        engine = get_image_engine()
        result = await engine.generate(prompt)
        return result or "画图失败了喵…可能是服务暂时不可用，稍后再试～"


class ImageGenEngine:
    """通义万相 (DashScope) 图片生成引擎"""

    def __init__(self, api_key: str = "", model: str = "wanx-v1",
                 size: str = "1024*1024"):
        self.api_key = api_key
        self.model = model
        self.size = size
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(180))
        return self._client

    async def generate(self, prompt: str) -> str | None:
        """
        调用通义万相生成图片，返回 CQ 码或错误消息。
        异步任务：提交 → 轮询 → 下载。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",  # 异步模式，避免 HTTP 超时
        }

        body: dict = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {
                "n": 1,
                "size": self.size,
            },
        }

        logger.info(f"🎨 通义万相: model={self.model} prompt={prompt[:80]}...")

        try:
            # Step 1: 提交任务
            resp = await self.client.post(
                DASHSCOPE_IMAGE_URL,
                headers=headers,
                json=body,
            )

            if resp.status_code != 200:
                logger.warning(f"🎨 画图API返回 {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            task_id = data.get("output", {}).get("task_id", "")
            task_status = data.get("output", {}).get("task_status", "")

            if not task_id:
                logger.warning(f"🎨 画图API未返回 task_id: {data}")
                return None

            # Step 2: 轮询等待完成
            if task_status != "SUCCEEDED":
                poll_task_id = data.get("output", {}).get("task_id", "")
                for _ in range(60):  # 最多等 2 分钟
                    await __import__('asyncio').sleep(2)
                    r = await self.client.get(
                        f"https://dashscope.aliyuncs.com/api/v1/tasks/{poll_task_id}",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    if r.status_code == 200:
                        rd = r.json()
                        ts = rd.get("output", {}).get("task_status", "")
                        if ts == "SUCCEEDED":
                            data = rd
                            break
                        elif ts == "FAILED":
                            logger.warning(f"🎨 画图任务失败: {rd.get('output', {}).get('message', '')}")
                            return None
                    else:
                        break

            # Step 3: 提取图片 URL
            results = data.get("output", {}).get("results", [])
            if not results:
                logger.warning("🎨 画图API返回空结果")
                return None

            image_url = results[0].get("url", "")
            if not image_url:
                return None

            # Step 4: 下载图片
            img_resp = await self.client.get(image_url)
            if img_resp.status_code != 200:
                logger.warning(f"🎨 下载图片失败: {img_resp.status_code}")
                return None
            img_bytes = img_resp.content

            # 保存
            out_dir = Path("generated_images")
            await run_bounded_blocking(
                "image_gen.output_dir_create",
                out_dir.mkdir,
                exist_ok=True,
                logger=logger,
                log_prefix="🎨 画图输出目录创建较慢",
            )
            out_path = out_dir / f"gen_{int(time.time())}.png"
            await run_bounded_blocking(
                "image_gen.output_write",
                out_path.write_bytes,
                img_bytes,
                logger=logger,
                log_prefix="🎨 画图结果文件写入较慢",
            )

            logger.info(f"🎨 图片已生成: {out_path} ({len(img_bytes) / 1024:.0f}KB)")
            return f"[CQ:image,file=file:///{out_path.absolute().as_posix()}]"

        except Exception as e:
            logger.exception(f"🎨 画图失败: {e}")
            return None


_engine: ImageGenEngine | None = None


def get_image_engine(api_key: str = "", model: str = "wanx-v1",
                     size: str = "1024*1024") -> ImageGenEngine:
    """获取 ImageGenEngine 单例"""
    global _engine
    if _engine is None:
        _engine = ImageGenEngine(api_key=api_key, model=model, size=size)
    return _engine
