import json
import logging
import concurrent.futures
import threading

import numpy as np

from agent.knowledge import KnowledgeBase, KnowledgeEvidence


def test_lexical_search_readers_can_overlap(tmp_path):
    """只读检索不应被另一条只读检索串行阻塞。"""
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0

    def slow_search(_message, embed_engine=None, reranker=None):
        nonlocal active
        with state_lock:
            active += 1
            if active == 2:
                entered.set()
        if not entered.wait(timeout=1.0):
            raise AssertionError("只读检索被意外串行化")
        if not release.wait(timeout=1.0):
            raise AssertionError("测试未释放检索读锁")
        return []

    kb._search_evidence_unlocked = slow_search
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(kb.search_evidence, "安装") for _ in range(2)]
        assert entered.wait(timeout=1.0)
        release.set()
        assert [future.result(timeout=1.0) for future in futures] == [[], []]


def test_warmed_semantic_search_readers_can_overlap(tmp_path):
    """embedding 已预热后，语义评分阶段不应继续占用写锁。"""
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    class Embed:
        ready = True
        fingerprint = "semantic-read-overlap-v1"
        dimension = 1

        def encode_batch(self, texts):
            return [np.array([1.0], dtype=np.float32) for _ in texts]

        def encode(self, _text):
            return np.array([1.0], dtype=np.float32)

    embed = Embed()
    kb.warm_embeddings(embed)
    entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0

    def slow_search(_message, embed_engine=None, reranker=None):
        nonlocal active
        with state_lock:
            active += 1
            if active == 2:
                entered.set()
        if not entered.wait(timeout=1.0):
            raise AssertionError("预热后的语义检索被意外串行化")
        if not release.wait(timeout=1.0):
            raise AssertionError("测试未释放语义检索读锁")
        return []

    kb._search_evidence_unlocked = slow_search
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(kb.search_evidence, "安装", embed_engine=embed)
                   for _ in range(2)]
        assert entered.wait(timeout=1.0)
        release.set()
        assert [future.result(timeout=1.0) for future in futures] == [[], []]


def test_structured_search_returns_stable_source_and_scores(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    hits = kb.search_evidence("安装")

    assert hits and isinstance(hits[0], KnowledgeEvidence)
    hit = hits[0]
    assert hit.source == "guide"
    assert hit.document_id
    assert hit.chunk_id
    assert hit.start_char >= 0
    assert hit.end_char > hit.start_char
    assert hit.score > 0
    assert "fts" in hit.match_type
    payload = hit.to_dict()
    assert payload["chunk_id"] == hit.chunk_id
    assert payload["source"] == "guide"
    assert payload["scores"]["rrf"] == hit.score


def test_structured_search_uses_tokenized_cjk_query_for_fts(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    hits = kb.search_evidence("怎么安装")

    assert hits
    assert "fts" in hits[0].match_type


def test_fts_single_body_term_does_not_override_evidence_gate(tmp_path):
    (tmp_path / "features.md").write_text(
        "## 功能\n糖糖可以查天气和翻译\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    assert kb.search_evidence("今天天气怎么样") == []


def test_generic_source_token_does_not_create_knowledge_evidence(tmp_path):
    """泛词“系统”只命中文件名时，不应把无关文档当作证据。"""
    (tmp_path / "语音系统.md").write_text(
        "## 语音\n这里只记录语音配置\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    assert kb.search_evidence("完全不存在的随机天气系统") == []


def test_generic_process_token_does_not_create_knowledge_evidence(tmp_path):
    """泛词“流程”只命中知识文档的通用标题时，不应误召回。"""
    (tmp_path / "记忆系统技术文档.md").write_text(
        "## 提取流程\n这里只记录记忆提取\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    assert kb.search_evidence("医院预约流程") == []


def test_long_body_concept_is_strong_enough_for_evidence(tmp_path):
    """四字正文概念是实质查询词，不应被单正文词门槛误杀。"""
    (tmp_path / "思维框架.md").write_text(
        "## 方法\n遇到复杂问题先找主要矛盾，再处理次要矛盾。\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    hits = kb.search_evidence("主要矛盾怎么用")

    assert hits and hits[0].source == "思维框架"


def test_structured_search_does_not_use_semantic_only_for_casual_query(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 聊天能力\n可以陪你聊天\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    class HighSimilarityEmbed:
        ready = True

        def encode(self, _text):
            return np.array([0.9], dtype=np.float32)

        def encode_batch(self, texts):
            return [np.array([1.0], dtype=np.float32) for _ in texts]

    assert kb.search_evidence(
        "我想随便聊聊天", embed_engine=HighSimilarityEmbed(),
    ) == []


def test_structured_search_uses_multiple_retrieval_paths_in_match_type(tmp_path):
    (tmp_path / "manual.md").write_text(
        "## API 指南\nHTTPX API 的安装和使用步骤\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    hits = kb.search_evidence("HTTPX API")

    assert hits
    assert "fts" in hits[0].match_type
    assert "keyword" in hits[0].match_type
    assert hits[0].to_dict()["scores"]["rrf"] > 0


def test_structured_search_emits_summary_trace_without_query_text(tmp_path, caplog):
    (tmp_path / "guide.md").write_text(
        "## 安装指南\n使用启动器安装游戏\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))
    secret_query = "绝密查询词"

    with caplog.at_level(logging.INFO, logger="糖糖.Knowledge"):
        assert kb.search_evidence(secret_query) == []

    trace = next(
        record.message for record in caplog.records
        if "Knowledge Retrieval" in record.message
    )
    assert "hits=0" in trace
    assert "paths=none" in trace
    assert secret_query not in caplog.text


def test_structured_search_trace_exposes_only_match_type_and_rerank_score(
        tmp_path, caplog):
    for name, body in (("first", "安装流程甲"), ("second", "安装流程乙")):
        (tmp_path / f"{name}.md").write_text(
            f"## 安装指南\n{body}\n", encoding="utf-8",
        )
    kb = KnowledgeBase(str(tmp_path))
    secret_query = "绝密安装"

    class Reranker:
        ready = True

        def rerank(self, _query, candidates, top_k=6):
            assert len(candidates) == 2
            assert top_k == 2
            return [(1, 0.875), (0, -0.25)]

    with caplog.at_level(logging.INFO, logger="糖糖.Knowledge"):
        hits = kb.search_evidence(secret_query, reranker=Reranker())

    assert len(hits) == 2
    trace = next(
        record.message for record in caplog.records
        if "Knowledge Retrieval" in record.message
    )
    encoded = trace.split("trace=", 1)[1]
    payload = json.loads(encoded)
    assert payload == [
        {"match_type": hit.match_type, "rerank_score": hit.rerank_score}
        for hit in hits
    ]
    assert secret_query not in trace
    assert "first.md" not in trace
    assert "second.md" not in trace


def test_structured_search_fallback_ids_keep_same_named_documents_distinct(tmp_path):
    """索引不可用时，嵌套目录下同名文档仍必须拥有不同证据身份。"""
    for directory, marker in (("a", "甲"), ("b", "乙")):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "guide.md").write_text(
            f"## 安装\n{marker}内容 安装指南\n", encoding="utf-8",
        )

    knowledge = KnowledgeBase(str(tmp_path))
    # 模拟索引创建/同步不可用时的内存降级路径。
    for chunk in knowledge.chunks:
        chunk.document_id = ""
        chunk.chunk_id = ""

    evidence = knowledge.search_evidence("安装指南")

    assert {item.relative_path for item in evidence} == {"a/guide.md", "b/guide.md"}
    assert len({item.document_id for item in evidence}) == 2


def test_semantic_search_covers_tail_of_long_parent_chunk(tmp_path):
    """长父块尾部的语义事实不能因 450 字 embedding 窗口而丢失。"""
    prefix = "。".join(f"前置背景{i}" for i in range(90))
    (tmp_path / "manual.md").write_text(
        f"## 深层资料\n{prefix}。TARGET_SIGNAL 深层事实\n", encoding="utf-8",
    )
    kb = KnowledgeBase(str(tmp_path))

    class TailAwareEmbed:
        ready = True
        fingerprint = "tail-aware-v1"
        dimension = 2

        def encode(self, text):
            if str(text) == "语义别名":
                return np.array([1.0, 0.0], dtype=np.float32)
            return np.array(
                [1.0, 0.0] if "TARGET_SIGNAL" in str(text)
                else [0.0, 1.0], dtype=np.float32,
            )

        def encode_batch(self, texts):
            return [self.encode(text) for text in texts]

    hits = kb.search_evidence("语义别名", embed_engine=TailAwareEmbed())

    assert hits
    assert "TARGET_SIGNAL" in hits[0].content
