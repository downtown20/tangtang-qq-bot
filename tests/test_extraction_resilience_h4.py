"""H4：提取状态机的小批量、公平性、故障与崩溃恢复回归。"""

import asyncio

from tools.extraction_resilience_h4 import run_h4_resilience


def test_h4_extraction_resilience_matrix_uses_only_temporary_databases():
    result = asyncio.run(run_h4_resilience())

    assert result["passed"] is True
    assert result["fairness"]["jobs"] == 24
    assert result["fairness"]["users_covered"] == 12
    assert result["fairness"]["worker_count"] == 1
    assert set(result["faults"]) == {
        "429", "5xx", "timeout", "invalid_json", "empty", "disconnect",
    }
    assert result["retry_cooldown"]["recovered_status"] == "done"
    assert result["half_write"]["recovered_status"] == "done"
    assert result["kill_recovery"]["status_after_restart"] == "done"
    assert result["expired_lease_recovery"]["status_after_restart"] == "done"
