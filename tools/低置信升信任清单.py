"""低置信升信任清单 —— 只读，不改任何数据（2026-10-25 第五轮自查）

## 背景

`update_memory_evidence`（legacy → verified 的重验证路径）原来只有一道门：
**有被原文逐字支持的 quote**。但 quote 只证明「这句话说过」，不证明
「这条记忆是关于 ta 本人的事实」——同一句原话可以是小说情节、玩笑、转述、
引用、假设。

2026-10-25 全库查证：经重验证升成 verified 的记忆里，**confidence < 0.7 的共 155 条**
（<0.6 的 78 条 + 0.6~0.69 的 77 条），抽样可见：

- `mem#25510`「小说开头设定主角是广告公司策划，凌晨三点改完第十八版方案后猝死
  （小说情节内容）」
- `mem#19337`「自称是Ezfic大神」（玩笑自称）
- `mem#11960`「在等别人救我」（情绪状态不是事实）
- `mem#8643`「独自一人在天台上抽烟」（疑似小说/引述）

它们现在都是 verified —— 而信任门只放行 verified/manual/corrected，
所以**它们正在被注入她的上下文**。

## 本工具的立场：只列不改

回滚会改变她看到什么、进而改变她说什么，属于**价值决策**，由主人拍板。
技术上回滚是受控且可逆的（`store.rollback_memory_reverification()` 只降
trust_level、保留全部审计、不删证据）。

## 用法

    python tools/低置信升信任清单.py               # 摘要 + 落盘明细
    python tools/低置信升信任清单.py --limit 30    # 只打印前 30 条
    python tools/低置信升信任清单.py --max-conf 0.7 --min-conf 0

明细落 `日志/_低置信升信任清单_<日期>.json`，可直接喂给回滚脚本。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def collect(db_path: str, *, min_conf: float = 0.0, max_conf: float = 0.7) -> list[dict]:
    """列出「经重验证升成 verified、但置信度低于门槛」的记忆（只读）。"""
    from agent.store import Store
    store = Store(db_path)
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.qq_id, m.key, m.value, m.confidence, m.trust_level,
                   m.origin, m.importance, m.source_group_id,
                   e.previous_trust_level, e.reviewer, e.reviewed_at
            FROM memories m
            JOIN memory_reverification_events e ON e.memory_id = m.id
            WHERE m.trust_level = 'verified'
              AND COALESCE(m.status, 'active') = 'active'
              AND m.confidence >= ? AND m.confidence < ?
            GROUP BY m.id
            ORDER BY m.confidence ASC, m.id ASC
            """,
            (float(min_conf), float(max_conf)),
        ).fetchall()
    out = []
    for row in rows:
        out.append({
            "id": int(row[0]),
            "qq_id": str(row[1] or ""),
            "key": str(row[2] or ""),
            "value": str(row[3] or ""),
            "confidence": float(row[4] or 0),
            "trust_level": str(row[5] or ""),
            "origin": str(row[6] or ""),
            "importance": int(row[7] or 0),
            "source_group_id": str(row[8] or ""),
            "previous_trust_level": str(row[9] or ""),
            "reviewer": str(row[10] or ""),
            "reviewed_at": str(row[11] or ""),
        })
    return out


def summarize(items: list[dict]) -> dict:
    by_key: dict[str, int] = {}
    by_band: dict[str, int] = {}
    for it in items:
        by_key[it["key"] or "(空)"] = by_key.get(it["key"] or "(空)", 0) + 1
        c = it["confidence"]
        band = "<0.6" if c < 0.6 else ("0.6-0.69" if c < 0.7 else ">=0.7")
        by_band[band] = by_band.get(band, 0) + 1
    return {"total": len(items), "by_key": by_key, "by_confidence": by_band}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="低置信升信任清单（只读）")
    ap.add_argument("--db", default=str(ROOT / "memory.db"))
    ap.add_argument("--min-conf", type=float, default=0.0)
    ap.add_argument("--max-conf", type=float, default=0.7)
    ap.add_argument("--limit", type=int, default=0, help="只打印前 N 条（0 = 全部）")
    args = ap.parse_args()

    items = collect(args.db, min_conf=args.min_conf, max_conf=args.max_conf)
    stats = summarize(items)

    print(f"\n🔍 经重验证升成 verified、但 confidence < {args.max_conf} 的记忆："
          f"**{stats['total']} 条**（只读清单，未改动任何数据）\n")
    if not items:
        print("（空）")
        return 0
    print("按 key 分布：", json.dumps(stats["by_key"], ensure_ascii=False))
    print("按置信度分布：", json.dumps(stats["by_confidence"], ensure_ascii=False))
    print()
    show = items if args.limit <= 0 else items[: args.limit]
    for it in show:
        print(f"  mem#{it['id']:<7} conf={it['confidence']:<5} "
              f"key={it['key']:<6} [{it['reviewer']}]")
        print(f"      {it['value'][:100]}")
    if len(show) < len(items):
        print(f"\n  ……还有 {len(items) - len(show)} 条，见落盘文件")

    out_dir = ROOT / "日志"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"_低置信升信任清单_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"generated_at": datetime.now().isoformat(),
                               "max_conf": args.max_conf,
                               "stats": stats, "items": items},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n💾 明细已落盘：{out}")
    print("   ⚠ 本工具只列不改。回滚会改变她注入的内容，属价值决策——"
          "确认后用 store.rollback_memory_reverification()（受控、留痕、可逆）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
