"""只读列出入站 inbox 终态，供人工核验，不执行重试或重放。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.store import ReadOnlyStore


_STATUSES = ("received", "claimed", "executing", "processed", "failed", "uncertain")


def build_report(
        db_path: str = "memory.db", *,
        statuses: tuple[str, ...] = ("failed", "uncertain"),
        limit: int = 100) -> dict:
    """返回脱敏入站元数据；正文请走已有的作用域历史查询。"""
    normalized = tuple(dict.fromkeys(
        str(status) for status in statuses if str(status) in _STATUSES
    ))
    rows = ReadOnlyStore(db_path).list_inbound_events(
        statuses=normalized, limit=limit,
    )
    return {
        "db_path": str(Path(db_path).resolve()),
        "statuses": list(normalized),
        "count": len(rows),
        "events": rows,
        "mutating_actions": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读查看 inbound_events；不会重试、重放或修改数据库。",
    )
    parser.add_argument("--db-path", default="memory.db")
    parser.add_argument(
        "--status", action="append", dest="statuses",
        choices=_STATUSES, help="重复传入以筛选多个状态（默认 failed/uncertain）",
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    statuses = tuple(args.statuses or ("failed", "uncertain"))
    print(json.dumps(
        build_report(args.db_path, statuses=statuses, limit=args.limit),
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
