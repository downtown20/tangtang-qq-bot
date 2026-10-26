# -*- coding: utf-8 -*-
"""把「名字是X / 我叫X / 叫我X」这类自述归档成 aliases（source='self_declared'）。

用法：
    python tools/补自称名字.py            # 默认 dry-run，只统计不写
    python tools/补自称名字.py --apply    # 真正写入

为什么要单独一个回填脚本：识别逻辑已接在 `complete_extraction_job` 里（新记忆
会自动归档），但**库里已有的老记忆不会再被提取一遍**——它们需要一次性扫过去。
本脚本是幂等的：重复跑只会补新增的，不会重复写（aliases 有 (alias,qq_id) 唯一索引）。

安全边界（与 store._promote_self_declared_names 一致）：
- **绝不改 people.nickname**。自述可能是角色扮演，写进身份字段等于替她认定
  一个假名字。
- 只认「名字/叫」句式，不认「我是X」。
- 每条外号带 memory_id 证据指针，可回溯、可撤销。
- dry-run 是默认值：写生产库必须显式 --apply。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from agent.store import Store  # noqa: E402

APPLY = "--apply" in sys.argv


def main() -> int:
    store = Store(str(Path(__file__).resolve().parent.parent / "memory.db"))
    with store._connect() as conn:
        owners = [str(row[0]) for row in conn.execute(
            "SELECT qq_id FROM people ORDER BY qq_id").fetchall()]

    print(f"扫描 {len(owners)} 个档案…")
    hits: list[tuple[str, str]] = []
    for qq_id in owners:
        with store._connect() as conn:
            # dry-run 也要走同一段识别逻辑，但不开事务提交
            found = store._promote_self_declared_names(conn, qq_id)
            if APPLY:
                conn.commit()
            else:
                conn.rollback()
        for name in found:
            hits.append((qq_id, name))

    print(f"\n{'=' * 60}")
    if APPLY:
        print(f"已归档 {len(hits)} 个自称名字")
    else:
        print(f"DRY RUN —— 将归档 {len(hits)} 个自称名字（加 --apply 才写入）")
    print("=" * 60)
    for qq_id, name in hits:
        with store._connect() as conn:
            nick = conn.execute(
                "SELECT COALESCE(nickname,'') FROM people WHERE qq_id=?",
                (qq_id,)).fetchone()
        print(f"  qq={qq_id:>12s}  自称={name!r}  （people.nickname={nick[0]!r}，不会被改）")

    print("\n注意：这些名字只进 aliases（可展示/可撤销），**不改 people.nickname**——"
          "自称可能是角色扮演。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
