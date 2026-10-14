"""可重复的知识库检索基线评估。

默认只跑与线上降级链一致的词法检索，避免评估脚本因为加载大模型而改变
基线含义；需要验证 BGE 语义轮时显式传 ``--semantic``。脚本只读知识库，
不会写入索引或修改运行时状态。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 直接以 ``python tools/knowledge_retrieval_eval.py`` 运行时，Python 会把
# tools/ 放在 sys.path 首位；显式补上项目根，保证与项目测试入口一致。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.knowledge import KnowledgeBase


DEFAULT_CASES = Path(__file__).with_name("knowledge_retrieval_baseline.json")


def _latency_summary(samples: list[float]) -> dict[str, Any]:
    """Return deterministic percentile evidence for a baseline run."""
    if not samples:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(float(value) for value in samples)

    def percentile(fraction: float) -> float:
        index = round((len(ordered) - 1) * fraction)
        return round(ordered[max(0, min(len(ordered) - 1, index))], 3)

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(ordered[-1], 3),
    }


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """加载并校验基线问句，拒绝静默跳过坏样本。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("基线文件必须是包含 cases 数组的 JSON 对象")
    cases: list[dict[str, Any]] = []
    for index, case in enumerate(payload["cases"], start=1):
        if not isinstance(case, dict):
            raise ValueError(f"第 {index} 条基线不是对象")
        query = str(case.get("query", "")).strip()
        if not query:
            raise ValueError(f"第 {index} 条基线缺少 query")
        if not isinstance(case.get("expect_hit"), bool):
            raise ValueError(f"第 {index} 条基线的 expect_hit 必须是 bool")
        expected_sources = case.get("expected_sources", [])
        if not isinstance(expected_sources, list) or not all(
            isinstance(source, str) and source.strip() for source in expected_sources
        ):
            raise ValueError(f"第 {index} 条基线的 expected_sources 必须是字符串数组")
        cases.append({
            "id": str(case.get("id") or f"case-{index}"),
            "query": query,
            "expect_hit": case["expect_hit"],
            "expected_sources": [source.strip() for source in expected_sources],
        })
    return cases


def extract_sources(result: str, known_sources: set[str] | None = None) -> list[str]:
    """从当前兼容的文本结果提取来源名，不改变线上返回格式。

    结果正文自身可能包含 Markdown ``###`` 标题；有知识库来源注册表时只
    接受已注册的文件 stem，避免把正文标题误报成来源。
    """
    sources: list[str] = []
    for line in result.splitlines():
        if not line.startswith("### "):
            continue
        source = line[4:].split(" — ", 1)[0].strip()
        if known_sources is not None and source not in known_sources:
            continue
        if source and source not in sources:
            sources.append(source)
    return sources


def evaluate_cases(
    knowledge_dir: str | Path,
    cases: list[dict[str, Any]],
    *,
    embed_engine=None,
) -> dict[str, Any]:
    """执行基线并返回机器可读结果；异常样本按失败记录而非丢弃。

    优先评估结构化证据旁路，以便记录稳定来源和排名；旧 KnowledgeBase
    替身没有该接口时仍回退到兼容文本搜索。
    """
    kb = KnowledgeBase(str(knowledge_dir))
    rows: list[dict[str, Any]] = []
    latency_samples: list[float] = []
    warmup_ms: float | None = None
    if embed_engine is not None and getattr(embed_engine, "ready", False):
        warmup_started = time.perf_counter()
        kb.warm_embeddings(embed_engine)
        warmup_ms = round((time.perf_counter() - warmup_started) * 1000, 3)
    structured = callable(getattr(kb, "search_evidence", None))
    for case in cases:
        started = time.perf_counter()
        evidence = []
        if structured:
            evidence = kb.search_evidence(
                case["query"], embed_engine=embed_engine,
            )
            result = kb.format_evidence(evidence)
            sources = list(dict.fromkeys(hit.source for hit in evidence))
        else:
            result = kb.search(case["query"], embed_engine=embed_engine)
            sources = extract_sources(result, {chunk.source for chunk in kb.chunks})
        hit = bool(evidence) if structured else bool(result)
        source_ok = not case["expected_sources"] or any(
            expected in sources for expected in case["expected_sources"]
        )
        expected_rank = next(
            (
                index
                for index, item in enumerate(evidence, start=1)
                if not case["expected_sources"]
                or item.source in case["expected_sources"]
            ),
            None,
        ) if structured else None
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "expect_hit": case["expect_hit"],
            "hit": hit,
            "sources": sources,
            "ranked_sources": [item.source for item in evidence],
            "match_types": [item.match_type for item in evidence],
            "expected_rank": expected_rank,
            "chars": len(result),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "passed": hit == case["expect_hit"] and source_ok,
            "source_ok": source_ok,
        })
        latency_samples.append(rows[-1]["latency_ms"])

    positives = [row for row in rows if row["expect_hit"]]
    negatives = [row for row in rows if not row["expect_hit"]]
    ranked_positives = [
        row for row in positives if row["expected_rank"] is not None
    ]
    hit_at_1 = sum(row["expected_rank"] <= 1 for row in ranked_positives)
    hit_at_3 = sum(row["expected_rank"] <= 3 for row in ranked_positives)
    reciprocal_rank = sum(
        1.0 / row["expected_rank"] for row in ranked_positives
    )
    ranking = {
        "positive_count": len(positives),
        "ranked_positive_count": len(ranked_positives),
        "hit_at_1": hit_at_1 / len(positives)
        if structured and positives else None,
        "hit_at_3": hit_at_3 / len(positives)
        if structured and positives else None,
        "mrr": reciprocal_rank / len(positives)
        if structured and positives else None,
    }
    # 首条样本通常包含 jieba/SQLite/模型懒加载等冷路径；把它和后续稳态
    # 分开报告，避免用一次冷启动尖峰误判线上检索尾延迟。
    latency = _latency_summary(latency_samples)
    latency["cold"] = _latency_summary(latency_samples[:1])
    latency["steady_state"] = _latency_summary(latency_samples[1:])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knowledge_dir": str(Path(knowledge_dir)),
        "retrieval_mode": "structured_evidence" if structured else "legacy_text",
        "file_count": kb._file_count,
        "chunk_count": len(kb.chunks),
        "case_count": len(rows),
        "passed_count": sum(row["passed"] for row in rows),
        "failed_count": sum(not row["passed"] for row in rows),
        "positive_hit_rate": (
            sum(row["hit"] for row in positives) / len(positives)
            if positives else None
        ),
        "negative_empty_rate": (
            sum(not row["hit"] for row in negatives) / len(negatives)
            if negatives else None
        ),
        "ranking": ranking,
        "warmup_ms": warmup_ms,
        "latency": latency,
        "cases": rows,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-dir", default="knowledge")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--semantic", action="store_true", help="加载 BGE 并验证语义轮")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cases = load_cases(args.cases)
        embed_engine = None
        if args.semantic:
            from agent.embeddings import EmbeddingEngine

            embed_engine = EmbeddingEngine()
            embed_engine.load()
        report = evaluate_cases(args.knowledge_dir, cases, embed_engine=embed_engine)
    except Exception as exc:
        print(f"knowledge baseline failed: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "knowledge baseline: "
            f"{report['passed_count']}/{report['case_count']} passed | "
            f"positive_hit_rate={report['positive_hit_rate']} | "
            f"negative_empty_rate={report['negative_empty_rate']} | "
            f"hit@1={report['ranking']['hit_at_1']} | "
            f"hit@3={report['ranking']['hit_at_3']} | "
            f"mrr={report['ranking']['mrr']} | "
            f"warmup_ms={report['warmup_ms']} | "
            f"latency_p50_ms={report['latency']['p50_ms']} | "
            f"latency_p95_ms={report['latency']['p95_ms']} | "
            f"files={report['file_count']} chunks={report['chunk_count']}"
        )
        for row in report["cases"]:
            status = "PASS" if row["passed"] else "FAIL"
            print(f"[{status}] {row['id']}: {row['query']} -> {row['sources']}")
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
