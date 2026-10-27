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
EMOJI_PROMPT = ("这是一张QQ聊天表情包：**先说这类图通常用来表达什么**（情绪/态度/梗），"
                "再补画面元素，50字以内。**不要断言发送者本人此刻的心情**——"
                "表情包常被夸张戏谑使用，用「这类图常用来…」的说法。")


_DEFAULT_TAG_PROMPT = (
    "用空格分隔的情绪词描述这张图，如：开心 撒娇。最多3个词，只输出词。"
)


#: 本模块自己要读的 config.yaml（模块级常量，测试可以直接 monkeypatch）
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _resolve_env_values(mapping: dict) -> dict:
    """把 `${VAR}` 形态的值换成实际值（认不出就原样留着）。

    单独一个函数是因为它要跑**两次**：一次解析原配置，一次解析档位引用带进来的
    新值（2026-10-28 起 `llm.vision.model_ref` 会覆盖 base_url/model/key）。
    只写一次的话，档位里的 `${QWEN_KEY}` 会被当字面量发出去 → 401。
    """
    env_pattern = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
    resolved = {}
    for key, value in mapping.items():
        if isinstance(value, str):
            match = env_pattern.match(value)
            if match:
                value = os.environ.get(match.group(1), value)
        resolved[key] = value
    return resolved


def _load_vision_cfg() -> dict:
    try:
        import yaml
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            # 主程序已加载 .env；独立标注工具没有时也不应因可选依赖崩溃。
            pass
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        vision = (config.get("llm", {}) or {}).get("vision", {}) or {}
        # VisionRouter 独立读取 config.yaml，不能直接复用 main 的已解析副本；
        # 否则 `${QWEN_KEY}` 会作为字面量发送，稳定得到 401 云端错误。
        resolved = _resolve_env_values(vision)

        # 2026-10-28：**只剩「选一个模型」**（`llm.vision.model_ref`），不再有
        # 「识图方式」这个第二维度开关（主人原话：「这类方式选择完全不用了，
        # 只需要给一个像图库选择模型的那样就行了，总要选一个模型」）。
        #
        # 值的三种写法见 `model_refs.resolve_conn`：空 = 当前聊天档位、
        # `ollama:模型名` = 本机 Ollama、档位 id = 那个档位。
        # 选中了就一律走 api 后端——Ollama 的 `/v1/chat/completions` 也是
        # OpenAI 兼容的（2026-10-28 实测能传 base64 图片并返回描述），
        # 本机 MiniCPM-V 引擎留作兜底。
        ref = str(resolved.get("model_ref") or "").strip()
        legacy_base = str(resolved.get("base_url") or "").strip()
        legacy_model = str(resolved.get("model") or "").strip()
        if not ref and legacy_base and legacy_model:
            # 老配置（「方式」+ 独立端点 + 模型名，没有 model_ref）：原样沿用。
            # 新写法是**推荐**，不是「不迁移就坏掉」——否则一次升级就把所有
            # 老配置的识图打断（教训 #21：有原则的例外要写明为什么）。
            resolved["provider"] = str(resolved.get("provider") or "local").strip().lower() or "local"
            return resolved
        try:
            from .model_refs import ollama_host_of, resolve_conn

            conn = resolve_conn(config, ref, ollama_host=ollama_host_of(config))
        except Exception:
            conn = {}
        if conn.get("error"):
            resolved["model_ref_error"] = conn["error"]
            resolved["provider"] = "local"
        elif str(conn.get("base_url") or "").strip() and str(conn.get("model") or "").strip():
            resolved["base_url"] = conn["base_url"]
            resolved["model"] = conn["model"]
            if conn.get("api_key"):
                resolved["api_key"] = conn["api_key"]
            resolved["provider"] = "api"
            resolved["model_ref_label"] = conn["label"]
            # 覆盖进来的新值同样可能是 `${VAR}` —— 再解析一次
            resolved = _resolve_env_values(resolved)
        else:
            # 一个可用连接都没有（老配置 + 扁平字段也空）：退回本机引擎
            resolved["provider"] = "local"
        return resolved
    except Exception:
        return {}


class VisionRouter:
    """统一识图路由：控制台选的 provider 优先，另一后端兜底"""

    def __init__(self):
        self._cfg = _load_vision_cfg()
        self._local = None
        self._warned_no_key = False   # 「云端缺 Key」只提醒一次，不刷屏

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
                # 原先这里**静默返回空串**。配合 `_load_vision_cfg` 异常时返回 {}、
                # provider 默认 local、enabled 默认 True —— 用户以为识图开着，
                # 实际两个后端都在失败，最后只表现为「糖糖答非所问」，
                # 日志里一条线索都没有。（2026-09-20 Code Review A-10）
                # 只在真要走云端且缺 Key 时说一次，别每次识图都刷。
                if not self._warned_no_key:
                    self._warned_no_key = True
                    logger.warning(
                        "识图方式选的是云端，但 LLM.vision.api_key 是空的——"
                        "云端识图会一直失败。拿 Key：控制台 →「🔑 密钥管理」→「识图 API Key」；"
                        "或者把「识图方式」改回 local（本地 MiniCPM-V，不用 Key）。")
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
