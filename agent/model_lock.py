"""
模型加载互斥锁（2026-08-15 23:55 事故）

transformers 的 fast-init（low_cpu_mem_usage 自动启用）使用模块级全局状态——
两个 from_pretrained 并发执行时，其中一个模型的权重停留在 meta（空）状态：
「Cannot copy out of meta tensor; no data!」「Tensor.item() cannot be called on
meta tensors」。已实锤复现：双线程并发加载同一模型，一个正常、一个空权重。

所有 transformers 模型加载（BGE / Reranker / StructBERT 心情）必须持有此锁。
"""
import threading

MODEL_LOAD_LOCK = threading.Lock()
