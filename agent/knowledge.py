"""
小糖糖的知识库 📚
从 knowledge/ 文件夹加载 .md 文件，按关键词匹配注入到 LLM 上下文

v2: 自动分块 + 精准召回
  - 短文件（<2000字）整体作为一个块，享受高上限
  - 长文件按 ## 标题 / 空行自动切片，每块带标签
  - 搜索时按块匹配，只召回相关的块，不浪费上下文
  - 输出总量控制在 3000 字以内
"""

from __future__ import annotations

import logging
import json
import hashlib
import math
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from .knowledge_index import IndexChunk, IndexDocument, KnowledgeIndex, document_id_for

logger = logging.getLogger("糖糖.Knowledge")

# 停用词
STOP_WORDS = {
    # 虚词/连接词
    "什么", "怎么", "为什么", "这个", "那个", "就是", "可以", "觉得",
    "一个", "一下", "真的", "还是", "不过", "但是", "然后", "如果",
    "应该", "可能", "已经", "没有", "不是", "知道", "因为", "所以",
    "的话", "吧", "吗", "呢", "啊", "哦", "嗯",
    # 招呼/日常高频词——容易跨文档命中产生噪音
    "你好", "好呀", "哈喽", "嗨嗨", "早上好", "晚安", "再见",
    "哈哈", "嘿嘿", "嘻嘻", "呵呵", "嗯嗯", "哦哦",
    "在吗", "在了", "来了", "走了", "睡了", "起了",
    "今天", "昨天", "明天", "今天天气", "现在", "刚才", "等下",
    "谢谢", "对不起", "没关系", "没事", "不用谢",
    # 呼唤/昵称高频片段——不应该作为知识库搜索关键词
    "糖糖", "小糖", "糖你",  # "糖你"来自"糖糖你好呀"的2-gram碎片
    # 泛化/闲聊词——单独命中标题或向量不构成知识证据
    "完全", "存在", "随机", "问题", "功能", "告诉", "介绍",
    "普通", "随便", "聊聊天", "怎么样", "干什么", "系统", "流程",
}

# 输出限制
MAX_TOTAL_CHARS = 3000       # 一次注入 LLM 的总字符数上限
MAX_PER_CHUNK = 1500          # 单个块的字符上限
MAX_RESULTS = 3               # 最多返回几个块
SEMANTIC_EVIDENCE_THRESHOLD = 0.58
SEMANTIC_SEGMENT_CHARS = 450  # 与 BGE 512 token 窗口匹配的语义子段上限

# 普通知识工具的安全边界。敏感资料由显式授权场景的专用加载器读取，
# 不进入全局 search_knowledge 索引，避免群聊/普通私聊绕过场景授权。
SENSITIVE_DIRECTORY_NAMES = frozenset({"色色参考", "adult", "nsfw"})


def discover_document_files(knowledge_dir: str | Path) -> list[Path]:
    """知识库的唯一文档发现器：递归扫描、只允许 md/txt、统一过滤敏感目录。
    search/read/extract 应共用这个边界，不得各自 glob 决定可读面。
    """
    root = Path(knowledge_dir)
    if not root.exists():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if path.suffix.casefold() not in {".md", ".txt"}:
            continue
        relative_parts = {
            part.casefold() for part in path.relative_to(root).parts[:-1]
        }
        if relative_parts & SENSITIVE_DIRECTORY_NAMES:
            continue
        files.append(path)
    return files


def resolve_document_path(knowledge_dir: str | Path, document: str) -> Path | None:
    """按文件名解析文档，不超出过滤后的文档清单。"""
    query = str(document or "").strip()
    if not query:
        return None
    files = discover_document_files(knowledge_dir)
    query_fold = query.casefold()
    for path in files:
        if path.stem.casefold() == query_fold or path.name.casefold() == query_fold:
            return path
    compact_query = query_fold.replace(" ", "").replace("·", "")
    for path in files:
        compact_stem = path.stem.casefold().replace(" ", "").replace("·", "")
        if compact_query in compact_stem:
            return path
    for path in files:
        if all(part in path.stem.casefold() for part in compact_query):
            return path
    return None


@dataclass
class KnowledgeChunk:
    """知识库的一个切片"""
    source: str                 # 来源文件名（不含.md）
    label: str                  # 块标签（标题或首行摘要）
    content: str                # 块内容
    char_count: int = 0         # 字符数
    document_id: str = ""       # 跨重启稳定的文档 ID
    chunk_id: str = ""          # 内容变化即变化的稳定块 ID
    content_sha256: str = ""    # 块内容 hash
    start_char: int = -1        # 在原始文档中的字符起点
    end_char: int = -1          # 在原始文档中的字符终点（开区间）
    relative_path: str = ""     # 来源文档相对路径（区分不同目录的同名文件）

    def __post_init__(self):
        self.char_count = len(self.content)
        if not self.content_sha256:
            self.content_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class KnowledgeSemanticSegment:
    """只供语义召回的父块子段；词法/FTS 仍使用父块。"""

    parent: KnowledgeChunk
    start_char: int
    end_char: int
    text: str
    embedding: object | None = None
    embedding_fingerprint: str = ""

    @property
    def segment_id(self) -> str:
        """从父块 ID、偏移和内容生成跨重启稳定的子段 ID。"""
        parent_id = self.parent.chunk_id or (
            self.parent.relative_path or self.parent.source
        )
        material = (
            f"{parent_id}\0{self.start_char}\0{self.end_char}\0"
            f"{hashlib.sha256(self.text.encode('utf-8')).hexdigest()}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:28]


@dataclass(frozen=True)
class KnowledgeEvidence:
    """一次结构化知识召回的证据，不改变旧的文本搜索接口。"""

    source: str
    relative_path: str
    source_mtime_ns: int | None
    label: str
    content: str
    document_id: str
    chunk_id: str
    start_char: int
    end_char: int
    content_sha256: str
    score: float
    match_type: str
    keyword_score: int = 0
    semantic_score: float | None = None
    fts_rank: float | None = None
    rerank_score: float | None = None

    def to_dict(self) -> dict:
        """转成给日志/LLM 适用的来源结构，保留各路分数而非只留总分。"""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "source": self.source,
            "relative_path": self.relative_path,
            "source_mtime_ns": self.source_mtime_ns,
            "label": self.label,
            "start": self.start_char,
            "end": self.end_char,
            "content_sha256": self.content_sha256,
            "score": self.score,
            "match_type": self.match_type,
            "scores": {
                "rrf": self.score,
                "keyword": self.keyword_score,
                "semantic": self.semantic_score,
                "fts": self.fts_rank,
                "rerank": self.rerank_score,
            },
            "content": self.content,
        }


class _ReadWriteLock:
    """允许只读检索并发，同时让 reload/warm 独占状态。"""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextmanager
    def read(self):
        with self._condition:
            while self._writer or self._writers_waiting:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self):
        with self._condition:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._writers_waiting -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


class KnowledgeBase:
    """知识库引擎 v2 —— 分块 + 精准召回"""

    def __init__(self, knowledge_dir: str = "./knowledge"):
        self.knowledge_dir = Path(knowledge_dir)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.chunks: list[KnowledgeChunk] = []      # 所有切片
        self._semantic_segments: list[KnowledgeSemanticSegment] = []
        self._semantic_segments_fingerprint = ""
        self._semantic_segments_engine_id: int | None = None
        self._file_count = 0
        self._document_registry: dict[str, dict] = {}
        # reload/search/warm may run in separate bounded worker threads; keep
        # the in-memory snapshot and embedding cache from being observed half-way.
        self._state_lock = _ReadWriteLock()
        self._index: KnowledgeIndex | None = None
        self._index_sync_stats = None
        try:
            self._index = KnowledgeIndex(self.knowledge_dir / ".knowledge_index.sqlite3")
        except Exception as e:
            # 索引不是启动硬依赖；保留旧的内存检索并把故障暴露给日志。
            logger.warning(f"⚠ 知识库持久化索引不可用，回退内存路径: {e}")
        self._load()

    # ---- 加载与分块 ----

    def _load(self):
        """加载所有 .md/.txt 文件并切块，构建搜词扩展索引。
        2026-08-15：千恋万花_丛林.txt 因扩展名被 rglob("*.md") 忽略，从未加载——已修。"""
        self.chunks = []
        self._semantic_segments = []
        self._semantic_segments_fingerprint = ""
        self._semantic_segments_engine_id = None
        self._file_count = 0
        self._expansion: dict[str, set[str]] = {}  # 搜词 → 来源文档相对路径集合
        self._document_registry = {}
        index_documents: list[IndexDocument] = []
        index_bindings: list[tuple[list[KnowledgeChunk], tuple[IndexChunk, ...]]] = []
        read_failed = False

        for f in discover_document_files(self.knowledge_dir):
            try:
                content = f.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                self._file_count += 1
                relative = str(f.relative_to(self.knowledge_dir)).replace("\\", "/")
                self._document_registry[relative] = {
                    "path": f,
                    "doc_id": document_id_for(relative),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "mtime_ns": f.stat().st_mtime_ns,
                    "chars": len(content),
                }
                file_chunks = self._split_file(f.stem, content)
                for chunk in file_chunks:
                    chunk.relative_path = relative
                self.chunks.extend(file_chunks)
                index_document, index_chunks = self._make_index_snapshot(
                    relative, content, file_chunks,
                )
                index_documents.append(index_document)
                index_bindings.append((file_chunks, index_chunks))
                # 扩展索引必须绑定稳定相对路径；仅用 stem 会把不同目录的同名文档混在一起。
                self._build_expansion(relative, content)
            except Exception as e:
                read_failed = True
                logger.warning(f"加载知识文件失败 {f.name}: {e}")

        # 先完成一次事务，再把稳定元数据投影回内存 chunk；同步失败不影响旧检索。
        self._index_sync_stats = None
        if self._index is not None and not read_failed:
            try:
                self._index_sync_stats = self._index.sync(index_documents)
                for file_chunks, index_chunks in index_bindings:
                    for chunk, indexed in zip(file_chunks, index_chunks):
                        chunk.document_id = indexed.document_id
                        chunk.chunk_id = indexed.chunk_id
                        chunk.content_sha256 = indexed.content_sha256
                        chunk.start_char = indexed.start_char
                        chunk.end_char = indexed.end_char
                stats = self._index_sync_stats
                logger.info(
                    "📚 知识索引同步: +%d文档 ~%d变更 -%d删除, +%d块 ~%d替换 -%d清理",
                    stats.added_documents, stats.changed_documents,
                    stats.removed_documents, stats.added_chunks,
                    stats.replaced_chunks, stats.removed_chunks,
                )
            except Exception as e:
                logger.warning(f"⚠ 知识索引同步失败，继续使用内存路径: {e}")

        if self.chunks:
            logger.info(
                f"📚 知识库: {self._file_count} 个文件 → {len(self.chunks)} 个块 "
                f"({', '.join(sorted(set(c.source for c in self.chunks)))})"
            )
        else:
            logger.info("📚 知识库为空，把.md放进 knowledge/ 即可")

    def _make_index_snapshot(
        self, relative: str, content: str, chunks: list[KnowledgeChunk],
    ) -> tuple[IndexDocument, tuple[IndexChunk, ...]]:
        """把切块映射为索引快照，计算原文字符偏移。"""
        document_id = self._document_registry[relative]["doc_id"]
        cursor = 0
        indexed_chunks: list[IndexChunk] = []
        for ordinal, chunk in enumerate(chunks):
            start = content.find(chunk.content, cursor)
            if start < 0:
                # 重复正文只在前一块定位失败时从头找；找不到则让整批事务失败，
                # 不写入不可信偏移，也不清理上一次可用索引。
                start = content.find(chunk.content)
            if start < 0:
                raise ValueError(f"无法定位知识块偏移: {relative}#{ordinal}")
            end = start + len(chunk.content)
            indexed_chunks.append(IndexChunk(
                document_id=document_id,
                ordinal=ordinal,
                label=chunk.label,
                content=chunk.content,
                start_char=start,
                end_char=end,
            ))
            cursor = end

        registry = self._document_registry[relative]
        return IndexDocument(
            document_id=document_id,
            relative_path=relative,
            sha256=registry["sha256"],
            mtime_ns=registry["mtime_ns"],
            chars=registry["chars"],
            chunks=tuple(indexed_chunks),
        ), tuple(indexed_chunks)

    def _build_expansion(self, source_key: str, content: str) -> None:
        """解析「> 搜词：」行 → 查询扩展索引（搜词 → 来源文档）。
        2026-08-15：文档作者写下的搜词索引不再闲置——查询命中任何搜词，
        该文档所有块进入候选池，解决「糖糖你会什么」这类停用词死查询。"""
        for m in re.finditer(r'^\s*>\s*搜词[：:]\s*(.+)$', content, re.MULTILINE):
            for term in m.group(1).split():
                if len(term) >= 2:
                    self._expansion.setdefault(term, set()).add(source_key)

    def _split_file(self, name: str, text: str) -> list[KnowledgeChunk]:
        """
        将单个文件切分成块。
        策略：
          1. 按 ## 二级标题切分（最优先）
          2. 如果某段仍超过 MAX_PER_CHUNK，按 ### 三级标题或连续两个换行再切
          3. 每块用标题作为 label，无标题则用首行
        """
        # Step 1: 按 ## 标题分割
        sections = re.split(r'\n(?=## )', text)

        chunks = []
        for section in sections:
            section = section.strip()
            if not section:
                continue

            # 提取标题作为 label
            heading_match = re.match(r'^##\s+(.+)', section)
            if heading_match:
                label = heading_match.group(1).strip()
                body = section[heading_match.end():].strip()
            else:
                # 没有 ##，用首行做 label
                first_line = section.split("\n")[0].strip()
                # 去掉 markdown 标记
                first_line = re.sub(r'^#+\s*', '', first_line)
                label = first_line[:40] if len(first_line) > 40 else first_line
                body = section

            # 如果整个section还行，直接作为一个块
            if len(body) <= MAX_PER_CHUNK:
                if body:
                    chunks.append(KnowledgeChunk(
                        source=name,
                        label=label,
                        content=body,
                    ))
                continue

            # Step 2: 太长的段继续切 —— 按 ### 或空行
            sub_sections = re.split(r'\n(?=### )|\n\n+', body)
            for sub in sub_sections:
                sub = sub.strip()
                if not sub:
                    continue

                sub_heading = re.match(r'^###\s+(.+)', sub)
                if sub_heading:
                    sub_label = f"{label} > {sub_heading.group(1).strip()}"
                    sub_body = sub[sub_heading.end():].strip()
                else:
                    sub_label = label
                    sub_body = sub

                # 还是太长？按句子边界循环切成多个块
                # 2026-08-10 修复：之前只保留第一个 ~1500 字，剩余正文永久丢弃——
                # 与"自动分块"契约不符，长文档后半部分知识永远查不到
                if len(sub_body) > MAX_PER_CHUNK:
                    remaining = sub_body
                    while remaining:
                        piece = self._truncate_at_sentence(remaining, MAX_PER_CHUNK)
                        if not piece:
                            break  # 防死循环
                        chunks.append(KnowledgeChunk(
                            source=name, label=sub_label, content=piece,
                        ))
                        remaining = remaining[len(piece):].strip()
                elif sub_body:
                    chunks.append(KnowledgeChunk(
                        source=name, label=sub_label, content=sub_body,
                    ))

        return chunks

    def _truncate_at_sentence(self, text: str, max_chars: int) -> str:
        """在句子边界处截断文本"""
        if len(text) <= max_chars:
            return text
        snippet = text[:max_chars]
        # 尽量在句号/换行/问号/感叹号处断
        for ch in "。\n！？\n！?！？":
            idx = snippet.rfind(ch)
            if idx > max_chars * 0.6:
                return snippet[:idx + 1]
        return snippet

    def reload(self):
        """重新加载知识库"""
        with self._state_lock.write():
            self._load()

    def warm_embeddings(self, embed_engine) -> None:
        """预热：批量编码所有块并缓存。首次语义查询 ~2s（141 块批量编码，CPU 算力受限）
        挪到启动后台，之后语义查询毫秒级（2026-08-15）。"""
        if not embed_engine or not embed_engine.ready or not self.chunks:
            return
        with self._state_lock.write():
            self._ensure_chunk_embeddings(embed_engine)
            self._ensure_semantic_segments(embed_engine)

    def _prepare_embedding_search(self, embed_engine) -> None:
        """在写锁内完成一次语义检索的懒加载，随后允许只读并发检索。

        旧实现把整个 semantic search 都放在写锁里；预热完成后仍会串行化
        所有读者。把可能生成/持久化向量的懒加载单独收口到短写锁，真正的
        评分、融合和格式化阶段由外层 read lock 保护。
        """
        if embed_engine is None or not getattr(embed_engine, "ready", False):
            return
        with self._state_lock.write():
            if not self.chunks:
                return
            self._ensure_chunk_embeddings(embed_engine)
            self._ensure_semantic_segments(embed_engine)

    # ---- 搜索 ----

    def search(self, message: str, embed_engine=None, reranker=None) -> str:
        """
        混合检索（2026-08-15，对齐市场标准：hybrid + RRF + cross-encoder 重排）：
        reranker 就绪 → 三路召回（关键词/语义/搜词扩展）RRF 融合后重排；
        reranker 不可用 → 顺序降级链（关键词 → 语义 → 搜词扩展），行为与旧版一致。
        返回格式化的知识片段，无匹配返回空字符串。
        """
        self._prepare_embedding_search(embed_engine)
        # embedding 的懒加载已在上面的短写锁完成；没有 reranker 时，后续
        # 纯评分/格式化只读共享状态，可以让多个语义查询重叠执行。reranker
        # 仍保留写锁，避免未知的 cross-encoder 线程安全问题。
        lock = self._state_lock.write() if (
            reranker is not None and getattr(reranker, "ready", False)
        ) else self._state_lock.read()
        with lock:
            if not self.chunks:
                return ""
            if reranker is not None and reranker.ready:
                return self._search_fused(message, embed_engine, reranker)
            return self._search_sequential(message, embed_engine)

    def search_evidence(
        self, message: str, embed_engine=None, reranker=None,
    ) -> list[KnowledgeEvidence]:
        """在线程安全的知识证据检索入口。"""
        self._prepare_embedding_search(embed_engine)
        lock = self._state_lock.write() if (
            reranker is not None and getattr(reranker, "ready", False)
        ) else self._state_lock.read()
        with lock:
            return self._search_evidence_unlocked(
                message, embed_engine=embed_engine, reranker=reranker,
            )

    def _search_evidence_unlocked(
        self, message: str, embed_engine=None, reranker=None,
    ) -> list[KnowledgeEvidence]:
        """并行召回 FTS/关键词/Dense/搜词扩展并返回结构化证据。

        这是新检索契约的旁路入口：旧 ``search()`` 仍返回兼容文本，待真实
        canary 比较两条路径后再决定是否让线上工具切换。所有排序分数保留
        在证据中，避免 LLM 只看到一段没有来源的正文。
        """
        if not self.chunks or not str(message or "").strip():
            return []

        started_at = time.perf_counter()

        def emit_trace(evidence: list[KnowledgeEvidence], ranked_lists: list[tuple[str, list[KnowledgeChunk]]]) -> None:
            """记录脱敏检索摘要；trace 只保留算法字段，不记录查询或来源。"""
            path_summary = ",".join(
                f"{name}:{len(chunks)}" for name, chunks in ranked_lists
            ) or "none"
            source_keys = {
                self._chunk_document_key(chunk)
                for _name, chunks in ranked_lists
                for chunk in chunks
            }
            trace = []
            for hit in evidence:
                score = hit.rerank_score
                trace.append({
                    "match_type": hit.match_type,
                    "rerank_score": round(float(score), 6)
                    if isinstance(score, (int, float))
                    and not isinstance(score, bool)
                    and math.isfinite(float(score)) else None,
                })
            logger.info(
                "📚 Knowledge Retrieval | hits=%d sources=%d paths=%s latency_ms=%.1f trace=%s",
                len(evidence),
                len(source_keys),
                path_summary,
                (time.perf_counter() - started_at) * 1000,
                json.dumps(trace, ensure_ascii=True, separators=(",", ":")),
            )

        keywords = self._extract_keywords(message)
        chunks_by_id = {
            chunk.chunk_id: chunk for chunk in self.chunks if chunk.chunk_id
        }
        relative_by_doc = {
            entry["doc_id"]: path
            for path, entry in self._document_registry.items()
        }
        details: dict[str, dict] = {}
        ranked_lists: list[tuple[str, list[KnowledgeChunk]]] = []

        def key_for(chunk: KnowledgeChunk) -> str:
            # 正常路径使用持久化 chunk_id；索引不可用时用内容键避免进程内重复。
            return chunk.chunk_id or (
                f"{chunk.source}\0{chunk.label}\0{chunk.content_sha256}"
            )

        def ensure_detail(chunk: KnowledgeChunk) -> dict:
            key = key_for(chunk)
            return details.setdefault(key, {
                "chunk": chunk,
                "types": set(),
                "rrf": 0.0,
                "keyword": 0,
                "semantic": None,
                "fts": None,
                "rerank": None,
            })

        # 词法路径：沿用当前精确匹配权重，单正文命中（1 分）不作为独立证据。
        kw_scored: list[tuple[int, KnowledgeChunk]] = []
        if keywords:
            for chunk in self.chunks:
                score = 0
                for keyword in keywords:
                    folded = keyword.casefold()
                    if folded in chunk.label.casefold():
                        score += 3
                    elif folded in chunk.source.casefold():
                        score += 2
                    elif folded in chunk.content.casefold():
                        # 短正文词（如“启动器”）仍需第二个证据；四字以上
                        # 的完整概念通常已具备足够区分度，可单独形成弱证据。
                        score += 2 if len(folded) >= 4 else 1
                if score >= 2:
                    kw_scored.append((score, chunk))
            kw_scored.sort(key=lambda item: (-item[0], key_for(item[1])))
            if kw_scored:
                ranked_lists.append(("keyword", [chunk for _, chunk in kw_scored]))
                for score, chunk in kw_scored:
                    ensure_detail(chunk)["keyword"] = score

        # 持久化 FTS 路径：索引故障/FTS5 缺失时自然退化为其他路径。
        # 使用与关键词路径相同的 jieba 结果，避免把整句汉字串编码成
        # 不存在的跨词 bigram（如“怎么安装”中的“么安”），也不让停用词
        # 把 FTS 查询门槛抬高。无实质关键词时保持诚实空召回。
        fts_query = " ".join(keywords)
        if fts_query and self._index is not None and self._index.fts_available:
            try:
                rows = self._index.search_lexical(
                    fts_query, limit=max(MAX_RESULTS * 4, 8),
                )
                fts_chunks: list[KnowledgeChunk] = []
                for row in rows:
                    chunk = chunks_by_id.get(row["chunk_id"])
                    if chunk is None:
                        continue
                    detail = ensure_detail(chunk)
                    # FTS 是召回路径而不是事实门槛；单个正文词（例如“天气”）
                    # 与旧关键词纪律一样不足以构成独立证据，避免泛问句误命中。
                    if detail["keyword"] < 2:
                        continue
                    fts_chunks.append(chunk)
                    detail["fts"] = float(row["rank"])
                if fts_chunks:
                    ranked_lists.append(("fts", fts_chunks))
            except Exception as e:
                logger.warning(f"⚠ 结构化检索 FTS 失败，继续其他召回路径: {e}")

        # Dense 路径保持现有“有实质关键词 + 语义门槛”的证据纪律。
        if keywords and embed_engine is not None and embed_engine.ready:
            try:
                query_vec = embed_engine.encode(message)
                if query_vec is not None:
                    semantic_scored: list[tuple[float, KnowledgeChunk]] = []
                    for similarity, chunk in self._semantic_scores(query_vec):
                        if similarity > SEMANTIC_EVIDENCE_THRESHOLD:
                            semantic_scored.append((similarity, chunk))
                    semantic_scored.sort(key=lambda item: (-item[0], key_for(item[1])))
                    if semantic_scored:
                        semantic_chunks = [chunk for _, chunk in semantic_scored]
                        ranked_lists.append(("semantic", semantic_chunks))
                        for similarity, chunk in semantic_scored:
                            ensure_detail(chunk)["semantic"] = similarity
            except Exception as e:
                logger.warning(f"⚠ 结构化检索 Dense 失败，继续其他召回路径: {e}")

        expansion_chunks = self._expansion_hits(message)
        if expansion_chunks:
            ranked_lists.append(("expansion", expansion_chunks))
            for chunk in expansion_chunks:
                ensure_detail(chunk)["types"].add("expansion")

        if not ranked_lists:
            emit_trace([], ranked_lists)
            return []

        # RRF 只依赖各路排名，避免跨模型分数直接相加。
        for path_name, ranked_chunks in ranked_lists:
            for rank, chunk in enumerate(ranked_chunks):
                detail = ensure_detail(chunk)
                detail["types"].add(path_name)
                detail["rrf"] += 1.0 / (60 + rank + 1)

        ordered = sorted(
            details.values(),
            key=lambda detail: (-detail["rrf"], key_for(detail["chunk"])),
        )
        if reranker is not None and reranker.ready and len(ordered) > 1:
            rerank_pool = ordered[:8]
            try:
                ranked = reranker.rerank(
                    message,
                    [self._rerank_text(item["chunk"], keywords) for item in rerank_pool],
                    top_k=len(rerank_pool),
                )
                reranked: list[dict] = []
                seen_keys: set[str] = set()
                for index, score in ranked:
                    if 0 <= index < len(rerank_pool):
                        item = rerank_pool[index]
                        item["rerank"] = float(score)
                        reranked.append(item)
                        seen_keys.add(key_for(item["chunk"]))
                ordered = reranked + [
                    item for item in ordered
                    if key_for(item["chunk"]) not in seen_keys
                ]
            except Exception as e:
                logger.warning(f"⚠ 结构化检索重排失败，保留 RRF 顺序: {e}")

        path_order = ("fts", "keyword", "semantic", "expansion")
        evidence: list[KnowledgeEvidence] = []
        for item in ordered[:MAX_RESULTS]:
            chunk = item["chunk"]
            # 索引不可用时仍要按相对路径隔离同名文档；仅使用 stem 会让
            # nested/a/guide.md 与 nested/b/guide.md 共享证据身份。
            fallback_document_key = chunk.relative_path or chunk.source
            document_id = chunk.document_id or hashlib.sha256(
                fallback_document_key.encode("utf-8")
            ).hexdigest()[:16]
            chunk_id = chunk.chunk_id or hashlib.sha256(
                f"{document_id}\0{chunk.content_sha256}".encode("utf-8")
            ).hexdigest()[:24]
            types = [name for name in path_order if name in item["types"]]
            evidence.append(KnowledgeEvidence(
                source=chunk.source,
                relative_path=relative_by_doc.get(
                    document_id, chunk.relative_path or chunk.source,
                ),
                source_mtime_ns=next(
                    (
                        entry.get("mtime_ns")
                        for path, entry in self._document_registry.items()
                        if entry.get("doc_id") == document_id
                    ),
                    None,
                ),
                label=chunk.label,
                content=chunk.content,
                document_id=document_id,
                chunk_id=chunk_id,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                content_sha256=chunk.content_sha256,
                score=float(item["rrf"]),
                match_type="+".join(types),
                keyword_score=int(item["keyword"]),
                semantic_score=item["semantic"],
                fts_rank=item["fts"],
                rerank_score=item["rerank"],
            ))
        emit_trace(evidence, ranked_lists)
        return evidence

    def format_evidence(self, evidence: list[KnowledgeEvidence]) -> str:
        """把结构化证据格式化为兼容文本，并内嵌可核验来源标记。"""
        lines: list[str] = []
        total_chars = 0
        for hit in evidence[:MAX_RESULTS]:
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 100:
                break
            content = hit.content
            if len(content) > remaining:
                content = self._truncate_at_sentence(content, remaining)
            header = f"### {hit.source}"
            if hit.label and hit.label != hit.source:
                header += f" — {hit.label}"
            metadata = (
                f"[证据 source={hit.relative_path} document_id={hit.document_id} "
                f"chunk_id={hit.chunk_id} offset={hit.start_char}:{hit.end_char} "
                f"score={hit.score:.4f} match={hit.match_type} "
                f"hash={hit.content_sha256}"
            )
            if hit.source_mtime_ns is not None:
                metadata += f" mtime_ns={hit.source_mtime_ns}"
            metadata += "]"
            lines.append(f"{header}\n{metadata}\n{content}")
            total_chars += len(content)
        return "\n\n".join(lines)

    def _search_sequential(self, message: str, embed_engine=None) -> str:
        """降级链：关键词 → 语义兜底 → 搜词扩展（无 BGE/reranker 环境走这里）。"""
        keywords = self._extract_keywords(message)
        result = ""

        # ── 第一轮：关键词匹配 ──
        if keywords:
            scored: list[tuple[int, KnowledgeChunk]] = []
            for chunk in self.chunks:
                score = 0
                # 大小写不敏感：英文关键词已 lower，chunk 侧同步 lower（"qq" 才能命中 "QQ"）
                searchable = (chunk.source + "\n" + chunk.label + "\n" + chunk.content).casefold()
                for kw in keywords:
                    if kw.casefold() in chunk.label.casefold():
                        score += 3
                    elif kw.casefold() in chunk.source.casefold():
                        score += 2
                    elif kw.casefold() in chunk.content.casefold():
                        score += 2 if len(kw.casefold()) >= 4 else 1
                if score > 0:
                    scored.append((score, chunk))

            if scored and any(s >= 2 for s, _ in scored):
                result = self._format_results(scored)
                logger.info(f"🔍 知识库命中: {len(scored)}块, 关键词: {keywords[:5]}")

        # ── 第二轮：语义向量兜底 ──
        # 没有实质关键词时，语义模型容易把泛化闲聊映射到“问题/功能”等
        # 文档；扩展索引仍可在下一轮提供作者明确写下的证据。
        if not result and keywords and embed_engine and embed_engine.ready:
            query_vec = embed_engine.encode(message)
            if query_vec is not None:
                sem_scored = [
                    (sim, chunk)
                    for sim, chunk in self._semantic_scores(query_vec)
                    if sim > SEMANTIC_EVIDENCE_THRESHOLD
                ]

                if sem_scored:
                    sem_scored.sort(key=lambda x: x[0], reverse=True)
                    result = self._format_reranked(
                        [(s, c) for s, c in sem_scored[:MAX_RESULTS]],
                    )

        # ── 第三轮：搜词扩展兜底（停用词死查询如「糖糖你会什么」的救命轮）──
        if not result:
            exp = self._expansion_hits(message)
            if exp:
                result = self._format_results(
                    [(2, c) for c in exp[:MAX_RESULTS]], header="## 索引命中")

        return result

    def _search_fused(self, message: str, embed_engine, reranker) -> str:
        """三路召回（带证据门槛）→ RRF 融合 → 证据强直出 / 证据弱重排（2026-08-15 实测校准）。

        三路：关键词（精确）/ 语义向量（同义改写）/ 搜词扩展（文档自带索引）。
        证据门槛：关键词 ≥2 分、实质关键词存在且语义 >0.58、搜词命中——任一满足才算候选。
        实测校准（2026-08-30）：
        - 真实 BGE 的否定/闲聊问句最高相似度可到 0.568，已知相关改写为 0.608-0.674；
          门槛从 0.55 提到 0.58，并阻断无实质关键词的语义轮
        - reranker logit 在此环境整体负偏（正确块 -1~-5.7）→ 只用于排序，不做绝对门槛
        - 30 对重排 5.3s（2.2GB 模型 CPU）→ 缩池 15 + 截断 120 字 ~1.4s；
          关键词强命中（≥5 分）跳过重排直出"""
        keywords = self._extract_keywords(message)

        # 路 1：关键词（≥2 分为证据——label/source 或多内容命中）
        kw_strong: list[tuple[int, KnowledgeChunk]] = []
        if keywords:
            kw_scored: list[tuple[int, KnowledgeChunk]] = []
            for chunk in self.chunks:
                score = 0
                searchable = (chunk.source + "\n" + chunk.label + "\n" + chunk.content).casefold()
                for kw in keywords:
                    if kw.casefold() in chunk.label.casefold():
                        score += 3
                    elif kw.casefold() in chunk.source.casefold():
                        score += 2
                    elif kw.casefold() in chunk.content.casefold():
                        score += 2 if len(kw.casefold()) >= 4 else 1
                if score > 0:
                    kw_scored.append((score, chunk))
            kw_scored.sort(key=lambda x: -x[0])
            kw_strong = [(s, c) for s, c in kw_scored if s >= 2]

        # 路 2：语义（需实质关键词且 >0.58 才算证据）
        sem_strong: list[KnowledgeChunk] = []
        if keywords and embed_engine and embed_engine.ready:
            query_vec = embed_engine.encode(message)
            if query_vec is not None:
                sem_scored = [
                    (sim, chunk)
                    for sim, chunk in self._semantic_scores(query_vec)
                    if sim > SEMANTIC_EVIDENCE_THRESHOLD
                ]
                sem_scored.sort(key=lambda x: -x[0])
                sem_strong = [c for _, c in sem_scored]

        # 路 3：搜词扩展（作者亲手写的索引——最强证据）
        exp_list = self._expansion_hits(message)

        if not (kw_strong or sem_strong or exp_list):
            return ""

        # RRF 融合（k=60 业界标准，只按排名融合无需分数校准）
        rrf: dict[int, float] = {}
        chunk_by_id: dict[int, KnowledgeChunk] = {}
        lists = ([c for _, c in kw_strong], sem_strong, exp_list)
        for lst in lists:
            for rank, chunk in enumerate(lst):
                cid = id(chunk)
                chunk_by_id[cid] = chunk
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
        pool = [chunk_by_id[cid] for cid, _ in sorted(rrf.items(), key=lambda kv: -kv[1])]

        # 强关键词证据（≥5 分 = label+source 组合命中）→ 直出，不付重排代价
        if kw_strong and kw_strong[0][0] >= 5:
            strong_ids = {id(c) for score, c in kw_strong if score >= 5}
            strong = [c for score, c in kw_strong if score >= 5]
            remainder = [c for c in pool if id(c) not in strong_ids]
            ordered = (strong + remainder)[:MAX_RESULTS]
            return self._format_results([(5, c) for c in ordered])

        # 单来源池 → 重排无意义（排序不改变答案），直出（保留 small-to-big）
        if len({self._chunk_document_key(c) for c in pool}) == 1:
            return self._format_reranked([(0.0, c) for c in pool[:MAX_RESULTS]])

        # 多来源弱证据 → 重排定序（缩池 8——实测 15 对 ~1.4s、8 对 ~0.8s；
        # 只有答案来源真模糊时才付这个代价，2026-08-15 评估：10 条仅 1 条触发）
        pool = pool[:8]
        ranked = reranker.rerank(
            message, [self._rerank_text(c, keywords) for c in pool], top_k=3)
        chosen = [(s, pool[i]) for i, s in ranked if i < len(pool)]
        return self._format_reranked(chosen)

    def _rerank_text(self, chunk: KnowledgeChunk, keywords: list[str]) -> str:
        """reranker 候选文本——关键词定位截断（首个命中关键词前后窗口，上限 200 字）。"""
        text = chunk.label + " " + chunk.content
        if keywords:
            low = text.lower()
            pos = None
            for kw in keywords:
                i = low.find(kw)
                if i >= 0 and (pos is None or i < pos):
                    pos = i
            if pos is not None and pos > 80:
                text = text[max(0, pos - 40): pos + 160]
        return text[:200]

    def _expansion_hits(self, message: str) -> list[KnowledgeChunk]:
        """搜词索引命中——返回命中来源文档的全部块（按文档内顺序）。"""
        sources: set[str] = set()
        for term, srcs in self._expansion.items():
            if term.casefold() in message.casefold():
                sources.update(srcs)
        if not sources:
            return []
        # 新索引使用相对路径隔离同名文档；保留 stem 兜底以兼容外部构造的旧 chunk。
        return [
            c for c in self.chunks
            if (c.relative_path and c.relative_path in sources)
            or (not c.relative_path and c.source in sources)
        ]

    def _ensure_chunk_embeddings(self, embed_engine) -> None:
        """加载/生成 chunk 向量，按模型指纹持久化并在重启后复用。"""
        import numpy as np

        fingerprint = str(getattr(embed_engine, "fingerprint", "") or "").strip()
        try:
            expected_dimension = int(getattr(embed_engine, "dimension", 0) or 0)
        except (TypeError, ValueError):
            expected_dimension = 0

        # 同一 KnowledgeBase 可能在测试、热切换或模型升级时复用；模型指纹变化
        # 必须先摘掉旧向量，不能让旧模型的内存缓存绕过持久化指纹隔离。
        for chunk in self.chunks:
            previous_fingerprint = getattr(chunk, "_embedding_fingerprint", None)
            # 没有稳定指纹就无法证明当前引擎与上一次是同一模型；
            # 宁可重新编码，也不能把不同旧/伪引擎的向量跨实例复用。
            if not fingerprint or (
                previous_fingerprint != fingerprint and (
                    previous_fingerprint is not None or fingerprint
                )
            ):
                chunk._embedding = None
            chunk._embedding_fingerprint = fingerprint

        # 只有有稳定模型指纹时才读取持久化向量；旧/伪引擎继续走内存路径。
        if self._index is not None and fingerprint:
            try:
                rows = self._index.load_embeddings(
                    [c.chunk_id for c in self.chunks if c.chunk_id], fingerprint,
                )
                for chunk in self.chunks:
                    row = rows.get(chunk.chunk_id)
                    if row is None:
                        continue
                    vector = np.frombuffer(row["vector"], dtype=np.float32)
                    row_dimension = int(row["dimension"])
                    if row_dimension != len(vector) or (
                        expected_dimension and row_dimension != expected_dimension
                    ):
                        logger.warning(
                            "⚠ 忽略维度不匹配的知识向量: chunk=%s model=%s",
                            chunk.chunk_id, fingerprint,
                        )
                        continue
                    chunk._embedding = vector
                    chunk._embedding_fingerprint = fingerprint
            except Exception as e:
                logger.warning(f"⚠ 读取知识向量缓存失败，回退重新编码: {e}")

        uncached = [c for c in self.chunks if getattr(c, '_embedding', None) is None]
        if not uncached:
            return
        # 嵌入窗口 450 字（bge-small max_length=512 token，之前 200 浪费一半容量——
        # 正文深处的关键词根本没进向量）
        vecs = embed_engine.encode_batch(
            [c.label + " " + c.content[:450] for c in uncached])
        if len(vecs) == len(uncached):
            generated = zip(uncached, vecs)
        else:
            generated = (
                (chunk, embed_engine.encode(chunk.label + " " + chunk.content[:450]))
                for chunk in uncached
            )

        to_persist: list[tuple[str, str, int, bytes]] = []
        for chunk, vec in generated:
            if vec is None:
                continue
            vector = np.asarray(vec, dtype=np.float32).reshape(-1)
            if expected_dimension and len(vector) != expected_dimension:
                logger.warning(
                    "⚠ 忽略维度不匹配的知识向量: chunk=%s got=%d expected=%d",
                    chunk.chunk_id, len(vector), expected_dimension,
                )
                continue
            chunk._embedding = vector
            chunk._embedding_fingerprint = fingerprint
            if self._index is not None and fingerprint and chunk.chunk_id:
                to_persist.append(
                    (chunk.chunk_id, fingerprint, len(vector), vector.tobytes())
                )

        if not to_persist or self._index is None:
            return
        batch_upsert = getattr(self._index, "upsert_embeddings", None)
        if callable(batch_upsert):
            try:
                batch_upsert(to_persist)
                return
            except Exception as e:
                logger.warning("⚠ 批量持久化知识向量失败，逐块重试: %s", e)

        # 兼容旧索引替身/热载对象；单块失败仍不影响内存语义。
        for chunk_id, model_name, dimension, vector in to_persist:
            try:
                self._index.upsert_embedding(
                    chunk_id, model_name, dimension, vector,
                )
            except Exception as e:
                logger.warning(
                    "⚠ 持久化知识向量失败: chunk=%s error=%s", chunk_id, e,
                )

    def _ensure_semantic_segments(self, embed_engine) -> None:
        """为长父块生成短语义子段，词法检索仍只观察父块。"""
        import numpy as np

        fingerprint = str(getattr(embed_engine, "fingerprint", "") or "").strip()
        if self._semantic_segments:
            same_engine = (
                not fingerprint
                and self._semantic_segments_engine_id == id(embed_engine)
            )
            if same_engine or (
                fingerprint
                and self._semantic_segments_fingerprint == fingerprint
                and all(segment.embedding is not None for segment in self._semantic_segments)
            ):
                return

        segments: list[KnowledgeSemanticSegment] = []
        for chunk in self.chunks:
            if len(chunk.content) <= SEMANTIC_SEGMENT_CHARS:
                continue
            for start in range(0, len(chunk.content), SEMANTIC_SEGMENT_CHARS):
                end = min(len(chunk.content), start + SEMANTIC_SEGMENT_CHARS)
                parent_start = chunk.start_char if chunk.start_char >= 0 else 0
                segments.append(KnowledgeSemanticSegment(
                    parent=chunk,
                    start_char=parent_start + start,
                    end_char=parent_start + end,
                    text=chunk.label + " " + chunk.content[start:end],
                ))

        if not segments:
            self._semantic_segments = []
            self._semantic_segments_fingerprint = fingerprint
            self._semantic_segments_engine_id = id(embed_engine)
            return

        try:
            expected_dimension = int(getattr(embed_engine, "dimension", 0) or 0)
        except (TypeError, ValueError):
            expected_dimension = 0
        persistable = (
            self._index is not None
            and bool(fingerprint)
            and all(segment.parent.chunk_id for segment in segments)
        )
        if persistable:
            try:
                self._index.sync_semantic_segments([
                    (
                        segment.segment_id, segment.parent.chunk_id,
                        index, segment.start_char, segment.end_char,
                        hashlib.sha256(segment.text.encode("utf-8")).hexdigest(),
                        segment.text,
                    )
                    for index, segment in enumerate(segments)
                ])
                cached = self._index.load_semantic_embeddings(
                    [segment.segment_id for segment in segments], fingerprint,
                )
                for segment in segments:
                    row = cached.get(segment.segment_id)
                    if row is None:
                        continue
                    vector = np.frombuffer(row["vector"], dtype=np.float32)
                    if int(row["dimension"]) != len(vector) or (
                        expected_dimension and int(row["dimension"]) != expected_dimension
                    ):
                        logger.warning(
                            "⚠ 忽略维度不匹配的知识语义缓存: segment=%s model=%s",
                            segment.segment_id, fingerprint,
                        )
                        continue
                    segment.embedding = vector
                    segment.embedding_fingerprint = fingerprint
            except Exception as e:
                logger.warning("⚠ 读取知识语义子段缓存失败，回退重新编码: %s", e)

        uncached = [segment for segment in segments if segment.embedding is None]
        if uncached:
            texts = [segment.text for segment in uncached]
            vectors = embed_engine.encode_batch(texts)
            if len(vectors) != len(uncached):
                vectors = [embed_engine.encode(text) for text in texts]
            for segment, vector in zip(uncached, vectors):
                if vector is None:
                    continue
                vector = np.asarray(vector, dtype=np.float32).reshape(-1)
                if expected_dimension and len(vector) != expected_dimension:
                    logger.warning(
                        "⚠ 忽略维度不匹配的知识语义子段: got=%d expected=%d",
                        len(vector), expected_dimension,
                    )
                    continue
                segment.embedding = vector
                segment.embedding_fingerprint = fingerprint

        to_persist: list[tuple[str, str, int, bytes]] = []
        if persistable:
            for segment in segments:
                if segment.embedding is not None and segment.embedding_fingerprint == fingerprint:
                    vector = np.asarray(segment.embedding, dtype=np.float32).reshape(-1)
                    to_persist.append(
                        (segment.segment_id, fingerprint, len(vector), vector.tobytes())
                    )
            upsert = getattr(self._index, "upsert_semantic_embeddings", None)
            if to_persist and callable(upsert):
                try:
                    upsert(to_persist)
                except Exception as e:
                    logger.warning("⚠ 持久化知识语义子段失败: %s", e)

        valid = [segment for segment in segments if segment.embedding is not None]

        self._semantic_segments = valid
        self._semantic_segments_fingerprint = fingerprint
        self._semantic_segments_engine_id = id(embed_engine)
        logger.info(
            "📚 知识语义子段就绪: %d 段/%d 父块 (模型=%s)",
            len(valid), len({id(segment.parent) for segment in valid}),
            fingerprint or "unfingerprinted",
        )

    def _semantic_scores(self, query_vec) -> list[tuple[float, KnowledgeChunk]]:
        """按父块汇总父向量与子段向量的最高相似度。"""
        import numpy as np

        scores: dict[int, tuple[float, KnowledgeChunk]] = {}
        for chunk in self.chunks:
            cached = getattr(chunk, "_embedding", None)
            if cached is not None:
                scores[id(chunk)] = (float(np.dot(query_vec, cached)), chunk)
        for segment in self._semantic_segments:
            if segment.embedding is None:
                continue
            similarity = float(np.dot(query_vec, segment.embedding))
            parent_key = id(segment.parent)
            previous = scores.get(parent_key)
            if previous is None or similarity > previous[0]:
                scores[parent_key] = (similarity, segment.parent)
        return list(scores.values())

    def _format_reranked(self, chosen: list[tuple[float, KnowledgeChunk]]) -> str:
        """重排结果格式化——只控总量。
        small-to-big：标题短块（<200 字）合并同文档的下一块正文——
        实测「毛选里的方法论」重排第一是 91 字标题块，不带正文等于没召回。"""
        lines = []
        total_chars = 0
        seen: set[int] = set()
        for _logit, chunk in chosen:
            content = chunk.content
            # small-to-big：短块拼上同文档下一块
            if len(content) < 200:
                nxt = self._next_chunk_of(chunk)
                if nxt is not None and id(nxt) not in seen:
                    content = content + "\n" + nxt.content
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 100:
                break
            if len(content) > remaining:
                content = self._truncate_at_sentence(content, remaining)
            chunk_header = f"### {chunk.source}"
            if chunk.label and chunk.label != chunk.source:
                chunk_header += f" — {chunk.label}"
            lines.append(f"{chunk_header}\n{content}")
            total_chars += len(content)
            seen.add(id(chunk))
        return "\n\n".join(lines) if lines else ""

    @staticmethod
    def _chunk_document_key(chunk: KnowledgeChunk) -> str:
        """返回不会把不同目录同名文件合并的文档键。"""
        return chunk.document_id or chunk.relative_path or chunk.source

    def _next_chunk_of(self, chunk: KnowledgeChunk) -> KnowledgeChunk | None:
        """同文档的下一块（chunks 按文件顺序连续存放）——small-to-big 用。"""
        for i, c in enumerate(self.chunks):
            if c is chunk and i + 1 < len(self.chunks):
                nxt = self.chunks[i + 1]
                return (
                    nxt
                    if self._chunk_document_key(nxt) == self._chunk_document_key(chunk)
                    else None
                )
        return None

    def _format_results(self, scored: list, header: str = "") -> str:
        """格式化搜索结果——复用原有的截断+拼接逻辑"""
        scored.sort(key=lambda x: x[0], reverse=True)

        lines = []
        if header:
            lines.append(header)
        total_chars = 0
        # 2026-08-14 修复：关键词路径门槛 3 会把 source 命中（2分）和双 content 命中（2分）全过滤——
        # 加权设计 label=3/source=2/content=1 的"2 分档"形同虚设（"minecraft怎么玩"实测 0 召回）。
        # 统一门槛 2：source 命中有效、单 content 命中（1分）仍过滤防噪音。
        min_score = 2

        for score, chunk in scored[:MAX_RESULTS]:
            if score < min_score:
                continue
            # 如果加上这个块会超总量，截断
            remaining = MAX_TOTAL_CHARS - total_chars
            if remaining <= 100:
                break

            content = chunk.content
            if len(content) > remaining:
                content = self._truncate_at_sentence(content, remaining)

            chunk_header = f"### {chunk.source}"
            if chunk.label and chunk.label != chunk.source:
                chunk_header += f" — {chunk.label}"
            lines.append(f"{chunk_header}\n{content}")
            total_chars += len(content)

        if not lines or (len(lines) == 1 and header and not lines[0].startswith("###")):
            return ""

        result = "\n\n".join(lines)
        logger.info(f"🔍 知识库命中: {len(lines)}块 / {total_chars}字")
        return result

    # ---- 关键词提取 ----

    def _extract_keywords(self, message: str) -> list[str]:
        """从消息中提取有意义的关键词——用 jieba 分词，不再暴力 n-gram"""
        for ch in "，。！？、：；""''（）【】《》…—\n":
            message = message.replace(ch, " ")

        keywords = []

        # 英文/数字词
        en_words = re.findall(r'[a-zA-Z0-9]{2,}', message)
        for w in en_words:
            if w.lower() not in STOP_WORDS:
                keywords.append(w.lower())

        # jieba 中文分词
        try:
            import jieba
            words = jieba.cut(message)
            for w in words:
                w = w.strip()
                if len(w) >= 2 and w not in STOP_WORDS:
                    keywords.append(w)
        except ImportError:
            # 兜底：简单空格分词
            for w in message.split():
                w = w.strip()
                if len(w) >= 2 and w not in STOP_WORDS:
                    keywords.append(w)

        # 去重，最多 16 个
        seen = set()
        result = []
        for w in keywords:
            if w not in seen:
                seen.add(w)
                result.append(w)
        return result[:16]
