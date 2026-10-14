"""知识库 SQLite 持久化索引的元数据层。

本模块只负责公共知识文档的文档/chunk 元数据和可选 embedding BLOB，
不参与 LLM 决策，也不改变 ``KnowledgeBase.search`` 的返回契约。每个
操作使用短连接，避免 sqlite 连接跨线程复用；批量同步通过单事务提交。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


SCHEMA_VERSION = 1
SEMANTIC_SCHEMA_VERSION = 1
_SAFE_RELATIVE_PATH = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)(?:\.\.?)(?:/|$)).+")


def _normalize_relative_path(relative_path: str) -> str:
    value = str(relative_path or "").replace("\\", "/").strip("/")
    if not value or not _SAFE_RELATIVE_PATH.fullmatch(value):
        raise ValueError(f"invalid relative knowledge path: {relative_path!r}")
    return value


def document_id_for(relative_path: str) -> str:
    """从规范化相对路径生成跨重启稳定的文档 ID。"""
    normalized = _normalize_relative_path(relative_path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class IndexChunk:
    """待写入索引的一个 chunk 快照。"""

    document_id: str
    ordinal: int
    label: str
    content: str
    start_char: int
    end_char: int

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def chunk_id(self) -> str:
        material = (
            f"{self.document_id}\0{self.ordinal}\0{self.content_sha256}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:24]


@dataclass(frozen=True)
class IndexDocument:
    """待同步的文档及其完整 chunk 列表。"""

    document_id: str
    relative_path: str
    sha256: str
    mtime_ns: int
    chars: int
    chunks: tuple[IndexChunk, ...]


@dataclass(frozen=True)
class IndexSyncStats:
    """一次同步的可观测结果。"""

    added_documents: int = 0
    changed_documents: int = 0
    unchanged_documents: int = 0
    removed_documents: int = 0
    added_chunks: int = 0
    replaced_chunks: int = 0
    removed_chunks: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_terms(text: str) -> str:
    """把中文转成可由 unicode61 索引的 ASCII bigram token。

    SQLite 的 unicode61 默认不会把 CJK 字符作为可检索 token；用字符码点
    生成 ASCII token 既保留中文二元组召回，又让查询始终是参数化的普通词。
    拉丁词保留为小写 token，兼顾英文文档。
    """
    tokens: list[str] = []
    for part in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", str(text or "")):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) == 1:
                tokens.append(f"cjk_{ord(part):x}")
            else:
                tokens.extend(
                    f"cjk_{ord(left):x}_{ord(right):x}"
                    for left, right in zip(part, part[1:])
                )
        else:
            tokens.append(f"word_{part.casefold()}")
    return " ".join(tokens)


def _fts_query(query: str) -> str:
    """生成只含安全 token 的 FTS 查询；无有效词时返回空字符串。"""
    terms = _fts_terms(query).split()
    # 多个中文 bigram 必须同时出现，否则「旧内容」会因共享「内容」
    # 与「新内容」误召回；单词查询自然退化为一个 term。
    return " AND ".join(terms)


class KnowledgeIndex:
    """公共知识库的 SQLite 索引。

    ``db_path`` 可以是独立测试文件，也可以是生产的
    ``knowledge/.knowledge_index.sqlite3``。构造和同步都不抛出隐藏降级；
    调用方决定索引故障是否回退到内存路径。
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.fts_available = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path), timeout=5.0, isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    chars INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                        ON DELETE CASCADE,
                    UNIQUE(document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document
                    ON chunks(document_id, ordinal);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chunk_id, model_name),
                    FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS semantic_segments (
                    segment_id TEXT PRIMARY KEY,
                    parent_chunk_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    text TEXT NOT NULL,
                    FOREIGN KEY(parent_chunk_id) REFERENCES chunks(chunk_id)
                        ON DELETE CASCADE,
                    UNIQUE(parent_chunk_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_semantic_segments_parent
                    ON semantic_segments(parent_chunk_id, ordinal);
                CREATE TABLE IF NOT EXISTS semantic_embeddings (
                    segment_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(segment_id, model_name),
                    FOREIGN KEY(segment_id) REFERENCES semantic_segments(segment_id)
                        ON DELETE CASCADE
                );
                """
            )
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO index_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                try:
                    stored_version = int(row["value"])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "unsupported knowledge index schema version"
                    ) from exc
                if stored_version != SCHEMA_VERSION:
                    # Never overwrite a newer/unknown schema: opening the index
                    # must fail closed so an old binary cannot corrupt it.
                    raise RuntimeError("unsupported knowledge index schema version")
            semantic_version = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'semantic_schema_version'"
            ).fetchone()
            if semantic_version is None:
                conn.execute(
                    "INSERT INTO index_meta(key, value) VALUES ('semantic_schema_version', ?)",
                    (str(SEMANTIC_SCHEMA_VERSION),),
                )
            elif str(semantic_version["value"]) != str(SEMANTIC_SCHEMA_VERSION):
                raise RuntimeError("unsupported semantic index schema version")
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        chunk_id UNINDEXED,
                        terms,
                        label,
                        content,
                        tokenize = 'unicode61 remove_diacritics 2'
                    )
                    """
                )
                self.fts_available = True
            except sqlite3.OperationalError as exc:
                if "fts5" not in str(exc).casefold():
                    raise
                # FTS5 是增强能力；主表仍可用于元数据/向量持久化。
                self.fts_available = False
            if self.fts_available:
                expected_rows = conn.execute(
                    "SELECT chunk_id, label, content FROM chunks ORDER BY chunk_id"
                ).fetchall()
                actual_rows = conn.execute(
                    "SELECT chunk_id, label, content, terms FROM chunks_fts ORDER BY chunk_id"
                ).fetchall()
                fts_consistent = len(actual_rows) == len(expected_rows) and all(
                    actual["chunk_id"] == expected["chunk_id"]
                    and actual["label"] == expected["label"]
                    and actual["content"] == expected["content"]
                    and actual["terms"] == _fts_terms(
                        expected["label"] + "\n" + expected["content"]
                    )
                    for actual, expected in zip(actual_rows, expected_rows)
                )
                if not fts_consistent:
                    conn.execute("DELETE FROM chunks_fts")
                    for chunk in expected_rows:
                        conn.execute(
                            "INSERT INTO chunks_fts(chunk_id, terms, label, content) VALUES (?, ?, ?, ?)",
                            (
                                chunk["chunk_id"],
                                _fts_terms(chunk["label"] + "\n" + chunk["content"]),
                                chunk["label"],
                                chunk["content"],
                            ),
                        )

    @staticmethod
    def _validate_document(document: IndexDocument) -> str:
        relative_path = _normalize_relative_path(document.relative_path)
        if document.document_id != document_id_for(relative_path):
            raise ValueError("document_id does not match relative_path")
        if document.chars < 0 or document.mtime_ns < 0:
            raise ValueError("document metadata must be non-negative")
        expected_ordinal = 0
        for chunk in document.chunks:
            if chunk.document_id != document.document_id:
                raise ValueError("chunk document_id mismatch")
            if chunk.ordinal != expected_ordinal:
                raise ValueError("chunk ordinals must be contiguous from zero")
            if chunk.start_char < 0 or chunk.end_char < chunk.start_char:
                raise ValueError("invalid chunk character offsets")
            expected_ordinal += 1
        return relative_path

    def sync(self, documents: Iterable[IndexDocument]) -> IndexSyncStats:
        """原子同步当前文档集合，并清理已删除文档及其派生记录。"""
        current = list(documents)
        normalized: list[tuple[IndexDocument, str]] = []
        seen_paths: set[str] = set()
        seen_ids: set[str] = set()
        for document in current:
            relative_path = self._validate_document(document)
            if relative_path in seen_paths or document.document_id in seen_ids:
                raise ValueError("duplicate knowledge document")
            seen_paths.add(relative_path)
            seen_ids.add(document.document_id)
            normalized.append((document, relative_path))

        added_documents = changed_documents = unchanged_documents = 0
        added_chunks = replaced_chunks = removed_chunks = 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old_rows = conn.execute(
                    "SELECT document_id, relative_path, sha256, updated_at "
                    "FROM documents"
                ).fetchall()
                old_by_path = {row["relative_path"]: row for row in old_rows}

                for document, relative_path in normalized:
                    old = old_by_path.get(relative_path)
                    if old is None:
                        added_documents += 1
                        needs_chunks = True
                    elif old["sha256"] != document.sha256:
                        changed_documents += 1
                        needs_chunks = True
                    else:
                        unchanged_documents += 1
                        persisted_chunks = conn.execute(
                            "SELECT chunk_id, ordinal, label, content, content_sha256, "
                            "start_char, end_char FROM chunks WHERE document_id = ? "
                            "ORDER BY ordinal",
                            (document.document_id,),
                        ).fetchall()
                        count = len(persisted_chunks)
                        needs_chunks = len(persisted_chunks) != len(document.chunks)
                        if not needs_chunks:
                            needs_chunks = any(
                                row["chunk_id"] != chunk.chunk_id
                                or row["ordinal"] != chunk.ordinal
                                or row["label"] != chunk.label
                                or row["content"] != chunk.content
                                or row["content_sha256"] != chunk.content_sha256
                                or row["start_char"] != chunk.start_char
                                or row["end_char"] != chunk.end_char
                                for row, chunk in zip(
                                    persisted_chunks, document.chunks,
                                )
                            )
                        fts_count = count
                        if self.fts_available:
                            fts_count = conn.execute(
                                """
                                SELECT COUNT(*) AS count FROM chunks_fts
                                WHERE chunk_id IN (
                                    SELECT chunk_id FROM chunks WHERE document_id = ?
                                )
                                """,
                                (document.document_id,),
                            ).fetchone()["count"]
                        needs_chunks = (
                            needs_chunks
                            or fts_count != len(document.chunks)
                        )

                    # updated_at 表示“内容版本进入索引的时间”，而不是每次
                    # 启动/同步的观察时间。未变化文档（包括只修复损坏 FTS
                    # 或 chunk 的情况）必须保留原时间，才能支持来源时间审计
                    # 和后续版本 diff；只有新增或内容 hash 变化才生成新时间。
                    updated_at = (
                        _utc_now()
                        if old is None or old["sha256"] != document.sha256
                        else old["updated_at"]
                    )
                    conn.execute(
                        """
                        INSERT INTO documents(
                            document_id, relative_path, sha256, mtime_ns, chars, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            document_id = excluded.document_id,
                            sha256 = excluded.sha256,
                            mtime_ns = excluded.mtime_ns,
                            chars = excluded.chars,
                            updated_at = excluded.updated_at
                        """,
                        (
                            document.document_id,
                            relative_path,
                            document.sha256,
                            document.mtime_ns,
                            document.chars,
                            updated_at,
                        ),
                    )

                    if needs_chunks:
                        # FTS5 没有外键级联，必须先删派生行再删主表 chunk。
                        if self.fts_available:
                            conn.execute(
                                """
                                DELETE FROM chunks_fts
                                WHERE chunk_id IN (
                                    SELECT chunk_id FROM chunks WHERE document_id = ?
                                )
                                """,
                                (document.document_id,),
                            )
                        old_count = conn.execute(
                            "SELECT COUNT(*) AS count FROM chunks WHERE document_id = ?",
                            (document.document_id,),
                        ).fetchone()["count"]
                        if old_count:
                            removed_chunks += int(old_count)
                            conn.execute(
                                "DELETE FROM chunks WHERE document_id = ?",
                                (document.document_id,),
                            )
                        for chunk in document.chunks:
                            conn.execute(
                                """
                                INSERT INTO chunks(
                                    chunk_id, document_id, ordinal, label, content,
                                    content_sha256, start_char, end_char
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    chunk.chunk_id,
                                    chunk.document_id,
                                    chunk.ordinal,
                                    chunk.label,
                                    chunk.content,
                                    chunk.content_sha256,
                                    chunk.start_char,
                                    chunk.end_char,
                                ),
                            )
                            if self.fts_available:
                                conn.execute(
                                    """
                                    INSERT INTO chunks_fts(chunk_id, terms, label, content)
                                    VALUES (?, ?, ?, ?)
                                    """,
                                    (
                                        chunk.chunk_id,
                                        _fts_terms(chunk.label + "\n" + chunk.content),
                                        chunk.label,
                                        chunk.content,
                                    ),
                                )
                        if old_count:
                            replaced_chunks += len(document.chunks)
                        else:
                            added_chunks += len(document.chunks)

                for row in old_rows:
                    if row["relative_path"] not in seen_paths:
                        if self.fts_available:
                            conn.execute(
                                """
                                DELETE FROM chunks_fts
                                WHERE chunk_id IN (
                                    SELECT chunk_id FROM chunks WHERE document_id = ?
                                )
                                """,
                                (row["document_id"],),
                            )
                        count = conn.execute(
                            "SELECT COUNT(*) AS count FROM chunks WHERE document_id = ?",
                            (row["document_id"],),
                        ).fetchone()["count"]
                        removed_chunks += int(count)
                        conn.execute(
                            "DELETE FROM documents WHERE document_id = ?",
                            (row["document_id"],),
                        )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return IndexSyncStats(
            added_documents=added_documents,
            changed_documents=changed_documents,
            unchanged_documents=unchanged_documents,
            removed_documents=sum(
                row["relative_path"] not in seen_paths for row in old_rows
            ),
            added_chunks=added_chunks,
            replaced_chunks=replaced_chunks,
            removed_chunks=removed_chunks,
        )

    def snapshot(self) -> dict[str, int]:
        with self._connection() as conn:
            rows = {
                row["name"]: row["count"]
                for row in conn.execute(
                    """
                    SELECT 'documents' AS name, COUNT(*) AS count FROM documents
                    UNION ALL SELECT 'chunks', COUNT(*) FROM chunks
                    UNION ALL SELECT 'embeddings', COUNT(*) FROM embeddings
                    """
                ).fetchall()
            }
            return {
                "schema_version": SCHEMA_VERSION,
                "documents": int(rows["documents"]),
                "chunks": int(rows["chunks"]),
                "embeddings": int(rows["embeddings"]),
            }

    def get_document(self, relative_path: str) -> sqlite3.Row | None:
        path = _normalize_relative_path(relative_path)
        with self._connection() as conn:
            return conn.execute(
                "SELECT * FROM documents WHERE relative_path = ?", (path,)
            ).fetchone()

    def get_chunks(self, document_id: str) -> list[sqlite3.Row]:
        with self._connection() as conn:
            return conn.execute(
                "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal",
                (document_id,),
            ).fetchall()

    def sync_semantic_segments(
        self,
        segments: Iterable[tuple[str, str, int, int, int, str, str]],
    ) -> int:
        """原子同步父块的语义子段元数据，删除同一父块的陈旧子段。"""
        rows = []
        by_parent: dict[str, list[str]] = {}
        for segment_id, parent_chunk_id, ordinal, start_char, end_char, content_sha256, text in segments:
            segment_id = str(segment_id or "").strip()
            parent_chunk_id = str(parent_chunk_id or "").strip()
            if not segment_id or not parent_chunk_id:
                raise ValueError("semantic segment IDs are required")
            if int(ordinal) < 0 or int(start_char) < 0 or int(end_char) < int(start_char):
                raise ValueError("invalid semantic segment offsets")
            rows.append((
                segment_id, parent_chunk_id, int(ordinal), int(start_char),
                int(end_char), str(content_sha256 or ""), str(text or ""),
            ))
            by_parent.setdefault(parent_chunk_id, []).append(segment_id)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for parent_chunk_id, segment_ids in by_parent.items():
                    placeholders = ", ".join("?" for _ in segment_ids)
                    conn.execute(
                        f"DELETE FROM semantic_segments WHERE parent_chunk_id = ? "
                        f"AND segment_id NOT IN ({placeholders})",
                        [parent_chunk_id, *segment_ids],
                    )
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO semantic_segments(
                            segment_id, parent_chunk_id, ordinal, start_char,
                            end_char, content_sha256, text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(segment_id) DO UPDATE SET
                            parent_chunk_id = excluded.parent_chunk_id,
                            ordinal = excluded.ordinal,
                            start_char = excluded.start_char,
                            end_char = excluded.end_char,
                            content_sha256 = excluded.content_sha256,
                            text = excluded.text
                        """,
                        rows,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    def load_semantic_segments(
        self, parent_chunk_ids: Iterable[str],
    ) -> list[sqlite3.Row]:
        """按父块批量加载语义子段元数据。"""
        ids = list(dict.fromkeys(str(value) for value in parent_chunk_ids if value))
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connection() as conn:
            return conn.execute(
                f"SELECT * FROM semantic_segments WHERE parent_chunk_id IN ({placeholders}) "
                "ORDER BY parent_chunk_id, ordinal",
                ids,
            ).fetchall()

    def load_semantic_embeddings(
        self, segment_ids: Iterable[str], model_name: str,
    ) -> dict[str, sqlite3.Row]:
        """按语义子段 ID 和模型指纹批量读取向量。"""
        ids = list(dict.fromkeys(str(segment_id) for segment_id in segment_ids if segment_id))
        model = str(model_name or "").strip()
        if not ids or not model:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT segment_id, model_name, dimension, vector, updated_at
                FROM semantic_embeddings
                WHERE model_name = ? AND segment_id IN ({placeholders})
                """,
                [model, *ids],
            ).fetchall()
        return {row["segment_id"]: row for row in rows}

    def upsert_semantic_embeddings(
        self,
        embeddings: Iterable[tuple[str, str, int, bytes]],
    ) -> int:
        """在一个事务内写入多个语义子段向量。"""
        rows = []
        for segment_id, model_name, dimension, vector in embeddings:
            model = str(model_name or "").strip()
            if not str(segment_id or "").strip() or not model:
                raise ValueError("segment_id and model_name are required")
            if dimension <= 0:
                raise ValueError("dimension must be positive")
            if not isinstance(vector, (bytes, bytearray, memoryview)):
                raise TypeError("vector must be bytes-like")
            rows.append((
                str(segment_id), model, int(dimension), bytes(vector), _utc_now(),
            ))
        if not rows:
            return 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    """
                    INSERT INTO semantic_embeddings(
                        segment_id, model_name, dimension, vector, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(segment_id, model_name) DO UPDATE SET
                        dimension = excluded.dimension,
                        vector = excluded.vector,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    def search_lexical(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        """使用持久化 FTS5 bigram 索引召回 chunk；无有效词时诚实返回空。"""
        if not self.fts_available:
            return []
        match_query = _fts_query(query)
        if not match_query:
            return []
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            bounded_limit = 10
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT c.chunk_id, c.document_id, d.relative_path, c.ordinal,
                       c.label, c.content, c.content_sha256,
                       c.start_char, c.end_char,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks AS c ON c.chunk_id = chunks_fts.chunk_id
                JOIN documents AS d ON d.document_id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC, c.chunk_id ASC
                LIMIT ?
                """,
                (match_query, bounded_limit),
            ).fetchall()

    def load_embeddings(
        self, chunk_ids: Iterable[str], model_name: str,
    ) -> dict[str, sqlite3.Row]:
        """按 chunk ID 和模型指纹批量读取 embedding，避免逐块打开连接。"""
        ids = list(dict.fromkeys(str(chunk_id) for chunk_id in chunk_ids if chunk_id))
        model = str(model_name or "").strip()
        if not ids or not model:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT chunk_id, model_name, dimension, vector, updated_at
                FROM embeddings
                WHERE model_name = ? AND chunk_id IN ({placeholders})
                """,
                [model, *ids],
            ).fetchall()
        return {row["chunk_id"]: row for row in rows}

    def upsert_embedding(
        self, chunk_id: str, model_name: str, dimension: int, vector: bytes,
    ) -> None:
        self.upsert_embeddings([(chunk_id, model_name, dimension, vector)])

    def upsert_embeddings(
        self,
        embeddings: Iterable[tuple[str, str, int, bytes]],
    ) -> int:
        """在一个事务内写入多个 chunk 向量，返回成功写入的数量。"""
        rows = []
        for chunk_id, model_name, dimension, vector in embeddings:
            model = str(model_name or "").strip()
            if not model:
                raise ValueError("model_name is required")
            if dimension <= 0:
                raise ValueError("dimension must be positive")
            if not isinstance(vector, (bytes, bytearray, memoryview)):
                raise TypeError("vector must be bytes-like")
            rows.append((
                chunk_id, model, dimension, bytes(vector), _utc_now(),
            ))
        if not rows:
            return 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    """
                    INSERT INTO embeddings(chunk_id, model_name, dimension, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id, model_name) DO UPDATE SET
                        dimension = excluded.dimension,
                        vector = excluded.vector,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return len(rows)

    def get_embedding(self, chunk_id: str, model_name: str) -> sqlite3.Row | None:
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT chunk_id, model_name, dimension, vector, updated_at
                FROM embeddings WHERE chunk_id = ? AND model_name = ?
                """,
                (chunk_id, model_name.strip()),
            ).fetchone()

    def close(self) -> None:
        """兼容调用方的生命周期接口（连接按操作短暂开启，此处无需动作）。"""
