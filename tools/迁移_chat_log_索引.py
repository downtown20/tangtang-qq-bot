"""在 bot 停止时为旧 memory.db 显式创建 chat_log 性能索引（幂等）。"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


INDEXES = {
    "idx_chat_log_qq_user_id":
        "CREATE INDEX IF NOT EXISTS idx_chat_log_qq_user_id "
        "ON chat_log(qq_id, is_bot_reply, id)",
    # 覆盖自治积压快照，避免每条候选再回表读取 timestamp/quarantined_at。
    "idx_chat_log_extraction_backlog":
        "CREATE INDEX IF NOT EXISTS idx_chat_log_extraction_backlog "
        "ON chat_log(qq_id, is_bot_reply, quarantined_at, id, timestamp)",
    "idx_chat_log_group_id":
        "CREATE INDEX IF NOT EXISTS idx_chat_log_group_id ON chat_log(group_id, id)",
    # 覆盖群聊时间线查询，避免对大群记录建立临时排序 B-tree。
    "idx_chat_log_group_time":
        "CREATE INDEX IF NOT EXISTS idx_chat_log_group_time "
        "ON chat_log(group_id, timestamp DESC, id DESC)",
    # 覆盖群内指定用户/私聊时间线查询，避免先扫描整群再过滤用户。
    "idx_chat_log_group_user_time":
        "CREATE INDEX IF NOT EXISTS idx_chat_log_group_user_time "
        "ON chat_log(group_id, qq_id, timestamp DESC, id DESC)",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="?", default="memory.db")
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    print(f"数据库: {db_path} ({db_path.stat().st_size / 1024 / 1024:.1f} MiB)")
    with sqlite3.connect(str(db_path), timeout=60) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_log'"
        ).fetchone()
        if not table_exists:
            parser.error("数据库中没有 chat_log 表")
        row_count = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
        print(f"chat_log: {row_count:,} 行")

        last_progress = time.monotonic()

        def progress() -> int:
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= 5:
                print("  索引仍在构建中……", flush=True)
                last_progress = now
            return 0

        conn.set_progress_handler(progress, 50_000)
        for name, sql in INDEXES.items():
            started = time.perf_counter()
            print(f"创建/确认 {name}……", flush=True)
            conn.execute(sql)
            conn.commit()
            print(f"  完成，用时 {time.perf_counter() - started:.2f}s")
        conn.set_progress_handler(None, 0)

        present = {row[1] for row in conn.execute("PRAGMA index_list('chat_log')")}
        missing = set(INDEXES) - present
        if missing:
            raise RuntimeError(f"索引创建后仍缺失: {sorted(missing)}")
    print("chat_log 索引迁移完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
