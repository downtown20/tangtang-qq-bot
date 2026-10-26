"""撤回一条记忆（标 retracted）——快照先行、留痕、幂等、可逆

## 为什么需要这个工具

`correct_memory` 是 LLM 在对话里调用的纠正闭环，它只能撤销**当时搜得到**的
记忆：搜索走 `search_memories_by_text` + 可信档语义兜底，legacy 档的行可能
根本不在搜索范围内。于是会出现这种残局——

2026-10-25 实测：`mem#18121`（qq=2524950083，verified「今年7月刚靠指标生名额
考上一中，是高一新生」）与 `mem#17589`（corrected「成展是大学大二学生
（不是高中生）」）同时 active。前者根本不是 2524950083 的事实——它的证据
`chat#199048` 是主人**转述别人**（「穷到吃外卖」= 2377255095）的一句话：

    帮我修正一下「穷到吃外卖」的记忆：他说的「33岁上班族」是开玩笑的，
    不是事实。**他**实际是今年7月刚靠指标生名额考上一中的高一新生…用
    correct_memory 工具修正一下。

08-17 的纠正其实**成功了**——同批的 `mem#17475/#17476` 已被 retract。但
`mem#18121` 当时还是 legacy，不在纠正的搜索范围里；2026-10-24 的证据回挂
（`tools/语义回挂证据.py`）又把它升成 verified，于是它现在与纠正结论并列注入。

这类残局**不能靠再对话一次解决**（搜索口径没变），需要一次显式的、留痕的撤回。

## 口径：retracted 而不是 superseded

与 `MemorySystem.correct_memory` 第 2 步同一口径：
- `key` 属于 `protocols.SYNTHESIS_KEYS` 的**合成行** → `superseded`（被新版本取代）
- 其余**原始事实** → `retracted`（被否定）

也**不挂** `superseded_by` 指针：那个字段的语义是「被谁取代」，用在「被否定」
上会污染演化史（console_api 里已经能看到 1261 条 superseded 却无指针的噪声）。

## 三条纪律

1. **快照先行**：`--apply` 第一步落 `日志/_撤回快照_<时间戳>.json`，导出成功才动手。
2. **幂等**：`set_memory_status` 在「状态已经等于目标值」时返回 False 且不留流水，
   连跑两次不会把幂等重放写成第二次变更。
3. **可逆**：`--restore` 把快照里的 status 写回去（同样留痕）。

## 用法

    python tools/撤回记忆.py --id 18121 --reason "…"            # 预演（默认，不改数据）
    python tools/撤回记忆.py --id 18121 --reason "…" --apply    # 执行
    python tools/撤回记忆.py --restore 日志/_撤回快照_20261025_090000.json

`--reason` 必填且会被写入 revision 流水 —— 事后复盘「这条凭什么被撤」全靠它。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_FIELDS = ("id", "qq_id", "key", "value", "confidence", "trust_level", "origin",
           "importance", "source_group_id", "evidence_ids", "status")


def _fetch(store, memory_id: int) -> dict:
    with store._connect() as conn:
        row = conn.execute(
            "SELECT id,qq_id,key,value,confidence,trust_level,origin,importance,"
            "source_group_id,evidence_ids,COALESCE(status,'active') "
            "FROM memories WHERE id=?",
            (int(memory_id),),
        ).fetchone()
    return dict(zip(_FIELDS, row)) if row else {}


def _evidence(store, memory_id: int) -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, relation, created_at FROM memory_evidence "
            "WHERE memory_id=?", (int(memory_id),),
        ).fetchall()
    return [{"chat_id": r[0], "relation": r[1], "created_at": r[2]} for r in rows]


def _corrected_siblings(store, qq_id: str) -> list[dict]:
    """同一个人名下仍然 active 的 corrected 结论——撤回一个就是给这些让路。"""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id,key,value FROM memories WHERE qq_id=? "
            "AND trust_level='corrected' AND COALESCE(status,'active')='active' "
            "ORDER BY id", (str(qq_id),),
        ).fetchall()
    return [{"id": r[0], "key": r[1], "value": r[2]} for r in rows]


def cmd_preview(store, memory_id: int, *, apply: bool, reason: str) -> int:
    row = _fetch(store, memory_id)
    if not row:
        print(f"❌ 记忆 #{memory_id} 不存在")
        return 1
    print(f"\n{'🔧 执行' if apply else '🔍 预演（不改数据）'}\n")
    for k in _FIELDS:
        print(f"  {k:<16} {row[k]}")

    ev = _evidence(store, memory_id)
    print(f"\n  证据链（{len(ev)} 条）:")
    for e in ev:
        print(f"    chat#{e['chat_id']} [{e['relation']}] {e['created_at']}")
        with store._connect() as conn:
            msg = conn.execute("SELECT message FROM chat_log WHERE id=?",
                               (e["chat_id"],)).fetchone()
        if msg:
            print(f"      原文：{str(msg[0])[:160]}")

    sib = _corrected_siblings(store, row["qq_id"])
    print(f"\n  同一个人（qq={row['qq_id']}）名下 active 的 corrected 结论 {len(sib)} 条:")
    for s in sib:
        print(f"    mem#{s['id']} [{s['key']}] {str(s['value'])[:70]}")

    if not reason.strip():
        print("\n❌ 必须给 --reason（会写进 revision 流水）")
        return 1

    if not apply:
        print(f"\n预演结束。当前 status={row['status']}；执行后为 retracted。加 --apply 真执行。")
        return 0

    out_dir = ROOT / "日志"
    out_dir.mkdir(exist_ok=True)
    snap = out_dir / f"_撤回快照_{datetime.now():%Y%m%d_%H%M%S}.json"
    snap.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "purpose": f"撤回记忆 #{memory_id} 前的完整快照",
        "reason": reason,
        "items": [row],
        "evidence": ev,
        "corrected_siblings": sib,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 撤回前快照已落盘：{snap}")
    print(f"   反向恢复：python tools/撤回记忆.py --restore \"{snap}\"\n")

    changed = store.set_memory_status(
        memory_id, "retracted",
        actor="tool:撤回记忆", reason=reason,
    )
    after = _fetch(store, memory_id)
    print(f"{'✅ 已撤回' if changed else 'ℹ️ 状态本来就是 retracted（幂等，未写流水）'}"
          f"：mem#{memory_id} trust={after.get('trust_level')} status={after.get('status')}")
    return 0


def cmd_restore(path: str) -> int:
    snap = Path(path)
    if not snap.is_absolute():
        snap = ROOT / snap
    if not snap.exists():
        print(f"❌ 快照不存在：{snap}")
        return 1
    payload = json.loads(snap.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items:
        print("❌ 快照里没有条目")
        return 1
    store = _store()
    ok = fail = 0
    for it in items:
        target = str(it.get("status") or "active")
        changed = store.set_memory_status(
            int(it["id"]), target,
            actor="tool:撤回记忆:restore",
            reason=f"从快照恢复撤回：{snap.name}",
        )
        after = _fetch(store, int(it["id"]))
        print(f"  {'✅' if changed else 'ℹ️ 未变'} mem#{it['id']} → {after.get('status')}")
        ok += 1 if changed else 0
    print(f"\n恢复完成：{ok} 条发生变更，{fail} 条失败")
    return 0 if fail == 0 else 1


def _store():
    from agent.store import Store
    return Store(str(ROOT / "memory.db"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="撤回一条记忆（标 retracted，快照 + 留痕 + 可逆）")
    ap.add_argument("--db", default=str(ROOT / "memory.db"))
    ap.add_argument("--id", type=int, default=0, help="要撤回的记忆 id")
    ap.add_argument("--reason", default="", help="撤回理由（必填，写进 revision 流水）")
    ap.add_argument("--apply", action="store_true", help="真执行（默认只预演）")
    ap.add_argument("--restore", default="", help="从快照反向恢复")
    args = ap.parse_args()

    from agent.store import Store
    store = Store(args.db)

    if args.restore:
        return cmd_restore(args.restore)
    if not args.id:
        print("❌ 需要 --id")
        return 1
    return cmd_preview(store, args.id, apply=args.apply, reason=args.reason)


if __name__ == "__main__":
    raise SystemExit(main())
