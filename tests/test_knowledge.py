"""
测试知识库检索（2026-08-14 修复）：
- source 命中（2分）应召回——此前 min_score=3 门槛把 2 分档全过滤
- 英文大小写不敏感（"qq" 命中 "QQ"）
- label 命中（3分）召回；单 content 命中（1分）过滤防噪音
"""

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from agent.knowledge import KnowledgeBase
from agent.knowledge_index import document_id_for
from agent.handler import MessageHandler


@pytest.fixture
def kb(tmp_path):
    (tmp_path / "minecraft.md").write_text(
        "## 安装\n用启动器下载游戏\n\n## 玩法\n合成需要工作台\n", encoding="utf-8")
    (tmp_path / "snowluma.md").write_text(
        "## QQ机器人\nSnowLuma 是 QQ 机器人框架\n", encoding="utf-8")
    (tmp_path / "杂谈.md").write_text(
        "## 闲聊\n今天天气不错\n", encoding="utf-8")
    return KnowledgeBase(str(tmp_path))


class TestSearch:
    def test_source_only_hit_recalled(self, kb):
        """查询词只在文件名出现（source=2分）→ 必须召回（旧门槛 3 会漏）"""
        r = kb.search("minecraft怎么玩")
        assert "minecraft" in r
        assert "合成" in r  # 相关块内容被带出

    def test_case_insensitive_english(self, kb):
        r = kb.search("QQ机器人怎么搭")
        assert "snowluma" in r

    def test_label_hit(self, kb):
        r = kb.search("怎么安装")
        assert "安装" in r

    def test_single_content_hit_filtered(self, kb):
        """单个 content 命中（1分）→ 噪音，不召回"""
        r = kb.search("启动器")
        assert r == ""

    def test_irrelevant_returns_empty(self, kb):
        assert kb.search("完全无关的话题zzzz") == ""

    def test_keyword_matching_is_case_insensitive_on_chunk_fields(self, tmp_path):
        (tmp_path / "manual.md").write_text(
            "## API\nUse HTTPX for the API client\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))
        assert "HTTPX" in kb.search("httpx API")

    def test_expansion_matching_is_case_insensitive(self, tmp_path):
        (tmp_path / "manual.md").write_text(
            "> 搜词：OpenAI API\n\n## Guide\nUse the API\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))
        assert any(chunk.label == "Guide" for chunk in kb._expansion_hits("openai api"))

    def test_strong_keyword_evidence_is_kept_ahead_of_rrf_candidates(self, tmp_path):
        (tmp_path / "openai.md").write_text(
            "## OpenAI\nOpenAI API setup details\n", encoding="utf-8",
        )
        (tmp_path / "other.md").write_text(
            "## General\nOpenAI related discussion\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))

        class Reranker:
            ready = True

            def rerank(self, _message, texts, top_k=3):
                return [(0.99, min(1, len(texts) - 1))]

        result = kb.search("OpenAI API", reranker=Reranker())
        assert result.index("openai") < result.index("other")

    def test_semantic_fallback_preserves_scores_between_threshold_and_integer_cutoff(self, tmp_path):
        (tmp_path / "manual.md").write_text(
            "## Guide\nA semantic match at moderate confidence\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))

        class Embed:
            ready = True

            def encode(self, _text):
                return [0.6]

            def encode_batch(self, texts):
                return [[1.0] for _ in texts]

        result = kb.search("unrelated phrasing", embed_engine=Embed())
        assert "semantic match" in result

    def test_generic_negative_query_does_not_recall_knowledge(self, tmp_path):
        """泛词/否定句不能仅凭标题或临界向量误召回知识。"""
        (tmp_path / "manual.md").write_text(
            "## 常见问题\n系统功能介绍\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))

        class BorderlineEmbed:
            ready = True

            def encode(self, _text):
                return np.array([0.56], dtype=np.float32)

            def encode_batch(self, texts):
                return [np.array([1.0], dtype=np.float32) for _ in texts]

        assert kb.search(
            "完全不存在的随机问题", embed_engine=BorderlineEmbed(),
        ) == ""

    def test_casual_chat_query_does_not_enter_semantic_route(self, tmp_path):
        """没有实质检索词的闲聊不得被语义模型强行映射到知识。"""
        (tmp_path / "manual.md").write_text(
            "## 聊天能力\n可以陪你聊天\n", encoding="utf-8",
        )
        kb = KnowledgeBase(str(tmp_path))

        class HighSimilarityEmbed:
            ready = True

            def encode(self, _text):
                return np.array([0.9], dtype=np.float32)

            def encode_batch(self, texts):
                return [np.array([1.0], dtype=np.float32) for _ in texts]

        assert kb.search("我想随便聊聊天", embed_engine=HighSimilarityEmbed()) == ""


def _make_handler(kb):
    h = object.__new__(MessageHandler)
    h.knowledge = kb
    h.embed_engine = None
    return h


class TestSkillSearchKnowledge:
    """search_knowledge 技能——知识检索由 LLM 自主调用（2026-08-14 工具化）"""

    def test_hit_returns_fragments(self, kb):
        h = _make_handler(kb)
        r = asyncio.run(h._skill_search_knowledge("minecraft怎么玩"))
        assert "minecraft" in r

    def test_miss_honest(self, kb):
        h = _make_handler(kb)
        r = asyncio.run(h._skill_search_knowledge("完全不相关的zzzz"))
        assert "没找到" in r

    def test_empty_query(self, kb):
        h = _make_handler(kb)
        r = asyncio.run(h._skill_search_knowledge("  "))
        assert "需要搜索词" in r

    def test_slow_search_does_not_block_event_loop(self):
        """知识检索的同步编码/重排不得占用前台事件循环。"""
        class SlowKnowledge:
            def search(self, *_args, **_kwargs):
                time.sleep(0.05)
                return "### guide\nresult"

        h = object.__new__(MessageHandler)
        h.knowledge = SlowKnowledge()
        h.embed_engine = None
        h.reranker = None

        async def scenario():
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                result = await h._skill_search_knowledge("guide")
            finally:
                stop.set()
                await task
            return result, ticks

        result, ticks = asyncio.run(scenario())
        assert "result" in result
        assert ticks > 0

    def test_slow_structured_search_does_not_block_event_loop(self):
        """结构化 FTS/Dense/RRF 检索同样必须在线程边界执行。"""
        class SlowStructuredKnowledge:
            def search_evidence(self, *_args, **_kwargs):
                time.sleep(0.05)
                return ["evidence"]

            def format_evidence(self, evidence):
                return "result:" + ",".join(evidence)

        h = object.__new__(MessageHandler)
        h.knowledge = SlowStructuredKnowledge()
        h.embed_engine = None
        h.reranker = None

        async def scenario():
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                result = await h._skill_search_knowledge("guide")
            finally:
                stop.set()
                await task
            return result, ticks

        result, ticks = asyncio.run(scenario())
        assert result == "result:evidence"
        assert ticks > 0

    def test_structured_search_failure_falls_back_to_legacy_path(self):
        class BrokenStructuredKnowledge:
            def search_evidence(self, *_args, **_kwargs):
                raise RuntimeError("fts unavailable")

            def format_evidence(self, _evidence):
                return ""

            def search(self, *_args, **_kwargs):
                return "legacy result"

        h = object.__new__(MessageHandler)
        h.knowledge = BrokenStructuredKnowledge()
        h.embed_engine = None
        h.reranker = None

        result = asyncio.run(h._skill_search_knowledge("guide"))

        assert result == "legacy result"


def test_read_document_uses_same_recursive_registry_and_sensitive_acl(tmp_path):
    public = tmp_path / "nested"
    public.mkdir()
    (public / "guide.txt").write_text("nested text document", encoding="utf-8")
    sensitive = tmp_path / "adult"
    sensitive.mkdir()
    (sensitive / "secret.md").write_text("must stay hidden", encoding="utf-8")

    handler = object.__new__(MessageHandler)
    ok, content = asyncio.run(handler._load_document_content(
        "guide", str(tmp_path),
    ))
    denied, error = asyncio.run(handler._load_document_content(
        "secret", str(tmp_path),
    ))

    assert ok is True
    assert "nested text document" in content
    assert denied is False
    assert "must stay hidden" not in error


def test_read_document_file_io_does_not_block_event_loop(tmp_path):
    """LLM 读文档技能读取全文时不能同步占用事件循环。"""
    (tmp_path / "guide.md").write_text("nested text document", encoding="utf-8")
    handler = object.__new__(MessageHandler)
    main_thread = threading.get_ident()
    reads = []
    original_read = Path.read_text

    def slow_read(path_obj, *args, **kwargs):
        reads.append(threading.get_ident())
        time.sleep(0.05)
        return original_read(path_obj, *args, **kwargs)

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            result = await handler._load_document_content("guide", str(tmp_path))
        finally:
            stopped = True
            await task
        return ticks, result

    with patch.object(Path, "read_text", slow_read):
        ticks, (ok, content) = asyncio.run(scenario())

    assert ok is True
    assert "nested text document" in content
    assert ticks > 0
    assert reads and all(thread_id != main_thread for thread_id in reads)


def test_knowledge_registry_has_stable_doc_id_and_content_hash(tmp_path):
    (tmp_path / "guide.md").write_text("stable content", encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    entry = kb._document_registry["guide.md"]

    assert len(entry["doc_id"]) == 16
    assert len(entry["sha256"]) == 64
    assert entry["chars"] == len("stable content")


def test_knowledge_load_assigns_persistent_chunk_metadata(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装\n用启动器安装\n\n## 玩法\n合成需要工作台\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    assert kb._index.snapshot()["documents"] == 1
    assert kb._index.snapshot()["chunks"] == len(kb.chunks)
    assert all(chunk.document_id == document_id_for("guide.md") for chunk in kb.chunks)
    assert all(chunk.chunk_id and chunk.start_char >= 0 for chunk in kb.chunks)
    assert all(chunk.end_char >= chunk.start_char for chunk in kb.chunks)


def test_knowledge_reload_reuses_chunk_ids_for_unchanged_document(tmp_path):
    (tmp_path / "guide.md").write_text("## 安装\n用启动器安装\n", encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    first = [(chunk.chunk_id, chunk.start_char, chunk.end_char) for chunk in kb.chunks]

    kb.reload()

    assert [(chunk.chunk_id, chunk.start_char, chunk.end_char) for chunk in kb.chunks] == first


def test_knowledge_reload_and_search_are_consistent(tmp_path):
    """热重载期间检索必须等待完整快照，不能返回瞬时空结果。"""
    (tmp_path / "guide.md").write_text("## 安装\n用启动器安装\n", encoding="utf-8")
    kb = KnowledgeBase(str(tmp_path))
    original_load = kb._load
    load_started = threading.Event()
    release_load = threading.Event()

    def slow_load():
        kb.chunks = []
        load_started.set()
        release_load.wait(timeout=2)
        original_load()

    kb._load = slow_load
    reload_thread = threading.Thread(target=kb.reload)
    reload_thread.start()
    assert load_started.wait(timeout=1)

    result: list[str] = []
    search_done = threading.Event()

    def search():
        result.append(kb.search("怎么安装"))
        search_done.set()

    search_thread = threading.Thread(target=search)
    search_thread.start()
    assert search_done.wait(timeout=0.1) is False

    release_load.set()
    reload_thread.join(timeout=2)
    search_thread.join(timeout=2)
    assert reload_thread.is_alive() is False
    assert search_thread.is_alive() is False
    assert result and "安装" in result[0]


def test_knowledge_reuses_persisted_embeddings_after_restart(tmp_path):
    (tmp_path / "guide.md").write_text("## 安装\n用启动器安装\n", encoding="utf-8")

    class PersistentFakeEmbed:
        ready = True
        fingerprint = "fake-embedding-v1"
        dimension = 2

        def __init__(self):
            self.batch_calls = 0

        def encode_batch(self, texts):
            self.batch_calls += 1
            return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        def encode(self, _text):
            return np.array([1.0, 0.0], dtype=np.float32)

    first_kb = KnowledgeBase(str(tmp_path))
    first_embed = PersistentFakeEmbed()
    first_kb.warm_embeddings(first_embed)
    assert first_embed.batch_calls == 1

    second_kb = KnowledgeBase(str(tmp_path))
    second_embed = PersistentFakeEmbed()
    second_kb.warm_embeddings(second_embed)

    assert second_embed.batch_calls == 0
    assert all(getattr(chunk, "_embedding", None) is not None for chunk in second_kb.chunks)


def test_knowledge_reuses_persisted_semantic_segment_embeddings_after_restart(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 长指南\n" + "。".join(f"背景{i}" for i in range(220)),
        encoding="utf-8",
    )

    class PersistentFakeEmbed:
        ready = True
        fingerprint = "semantic-segment-v1"
        dimension = 2

        def __init__(self):
            self.batch_calls = 0

        def encode_batch(self, texts):
            self.batch_calls += 1
            return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        def encode(self, _text):
            return np.array([1.0, 0.0], dtype=np.float32)

    first_kb = KnowledgeBase(str(tmp_path))
    first_embed = PersistentFakeEmbed()
    first_kb.warm_embeddings(first_embed)
    assert first_embed.batch_calls == 2

    second_kb = KnowledgeBase(str(tmp_path))
    second_embed = PersistentFakeEmbed()
    second_kb.warm_embeddings(second_embed)

    assert second_embed.batch_calls == 0
    assert second_kb._semantic_segments
    assert all(segment.embedding is not None for segment in second_kb._semantic_segments)


def test_knowledge_reload_replaces_changed_semantic_segments_without_orphans(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text(
        "## 长指南\n" + "。".join(f"旧事实{i}" for i in range(220)),
        encoding="utf-8",
    )

    class PersistentFakeEmbed:
        ready = True
        fingerprint = "semantic-reload-v1"
        dimension = 2

        def encode_batch(self, texts):
            return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        def encode(self, _text):
            return np.array([1.0, 0.0], dtype=np.float32)

    kb = KnowledgeBase(str(tmp_path))
    kb.warm_embeddings(PersistentFakeEmbed())
    first_ids = {segment.segment_id for segment in kb._semantic_segments}

    path.write_text(
        "## 长指南\n" + "。".join(f"新事实{i}" for i in range(220)),
        encoding="utf-8",
    )
    kb.reload()
    kb.warm_embeddings(PersistentFakeEmbed())
    second_ids = {segment.segment_id for segment in kb._semantic_segments}

    assert first_ids
    assert second_ids
    assert first_ids.isdisjoint(second_ids)
    rows = kb._index.load_semantic_embeddings(
        list(first_ids), "semantic-reload-v1",
    )
    assert rows == {}


def test_knowledge_persists_new_embeddings_through_batch_index_api(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装\n用启动器安装\n\n## 玩法\n合成需要工作台\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    class BatchOnlyIndex:
        fts_available = False

        def __init__(self):
            self.rows = []

        def load_embeddings(self, _chunk_ids, _fingerprint):
            return {}

        def upsert_embeddings(self, rows):
            self.rows.extend(rows)
            return len(rows)

        def upsert_embedding(self, *_args):
            raise AssertionError("embedding persistence must use the batch API")

    class FakeEmbed:
        ready = True
        fingerprint = "batch-test-v1"
        dimension = 2

        def encode_batch(self, texts):
            return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

        def encode(self, _text):
            return np.array([1.0, 0.0], dtype=np.float32)

    index = BatchOnlyIndex()
    kb._index = index
    kb.warm_embeddings(FakeEmbed())

    assert len(index.rows) == len(kb.chunks)


def test_knowledge_does_not_reuse_vector_from_a_different_model_profile(tmp_path):
    (tmp_path / "guide.md").write_text("## 安装\n用启动器安装\n", encoding="utf-8")

    class ProfileEmbed:
        ready = True
        dimension = 2

        def __init__(self, fingerprint, vector):
            self.fingerprint = fingerprint
            self.vector = np.array(vector, dtype=np.float32)
            self.batch_calls = 0

        def encode_batch(self, texts):
            self.batch_calls += 1
            return [self.vector.copy() for _ in texts]

        def encode(self, _text):
            return self.vector.copy()

    kb = KnowledgeBase(str(tmp_path))
    first = ProfileEmbed("profile-a", [1.0, 0.0])
    second = ProfileEmbed("profile-b", [0.0, 1.0])
    kb.warm_embeddings(first)
    kb.warm_embeddings(second)

    assert second.batch_calls == 1
    assert np.array_equal(kb.chunks[0]._embedding, second.vector)


def test_knowledge_does_not_reuse_vector_when_model_fingerprint_is_missing(tmp_path):
    """无指纹引擎不能证明跨实例同模，必须重新编码避免串模。"""
    (tmp_path / "guide.md").write_text("## 安装\n用启动器安装\n", encoding="utf-8")

    class UnfingerprintedEmbed:
        ready = True
        fingerprint = ""
        dimension = 2

        def __init__(self, vector):
            self.vector = np.array(vector, dtype=np.float32)
            self.batch_calls = 0

        def encode_batch(self, texts):
            self.batch_calls += 1
            return [self.vector.copy() for _ in texts]

        def encode(self, _text):
            return self.vector.copy()

    kb = KnowledgeBase(str(tmp_path))
    first = UnfingerprintedEmbed([1.0, 0.0])
    second = UnfingerprintedEmbed([0.0, 1.0])
    kb.warm_embeddings(first)
    kb.warm_embeddings(second)

    assert first.batch_calls == 1
    assert second.batch_calls == 1
    assert np.array_equal(kb.chunks[0]._embedding, second.vector)


def test_skill_search_knowledge_exposes_structured_evidence_metadata(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    handler = _make_handler(KnowledgeBase(str(tmp_path)))
    handler.reranker = None

    result = asyncio.run(handler._skill_search_knowledge("安装"))

    assert "chunk_id=" in result
    assert "document_id=" in result
    assert "offset=" in result
    assert "score=" in result
    assert "使用启动器安装游戏" in result
