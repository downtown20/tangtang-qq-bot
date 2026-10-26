# -*- coding: utf-8 -*-
"""记忆能力判据 J1（线索回忆）/ J2（保持率）——离线量尺（2026-10-25 第 15 轮）。

## 测什么

判据文档 `docs/开发规划/记忆能力判据_20261025.md` §2：

- **J1 线索回忆**：给一个对象一个自然问题（「你还记得你答应过我什么吗？」），
  看 top5 候选里**真承诺**占多少。目标 precision@5 **≥ 0.80**。
- **J2 保持率**：真值集换成「30 天前写入的承诺」，其余同上。目标 **≥ 0.90**
  （人类无复习 6 天只剩 25%，这条是「胜过人」最硬的一条）。

## 三条口径（缺一条数字就不可信）

1. **与生产同一条召回**：直接调 `memory.recall(...)`，参数对齐
   `handler._get_self_memory_context`（主体=`bot_qq`、`target_qq`=对方、
   `grounded_only=True`、取 top5）——不自己写一套 SQL 排序，
   否则量到的是尺子不是系统（教训 #37）。
2. **只读**：`reinforce_after=False`。判据工具跑一次不能改写 `recall_count` / `importance`
   （第 11 轮写放大的教训：底层被调用 ≠ 她想起来了）。
3. **真值集只用「召回取得到的」**：`trust_level='verified'` 且 `target_qq` 是对方。
   523 条 active promise 里有 56 条是 `legacy_unverified`（且全部没有对象归属），
   把它们算进分母是冤枉系统——那是 2026-08-16 治理定死的「legacy 不自动注入」，
   与记忆能力无关。

## 用法

    python tools/判据_J1线索回忆.py               # J1
    python tools/判据_J1线索回忆.py --retention   # J2（30 天前的承诺）
    python tools/判据_J1线索回忆.py --users 5 --show-miss

输出同时落 `日志/_判据J1.json`（或 `--json` 指定），供下轮做回归对比。
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from agent.embeddings import EmbeddingEngine
from agent.memory import MemorySystem
from agent.store import Store

# 三种问法轮换——单一问法会让 BGE 的措辞偏好冒充「记忆能力」
QUESTIONS = [
    "你还记得你答应过我什么吗？",
    "你之前答应过我的事，能再说一遍吗？",
    "我让你帮我做的事，你还记得哪些？",
]

TARGET = {"J1": 0.80, "J2": 0.90}
CUE_TARGET = 0.60     # J2 线索化口径的工程目标（真实对话里对方一开口就带线索）
TOP_K = 5


def load_bot_qq() -> str:
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    return str((cfg.get("bot") or {}).get("qq_id") or "")


def candidate_targets(store, bot_qq: str, min_truth: int) -> list[tuple[str, int, int]]:
    """挑「真值集足够大」的对象——真值只有 2 条时 precision@5 没有意义。

    同时返回该对象名下 verified 记忆总数：**这是随机基线**。
    不报随机基线的 precision 没有信息量——如果某个对象名下 90% 的记忆都是
    promise，随便召回 5 条也能得 0.9（教训 #37：判据必须带对照）。
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT target_qq, COUNT(1) AS total, "
            "       SUM(CASE WHEN key='promise' THEN 1 ELSE 0 END) AS prom "
            "FROM memories "
            "WHERE qq_id=? AND status='active' AND trust_level='verified' "
            "AND COALESCE(target_qq,'') != '' "
            "GROUP BY target_qq HAVING prom >= ? ORDER BY prom DESC",
            (bot_qq, int(min_truth)),
        ).fetchall()
    return [(str(r[0]), int(r[2] or 0), int(r[1] or 0))
            for r in rows if str(r[0] or "").strip()]


def truth_set(store, bot_qq: str, target_qq: str, retention: bool,
              days: int = 30) -> dict[int, dict]:
    """真值集：该对象的**已核验**承诺。retention=True 时只留 `days` 天前的。"""
    sql = ("SELECT id, value, timestamp FROM memories "
           "WHERE qq_id=? AND key='promise' AND status='active' "
           "AND trust_level='verified' AND target_qq=?")
    args: list = [bot_qq, target_qq]
    if retention:
        sql += " AND timestamp <= datetime('now', ?)"
        args.append("-%d day" % int(days))
    with store._connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return {int(r[0]): {"value": str(r[1] or ""), "timestamp": str(r[2] or "")}
            for r in rows}


def measure(memory, engine, bot_qq: str, target_qq: str,
            truth: dict[int, dict], questions: list[str]) -> list[dict]:
    """跑一个对象的全部问法。只读：reinforce_after=False。"""
    results = []
    for question in questions:
        entries = memory.recall(
            bot_qq, limit=10, query_text=question, embed_engine=engine,
            target_qq=target_qq, grounded_only=True, reinforce_after=False,
        )
        top = list(entries or [])[:TOP_K]
        hits, misses = [], []
        for entry in top:
            mid = int(getattr(entry, "id", 0) or 0)
            value = str(getattr(entry, "value", "") or "")
            (hits if mid in truth else misses).append(
                {"id": mid, "value": value[:48]})
        results.append({
            "question": question,
            "top_n": len(top),
            "hits": len(hits),
            "precision": round(len(hits) / len(top), 4) if top else 0.0,
            "hit_ids": [h["id"] for h in hits],
            "hit_values": [h["value"] for h in hits],
            "miss_values": [m["value"] for m in misses],
        })
    return results


def cue_text(value: str) -> str:
    """从一条承诺的内容里摘出「线索」——判据里的 encoding specificity。

    为什么要线索化：J2 的泛问（「你答应过我什么」）是**自由回忆**，不携带任何
    具体线索，对老记忆天然不利。人类在同样条件下也记不住（Ebbinghaus）。
    线索化口径问的是「提到这件事时，她能不能想起当时答应过什么」——
    这才是真实对话里发生的事（对方一开口就是线索）。
    """
    text = str(value or "").strip()
    for prefix in ("我答应过", "答应过", "我答应", "答应",
                   "说好", "说过", "承诺过", "承诺"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("，,：: 我你")
            break
    return text[:14]


def measure_cue(memory, engine, bot_qq: str, target_qq: str,
                truth: dict[int, dict], limit: int = 10) -> list[dict]:
    """逐条测「给线索能不能召回那一条」（recall@5 / @10）。"""
    rows = []
    for memory_id, meta in truth.items():
        cue = cue_text(meta["value"])
        if not cue:
            continue
        question = "你还记得「%s」这件事吗？" % cue
        entries = memory.recall(
            bot_qq, limit=limit, query_text=question, embed_engine=engine,
            target_qq=target_qq, grounded_only=True, reinforce_after=False,
        )
        ids = [int(getattr(e, "id", 0) or 0) for e in (entries or [])]
        rows.append({
            "id": memory_id, "cue": cue, "question": question,
            "hit5": memory_id in ids[:5],
            "hit10": memory_id in ids[:limit],
            "returned": len(ids),
        })
    return rows


def run_cue(memory, engine, store, bot_qq: str, targets, args) -> int:
    """J2 线索化：给每条老承诺一个线索，看那一条能不能被召回。"""
    print("=" * 78)
    print("J2-cue 线索化（%d 天前承诺，recall@5 目标 ≥%.2f）" % (
        args.days, CUE_TARGET))
    print("主体 bot_qq=%s  口径=memory.recall(limit=10, reinforce_after=False)"
          % bot_qq)

    detail = []
    hits5 = hits10 = total = 0
    rnd_sum = 0.0
    for target_qq, truth_n, total_n in targets:
        truth = truth_set(store, bot_qq, target_qq, args.retention, args.days)
        if not truth:
            print("  %s  该窗口内没有已核验承诺——跳过" % target_qq)
            continue
        rows = measure_cue(memory, engine, bot_qq, target_qq, truth)
        if not rows:
            continue
        h5 = sum(1 for r in rows if r["hit5"])
        h10 = sum(1 for r in rows if r["hit10"])
        # 随机基线：随机取 5 条恰好命中那一条的概率
        random_baseline = round(5.0 / total_n, 4) if total_n else 0.0
        rnd_sum += random_baseline
        hits5 += h5
        hits10 += h10
        total += len(rows)
        print("-" * 78)
        print("对象 %s  待测 %d 条 / 名下已核验记忆 %d 条  "
              "→ 随机基线 recall@5 ≈ %.3f" % (
                  target_qq, len(rows), total_n, random_baseline))
        print("  recall@5 = %.2f（%d/%d）   recall@10 = %.2f（%d/%d）" % (
            h5 / len(rows), h5, len(rows), h10 / len(rows), h10, len(rows)))
        if args.show_miss:
            for row in rows:
                if not row["hit5"]:
                    print("      ✗ 「%s」→ 返回 %d 条，该条不在前5" % (
                        row["cue"], row["returned"]))
        detail.append({"target_qq": target_qq, "n": len(rows),
                       "recall5": round(h5 / len(rows), 4),
                       "recall10": round(h10 / len(rows), 4),
                       "random_baseline": random_baseline, "rows": rows})

    if not total:
        print("❌ 没有可测条目", file=sys.stderr)
        return 2
    recall5 = round(hits5 / total, 4)
    recall10 = round(hits10 / total, 4)
    baseline = round(rnd_sum / len(detail), 4) if detail else 0.0
    lift = round(recall5 / baseline, 1) if baseline else 0.0
    ok = recall5 >= CUE_TARGET
    print("=" * 78)
    print("J2-cue 基线：**recall@5 = %.4f**（%d/%d） / recall@10 = %.4f" % (
        recall5, hits5, total, recall10))
    print("随机基线 %.3f → **提升 %.1f×**" % (baseline, lift))
    print(("✅ 达标" if ok else "❌ 未达标") + "（目标 ≥%.2f）" % CUE_TARGET)
    print("⚠ 与泛问口径对照：泛问 J2 = 0.2889。两组放在一起才能分清"
          "「检索弱」还是「记忆被时间压没」——只有泛问低、线索化也低才是后者。")

    out = Path(args.json) if args.json else PROJECT_ROOT / "日志" / "_判据J2cue.json"
    try:
        out.write_text(json.dumps({
            "mode": "J2-cue", "bot_qq": bot_qq, "days": args.days,
            "recall5": recall5, "recall10": recall10,
            "target": CUE_TARGET, "random_baseline": baseline,
            "lift": lift, "detail": detail,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细已存 %s" % out)
    except OSError as exc:
        print("⚠ 明细落盘失败: %s" % exc, file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆能力判据 J1/J2（只读）")
    ap.add_argument("--retention", action="store_true",
                    help="跑 J2：真值集只取 30 天前写入的承诺")
    ap.add_argument("--cue", action="store_true",
                    help="跑 J2 线索化口径：给每条老承诺一个线索，测 recall@5")
    ap.add_argument("--users", type=int, default=3, help="测几个对象（默认 3）")
    ap.add_argument("--min-truth", type=int, default=5,
                    help="真值集少于这么多条的对象不测（默认 5）")
    ap.add_argument("--days", type=int, default=30, help="J2 的时间窗（默认 30 天）")
    ap.add_argument("--questions", type=int, default=len(QUESTIONS),
                    help="用前几种问法（默认全部）")
    ap.add_argument("--json", default="", help="明细落盘路径")
    ap.add_argument("--show-miss", action="store_true", help="打印没命中的候选")
    args = ap.parse_args()

    mode = "J2" if args.retention else "J1"
    bot_qq = load_bot_qq()
    if not bot_qq:
        print("❌ config.yaml 里没有 bot.qq_id", file=sys.stderr)
        return 2

    store = Store(str(PROJECT_ROOT / "memory.db"))
    memory = MemorySystem(str(PROJECT_ROOT / "memory.db"), store=store)

    engine = EmbeddingEngine()
    engine.load()
    if not engine.ready:
        print("❌ BGE 未就绪——J1/J2 量的就是语义召回，没有 BGE 测不了"
              "（模型路径见 agent/embeddings.py MODEL_PATHS）", file=sys.stderr)
        return 2

    targets = candidate_targets(store, bot_qq, args.min_truth)
    if not targets:
        print("❌ 没有真值集 ≥%d 的对象" % args.min_truth, file=sys.stderr)
        return 2
    targets = targets[:max(1, args.users)]
    questions = QUESTIONS[:max(1, args.questions)]

    if args.cue:
        return run_cue(memory, engine, store, bot_qq, targets, args)

    print("=" * 78)
    print("%s %s（%s，precision@%d，目标 ≥%.2f）" % (
        mode, "保持率" if args.retention else "线索回忆",
        "30 天前的承诺" if args.retention else "全部已核验承诺",
        TOP_K, TARGET[mode]))
    print("主体 bot_qq=%s  召回口径=memory.recall(grounded_only=True, reinforce_after=False)"
          % bot_qq)

    detail = []
    all_prec: list[float] = []
    all_hits = all_top = 0
    all_rnd: list[float] = []
    for target_qq, truth_n, total_n in targets:
        truth = truth_set(store, bot_qq, target_qq, args.retention, args.days)
        if not truth:
            print("  %s  真值集为空（该窗口内没有已核验承诺）——跳过" % target_qq)
            continue
        random_baseline = round(len(truth) / total_n, 4) if total_n else 0.0
        per = measure(memory, engine, bot_qq, target_qq, truth, questions)
        print("-" * 78)
        print("对象 %s  真值 %d 条 / 名下已核验记忆 %d 条  "
              "→ 随机基线 precision@5 ≈ %.2f" % (
                  target_qq, len(truth), total_n, random_baseline))
        for item in per:
            all_prec.append(item["precision"])
            all_rnd.append(random_baseline)
            all_hits += item["hits"]
            all_top += item["top_n"]
            print("  「%s」top%d 命中 %d  precision=%.2f" % (
                item["question"], item["top_n"], item["hits"], item["precision"]))
            if args.show_miss and item["miss_values"]:
                for miss in item["miss_values"]:
                    print("      ✗ %s" % miss)
        detail.append({"target_qq": target_qq, "truth_n": len(truth),
                       "memory_total": total_n,
                       "random_baseline": random_baseline,
                       "attempts": per, "truth_values": [
                           v["value"][:48] for v in list(truth.values())[:10]]})

    if not all_prec:
        print("❌ 没有任何对象可测", file=sys.stderr)
        return 2

    precision = round(sum(all_prec) / len(all_prec), 4)
    pooled = round(all_hits / all_top, 4) if all_top else 0.0
    baseline = round(sum(all_rnd) / len(all_rnd), 4) if all_rnd else 0.0
    lift = round(precision / baseline, 2) if baseline else 0.0
    ok = precision >= TARGET[mode]
    print("=" * 78)
    print("%s 基线：precision@%d 均值 **%.4f**（%d 问，按问平均）/ 合并 **%.4f**（%d/%d）" % (
        mode, TOP_K, precision, len(all_prec), pooled, all_hits, all_top))
    print("随机基线 %.4f  →  **提升 %.2f×**（这才是这把尺子的信息量；"
          "只报绝对精度不报基线等于没测）" % (baseline, lift))
    print(("✅ 达标" if ok else "❌ 未达标") + "（目标 ≥%.2f）" % TARGET[mode])

    out = Path(args.json) if args.json else PROJECT_ROOT / "日志" / ("_判据%s.json" % mode)
    try:
        out.write_text(json.dumps({
            "mode": mode, "bot_qq": bot_qq, "top_k": TOP_K,
            "target": TARGET[mode], "precision_mean": precision,
            "precision_pooled": pooled, "random_baseline": baseline,
            "lift": lift, "questions": questions,
            "detail": detail,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("明细已存 %s" % out)
    except OSError as exc:
        print("⚠ 明细落盘失败: %s" % exc, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
