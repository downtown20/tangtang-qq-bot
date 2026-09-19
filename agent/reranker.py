"""
Reranker 引擎 — Cross-Encoder 精排，提升记忆检索精度

两阶段检索：
1. BGE 向量粗排 → top-30（在 memory.recall() Phase 1 中完成）
2. Cross-Encoder 精排 → top-6（本模块）

BGE-Reranker (BAAI/bge-reranker-v2-m3)：
- 支持中文，~0.5B 参数
- CPU 推理 ~0.8s/8 对、~5s/30 对（本机实测，knowledge.py:283 校准记录）；GPU ~50ms
- 精度比纯余弦相似度提升 15-30%

按需加载——如果模型未下载，回退到纯余弦排序。
"""

from __future__ import annotations

import logging
import numpy as np
from typing import Optional

logger = logging.getLogger("糖糖.Reranker")

# ModelScope 下载路径
RERANKER_PATHS = [
    "models/BAAI/bge-reranker-v2-m3",
    "models/bge-reranker-v2-m3",
]


def _reranker_runtime_check() -> tuple[bool, str]:
    """检查当前 PyTorch 运行时是否适合加载可选的重排模型。

    Reranker 是增强能力，不应因为实验性运行时的原生崩溃拖垮主进程。
    ``torch`` 的开发版没有经过本项目的模型加载验收，先安全降级到
    BGE/关键词检索；稳定版仍走正常加载与异常回滚路径。
    """
    try:
        import torch
    except Exception as exc:
        return False, f"torch 不可用: {exc}"

    version = str(getattr(torch, "__version__", "unknown"))
    if ".dev" in version.casefold():
        return False, f"不支持的 PyTorch 开发版运行时: {version}"
    return True, ""


class RerankerEngine:
    """Cross-Encoder 精排引擎"""

    def __init__(self):
        self._model = None
        self._tokenizer = None
        self._ready = False
        self._device = "cpu"
        self._last_load_error = ""

    def load(self):
        """加载 Reranker 模型——按需调用，不阻塞启动。幂等：重复调用不重复加载。"""
        if self._ready:
            return
        import os
        local = None
        for d in RERANKER_PATHS:
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "config.json")):
                local = d
                break

        if not local:
            logger.info(
                "📥 Reranker 模型未下载。运行以下命令下载：\n"
                "   python -c \"from modelscope import snapshot_download; "
                "snapshot_download('BAAI/bge-reranker-v2-m3', cache_dir='./models/')\""
            )
            return

        runtime_ok, runtime_reason = _reranker_runtime_check()
        if not runtime_ok:
            self._last_load_error = runtime_reason
            logger.warning("⚠ Reranker 跳过加载（安全降级）: %s", runtime_reason)
            return

        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            logger.info(f"📥 加载 Reranker 模型: {local}")
            # 2026-08-15 23:55 事故：并发 from_pretrained 竞态出 meta 空权重——
            # 所有模型加载持 MODEL_LOAD_LOCK 串行（见 agent/model_lock.py）
            from .model_lock import MODEL_LOAD_LOCK
            with MODEL_LOAD_LOCK:
                self._tokenizer = AutoTokenizer.from_pretrained(local)
                try:
                    # safetensors + accelerate 的低峰值加载避免先构造完整 state_dict；
                    # Windows 页面文件不足时，这比默认路径更不容易触发 os error 1455。
                    self._model = AutoModelForSequenceClassification.from_pretrained(
                        local, low_cpu_mem_usage=True,
                    )
                except (TypeError, ImportError) as e:
                    # 旧 transformers/未安装 accelerate 时保留兼容路径；其他异常必须
                    # 继续向外抛出，不能把真实模型损坏误判为参数不兼容。
                    message = str(e).casefold()
                    if isinstance(e, TypeError) and "low_cpu_mem_usage" not in message:
                        raise
                    if isinstance(e, ImportError) and "accelerate" not in message:
                        raise
                    logger.warning("⚠ 低峰值加载不可用，回退标准 Reranker 加载: %s", e)
                    self._model = AutoModelForSequenceClassification.from_pretrained(local)
            self._model.eval()
            # 2026-08-15 整体审查 Critical：.to() 是非原子搬运——中途 CUDA OOM 时
            # 权重一半在 GPU 一半在 CPU，此时 _ready=True 就是「在说谎」：
            # 之后每次推理必抛 device 错、静默返回零分原序，终生不报错。
            # 修复：失败必须回滚（搬回 CPU / 丢弃模型），_ready 只在设备落定后置位。
            try:
                import torch
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
                self._model.to(self._device)
            except Exception as e:
                logger.warning(f"⚠ Reranker 上 {self._device} 失败（{e}）——回滚 CPU")
                try:
                    self._model.to("cpu")
                    self._device = "cpu"
                except Exception as e2:
                    logger.warning(f"⚠ 回滚失败，丢弃模型: {e2}")
                    self._model = None
                    self._tokenizer = None
            if self._model is not None:
                self._ready = True
                self._last_load_error = ""
                logger.info(f"✅ Reranker 就绪（{self._device}）")
        except ImportError:
            self._model = None
            self._tokenizer = None
            self._ready = False
            self._last_load_error = "transformers 未安装"
            logger.warning("⚠ transformers 未安装，Reranker 不可用")
        except Exception as e:
            # 失败后清理半加载对象，避免后续重试继续持有数 GB 权重；ready 永远
            # 只表示设备已落定且模型完整可用。
            self._model = None
            self._tokenizer = None
            self._ready = False
            self._last_load_error = str(e)
            logger.warning(f"⚠ Reranker 加载失败: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def last_load_error(self) -> str:
        """最近一次加载失败原因，供健康检查/诊断使用。"""
        return self._last_load_error

    def rerank(
        self, query: str, candidates: list[str], top_k: int = 6
    ) -> list[tuple[int, float]]:
        """对候选列表精排，返回 [(原始索引, 分数), ...]，按分数降序。

        Args:
            query: 查询文本（当前消息）
            candidates: 候选记忆文本列表
            top_k: 返回前 K 个

        Returns:
            [(index, score), ...] — index 是 candidates 中的位置
        """
        if not self._ready or not candidates:
            return [(i, 0.0) for i in range(min(top_k, len(candidates)))]

        if len(candidates) == 1:
            return [(0, 1.0)]

        try:
            import torch

            # 构建 (query, candidate) 对
            pairs = [[query, c[:200]] for c in candidates]  # 截断长文本

            # Tokenize + 批量推理
            with torch.no_grad():
                inputs = self._tokenizer(
                    pairs, padding=True, truncation=True,
                    max_length=512, return_tensors="pt"
                )
                if self._device == "cuda":
                    inputs = {k: v.to(self._device) for k, v in inputs.items()}
                scores = self._model(**inputs, return_dict=True).logits.view(-1)

            # 转为 (index, score) 并按分数降序
            if isinstance(scores, torch.Tensor):
                scores = scores.cpu().numpy()

            indexed = [(i, float(s)) for i, s in enumerate(scores)]
            indexed.sort(key=lambda x: x[1], reverse=True)

            self._fail_count = 0
            return indexed[:top_k]

        except Exception as e:
            # 2026-08-15 整体审查 Critical 补刀：连续失败 3 次 → 自动摘掉 _ready——
            # 不再静默返回零分原序（「ready 在说谎」态），调用方走降级链
            self._fail_count = getattr(self, "_fail_count", 0) + 1
            logger.warning(f"⚠ Reranker 推理失败({self._fail_count}/3): {e}")
            if self._fail_count >= 3:
                self._ready = False
                logger.warning("⚠ Reranker 连续失败 3 次——自动降级为纯余弦排序")
            return [(i, 0.0) for i in range(min(top_k, len(candidates)))]

    def pick_top(
        self, query: str, candidates: list, top_k: int = 6,
        value_getter=None
    ) -> list:
        """精排并返回 top-K 候选对象。

        Args:
            query: 查询文本
            candidates: 候选对象列表（MemoryEntry 或其他）
            top_k: 返回前 K 个
            value_getter: 从候选对象提取文本的函数，默认取 .value 属性

        Returns:
            精排后的 top-K 候选对象列表
        """
        if not candidates:
            return []

        # 提取文本
        if value_getter is None:
            texts = [
                c.value if hasattr(c, 'value') else str(c)
                for c in candidates
            ]
        else:
            texts = [value_getter(c) for c in candidates]

        # 精排
        ranked = self.rerank(query, texts, top_k=top_k)

        # 取 top-K 原始对象
        return [candidates[i] for i, _ in ranked if i < len(candidates)]

