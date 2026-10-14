"""受控重验证 legacy extracted 记忆；默认只读预演，应用必须显式逐条指定。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.store import Store


def preview(db_path: str, memory_id: int) -> dict | None:
    resolved = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id,qq_id,key,value,origin,trust_level,source_group_id,evidence_ids "
            "FROM memories WHERE id=? AND COALESCE(status,'active')='active'",
            (int(memory_id),),
        ).fetchone()
        if not row:
            return None
        evidence_ids = [int(token) for token in str(row["evidence_ids"] or "").split(",")
                        if token.strip().isdigit() and int(token) > 0]
        messages = []
        if evidence_ids:
            marks = ",".join("?" for _ in evidence_ids)
            messages = [dict(item) for item in conn.execute(
                "SELECT id,qq_id,group_id,is_bot_reply,message,timestamp "
                f"FROM chat_log WHERE id IN ({marks}) ORDER BY id", evidence_ids,
            ).fetchall()]
        return {"memory": dict(row), "evidence": messages}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="逐条受控重验证 legacy extracted 记忆")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--memory-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="实际提升该单条记忆")
    parser.add_argument("--rollback", action="store_true", help="撤回该条受控提升")
    parser.add_argument("--reject", action="store_true", help="记录拒绝复核，不改信任等级")
    parser.add_argument("--defer", action="store_true", help="记录延后复核，不改信任等级")
    parser.add_argument("--quote", default="", help="可在原始证据中逐字找到的引用")
    parser.add_argument("--reviewer", default="", help="复核人标识")
    parser.add_argument("--reason", default="", help="复核理由")
    args = parser.parse_args()
    actions = [args.apply, args.rollback, args.reject, args.defer]
    if sum(bool(action) for action in actions) > 1:
        parser.error("--apply, --rollback, --reject and --defer are mutually exclusive")

    candidate = preview(args.db, args.memory_id)
    if candidate is None:
        print(json.dumps({"ok": False, "reason": "memory_not_active"}, ensure_ascii=False))
        return 2
    if not any(actions):
        print(json.dumps({"ok": True, "mode": "preview", **candidate}, ensure_ascii=False, indent=2))
        return 0

    memory = candidate["memory"]
    if args.reject or args.defer:
        if not args.reviewer or not args.reason:
            parser.error("--reject/--defer requires --reviewer and --reason")
        mode = "rejected" if args.reject else "deferred"
        store = Store(args.db)
        ok = store.record_memory_reverification_disposition(
            int(memory["id"]), mode, reviewer=args.reviewer, reason=args.reason,
            reviewed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        print(json.dumps({"ok": ok, "mode": mode, "memory_id": memory["id"]}, ensure_ascii=False))
        return 0 if ok else 1
    if args.rollback:
        if not args.reviewer or not args.reason:
            parser.error("--rollback requires --reviewer and --reason")
        store = Store(args.db)
        ok = store.rollback_memory_reverification(
            int(memory["id"]), reviewer=args.reviewer, review_reason=args.reason,
        )
        print(json.dumps({"ok": ok, "mode": "rolled_back", "memory_id": memory["id"]}, ensure_ascii=False))
        return 0 if ok else 1
    if not args.quote or not args.reviewer or not args.reason:
        parser.error("--apply requires --quote, --reviewer and --reason")
    evidence_groups = {str(item.get("group_id") or "") for item in candidate["evidence"]}
    source_group_id = str(memory["source_group_id"] or "")
    if not source_group_id and len(evidence_groups) == 1:
        source_group_id = next(iter(evidence_groups))
    if not source_group_id and not (len(evidence_groups) == 1 and "" in evidence_groups):
        parser.error("evidence must have one unambiguous source group")
    store = Store(args.db)
    ok = store.update_memory_evidence(
        int(memory["id"]), str(memory["evidence_ids"] or ""), source_group_id,
        args.quote,
        allow_legacy_promotion=True, reviewer=args.reviewer,
        review_reason=args.reason,
    )
    print(json.dumps({"ok": ok, "mode": "applied", "memory_id": memory["id"]}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
