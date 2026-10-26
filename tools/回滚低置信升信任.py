"""回滚低置信升信任 —— 主人 2026-10-25 决策 B 档的执行工具

## 决策依据（价值判断，不是技术判断）

`tools/低置信升信任清单.py` 查出 **155 条**「经重验证升成 verified、但置信度
< 0.7」的记忆。它们正在被注入她的上下文（信任门只放行 verified/manual/corrected）。

主人 2026-10-25 拍板 **B 档：只回滚 confidence < 0.6 的 78 条**，
0.6~0.69 的 77 条保留为 verified。

口径（查库实测）：<0.6 的 78 条置信度只有两档——`0.55` 67 条 + `0.50` 11 条；
全部 `origin=extracted`；全部来自两个语义回填批次
（`backfill:semantic:s6-rest` 66 / `backfill:substring:b2-full` 12）；
`previous_trust_level` 为 `legacy_unverified` 75 条 + `unverified` 3 条。
样本：`mem#25510`「小说开头设定主角…猝死（小说情节内容）」、
`mem#19337`「自称是Ezfic大神」、`mem#8643`「独自一人在天台抽烟」。

## 为什么回滚的是 trust_level 而不是删记忆

`store.rollback_memory_reverification()` 只把 trust_level 降回提升前的值：
- **不删**记忆本身、**不删**证据链（`memory_evidence` 原样保留）；
- **不删**审计（`memory_reverification_events` 原追加一条反向事件）；
- 额外写一条 `_record_revision`，让「信任漂移」和「内容变更」在同一时间线可读。

降回 `legacy_unverified` 后该行自动退出 `TRUSTED_TRUST_LEVELS`，
`query_memories(trusted_only=True)` 与画像注入都不再放行——这正是我们要的效果。

## 两条纪律

1. **备份先行**：`--apply` 的第一步是把 78 条的完整行导出成 JSON 快照
   （`日志/_回滚快照_<时间戳>.json`），**导出成功才动手**。快照就是反向操作依据。
2. **幂等**：回滚后 trust_level 已不在可信集合，第二次运行查不到这些行——
   连跑两次不会重复回滚，也不会重复记账（`--apply` 跑第二遍应报「0 条」）。

## 用法

    python tools/回滚低置信升信任.py                    # 预演（默认，不改数据）
    python tools/回滚低置信升信任.py --apply            # 真执行
    python tools/回滚低置信升信任.py --apply --max-conf 0.6
    python tools/回滚低置信升信任.py --restore 日志/_回滚快照_20261025_120000.json

`--restore` 是对称的反向操作：把快照里的 trust_level 写回去（带 CAS 守卫：
只有当前等级仍等于回滚后的值才写，避免覆盖期间发生的新决策），同样写 revision。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REVIEWER = "owner-decision:20261025-b"
DEFAULT_REASON = (
    "主人 2026-10-25 决策 B 档：回滚 confidence<0.6 的低置信度重验证"
    "（提取器当时明确不确定，疑为小说/玩笑/转述/情绪状态）"
)


def collect(db_path: str, *, max_conf: float) -> list[dict]:
    """列出待回滚的记忆（只读）。与 `低置信升信任清单.py` 同口径 + 快照所需字段。"""
    from agent.store import Store
    store = Store(db_path)
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.qq_id, m.key, m.value, m.confidence, m.trust_level,
                   m.origin, m.importance, m.source_group_id, m.evidence_ids,
                   m.event_time, COALESCE(m.status,'active'),
                   e.previous_trust_level, e.new_trust_level, e.reviewer, e.reviewed_at
            FROM memories m
            JOIN memory_reverification_events e ON e.memory_id = m.id
            WHERE m.trust_level = 'verified'
              AND COALESCE(m.status, 'active') = 'active'
              AND m.confidence >= 0 AND m.confidence < ?
            GROUP BY m.id
            ORDER BY m.confidence ASC, m.id ASC
            """,
            (float(max_conf),),
        ).fetchall()
    return [{
        "id": int(r[0]),
        "qq_id": str(r[1] or ""),
        "key": str(r[2] or ""),
        "value": str(r[3] or ""),
        "confidence": float(r[4] or 0),
        "trust_level_before": str(r[5] or ""),
        "origin": str(r[6] or ""),
        "importance": int(r[7] or 0),
        "source_group_id": str(r[8] or ""),
        "evidence_ids": str(r[9] or ""),
        "event_time": str(r[10] or ""),
        "status": str(r[11] or ""),
        "previous_trust_level": str(r[12] or ""),
        "promoted_trust_level": str(r[13] or ""),
        "reviewer": str(r[14] or ""),
        "reviewed_at": str(r[15] or ""),
    } for r in rows]


def write_snapshot(items: list[dict], max_conf: float) -> Path:
    out_dir = ROOT / "日志"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"_回滚快照_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "generated_at": datetime.now().isoformat(),
        "purpose": "低置信升信任回滚（B 档：confidence < %.2f）回滚前完整快照" % max_conf,
        "max_conf": max_conf,
        "count": len(items),
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def cmd_preview(items: list[dict], *, apply: bool, reason: str) -> int:
    print(f"\n{'🔧 执行' if apply else '🔍 预演（不改数据）'}：confidence < 门槛 且 "
          f"trust_level=verified 的记忆 **{len(items)} 条**\n")
    if not items:
        print("（空——可能已经回滚过；幂等）")
        return 0

    snap = None
    if apply:
        snap = write_snapshot(items, max_conf=max(i["confidence"] for i in items) + 0.001)
        print(f"💾 回滚前快照已落盘：{snap}")
        print(f"   反向恢复命令：python tools/回滚低置信升信任.py --restore \"{snap}\"\n")

    from agent.store import Store
    store = Store(str(ROOT / "memory.db"))

    ok = fail = 0
    failures: list[tuple[int, str]] = []
    for it in items:
        if not apply:
            print(f"  将回滚 mem#{it['id']:<7} conf={it['confidence']:.2f} "
                  f"{it['trust_level_before']}→{it['previous_trust_level']} "
                  f"| {it['value'][:48]}")
            continue
        try:
            done = store.rollback_memory_reverification(
                it["id"], reviewer=REVIEWER, review_reason=reason,
                actor=f"tool:回滚低置信升信任:{REVIEWER}",
            )
        except Exception as exc:  # 单条失败不拖垮整批
            done = False
            failures.append((it["id"], f"异常 {type(exc).__name__}: {exc}"))
        else:
            if not done:
                failures.append((it["id"], "rollback 返回 False（当前等级与提升事件不匹配？）"))
        if done:
            ok += 1
        else:
            fail += 1

    if not apply:
        print(f"\n预演结束：{len(items)} 条待回滚。加 --apply 真执行。")
        return 0

    print(f"\n✅ 成功回滚 {ok} 条，失败 {fail} 条")
    for mid, why in failures[:20]:
        print(f"   ❌ mem#{mid} {why}")
    if len(failures) > 20:
        print(f"   ……另有 {len(failures)-20} 条失败")
    return 0 if fail == 0 else 1


def cmd_restore(path: str) -> int:
    snap = Path(path)
    if not snap.is_absolute():
        snap = ROOT / snap
    if not snap.exists():
        print(f"❌ 快照不存在：{snap}")
        return 1
    payload = json.loads(snap.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    print(f"\n♻️ 从快照恢复：{snap}（{len(items)} 条）\n")

    from agent.store import Store
    store = Store(str(ROOT / "memory.db"))
    ok = skip = fail = 0
    for it in items:
        target = str(it.get("trust_level_before") or "")
        expect_now = str(it.get("previous_trust_level") or "")
        if not target:
            fail += 1
            continue
        with store._connect() as conn:
            row = conn.execute(
                "SELECT trust_level FROM memories WHERE id=? AND COALESCE(status,'active')='active'",
                (int(it["id"]),),
            ).fetchone()
            if not row:
                fail += 1
                continue
            cur = str(row[0] or "")
            # CAS：只在「仍处于回滚后状态」时恢复，避免覆盖期间的新决策
            if cur == target:
                skip += 1
                continue
            if cur != expect_now:
                print(f"   ⚠ mem#{it['id']} 当前等级 {cur} 既不是回滚后 {expect_now} "
                      f"也不是原等级 {target}——跳过（期间有新决策）")
                skip += 1
                continue
            conn.execute("UPDATE memories SET trust_level=? WHERE id=?",
                         (target, int(it["id"])))
            store._record_revision(
                conn, entity="memories", entity_id=int(it["id"]),
                qq_id=str(it.get("qq_id") or ""), op="update", field="trust_level",
                before=cur, after=target,
                reason=f"从快照恢复低置信升信任回滚：{snap.name}",
                actor="tool:回滚低置信升信任:restore",
            )
            conn.commit()
        ok += 1
    print(f"\n✅ 恢复 {ok} 条，跳过 {skip} 条，失败 {fail} 条")
    return 0 if fail == 0 else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="回滚低置信升信任（B 档：confidence < 0.6）")
    ap.add_argument("--db", default=str(ROOT / "memory.db"))
    ap.add_argument("--max-conf", type=float, default=0.6,
                    help="回滚 confidence < 该值的（默认 0.6 = 主人选的 B 档）")
    ap.add_argument("--apply", action="store_true", help="真执行（默认只预演）")
    ap.add_argument("--reason", default=DEFAULT_REASON)
    ap.add_argument("--restore", default="", help="从快照反向恢复")
    args = ap.parse_args()

    if args.restore:
        return cmd_restore(args.restore)

    items = collect(args.db, max_conf=args.max_conf)
    return cmd_preview(items, apply=args.apply, reason=args.reason)


if __name__ == "__main__":
    raise SystemExit(main())
