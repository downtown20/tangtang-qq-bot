import hashlib
import sqlite3

import pytest

from agent.knowledge_index import (
    IndexChunk,
    IndexDocument,
    KnowledgeIndex,
    document_id_for,
)


def _document(relative_path="guide.md", content="正文"):
    document_id = document_id_for(relative_path)
    chunk = IndexChunk(
        document_id=document_id,
        ordinal=0,
        label="指南",
        content=content,
        start_char=3,
        end_char=3 + len(content),
    )
    return IndexDocument(
        document_id=document_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        mtime_ns=1,
        chars=len(content),
        chunks=(chunk,),
    )


def test_sync_persists_schema_document_chunk_and_offset(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document()

    stats = index.sync([document])

    assert stats.added_documents == 1
    assert stats.added_chunks == 1
    assert index.snapshot() == {
        "schema_version": 1,
        "documents": 1,
        "chunks": 1,
        "embeddings": 0,
    }
    row = index.get_chunks(document.document_id)[0]
    assert row["chunk_id"] == document.chunks[0].chunk_id
    assert row["content_sha256"] == document.chunks[0].content_sha256
    assert (row["start_char"], row["end_char"]) == (3, 5)


def test_future_schema_version_is_not_overwritten(tmp_path):
    """未知索引版本必须 fail-closed，不能被初始化静默改写成当前版本。"""
    path = tmp_path / "knowledge_index.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO index_meta(key, value) VALUES ('schema_version', '999')"
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="unsupported knowledge index schema version"):
        KnowledgeIndex(path)

    with sqlite3.connect(path) as conn:
        value = conn.execute(
            "SELECT value FROM index_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert value == "999"


def test_restart_and_repeat_sync_are_idempotent(tmp_path):
    path = tmp_path / "knowledge_index.sqlite3"
    document = _document()
    first = KnowledgeIndex(path)
    first.sync([document])
    first.close()

    second = KnowledgeIndex(path)
    stats = second.sync([document])

    assert stats.changed_documents == 0
    assert stats.removed_documents == 0
    assert stats.added_chunks == 0
    assert second.get_document(document.relative_path)["document_id"] == document.document_id


def test_repeat_sync_preserves_document_updated_at(tmp_path, monkeypatch):
    """未变更文档的索引同步不能伪造新的版本时间。"""
    path = tmp_path / "knowledge_index.sqlite3"
    document = _document()
    index = KnowledgeIndex(path)
    monkeypatch.setattr("agent.knowledge_index._utc_now", lambda: "2026-01-01T00:00:00+00:00")
    index.sync([document])
    first = index.get_document(document.relative_path)["updated_at"]

    monkeypatch.setattr("agent.knowledge_index._utc_now", lambda: "2026-01-02T00:00:00+00:00")
    index.sync([document])

    assert index.get_document(document.relative_path)["updated_at"] == first


def test_changed_document_updates_version_time(tmp_path, monkeypatch):
    """内容 hash 变化时才产生新的索引版本时间。"""
    path = tmp_path / "knowledge_index.sqlite3"
    index = KnowledgeIndex(path)
    monkeypatch.setattr("agent.knowledge_index._utc_now", lambda: "2026-01-01T00:00:00+00:00")
    index.sync([_document(content="旧正文")])

    monkeypatch.setattr("agent.knowledge_index._utc_now", lambda: "2026-01-02T00:00:00+00:00")
    index.sync([_document(content="新正文")])

    assert index.get_document("guide.md")["updated_at"] == "2026-01-02T00:00:00+00:00"


def test_sync_repairs_corrupt_main_chunk_when_document_hash_matches(tmp_path):
    """主 chunks 损坏但文档 hash 未变时，重新同步不能静默接受坏正文。"""
    path = tmp_path / "knowledge_index.sqlite3"
    document = _document(content="原始正文")
    first = KnowledgeIndex(path)
    first.sync([document])

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE chunks SET label=?, content=? WHERE chunk_id=?",
            ("损坏标签", "损坏正文", document.chunks[0].chunk_id),
        )
        conn.commit()

    second = KnowledgeIndex(path)
    stats = second.sync([document])

    assert stats.changed_documents == 0
    assert second.get_chunks(document.document_id)[0]["label"] == "指南"
    assert second.get_chunks(document.document_id)[0]["content"] == "原始正文"


def test_close_does_not_reinitialize_short_lived_index(tmp_path, monkeypatch):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    calls = []
    monkeypatch.setattr(index, "_initialize", lambda: calls.append(True))

    index.close()

    assert calls == []


def test_changed_document_replaces_old_chunks_atomically(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    old = _document(content="旧正文")
    new = _document(content="新正文")
    index.sync([old])

    stats = index.sync([new])

    assert stats.changed_documents == 1
    rows = index.get_chunks(new.document_id)
    assert [row["content"] for row in rows] == ["新正文"]
    assert index.get_chunks(old.document_id)[0]["content"] == "新正文"


def test_removed_document_and_embedding_are_deleted(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document()
    index.sync([document])
    chunk_id = document.chunks[0].chunk_id
    index.upsert_embedding(chunk_id, "bge-small-zh-v1.5", 2, b"\x00\x01")

    stats = index.sync([])

    assert stats.removed_documents == 1
    assert index.snapshot()["documents"] == 0
    assert index.snapshot()["chunks"] == 0
    assert index.snapshot()["embeddings"] == 0
    assert index.get_embedding(chunk_id, "bge-small-zh-v1.5") is None


def test_removed_parent_chunk_cascades_semantic_segments_and_embeddings(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document(content="长正文")
    index.sync([document])
    parent_id = document.chunks[0].chunk_id
    index.sync_semantic_segments([
        ("segment-1", parent_id, 0, 3, 6, "hash-1", "长正文"),
    ])
    index.upsert_semantic_embeddings([
        ("segment-1", "model-a", 2, b"\x00\x01"),
    ])

    index.sync([])

    assert index.load_semantic_segments([parent_id]) == []
    assert index.load_semantic_embeddings(["segment-1"], "model-a") == {}


def test_embedding_fingerprint_roundtrip_and_validation(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document()
    index.sync([document])
    chunk_id = document.chunks[0].chunk_id

    index.upsert_embedding(chunk_id, "bge-small-zh-v1.5", 2, b"\x00\x01")
    row = index.get_embedding(chunk_id, "bge-small-zh-v1.5")
    assert row["dimension"] == 2
    assert row["vector"] == b"\x00\x01"

    with pytest.raises(ValueError, match="dimension"):
        index.upsert_embedding(chunk_id, "bge-small-zh-v1.5", 0, b"\x00\x01")


def test_batch_embedding_upsert_roundtrip_and_validation(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document()
    index.sync([document])
    chunk_id = document.chunks[0].chunk_id

    assert index.upsert_embeddings([
        (chunk_id, "bge-small-zh-v1.5", 2, b"\x00\x01"),
    ]) == 1
    row = index.get_embedding(chunk_id, "bge-small-zh-v1.5")
    assert row["dimension"] == 2
    assert row["vector"] == b"\x00\x01"

    with pytest.raises(ValueError, match="dimension"):
        index.upsert_embeddings([
            (chunk_id, "bge-small-zh-v1.5", 2, b"\x00\x01"),
            (chunk_id, "bge-small-zh-v1.5", 0, b"\x02\x03"),
        ])
    assert index.get_embedding(chunk_id, "bge-small-zh-v1.5")["vector"] == b"\x00\x01"


def test_load_embeddings_returns_only_requested_fingerprint(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document()
    index.sync([document])
    chunk_id = document.chunks[0].chunk_id
    index.upsert_embedding(chunk_id, "model-a", 2, b"\x00\x01")
    index.upsert_embedding(chunk_id, "model-b", 2, b"\x02\x03")

    rows = index.load_embeddings([chunk_id, "missing"], "model-a")

    assert list(rows) == [chunk_id]
    assert rows[chunk_id]["dimension"] == 2
    assert rows[chunk_id]["vector"] == b"\x00\x01"


def test_semantic_segment_metadata_and_embeddings_roundtrip(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document(content="长正文")
    index.sync([document])
    parent_id = document.chunks[0].chunk_id
    segment = (
        "segment-1", parent_id, 0, 3, 6, "hash-1", "长正文",
    )

    assert index.sync_semantic_segments([segment]) == 1
    rows = index.load_semantic_segments([parent_id])
    assert rows[0]["segment_id"] == "segment-1"
    assert rows[0]["text"] == "长正文"

    assert index.upsert_semantic_embeddings([
        ("segment-1", "model-a", 2, b"\x00\x01"),
    ]) == 1
    loaded = index.load_semantic_embeddings(["segment-1"], "model-a")
    assert loaded["segment-1"]["vector"] == b"\x00\x01"


def test_semantic_segment_sync_removes_stale_children(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document = _document(content="长正文")
    index.sync([document])
    parent_id = document.chunks[0].chunk_id
    index.sync_semantic_segments([
        ("segment-1", parent_id, 0, 3, 4, "hash-1", "长"),
        ("segment-2", parent_id, 1, 4, 5, "hash-2", "正"),
    ])

    index.sync_semantic_segments([
        ("segment-2", parent_id, 0, 3, 5, "hash-2", "正文"),
    ])

    rows = index.load_semantic_segments([parent_id])
    assert [row["segment_id"] for row in rows] == ["segment-2"]


def test_document_id_normalizes_separators_but_rejects_escape(tmp_path):
    assert document_id_for("nested\\guide.md") == document_id_for("nested/guide.md")
    with pytest.raises(ValueError):
        document_id_for("../outside.md")


def test_invalid_batch_does_not_partially_replace_existing_index(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    old = _document(content="旧正文")
    index.sync([old])
    invalid = IndexDocument(
        document_id=document_id_for("broken.md"),
        relative_path="broken.md",
        sha256="0" * 64,
        mtime_ns=1,
        chars=2,
        chunks=(IndexChunk(
            document_id=document_id_for("other.md"),
            ordinal=0,
            label="错误",
            content="坏数据",
            start_char=0,
            end_char=3,
        ),),
    )

    with pytest.raises(ValueError, match="chunk document_id"):
        index.sync([_document(content="新正文"), invalid])

    assert index.get_document("guide.md")["sha256"] == old.sha256
    assert index.get_document("broken.md") is None


def test_fts5_search_returns_stable_chunk_and_document_metadata(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    document_id = document_id_for("guide.md")
    chunk = IndexChunk(
        document_id=document_id,
        ordinal=0,
        label="安装指南",
        content="使用启动器安装游戏",
        start_char=0,
        end_char=9,
    )
    index.sync([IndexDocument(
        document_id=document_id,
        relative_path="guide.md",
        sha256="a" * 64,
        mtime_ns=1,
        chars=9,
        chunks=(chunk,),
    )])

    rows = index.search_lexical("安装")

    assert rows and rows[0]["chunk_id"] == chunk.chunk_id
    assert rows[0]["document_id"] == document_id
    assert rows[0]["relative_path"] == "guide.md"


def test_fts5_is_replaced_with_document_changes_and_query_is_escaped(tmp_path):
    index = KnowledgeIndex(tmp_path / "knowledge_index.sqlite3")
    old = _document(content="旧内容")
    index.sync([old])
    new = _document(content="新内容")

    index.sync([new])

    assert index.search_lexical("旧内容") == []
    assert index.search_lexical('" OR *') == []
    assert index.search_lexical("新内容")[0]["content"] == "新内容"


def test_fts5_content_corruption_is_rebuilt_even_when_row_count_matches(tmp_path):
    """FTS 派生词损坏时，重启不能把主表的有效 chunk 静默变成空召回。"""
    path = tmp_path / "knowledge_index.sqlite3"
    document = _document(content="使用启动器安装游戏")
    first = KnowledgeIndex(path)
    first.sync([document])
    assert first.search_lexical("安装")

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chunks_fts SET terms = 'word_corrupted'")

    recovered = KnowledgeIndex(path)

    assert recovered.search_lexical("安装")[0]["content"] == "使用启动器安装游戏"
