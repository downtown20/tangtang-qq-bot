"""2026-08-17 事故修复：correct_memory 主语误伤回滚 + 正确目标纠正（幂等）。

事故：主人让糖糖纠正「穷到吃外卖」(2377255095) 的「33岁上班族」假记忆，
LLM 无法从昵称解析 QQ 号，把 subject_qq 填成了主人自己 (2524950083)：
  - 误撤主人唯一的 active「大二学生」事实 (7644)
  - 给主人写入 2 条「高一新生」垃圾纠正行 (17475/17476)
  - 触发主人画像重合成 → 新画像 (17477) 丢失「大二学生」

本脚本两段：
  A. 回滚主人 (2524950083) 的误伤——撤销垃圾纠正行、恢复大二事实、补回画像
  B. 对群主 (2377255095) 执行主人原本要的纠正——事故批次的 33岁/三十三岁/
     上班族行全部撤销、写入纠正事实、notes 标脏（停止注入旧画像，等重合成）

2026-08-17 Codex 全天审查收紧：所有写操作带事故行 ID + qq_id + key/预期值
的 CAS 条件——不按宽泛文本匹配（重跑不得撤销同文本的无关事实）；B4 只在
本次实际撤销了错误行时标脏；apply 后自动重新 plan 并断言零变更。

用法：python tools/cleanup_wrong_subject_correction_20260817.py [--apply]
不加 --apply 为 dry-run 只打印将执行的变更。可重复执行，幂等收敛。
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "memory.db"

OWNER = "2524950083"          # 主人（误伤对象）
TARGET = "2377255095"         # 群主「穷到吃外卖」（正确纠正对象）

# 事故行 ID（2026-08-17 上午取证枚举——只动这些行，不按文本宽泛匹配）
GARBAGE_IDS = (17475, 17476)          # 主人身上的「高一新生」垃圾纠正行
OWNER_FACT_ID = 7644                   # 被误撤的「现在是大二学生」事实
OWNER_PROFILE_ROW = 17477              # 丢失大二学生的画像行
TARGET_WRONG_IDS = {
    6609, 13912, 14064, 14065, 14161, 14162, 14553, 14554, 14567, 14801,
    14802, 14822, 14823, 14931, 15174, 15520, 15585, 15667, 15762, 15843,
    16018, 16459, 16477,
}

SYNTHESIS_KEYS = {"profile_synthesis", "fact_synthesis"}

CORRECTED_FACT_TARGET = "今年7月靠指标生名额刚考上一中，是高一新生，8月19日-29日要军训"
CORRECTED_FACT_OWNER = "成展是大学大二学生（不是高中生）"


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def plan(conn) -> list[str]:
    """返回将执行的变更清单（幂等收敛：执行后重新 plan 必须为空）"""
    ops = []

    # A1 垃圾纠正行（CAS：事故行 ID + qq + key + active）
    for mid in GARBAGE_IDS:
        row = conn.execute(
            "SELECT value, status FROM memories WHERE id=? AND qq_id=? AND key='fact_correction'",
            (mid, OWNER),
        ).fetchone()
        if row and row["status"] == "active":
            ops.append(f"A1 撤销垃圾纠正行 id={mid}: {row['value'][:40]}")

    # A2 恢复被误撤事实（CAS：ID + qq + key + value + retracted）
    row = conn.execute(
        "SELECT value, status FROM memories WHERE id=? AND qq_id=? "
        "AND key='fact' AND value='现在是大二学生'",
        (OWNER_FACT_ID, OWNER),
    ).fetchone()
    if row and row["status"] == "retracted":
        ops.append(f"A2 恢复被误撤事实 id={OWNER_FACT_ID}: {row['value']} → active")

    # A3 确保有纠正事实支撑「大二学生」（幂等：存在即跳过）
    exists = conn.execute(
        "SELECT id FROM memories WHERE qq_id=? AND COALESCE(status,'active')='active' "
        "AND value=? AND (key='fact' OR key='fact_correction')",
        (OWNER, CORRECTED_FACT_OWNER),
    ).fetchone()
    if not exists:
        ops.append(f"A3 写入纠正事实: {CORRECTED_FACT_OWNER}")

    # A4 画像 notes 补回「大二学生」（幂等：替换是收敛的）
    p = conn.execute("SELECT notes FROM people WHERE qq_id=?", (OWNER,)).fetchone()
    if p and p["notes"] and "大二学生" not in p["notes"] and "成展是糖糖" in p["notes"]:
        ops.append("A4 people.notes 补回「大二学生」")

    # A5 画像行补回（CAS：ID + qq + key + 当前值不含大二）
    row = conn.execute(
        "SELECT value FROM memories WHERE id=? AND qq_id=? AND key='profile_synthesis'",
        (OWNER_PROFILE_ROW, OWNER),
    ).fetchone()
    if row and "大二学生" not in row["value"] and "成展是糖糖" in row["value"]:
        ops.append("A5 画像行 17477 补回「大二学生」")

    # B1 事故批次的错误行（CAS：行 ID + qq + active；合成行 superseded，原始行 retracted）
    revoked = 0
    for mid in sorted(TARGET_WRONG_IDS):
        row = conn.execute(
            "SELECT key, value, status FROM memories WHERE id=? AND qq_id=?",
            (mid, TARGET),
        ).fetchone()
        if row and row["status"] == "active":
            st = "superseded" if row["key"] in SYNTHESIS_KEYS else "retracted"
            ops.append(f"B1 {st} id={mid} ({row['key']}): {row['value'][:45]}")
            revoked += 1

    # B3 纠正事实（幂等：存在即跳过）
    exists = conn.execute(
        "SELECT id FROM memories WHERE qq_id=? AND COALESCE(status,'active')='active' "
        "AND value=? AND key IN ('fact','fact_correction')",
        (TARGET, CORRECTED_FACT_TARGET),
    ).fetchone()
    if not exists:
        ops.append(f"B3 写入纠正事实: {CORRECTED_FACT_TARGET}")

    # B4 只在本次实际撤销了错误行时标脏（Codex：重复标脏制造无意义重合成）
    p = conn.execute("SELECT notes_dirty FROM people WHERE qq_id=?", (TARGET,)).fetchone()
    if revoked > 0 and p and not p["notes_dirty"]:
        ops.append("B4 群主 notes_dirty=1（旧画像停止注入）")
    return ops


def apply(conn, ops: list[str]):
    with conn:  # 单事务——异常整体回滚
        if any(o.startswith("A1") for o in ops):
            for mid in GARBAGE_IDS:
                conn.execute(
                    "UPDATE memories SET status='retracted' WHERE id=? AND qq_id=? "
                    "AND key='fact_correction' AND COALESCE(status,'active')='active'",
                    (mid, OWNER),
                )
        if any(o.startswith("A2") for o in ops):
            conn.execute(
                "UPDATE memories SET status='active' WHERE id=? AND qq_id=? "
                "AND key='fact' AND value='现在是大二学生' AND status='retracted'",
                (OWNER_FACT_ID, OWNER),
            )
        if any(o.startswith("A3") for o in ops):
            conn.execute(
                "INSERT INTO memories (qq_id, key, value, importance, cognitive, confidence, "
                "origin, status, timestamp) VALUES (?, 'fact_correction', ?, 9, 'semantic', 1.0, "
                "'corrected', 'active', datetime('now','localtime'))",
                (OWNER, CORRECTED_FACT_OWNER),
            )
        if any(o.startswith("A4") for o in ops):
            conn.execute(
                "UPDATE people SET notes=REPLACE(notes, '成展是糖糖', '成展是大二学生，糖糖'), "
                "notes_dirty=0 WHERE qq_id=? AND notes LIKE '%成展是糖糖%'",
                (OWNER,),
            )
        if any(o.startswith("A5") for o in ops):
            conn.execute(
                "UPDATE memories SET value=REPLACE(value, '成展是糖糖', '成展是大二学生，糖糖') "
                "WHERE id=? AND qq_id=? AND key='profile_synthesis'",
                (OWNER_PROFILE_ROW, OWNER),
            )
        for o in ops:
            if o.startswith("B1"):
                mid = int(o.split("id=")[1].split(" ")[0])
                st = "superseded" if o.startswith("B1 superseded") else "retracted"
                conn.execute(
                    "UPDATE memories SET status=? WHERE id=? AND qq_id=? "
                    "AND COALESCE(status,'active')='active'", (st, mid, TARGET),
                )
        if any(o.startswith("B3") for o in ops):
            conn.execute(
                "INSERT INTO memories (qq_id, key, value, importance, cognitive, confidence, "
                "origin, status, timestamp) VALUES (?, 'fact_correction', ?, 9, 'semantic', 1.0, "
                "'corrected', 'active', datetime('now','localtime'))",
                (TARGET, CORRECTED_FACT_TARGET),
            )
        if any(o.startswith("B4") for o in ops):
            conn.execute(
                "UPDATE people SET notes_dirty=1 WHERE qq_id=? AND COALESCE(notes_dirty,0)=0",
                (TARGET,),
            )


def main():
    dry = "--apply" not in sys.argv
    conn = connect()
    ops = plan(conn)
    if dry:
        print("DRY RUN（加 --apply 执行）:")
        for op in ops:
            print(" ", op)
        print(f"共 {len(ops)} 项")
        return
    if not ops:
        print("无变更（已幂等）")
        return
    apply(conn, ops)
    print("已执行:")
    for op in ops:
        print(" ", op)
    # 2026-08-17 Codex 全天审查：apply 后重新 plan 并断言零变更——
    # 幂等收敛由脚本自身证明，不靠口头声明
    leftovers = plan(conn)
    if leftovers:
        print(f"⚠️ 幂等校验失败：执行后仍有 {len(leftovers)} 项待变更:")
        for op in leftovers:
            print(" ", op)
        sys.exit(1)
    print("✅ 幂等校验通过：执行后重新 plan 零变更")


if __name__ == "__main__":
    main()
