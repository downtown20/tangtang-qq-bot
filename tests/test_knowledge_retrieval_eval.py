import json

from tools.knowledge_retrieval_eval import evaluate_cases, extract_sources, load_cases


def test_extract_sources_preserves_unique_source_order():
    result = "### guide — install\ntext\n\n### guide — play\nmore\n### other\ntext"
    assert extract_sources(result) == ["guide", "other"]


def test_extract_sources_ignores_markdown_headings_from_chunk_body():
    result = "### guide — install\ntext\n\n### 正文里的小标题\nmore"
    assert extract_sources(result, {"guide"}) == ["guide"]


def test_load_cases_rejects_missing_expectation(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [{"query": "x"}]}), encoding="utf-8")
    try:
        load_cases(path)
    except ValueError as exc:
        assert "expect_hit" in str(exc)
    else:
        raise AssertionError("invalid baseline should fail closed")


def test_evaluate_cases_reports_positive_and_negative_metrics(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装\n用启动器安装\n", encoding="utf-8",
    )
    cases = [
        {"id": "hit", "query": "怎么安装", "expect_hit": True,
         "expected_sources": ["guide"]},
        {"id": "miss", "query": "完全无关的zzzz", "expect_hit": False,
         "expected_sources": []},
    ]
    report = evaluate_cases(tmp_path, cases)
    assert report["failed_count"] == 0
    assert report["positive_hit_rate"] == 1.0
    assert report["negative_empty_rate"] == 1.0
    assert report["retrieval_mode"] == "structured_evidence"
    assert report["ranking"]["hit_at_1"] == 1.0
    assert report["ranking"]["hit_at_3"] == 1.0
    assert report["ranking"]["mrr"] == 1.0
    assert report["cases"][0]["expected_rank"] == 1
    assert report["cases"][0]["latency_ms"] >= 0
    assert report["latency"]["count"] == 2
    assert report["latency"]["p95_ms"] >= report["latency"]["p50_ms"]


def test_evaluate_cases_separates_first_sample_from_steady_state(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装\n用启动器安装\n", encoding="utf-8",
    )
    cases = [
        {"id": "hit-1", "query": "怎么安装", "expect_hit": True,
         "expected_sources": ["guide"]},
        {"id": "hit-2", "query": "如何安装", "expect_hit": True,
         "expected_sources": ["guide"]},
    ]

    report = evaluate_cases(tmp_path, cases)

    assert report["latency"]["cold"]["count"] == 1
    assert report["latency"]["steady_state"]["count"] == 1
    assert report["latency"]["cold"]["p95_ms"] >= 0
    assert report["latency"]["steady_state"]["p50_ms"] >= 0


def test_evaluate_cases_reports_semantic_warmup_separately(tmp_path):
    (tmp_path / "guide.md").write_text(
        "## 安装\n用启动器安装\n", encoding="utf-8",
    )

    class FakeEmbed:
        ready = True
        fingerprint = "eval-warmup-v1"
        dimension = 2

        def encode(self, _text):
            return [1.0, 0.0]

        def encode_batch(self, texts):
            return [[1.0, 0.0] for _ in texts]

    report = evaluate_cases(
        tmp_path,
        [{"id": "hit", "query": "怎么安装", "expect_hit": True,
          "expected_sources": ["guide"]}],
        embed_engine=FakeEmbed(),
    )

    assert report["warmup_ms"] >= 0
    assert report["latency"]["count"] == 1
