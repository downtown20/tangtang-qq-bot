"""只读汇总记忆 legacy、来源、证据与 synthetic 隔离状态。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.memory_truth_audit import audit


TRUSTED_SQL = "('verified','manual','corrected')"


def inventory(db_path: str) -> dict:
    """生产库只读盘点；所有直连均使用 SQLite ``mode=ro``。"""
    audit_result = audit(db_path)
    resolved = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        active_by_origin = {
            str(origin or ""): int(count)
            for origin, count in conn.execute(
                "SELECT COALESCE(origin,''),COUNT(*) FROM memories "
                "WHERE COALESCE(status,'active')='active' GROUP BY 1 ORDER BY 1"
            ).fetchall()
        }
        legacy_in_trusted_selector = int(conn.execute(
            "SELECT COUNT(*) FROM memories "
            "WHERE COALESCE(status,'active')='active' "
            "AND COALESCE(trust_level,'legacy_unverified')='legacy_unverified' "
            f"AND COALESCE(trust_level,'legacy_unverified') IN {TRUSTED_SQL}"
        ).fetchone()[0])
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(chat_log)")}
        if {"is_synthetic", "quarantined_at"} <= columns:
            chat_rows = int(conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE COALESCE(is_synthetic,0)=1"
            ).fetchone()[0])
            quarantined_chat_rows = int(conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE COALESCE(is_synthetic,0)=1 "
                "AND COALESCE(quarantined_at,'')<>''"
            ).fetchone()[0])
            synthetic = {
                "memory_origin_synthetic": int(active_by_origin.get("synthetic", 0)),
                "chat_rows": chat_rows,
                "quarantined_chat_rows": quarantined_chat_rows,
                "unquarantined_chat_rows": chat_rows - quarantined_chat_rows,
            }
        else:
            synthetic = {
                "memory_origin_synthetic": int(active_by_origin.get("synthetic", 0)),
                "chat_rows": None,
                "quarantined_chat_rows": None,
                "unquarantined_chat_rows": None,
            }
    finally:
        conn.close()

    return {
        "database": str(resolved),
        "active_total": int(audit_result["active_total"]),
        "trusted": int(audit_result["trusted"]),
        "legacy_unverified": int(audit_result["legacy_unverified"]),
        "legacy_users": int(audit_result["legacy_users"]),
        "active_by_origin": dict(sorted(active_by_origin.items())),
        "legacy_by_origin": dict(sorted(audit_result["by_origin"].items())),
        "legacy_evidence": {
            "without_evidence": int(audit_result["legacy_unverified"])
            - int(audit_result["legacy_with_evidence"]),
            "with_evidence": int(audit_result["legacy_with_evidence"]),
            "linked_evidence": int(audit_result["legacy_linked_evidence"]),
            "verifiable_candidates": int(audit_result["legacy_verifiable_candidates"]),
            "rejected": int(audit_result["rejected"]),
            "deferred": int(audit_result["deferred"]),
        },
        "legacy_in_trusted_selector": legacy_in_trusted_selector,
        "synthetic": synthetic,
        "candidate_sample": audit_result["candidate_sample"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = inventory(args.db)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
