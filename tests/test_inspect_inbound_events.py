def test_build_report_is_read_only_and_uses_review_statuses(monkeypatch, tmp_path):
    from tools import inspect_inbound_events as tool

    calls = {}

    class FakeReadOnlyStore:
        def __init__(self, db_path):
            calls["db_path"] = db_path

        def list_inbound_events(self, *, statuses, limit):
            calls["statuses"] = statuses
            calls["limit"] = limit
            return [{"event_key": "e", "status": "failed"}]

    monkeypatch.setattr(tool, "ReadOnlyStore", FakeReadOnlyStore)
    report = tool.build_report(
        str(tmp_path / "memory.db"),
        statuses=("failed", "failed", "not-a-status"),
        limit=3,
    )

    assert calls["statuses"] == ("failed",)
    assert calls["limit"] == 3
    assert report["count"] == 1
    assert report["mutating_actions"] is False
    assert "message" not in report["events"][0]
