#!python3.10
"""隔离历史事实簇无证据原子事实。

默认只读审计；只有显式 ``--apply`` 才会执行以下可回滚动作：

* 将 active 且 ``evidence_ids`` 为空的 ``cluster_facts`` 标为 ``retracted``；
* 保留原子行和原文本，不删除聊天记录；
* 清空受影响簇的摘要，并按剩余 active 行重算 ``fact_count``。

执行前自动生成 SQLite backup。脚本不调用 LLM，也不猜测证据来源；重复执行
只会得到 0 个目标，适合在人工验收后重跑。迁移范围刻意不包含有非空证据但
证据内容无效的行——那部分由 Store 启动迁移负责，避免两套校验规则漂移。

用法：
    python tools/migrate_unanchored_cluster_facts.py
    python tools/migrate_unanchored_cluster_facts.py --apply
    python tools/migrate_unanchored_cluster_facts.py --db path/to/memory.db
    # 仅在确认并发写入安全时覆盖在线保护
    python tools/migrate_unanchored_cluster_facts.py --apply --allow-online
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE / "memory.db"


def _target_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """返回当前仍可隔离的目标行；只读查询，不改变连接状态。"""
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        """
        SELECT id, cluster_id
        FROM cluster_facts
        WHERE COALESCE(status,'active')='active'
          AND TRIM(COALESCE(evidence_ids,''))=''
        ORDER BY id
        """
    ).fetchall())


def audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """生成可审计的存量报告，不返回事实正文或用户隐私。"""
    rows = _target_rows(conn)
    target_ids = [int(row["id"]) for row in rows]
    cluster_ids = sorted({int(row["cluster_id"]) for row in rows})
    contaminated = conn.execute(
        "SELECT COUNT(*) FROM fact_clusters fc "
        "WHERE TRIM(COALESCE(fc.summary,''))!='' AND EXISTS ("
        "SELECT 1 FROM cluster_facts cf "
        "WHERE cf.cluster_id=fc.id "
        "AND COALESCE(cf.status,'active')='active' "
        "AND TRIM(COALESCE(cf.evidence_ids,''))=''"
        ")"
    ).fetchone()[0]

    anchored = conn.execute(
        "SELECT COUNT(*) FROM cluster_facts "
        "WHERE COALESCE(status,'active')='active' "
        "AND TRIM(COALESCE(evidence_ids,''))!=''"
    ).fetchone()[0]
    return {
        "active_unanchored_facts": len(target_ids),
        "affected_clusters": len(cluster_ids),
        "affected_nonempty_summaries": int(contaminated),
        "active_anchored_facts": int(anchored),
        "sample_fact_ids": target_ids[:20],
        "sample_cluster_ids": cluster_ids[:20],
    }


def apply_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    """在单一事务中隔离目标行；调用方负责先做 backup。"""
    rows = _target_rows(conn)
    target_ids = [int(row["id"]) for row in rows]
    cluster_ids = sorted({int(row["cluster_id"]) for row in rows})
    if not target_ids:
        return {"retracted_facts": 0, "cleared_summaries": 0, "clusters": 0}

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TEMP TABLE migration_unanchored_targets "
            "(id INTEGER PRIMARY KEY)"
        )
        conn.executemany(
            "INSERT INTO migration_unanchored_targets(id) VALUES (?)",
            [(fact_id,) for fact_id in target_ids],
        )
        retracted = conn.execute(
            "UPDATE cluster_facts SET status='retracted' "
            "WHERE id IN (SELECT id FROM migration_unanchored_targets) "
            "AND COALESCE(status,'active')='active'"
        ).rowcount

        cleared = 0
        for cluster_id in cluster_ids:
            cleared += conn.execute(
                "UPDATE fact_clusters SET summary='', "
                "fact_count=(SELECT COUNT(*) FROM cluster_facts cf "
                "WHERE cf.cluster_id=fact_clusters.id "
                "AND COALESCE(cf.status,'active')='active'), "
                "updated_at=? WHERE id=? AND TRIM(COALESCE(summary,''))!=''",
                (now, cluster_id),
            ).rowcount
        conn.execute("DROP TABLE migration_unanchored_targets")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "retracted_facts": int(retracted),
        "cleared_summaries": int(cleared),
        "clusters": len(cluster_ids),
    }


def _open_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


def _backup(conn: sqlite3.Connection, db_path: Path, requested: str) -> Path:
    db_path = Path(db_path)
    backup = Path(requested) if requested else (
        BASE / "backups" /
        f"memory_{datetime.now().strftime('%Y%m%d_%H%M%S')}_unanchored_cluster_facts.db"
    )
    backup = backup.resolve()
    if backup == db_path.resolve():
        raise ValueError("backup path must differ from database path")
    backup.parent.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(backup))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return backup


def _bot_port_open(host: str = "127.0.0.1", port: int = 3001) -> bool:
    """检测小糖糖反向 WS 端口，避免默认在运行中的 bot 上改库。"""
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="memory.db 路径")
    parser.add_argument("--apply", action="store_true", help="执行隔离；默认只读")
    parser.add_argument("--backup", default="", help="--apply 时的 backup 路径")
    parser.add_argument(
        "--allow-online", action="store_true",
        help="覆盖 3001 在线保护（仅在确认并发写入安全时使用）",
    )
    args = parser.parse_args(argv)
    db_path = Path(args.db).resolve()

    if not args.apply:
        conn = _open_readonly(db_path)
        try:
            print(json.dumps({"mode": "dry-run", **audit(conn)}, ensure_ascii=False))
        finally:
            conn.close()
        return 0

    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        before = audit(conn)
        if before["active_unanchored_facts"] == 0:
            print(json.dumps({"mode": "apply", "backup": None, **before}, ensure_ascii=False))
            return 0
        if not args.allow_online and _bot_port_open():
            print(
                "拒绝在线迁移：检测到 127.0.0.1:3001 仍有小糖糖实例；"
                "请先停止 bot，或明确传入 --allow-online。",
                file=sys.stderr,
            )
            return 2
        backup = _backup(conn, db_path, args.backup)
        result = apply_migration(conn)
        after = audit(conn)
        print(json.dumps({
            "mode": "apply", "backup": str(backup),
            "before": before, "result": result, "after": after,
        }, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
