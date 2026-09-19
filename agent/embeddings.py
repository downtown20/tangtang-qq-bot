"""
语义向量引擎 — BGE 中文语义模型

ModelScope 下载（国内直连），transformers 加载，<50ms 编码。
"""
from __future__ import annotations

import logging
import numpy as np
import os
from typing import Optional

logger = logging.getLogger("糖糖.Embeddings")


class EmbeddingEngine:
    """中文语义向量——BGE-small-zh（512维）"""

    MODEL_FINGERPRINT = "BAAI/bge-small-zh-v1.5"

    # ModelScope 下载路径（可能因系统不同而变化）
    MODEL_PATHS = [
        os.path.expanduser("~/models/BAAI/bge-small-zh-v1___5"),
        "./models/BAAI/bge-small-zh-v1.5",
        "./models/BAAI/bge-small-zh-v1___5",
    ]

    def __init__(self):
        self._model = None
        self._tokenizer = None
        self._ready = False
        self._dimension = 0

    def load(self):
        try:
            from transformers import BertModel, BertTokenizer
        except ImportError:
            logger.warning("⚠ transformers 未安装，语义搜索不可用")
            return

        local = None
        for d in self.MODEL_PATHS:
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "config.json")):
                local = d
                break

        if not local:
            logger.warning("⚠ 未找到 BGE 模型。请运行: python -c \"from modelscope import snapshot_download; snapshot_download('BAAI/bge-small-zh-v1.5', cache_dir='./models/')\"")
            return

        try:
            logger.info(f"📥 加载 BGE 模型: {local}")
            # 2026-08-15 23:55 事故：并发 from_pretrained 竞态出 meta 空权重——
            # 所有模型加载持 MODEL_LOAD_LOCK 串行（见 agent/model_lock.py）
            from .model_lock import MODEL_LOAD_LOCK
            with MODEL_LOAD_LOCK:
                self._tokenizer = BertTokenizer.from_pretrained(local)
                self._model = BertModel.from_pretrained(local)
            # 编码用于持久化/比较，必须关闭 dropout；否则同一文本在不同
            # 查询中会得到不同向量，语义召回门槛和缓存都失去确定性。
            self._model.eval()
            self._dimension = int(
                getattr(getattr(self._model, "config", None), "hidden_size", 0) or 0
            )
            self._ready = True
            logger.info("✅ BGE 语义向量就绪（%d维）", self._dimension or 512)
        except Exception as e:
            logger.warning(f"⚠ BGE 加载失败: {e}")

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def fingerprint(self) -> str:
        """用于持久化向量隔离的模型版本标识。"""
        return self.MODEL_FINGERPRINT

    @property
    def dimension(self) -> int:
        """模型输出维度；加载失败时为 0。"""
        return self._dimension

    def encode(self, text: str) -> Optional[np.ndarray]:
        if not self._ready or not text:
            return None
        try:
            import torch
            encoded = self._tokenizer(text, padding=True, truncation=True,
                                      return_tensors='pt', max_length=512)
            with torch.no_grad():
                outputs = self._model(**encoded)
                vec = outputs.last_hidden_state[:, 0, :].squeeze().numpy()
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            return vec.astype(np.float32)
        except Exception:
            return None

    def encode_batch(self, texts: list[str]) -> list[np.ndarray]:
        """批量编码——一次前向传播。2026-08-15 之前是逐条循环 encode（假批量）。
        2026-08-15 整体审查性能 M3：按 32 条分批——知识库上千块时单批 CPU
        实测 ~28s 且瞬时内存线性涨；本机实测 141 块整批 2.8s。批量失败逐条兜底。"""
        if not self._ready:
            return []
        if not texts:
            return []
        try:
            import torch
            out = []
            for i in range(0, len(texts), 32):
                batch = list(texts[i: i + 32])
                encoded = self._tokenizer(
                    batch, padding=True, truncation=True,
                    return_tensors="pt", max_length=512)
                with torch.no_grad():
                    outputs = self._model(**encoded)
                vecs = outputs.last_hidden_state[:, 0, :].numpy()
                norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                out.extend((vecs / norms).astype(np.float32))
            return out
        except Exception:
            # 逐条兜底——结果数与 texts 不一致时由调用方逐个处理
            return [v for t in texts if (v := self.encode(t)) is not None]

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return 0.0
        return float(np.dot(a, b))
