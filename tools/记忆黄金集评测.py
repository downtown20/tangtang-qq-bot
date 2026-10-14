"""记忆真值黄金集评测器：仅在临时 SQLite fixture 上运行，绝不触碰生产库。"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.memory import MemorySystem
from agent.store import Store


DIMENSIONS = (
    "source_attribution",
    "event_time",
    "negation",
    "correction",
    "scope_isolation",
    "no_evidence",
    "restart_replay",
    # 2026-10-14 台账 T3.2：相对时间语义。
    # 判据不是「DB 里存了什么」（那由 event_time 维度管），
    # 而是「**渲染给 LLM 看的是什么**」——绝对时间戳必须附带「N 天前」，
    # 否则 LLM 要自己算时间差，算不好就把三周前当昨天。
    "relative_time",
)
DEFAULT_CASES = Path(__file__).with_name("记忆黄金集.json")


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """读取黄金集并拒绝缺维度、缺期望或重复 case。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("黄金集必须是含 cases 数组的 JSON 对象")
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    counts: Counter[str] = Counter()
    for index, raw in enumerate(payload["cases"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 条 case 不是对象")
        case_id = str(raw.get("id") or "").strip()
        dimension = str(raw.get("dimension") or "").strip()
        if not case_id or case_id in ids:
            raise ValueError(f"第 {index} 条 case id 缺失或重复")
        if dimension not in DIMENSIONS:
            raise ValueError(f"{case_id} 使用未知维度 {dimension!r}")
        if not isinstance(raw.get("expect"), dict):
            raise ValueError(f"{case_id} 缺少 expect 对象")
        if dimension != "no_evidence":
            for field in ("subject", "source_group_id", "value", "event_time"):
                if not str(raw.get(field) or "").strip():
                    raise ValueError(f"{case_id} 缺少 {field}")
        if dimension == "correction" and not str(raw.get("corrected_value") or "").strip():
            raise ValueError(f"{case_id} 缺少 corrected_value")
        if dimension == "scope_isolation":
            for field in ("other_group_id", "other_group_value", "other_subject", "other_user_value"):
                if not str(raw.get(field) or "").strip():
                    raise ValueError(f"{case_id} 缺少 {field}")
        ids.add(case_id)
        counts[dimension] += 1
        cases.append(dict(raw))
    missing = [dimension for dimension in DIMENSIONS if counts[dimension] < 10]
    if missing:
        raise ValueError(f"黄金集每个维度至少 10 条，缺少：{', '.join(missing)}")
    return cases


def _remember(store: Store, case: dict[str, Any], *, subject: str | None = None,
              source_group_id: str | None = None, value: str | None = None,
              idempotency_suffix: str = "") -> tuple[int, int | None]:
    """通过现有 Store 写接口构造一条 fixture，不调用 LLM。"""
    subject = str(subject if subject is not None else case.get("subject") or "")
    group_id = str(source_group_id if source_group_id is not None else case.get("source_group_id") or "")
    value = str(value if value is not None else case.get("value") or "")
    event_time = str(case.get("event_time") or "")
    has_evidence = case["dimension"] != "no_evidence"
    chat_id: int | None = None
    if has_evidence:
        chat_id = int(store.insert_chat(
            subject, value, group_id=group_id, is_bot=False, timestamp=event_time,
        ))
    memory_id = store.insert_memory(
        subject, "fact", value, importance=6, cognitive="semantic",
        confidence=0.95, origin="extracted", source_group_id=group_id,
        evidence_ids=str(chat_id) if chat_id is not None else "",
        event_time=event_time,
        idempotency_key=f"memory-golden:{case['id']}{idempotency_suffix}",
        evidence_quote=value if chat_id is not None else "",
        claim_type="stated",
    )
    return memory_id, chat_id


def _row_by_id(store: Store, memory_id: int) -> dict[str, Any]:
    rows = store.query_memories(limit=None, include_retracted=True)
    for row in rows:
        if int(row["id"]) == int(memory_id):
            return row
    raise AssertionError(f"memory {memory_id} disappeared from audit view")


def _trusted_values(store: Store, subject: str, source_group_id: str) -> list[str]:
    return [
        str(row["value"])
        for row in store.query_memories(
            subject, limit=None, trusted_only=True,
            source_group_id=source_group_id,
        )
    ]


def _check(errors: list[dict[str, Any]], field: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        errors.append({"field": field, "expected": expected, "actual": actual})


def _evaluate_case(case: dict[str, Any], database: Path) -> dict[str, Any]:
    store = Store(str(database))
    memory_id, chat_id = _remember(store, case)
    expected = dict(case["expect"])
    errors: list[dict[str, Any]] = []
    actual: dict[str, Any] = {}
    dimension = str(case["dimension"])
    subject = str(case.get("subject") or "")
    group_id = str(case.get("source_group_id") or "")

    if dimension == "source_attribution":
        row = _row_by_id(store, memory_id)
        evidence = store.get_memory_evidence(memory_id)
        actual = {
            "owner": row["qq_id"],
            "source_group_id": row["source_group_id"],
            "trust_level": row["trust_level"],
            "event_time": row["event_time"],
            "evidence_chat_ids": [item["chat_id"] for item in evidence],
        }
        for field in ("owner", "source_group_id", "trust_level", "event_time"):
            _check(errors, field, expected[field], actual[field])
        _check(errors, "evidence_chat_ids", [chat_id], actual["evidence_chat_ids"])
    elif dimension == "event_time":
        row = _row_by_id(store, memory_id)
        actual = {"event_time": row["event_time"], "trust_level": row["trust_level"]}
        _check(errors, "event_time", expected["event_time"], actual["event_time"])
        _check(errors, "trust_level", expected["trust_level"], actual["trust_level"])
    elif dimension == "negation":
        values = _trusted_values(store, subject, group_id)
        actual = {"active_values": values}
        _check(errors, "active_value_present", True, expected["active_value"] in values)
        _check(errors, "opposite_value_absent", True, expected["opposite_value"] not in values)
    elif dimension == "correction":
        result = MemorySystem(store=store).correct_memory(
            subject, str(case["value"]), str(case["corrected_value"]),
            source_group_id=group_id,
        )
        old = _row_by_id(store, memory_id)
        values = _trusted_values(store, subject, group_id)
        actual = {
            "old_status": old["status"],
            "active_values": values,
            "retracted": result["retracted"],
        }
        _check(errors, "old_status", expected["old_status"], actual["old_status"])
        _check(errors, "corrected_value_present", True, expected["active_value"] in values)
        _check(errors, "old_value_absent", True, str(case["value"]) not in values)
        _check(errors, "retracted", True, actual["retracted"] >= 1)
    elif dimension == "scope_isolation":
        _remember(
            store, case, source_group_id=str(case["other_group_id"]),
            value=str(case["other_group_value"]), idempotency_suffix=":other-group",
        )
        _remember(
            store, case, subject=str(case["other_subject"]),
            value=str(case["other_user_value"]), idempotency_suffix=":other-user",
        )
        values = _trusted_values(store, subject, group_id)
        actual = {"visible_values": values}
        _check(errors, "visible_values", expected["visible"], values)
        for hidden in expected["hidden"]:
            _check(errors, f"hidden:{hidden}", True, hidden not in values)
    elif dimension == "no_evidence":
        row = _row_by_id(store, memory_id)
        values = _trusted_values(store, subject, group_id)
        actual = {"trust_level": row["trust_level"], "trusted_values": values}
        _check(errors, "trust_level", expected["trust_level"], actual["trust_level"])
        _check(errors, "trusted_visible", expected["trusted_visible"], str(case["value"]) in values)
    elif dimension == "relative_time":
        # 渲染层判据：format_fact_time 输出的相对描述必须与「距 now 多久」一致。
        # 用 case 里的 fixed_now 冻结时钟——否则「刚刚」这类档位会随跑测时间漂移。
        import datetime as _dt
        from agent import protocols as _protocols
        fixed_now = _dt.datetime.strptime(str(case["fixed_now"]), "%Y-%m-%d %H:%M:%S")
        rendered = _protocols.format_fact_time(str(case["event_time"]), now=fixed_now)
        actual = {"rendered": rendered}
        _check(errors, "rendered", expected["rendered"], rendered)
        # 相对描述必须真的出现（或明确不该出现时确实没出现）
        _check(errors, "relative_present",
               bool(expected.get("relative_present")),
               bool(_protocols.humanize_age(str(case["event_time"]), now=fixed_now)))
        # 绝对时间不被丢掉（除「时间未知」的退化情形）
        if expected.get("absolute_preserved"):
            _check(errors, "absolute_preserved", True,
                   str(case["event_time"])[:16] in rendered)
    elif dimension == "restart_replay":
        reopened = Store(str(database))
        replay_id, _ = _remember(reopened, case)
        values = _trusted_values(reopened, subject, group_id)
        actual = {"same_memory_id": replay_id == memory_id, "active_count": len(values)}
        _check(errors, "same_memory_id", expected["same_memory_id"], actual["same_memory_id"])
        _check(errors, "active_count", expected["active_count"], actual["active_count"])
    else:  # load_cases() keeps this unreachable; retain fail-closed behavior.
        raise ValueError(f"unknown dimension {dimension}")

    return {
        "id": case["id"],
        "dimension": dimension,
        "database": str(database),
        "expected": expected,
        "actual": actual,
        "differences": errors,
        "passed": not errors,
    }


def evaluate_cases(cases: list[dict[str, Any]], database_dir: str | Path) -> dict[str, Any]:
    """在调用方提供的隔离目录创建每 case 一库，返回维度级评测结果。

    若目标数据库已存在即拒绝覆盖，避免任何形式的生产库误写。
    """
    root = Path(database_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        database = root / f"memory-golden-{index:02d}-{case['id']}.db"
        if database.exists():
            raise ValueError(f"refusing to overwrite existing database: {database}")
        rows.append(_evaluate_case(case, database))
    dimensions = {}
    for dimension in DIMENSIONS:
        selected = [row for row in rows if row["dimension"] == dimension]
        passed = sum(bool(row["passed"]) for row in selected)
        dimensions[dimension] = {
            "total": len(selected),
            "passed": passed,
            "failed": len(selected) - passed,
            "pass_rate": passed / len(selected) if selected else 0.0,
        }
    return {
        "case_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "failed_count": sum(not row["passed"] for row in rows),
        "dimensions": dimensions,
        "cases": rows,
    }


def run_baseline(cases_path: str | Path = DEFAULT_CASES) -> dict[str, Any]:
    """默认入口：仅使用系统临时目录，不接受生产库路径。"""
    cases = load_cases(cases_path)
    with tempfile.TemporaryDirectory(prefix="tangtang-memory-golden-") as temp_dir:
        return evaluate_cases(cases, temp_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--output", default="", help="可选 JSON 报告路径")
    args = parser.parse_args(argv)
    try:
        report = run_baseline(args.cases)
    except Exception as exc:
        print(f"memory golden evaluation failed: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(
        f"memory golden evaluation: {report['passed_count']}/{report['case_count']} passed "
        f"({len(DIMENSIONS)} dimensions)"
    )
    for row in report["cases"]:
        if not row["passed"]:
            print(f"[FAIL] {row['id']} expected={row['expected']} actual={row['actual']} differences={row['differences']}")
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
