import sqlite3

from tools.记忆存量清点 import inventory


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, qq_id TEXT, key TEXT, origin TEXT,
            trust_level TEXT, evidence_ids TEXT, source_group_id TEXT,
            event_time TEXT, status TEXT
        );
        CREATE TABLE chat_log (
            id INTEGER PRIMARY KEY, qq_id TEXT, group_id TEXT,
            is_bot_reply INTEGER, message TEXT, raw_message TEXT,
            segments TEXT, timestamp TEXT, quarantined_at TEXT,
            is_synthetic INTEGER
        );
        INSERT INTO memories VALUES
          (1,'u1','fact','extracted','legacy_unverified','','g1','','active'),
          (2,'u2','fact','extracted','legacy_unverified','10','g1','t1','active'),
          (3,'u3','fact','manual','manual','11','g1','t2','active');
        INSERT INTO chat_log VALUES
          (10,'u2','g1',0,'真人证据','真人证据','[]','t1','',0),
          (11,'u3','g1',0,'人工证据','人工证据','[]','t2','',0),
          (12,'u4','g1',0,'合成行','合成行','[]','t3','t3',1);
        """
    )
    conn.commit()
    conn.close()


def test_inventory_reports_evidence_origin_and_synthetic_isolation_read_only(tmp_path):
    path = tmp_path / "memory.db"
    _db(path)
    before = path.read_bytes()

    result = inventory(str(path))

    assert result["legacy_evidence"] == {
        "without_evidence": 1,
        "with_evidence": 1,
        "linked_evidence": 1,
        "verifiable_candidates": 1,
        "rejected": 0,
        "deferred": 0,
    }
    assert result["active_by_origin"] == {"extracted": 2, "manual": 1}
    assert result["legacy_in_trusted_selector"] == 0
    assert result["synthetic"] == {
        "memory_origin_synthetic": 0,
        "chat_rows": 1,
        "quarantined_chat_rows": 1,
        "unquarantined_chat_rows": 0,
    }
    assert path.read_bytes() == before


def test_inventory_opens_its_direct_connection_read_only(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    _db(path)
    real_connect = sqlite3.connect
    calls = []

    def capture_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("tools.记忆存量清点.sqlite3.connect", capture_connect)
    inventory(str(path))

    assert calls
    assert calls[0][0][0].endswith("?mode=ro")
    assert calls[0][1]["uri"] is True
