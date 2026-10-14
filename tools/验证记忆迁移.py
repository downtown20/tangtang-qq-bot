"""在临时一致性副本上演练 Store 迁移；生产数据库只读。"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.store import Store


def _snapshot(conn: sqlite3.Connection) -> dict:
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    counts = {}
    for table in (
        "chat_log", "memories", "memory_evidence", "extraction_jobs",
        "extraction_cursors", "schema_migrations",
    ):
        counts[table] = (
            int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            if table in tables else None
        )
    return {"tables": sorted(tables), "counts": counts}


def validate(source_path: Path) -> dict:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="tangtang-memory-migration-") as tmp:
        clone_path = Path(tmp).resolve() / "memory-clone.db"
        source_uri = source_path.as_uri() + "?mode=ro"
        backup_started = time.perf_counter()
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            before = _snapshot(source)
            with closing(sqlite3.connect(str(clone_path))) as target:
                source.backup(target, pages=4096)
        backup_seconds = time.perf_counter() - backup_started

        first_started = time.perf_counter()
        Store(str(clone_path))
        first_seconds = time.perf_counter() - first_started
        with closing(sqlite3.connect(str(clone_path))) as conn:
            after_first = _snapshot(conn)
            first_integrity = conn.execute("PRAGMA quick_check").fetchone()[0]

        second_started = time.perf_counter()
        Store(str(clone_path))
        second_seconds = time.perf_counter() - second_started
        with closing(sqlite3.connect(str(clone_path))) as conn:
            after_second = _snapshot(conn)
            second_integrity = conn.execute("PRAGMA quick_check").fetchone()[0]

        preserved = all(
            before["counts"].get(table) == after_second["counts"].get(table)
            for table in ("chat_log", "memories")
        )
        idempotent = after_first["counts"] == after_second["counts"]
        required_tables = {
            "memory_evidence", "extraction_jobs", "extraction_cursors",
            "schema_migrations",
        }
        passed = (
            first_integrity == "ok"
            and second_integrity == "ok"
            and preserved
            and idempotent
            and required_tables.issubset(set(after_second["tables"]))
        )
        return {
            "passed": passed,
            "source": str(source_path),
            "source_bytes": source_path.stat().st_size,
            "backup_seconds": round(backup_seconds, 3),
            "first_migration_seconds": round(first_seconds, 3),
            "second_migration_seconds": round(second_seconds, 3),
            "integrity": [first_integrity, second_integrity],
            "core_rows_preserved": preserved,
            "second_run_idempotent": idempotent,
            "before": before["counts"],
            "after": after_second["counts"],
            "total_seconds": round(time.perf_counter() - started, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", nargs="?", default=str(PROJECT_ROOT / "memory.db"))
    args = parser.parse_args()
    result = validate(Path(args.database))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
