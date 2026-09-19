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
    report = evaluate_cases(load_cases(DEFAULT_CASES), tmp_path)

    assert report["case_count"] == 70
    assert report["failed_count"] == 0
    assert all(
        report["dimensions"][dimension]["passed"] == 10
        for dimension in DIMENSIONS
    )
    assert all(
        row["database"].startswith(str(tmp_path))
        for row in report["cases"]
    )
