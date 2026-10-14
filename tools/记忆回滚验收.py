"""离线副本的受控重验证回滚验收；不接受生产库路径。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.store import Store


BACKUP_SUFFIX = ".bak-20260903-H3"


def _trusted_visible(store: Store, memory_id: int) -> bool:
    return any(
        int(row["id"]) == int(memory_id)
        for row in store.query_memories("rollback-user", trusted_only=True,
                                        source_group_id="rollback-group")
    )


def run_offline_reverification_rollback(work_dir: str | Path) -> dict:
    """在新建 fixture → 离线副本上验证 legacy 提升和可审计回退。"""
    root = Path(work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "memory-offline-source.db"
    copy = root / "memory-offline-copy.db"
    backup = root / f"memory-offline-copy.db{BACKUP_SUFFIX}"
    for path in (source, copy, backup):
        if path.exists():
            raise ValueError(f"refusing to overwrite existing offline artifact: {path}")

    source_store = Store(str(source))
    chat_id = source_store.insert_chat(
        "rollback-user", "我确认住在回滚测试城", group_id="rollback-group",
        is_bot=False, timestamp="2026-09-03 20:00:00",
    )
    memory_id = source_store.insert_memory(
        "rollback-user", "fact", "住在回滚测试城", importance=6,
        origin="extracted", source_group_id="rollback-group",
        evidence_ids=str(chat_id), event_time="2026-09-03 20:00:00",
        # 故意缺 quote：受控提升前必须保持 legacy。
        evidence_quote="", claim_type="stated",
        idempotency_key="h3-offline-rollback-fixture",
    )
    source_bytes = source.read_bytes()
    shutil.copy2(source, backup)
    shutil.copy2(source, copy)

    store = Store(str(copy))
    before = store.get_memory_by_id(memory_id)
    before_visible = _trusted_visible(store, memory_id)
    promoted_ok = store.update_memory_evidence(
        memory_id, str(chat_id), "rollback-group", "住在回滚测试城",
        allow_legacy_promotion=True, reviewer="h3-offline",
        review_reason="离线可回滚验收",
    )
    promoted = store.get_memory_by_id(memory_id)
    promoted_visible = _trusted_visible(store, memory_id)
    rolled_back_ok = store.rollback_memory_reverification(
        memory_id, reviewer="h3-offline", review_reason="离线验收回退",
    )
    rolled_back = store.get_memory_by_id(memory_id)
    rolled_back_visible = _trusted_visible(store, memory_id)
    with store._connect() as conn:
        events = [tuple(row) for row in conn.execute(
            "SELECT previous_trust_level,new_trust_level FROM memory_reverification_events "
            "WHERE memory_id=? ORDER BY id", (memory_id,),
        ).fetchall()]

    passed = (
        backup.read_bytes() == source_bytes
        and bool(promoted_ok)
        and bool(rolled_back_ok)
        and before is not None and before["trust_level"] == "legacy_unverified"
        and promoted is not None and promoted["trust_level"] == "verified"
        and rolled_back is not None and rolled_back["trust_level"] == "legacy_unverified"
        and not before_visible and promoted_visible and not rolled_back_visible
        and events == [("legacy_unverified", "verified"),
                       ("verified", "legacy_unverified")]
    )
    return {
        "passed": passed,
        "source": str(source),
        "offline_copy": str(copy),
        "backup": str(backup),
        "backup_name": backup.name,
        "memory_id": memory_id,
        "before": {"trust_level": before["trust_level"]},
        "promoted": {"trust_level": promoted["trust_level"], "ok": bool(promoted_ok)},
        "rolled_back": {"trust_level": rolled_back["trust_level"], "ok": bool(rolled_back_ok)},
        "trusted_visible": {
            "before": before_visible,
            "promoted": promoted_visible,
            "rolled_back": rolled_back_visible,
        },
        "events": events,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True,
                        help="空的离线工作目录；工具拒绝覆盖任何已有产物")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    result = run_offline_reverification_rollback(args.work_dir)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
