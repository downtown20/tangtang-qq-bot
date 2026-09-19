import sqlite3

from tools.memory_truth_audit import audit


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
            segments TEXT, timestamp TEXT, quarantined_at TEXT
        );
        INSERT INTO memories VALUES
          (1,'u1','fact','extracted','legacy_unverified','10','g1','t1','active'),
          (2,'u2','fact','extracted','legacy_unverified','11','g1','t2','active'),
          (3,'u3','fact','extracted','verified','12','g1','t3','active');
        INSERT INTO chat_log VALUES
          (10,'u1','g1',0,'真人证据','真人证据','[]','t1',''),
          (11,'u2','g1',1,'机器人回复','机器人回复','[]','t2',''),
          (12,'u3','g1',0,'可信证据','可信证据','[]','t3','');
        """
    )
    conn.commit()
    conn.close()


def test_audit_only_marks_non_bot_non_quarantined_as_candidates(tmp_path):
    path = tmp_path / "memory.db"
    _db(path)

    before = path.read_bytes()
    result = audit(str(path))

    assert result["active_total"] == 3
    assert result["trusted"] == 1
    assert result["legacy_unverified"] == 2
    assert result["legacy_with_evidence"] == 2
    assert result["legacy_verifiable_candidates"] == 1
    assert result["candidate_sample"][0]["id"] == 1
    assert path.read_bytes() == before


def test_audit_uses_read_only_sqlite_uri(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    _db(path)
    real_connect = sqlite3.connect
    calls = []

    def capture_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("tools.memory_truth_audit.sqlite3.connect", capture_connect)
    audit(str(path))
    assert calls
    assert calls[0][0][0].endswith("?mode=ro")
    assert calls[0][1]["uri"] is True


def test_audit_matches_store_null_status_semantics(tmp_path):
    path = tmp_path / "memory.db"
    _db(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE memories SET status=NULL WHERE id=1")
    conn.commit()
    conn.close()
    result = audit(str(path))
    assert result["active_total"] == 3
