# 知识库持久化索引阶段计划

- [x] 任务：建立固定问句集与只读评估入口
  - 验收：正例命中率、负例空召回率和来源统计可重复输出。
  - 验证：`python tools/knowledge_retrieval_eval.py --semantic`
  - 文件：`tools/knowledge_retrieval_eval.py`、`tools/knowledge_retrieval_baseline.json`

- [x] 任务：实现 SQLite 文档/chunk 元数据索引
  - 验收：schema 版本、文档 hash、稳定 ID、字符偏移、增删改同步和幂等写入均有回归。
  - 验证：`python -m pytest tests/test_knowledge_index.py -q`
  - 文件：`agent/knowledge_index.py`、`tests/test_knowledge_index.py`

- [x] 任务：将 KnowledgeBase 的切块映射接入索引（保持旧检索契约）
  - 验收：启动/重载失败可降级，现有 search 结果和固定基线不回归。
  - 验证：`python -m pytest tests/test_knowledge.py tests/test_knowledge_hybrid.py -q`
  - 文件：`agent/knowledge.py`、相关回归测试

- [x] 任务：按模型 fingerprint 持久化并复用 embedding BLOB
  - 验收：同一指纹跨重启复用；不同指纹清除旧内存向量；维度错误安全回退编码。
  - 验证：`python -m pytest tests/test_knowledge_index.py tests/test_knowledge.py -q`
  - 文件：`agent/knowledge_index.py`、`agent/knowledge.py`、`agent/embeddings.py`

- [x] 任务：建立 FTS5 CJK bigram 词法索引探针
  - 验收：中文 bigram、英文词、增删改同步和恶意查询转义均有回归；FTS5 不可用时主索引仍可用。
  - 验证：`python -m pytest tests/test_knowledge_index.py -q`
  - 文件：`agent/knowledge_index.py`、`tests/test_knowledge_index.py`

- [x] 任务：启动时校验 FTS 派生表完整性并自愈
  - 验收：FTS 词项损坏但行数未变时，重启能从主 `chunks` 表重建；不改变主表 schema、排序或降级链。
  - 验证：临时 SQLite 红测先得到 0 命中，修复后恢复 1 命中；全量 **1722 passed**。
  - 文件：`agent/knowledge_index.py`、`tests/test_knowledge_index.py`

- [x] 任务：批量持久化新生成的知识 embedding
  - 验收：单事务写入保持逐块 API 兼容、批量失败逐块回退、内存检索不受 SQLite 写失败影响。
  - 验证：400 块临时基准 p50 **3753.34ms→18.27ms**；全量 **1724 passed**，undefined=0，compileall=0。
  - 文件：`agent/knowledge_index.py`、`agent/knowledge.py`、相关回归测试

- [x] 任务：结构化 FTS/Dense/RRF 证据旁路并接入 search_knowledge
  - 验收：返回稳定来源/偏移/hash/各路分数；异常回退旧路径；检索不阻塞事件循环。
  - 验证：`python -m pytest tests/test_knowledge_evidence.py tests/test_knowledge.py -q`
  - 文件：`agent/knowledge.py`、`agent/handler.py`、相关回归测试

- [x] 任务：固定集排名质量指标
  - 验收：报告保留 ranked sources/match types/expected rank，并计算 hit@1、hit@3、MRR；旧接口
    没有排名时返回 null，不伪造 0 分。
  - 验证：`python tools/knowledge_retrieval_eval.py --semantic`、
    `python -m pytest tests/test_knowledge_retrieval_eval.py -q`
  - 结果：真实固定集 10/10，hit@1=1.0、hit@3=1.0、MRR=1.0。
  - 文件：`tools/knowledge_retrieval_eval.py`、`tests/test_knowledge_retrieval_eval.py`

- [ ] 任务：真实重启 canary 与删除/修改文档演练
  - 验收：日志有同步统计，重启后 ID 可复用，失败可关闭索引并回退旧路径。
  - 验证：手动重启 + `tools/knowledge_retrieval_eval.py --semantic` + 日志取证
  - 文件：`docs/开发规划/ROADMAP.md`

- [x] 任务：Reranker 真实加载安全探针
  - 验收：原生崩溃/页面文件不足有独立证据；不稳定运行时在加载权重前安全降级；失败原因可由
    `last_load_error` 观测，主检索不被阻断。
  - 结果：2.27GB 模型在当前 Windows + `torch 2.14.0.dev20260727+cu130` 环境下标准加载报
    `os error 1455`，低峰值路径触发 `torch_cpu.dll/0xc0000005`；现已 fail-closed 跳过开发版，
    稳定版仍保留兼容加载路径。
  - 验证：`python -m pytest tests/test_reranker.py -q`、独立 `RerankerEngine.load()` 探针
  - 文件：`agent/reranker.py`、`tests/test_reranker.py`
