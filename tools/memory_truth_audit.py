"""只读审计 memories 的真值与证据覆盖。

本工具不写数据库、不提升 trust_level，也不删除历史；输出可作为后续
重验证/归档的候选清单。证据链存在不等于事实已验证，只有原始聊天行且
不是机器人回复时才记为可核验候选，最终仍需经过受控流程确认。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


TRUSTED = {"verified", "manual", "corrected"}


def audit(db_path: str) -> dict:
    # 审计器必须在连接层拒绝写入；仅依赖“代码不调用 UPDATE”不够安全。
    resolved = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        dispositions = {}
        has_dispositions = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_reverification_dispositions'"
        ).fetchone()
        if has_dispositions:
            dispositions = {
                int(row["memory_id"]): row
                for row in conn.execute(
                    "SELECT d.memory_id,d.disposition,d.evidence_ids,d.reviewer,"
                    "d.reason,d.reviewed_at FROM memory_reverification_dispositions d "
                    "JOIN (SELECT memory_id,MAX(id) AS id "
                    "FROM memory_reverification_dispositions GROUP BY memory_id) latest "
                    "ON latest.id=d.id"
                ).fetchall()
            }
        rows = conn.execute(
            "SELECT id, qq_id, key, origin, trust_level, evidence_ids, "
            "source_group_id, event_time FROM memories "
            "WHERE COALESCE(status,'active')='active'"
        ).fetchall()
        counts = Counter()
        users = set()
        candidates = []
        disposition_samples = {"rejected": [], "deferred": []}
        for row in rows:
            trust = str(row["trust_level"] or "legacy_unverified")
            evidence_raw = str(row["evidence_ids"] or "").strip()
            evidence_ids = []
            for token in evidence_raw.split(","):
                try:
                    value = int(token.strip())
                except ValueError:
                    continue
                if value > 0:
                    evidence_ids.append(value)
            linked = 0
            non_bot = 0
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence_rows = conn.execute(
                    "SELECT id, is_bot_reply, COALESCE(quarantined_at,'') "
                    f"FROM chat_log WHERE id IN ({placeholders})",
                    evidence_ids,
                ).fetchall()
                linked = len(evidence_rows)
                non_bot = sum(
                    1 for item in evidence_rows
                    if not item[1] and not item[2]
                )
            bucket = (
                "trusted" if trust in TRUSTED else "legacy_unverified"
            )
            counts[bucket] += 1
            counts[f"{bucket}:with_evidence"] += bool(evidence_ids)
            counts[f"{bucket}:linked_evidence"] += linked
            counts[f"{bucket}:verifiable_evidence"] += non_bot
            if bucket == "legacy_unverified":
                users.add(str(row["qq_id"]))
                disposition = dispositions.get(int(row["id"]))
                if disposition:
                    decision = str(disposition["disposition"])
                    counts[decision] += 1
                    snapshot_ids = []
                    for token in str(disposition["evidence_ids"] or "").split(","):
                        if token.strip().isdigit() and int(token) > 0:
                            snapshot_ids.append(int(token))
                    disposition_samples[decision].append({
                        "memory_id": int(row["id"]),
                        "evidence_ids": snapshot_ids,
                        "reviewer": str(disposition["reviewer"]),
                        "reason": str(disposition["reason"]),
                        "reviewed_at": str(disposition["reviewed_at"]),
                    })
                elif non_bot:
                    candidates.append({
                        "id": int(row["id"]),
                        "qq_id": str(row["qq_id"]),
                        "key": str(row["key"]),
                        "origin": str(row["origin"] or ""),
                        "evidence_ids": evidence_ids,
                        "linked_evidence": linked,
                        "verifiable_evidence": non_bot,
                        "source_group_id": str(row["source_group_id"] or ""),
                        "event_time": str(row["event_time"] or ""),
                    })
        return {
            "database": str(Path(db_path).resolve()),
            "active_total": len(rows),
            "trusted": counts["trusted"],
            "legacy_unverified": counts["legacy_unverified"],
            "legacy_users": len(users),
            "legacy_with_evidence": counts["legacy_unverified:with_evidence"],
            "legacy_linked_evidence": counts["legacy_unverified:linked_evidence"],
            "legacy_verifiable_candidates": len(candidates),
            "rejected": counts["rejected"],
            "deferred": counts["deferred"],
            "rejected_sample": disposition_samples["rejected"][:100],
            "deferred_sample": disposition_samples["deferred"][:100],
            "by_origin": dict(Counter(
                str(row["origin"] or "")
                for row in rows
                if str(row["trust_level"] or "legacy_unverified") not in TRUSTED
            )),
            "by_key": dict(Counter(
                str(row["key"] or "")
                for row in rows
                if str(row["trust_level"] or "legacy_unverified") not in TRUSTED
            )),
            "candidate_sample": candidates[:100],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="只读审计记忆证据覆盖")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--output", help="可选 JSON 输出路径")
    args = parser.parse_args()
    result = audit(args.db)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
