# -*- coding: utf-8 -*-
"""记忆能力判据 J3（抗干扰 / 串忆）——离线量尺（2026-10-25 第 15 轮）。

## 为什么这条容错是零

判据文档 §4：J1/J2（想不起来）是「烦人」，**J3/J4（记错了）是「伤害」**。
「记不起来可以问，记错了会伤人」——所以 J3 的目标是 **串忆率 = 0**，非 0 即事故级别。

## 三把检查（对应三种真实事故形态）

| # | 检查 | 对应的事故 |
|---|---|---|
| 1 | 用户自述记忆（`qq_id`=某人）却带着**另一个**对象的 `target_qq` | 把甲的陈述记到乙名下 |
| 2 | bot 自忆的 `target_qq` **从未在 chat_log 出现过** | 幽灵对象：内容归属一个不存在的人（含 LLM 编造对象） |
| 3 | 同一条原话（`memory_evidence.chat_id`）被**两个不同对象**的记忆引用 | 一句话被拆着记给两个人 → 迟早张冠李戴 |

第 1 条在 2026-10-25 实测 **0 条**（干净）；第 2/3 条是本轮第一次量。

## 用法

    python tools/判据_J3抗干扰.py             # 量生产库
    python tools/判据_J3抗干扰.py --selftest  # 阳性对照：临时库里种 3 条异常，检查器必须全抓到
    python tools/判据_J3抗干扰.py --show 10   # 打印前 N 条样例

`--selftest` 是**必跑**的一步（教训 #37）：没有阳性对照的检查器，
「报 0」可能只是它从来看不见东西。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml


def load_bot_qq() -> str:
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    return str((cfg.get("bot") or {}).get("qq_id") or "")


# ══════════════════════════════════════════════════════════════════
# 三把检查（纯函数：传入连接，返回结果——便于 selftest）
# ══════════════════════════════════════════════════════════════════
def check1_user_memory_points_elsewhere(conn, bot_qq: str, show: int = 0) -> dict:
    """用户自述记忆不该带别的对象的 target_qq（那是把甲的事记到乙名下）。"""
    rows = conn.execute(
        "SELECT id, qq_id, target_qq, substr(value,1,40) FROM memories "
        "WHERE qq_id != ? AND COALESCE(target_qq,'') != '' AND target_qq != qq_id "
        "AND status='active'", (bot_qq,)).fetchall()
    total = conn.execute(
        "SELECT COUNT(1) FROM memories WHERE qq_id != ? AND status='active'",
        (bot_qq,)).fetchone()[0]
    return {"name": "用户自述记忆指向他人", "bad": len(rows), "total": total,
            "samples": [list(r) for r in rows[:show]]}


def check2_ghost_targets(conn, bot_qq: str, show: int = 0) -> dict:
    """bot 自忆的 target_qq 必须在 chat_log 里真的出现过——否则是幽灵对象。"""
    targets = conn.execute(
        "SELECT target_qq, COUNT(1) FROM memories "
        "WHERE qq_id=? AND COALESCE(target_qq,'') != '' "
        "GROUP BY target_qq ORDER BY 2 DESC", (bot_qq,)).fetchall()
    ghost = []
    for target_qq, count in targets:
        exists = conn.execute(
            "SELECT 1 FROM chat_log WHERE qq_id=? LIMIT 1", (str(target_qq),),
        ).fetchone()
        if not exists:
            ghost.append([str(target_qq), int(count)])
    total = conn.execute(
        "SELECT COUNT(1) FROM memories WHERE qq_id=? AND COALESCE(target_qq,'') != ''",
        (bot_qq,)).fetchone()[0]
    ghost_rows = sum(c for _, c in ghost)
    return {"name": "bot 自忆指向从未出现的人", "bad": ghost_rows,
            "total": total, "ghost_targets": len(ghost),
            "samples": ghost[:show] if show else []}


def check3_evidence_shared_across_targets(conn, bot_qq: str, show: int = 0) -> dict:
    """同一条原话被两个不同对象的记忆引用 → 迟早张冠李戴。"""
    rows = conn.execute(
        "SELECT me.chat_id, COUNT(DISTINCT m.target_qq) AS c, "
        "       GROUP_CONCAT(DISTINCT m.target_qq) "
        "FROM memory_evidence me JOIN memories m ON m.id = me.memory_id "
        "WHERE m.qq_id=? AND COALESCE(m.target_qq,'') != '' "
        "GROUP BY me.chat_id HAVING c > 1", (bot_qq,)).fetchall()
    total = conn.execute(
        "SELECT COUNT(DISTINCT me.chat_id) FROM memory_evidence me "
        "JOIN memories m ON m.id = me.memory_id "
        "WHERE m.qq_id=? AND COALESCE(m.target_qq,'') != ''", (bot_qq,),
    ).fetchone()[0]
    return {"name": "同一句原话被记给两个对象", "bad": len(rows), "total": total,
            "samples": [list(r) for r in rows[:show]]}


CHECKS = (check1_user_memory_points_elsewhere,
          check2_ghost_targets,
          check3_evidence_shared_across_targets)


def scan(conn, bot_qq: str, show: int = 0) -> list[dict]:
    return [fn(conn, bot_qq, show) for fn in CHECKS]


# ══════════════════════════════════════════════════════════════════
# 阳性对照：临时库里种异常，检查器必须全抓到
# ══════════════════════════════════════════════════════════════════
SELFTEST_DDL = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY, qq_id TEXT, target_qq TEXT, value TEXT,
    status TEXT, key TEXT);
CREATE TABLE chat_log (id INTEGER PRIMARY KEY, qq_id TEXT, message TEXT);
CREATE TABLE memory_evidence (
    memory_id INTEGER, chat_id INTEGER, relation TEXT, created_at TEXT);
"""


def selftest() -> int:
    bot = "999"
    conn = sqlite3.connect(":memory:")
    conn.executescript(SELFTEST_DDL)
    conn.execute("INSERT INTO chat_log (id, qq_id, message) VALUES (1,'111','hi')")
    conn.execute("INSERT INTO chat_log (id, qq_id, message) VALUES (2,'222','yo')")
    # 正常样本（不该被抓）
    conn.execute("INSERT INTO memories VALUES (1,'111','','正常自述','active','said')")
    conn.execute("INSERT INTO memories VALUES (2,?, '111','正常自忆','active','promise')", (bot,))
    conn.execute("INSERT INTO memory_evidence VALUES (2,1,'support','')")
    # 异常 1：用户记忆带别人的 target
    conn.execute("INSERT INTO memories VALUES (3,'111','222','把甲的事记到乙名下','active','said')")
    # 异常 2：幽灵对象 333 从未在 chat_log 出现
    conn.execute("INSERT INTO memories VALUES (4,?, '333','幽灵对象的事','active','promise')", (bot,))
    # 异常 3：同一条原话（chat_id=1）被 111 和 222 两个对象引用
    conn.execute("INSERT INTO memories VALUES (5,?, '222','同一句原话记给两个人','active','promise')", (bot,))
    conn.execute("INSERT INTO memory_evidence VALUES (5,1,'support','')")

    results = scan(conn, bot)
    conn.close()
    got = {r["name"]: r["bad"] for r in results}
    expect = {"用户自述记忆指向他人": 1,
              "bot 自忆指向从未出现的人": 1,
              "同一句原话被记给两个对象": 1}
    ok = True
    print("阳性对照（临时库种 3 类异常，检查器必须各抓到 1 条）：")
    for name, want in expect.items():
        actual = got.get(name, -1)
        flag = "✅" if actual == want else "❌"
        if actual != want:
            ok = False
        print("  %s %s：期望 %d，实得 %s" % (flag, name, want, actual))
    print(("✅ 阳性对照通过" if ok else "❌ 阳性对照失败——检查器看不见东西，"
          "它报的 0 不能当结论"))
    return 0 if ok else 2


def main() -> int:
    # GBK 控制台打不出 ✅/❌ 会抛 UnicodeEncodeError —— 那会让「判据通过」
    # 变成「退出码 1」的假失败（2026-10-26 实测：J3 报完 0 异常后崩在最后一行 print）。
    # 量尺的职责是把数字说清楚，不该死在终端编码上。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="记忆能力判据 J3 抗干扰/串忆（只读）")
    ap.add_argument("--selftest", action="store_true", help="阳性对照（必跑）")
    ap.add_argument("--show", type=int, default=5, help="每项打印几条样例")
    ap.add_argument("--json", default="", help="明细落盘路径")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    bot_qq = load_bot_qq()
    if not bot_qq:
        print("❌ config.yaml 里没有 bot.qq_id", file=sys.stderr)
        return 2
    db = PROJECT_ROOT / "memory.db"
    if not db.exists():
        print("❌ 找不到 memory.db", file=sys.stderr)
        return 2

    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    try:
        results = scan(conn, bot_qq, args.show)
    finally:
        conn.close()

    print("=" * 78)
    print("J3 抗干扰 / 串忆（容错 = 0，目标 串忆率 0.0000）")
    worst = 0.0
    for item in results:
        rate = (item["bad"] / item["total"]) if item["total"] else 0.0
        worst = max(worst, rate)
        print("-" * 78)
        print("%s：**%d** 条异常 / %d 条基数  → 占比 %.4f" % (
            item["name"], item["bad"], item["total"], rate))
        for sample in item.get("samples") or []:
            print("    · %s" % (sample,))
    print("=" * 78)
    ok = worst == 0.0
    print(("✅ 串忆率 0" if ok else "❌ 存在串忆，最高占比 %.4f" % worst))

    out = Path(args.json) if args.json else PROJECT_ROOT / "日志" / "_判据J3.json"
    try:
        out.write_text(json.dumps({"bot_qq": bot_qq, "results": results,
                                   "worst_rate": worst}, ensure_ascii=False,
                                  indent=1), encoding="utf-8")
        print("明细已存 %s" % out)
    except OSError as exc:
        print("⚠ 明细落盘失败: %s" % exc, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
