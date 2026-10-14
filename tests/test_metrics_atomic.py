"""P3 指标 flush 必须全成功或全回滚，失败后不丢/不重记。"""

from agent.metrics import MemoryMetrics
from agent.store import Store


def test_metrics_flush_is_atomic_and_preserves_buffer_after_failure(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    metrics = MemoryMetrics(store)
    metrics.incr("first")
    metrics.incr("second")
    day = metrics._today()

    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_second_metric BEFORE INSERT ON kv_store "
            "WHEN NEW.key LIKE '%:second' BEGIN "
            "SELECT RAISE(ABORT, 'probe failure'); END"
        )
        conn.commit()

    assert metrics.flush() is False

    assert store.kv_get(f"metric:{day}:first") is None
    assert store.kv_get(f"metric:{day}:second") is None
    assert metrics.get_current("first") == 1
    assert metrics.get_current("second") == 1

    with store._connect() as conn:
        conn.execute("DROP TRIGGER reject_second_metric")
        conn.commit()

    assert metrics.flush() is True

    assert metrics.get_current("first") == 1
    assert metrics.get_current("second") == 1
    assert store.kv_get("metric:latest:first") == "1"
    assert store.kv_get("metric:latest:second") == "1"
