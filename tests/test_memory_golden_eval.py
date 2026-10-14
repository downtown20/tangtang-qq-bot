from collections import Counter

from tools.记忆黄金集评测 import (
    DIMENSIONS,
    DEFAULT_CASES,
    evaluate_cases,
    load_cases,
)


def test_golden_set_covers_ten_cases_for_each_required_dimension():
    cases = load_cases(DEFAULT_CASES)

    assert Counter(case["dimension"] for case in cases) == {
        dimension: 10 for dimension in DIMENSIONS
    }


def test_golden_evaluator_runs_all_cases_on_isolated_temp_databases(tmp_path):
    cases = load_cases(DEFAULT_CASES)
    report = evaluate_cases(cases, tmp_path)

    # 计数从 case 文件**推导**，不写死数字——
    # 2026-10-14 T3.2 加 relative_time 维度时，原来是 `== 70` 的硬编码当场变红。
    # 硬编码计数的代价不是「改一次」，是「每次加维度都假红一次」，
    # 而假红会训练人忽略红。真正要钉的是**每条都跑了且都过了**。
    expected_counts = Counter(case["dimension"] for case in cases)
    assert report["case_count"] == len(cases)
    assert report["failed_count"] == 0
    assert all(
        report["dimensions"][dimension]["passed"] == expected_counts[dimension]
        for dimension in DIMENSIONS
    )
    assert all(
        row["database"].startswith(str(tmp_path))
        for row in report["cases"]
    )
