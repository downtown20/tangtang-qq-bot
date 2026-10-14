from datetime import datetime, timezone

from agent.extraction_telemetry import queue_age_seconds


def test_queue_age_accepts_timezone_aware_now_for_local_db_timestamp():
    age = queue_age_seconds(
        "2026-08-28 12:00:00",
        now=datetime(2026, 8, 28, 12, 0, 5, tzinfo=timezone.utc),
    )

    assert age >= 0

