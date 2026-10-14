#!python3.10
"""2026-08-16 特摄事故定向清洗——批 2b。

事故：糖糖把「特摄仙人/对时政感兴趣」（2026-06/07 单句提取的 like 记忆）经画像
合成洗白进 people.notes，主人 17:49 纠正「我不爱特摄也不迷时政、蓝发小妹Agent
就是糖糖自己、关系是主仆」后系统无写回路径——错误记忆原样存活。
批 2 已上线 status 真值体系（memories/cluster_facts）+ notes_dirty 画像脏标，
本脚本按事故基线（docs/审查/事故数据基线_20260816.md）做定向清洗。

安全设计：
- 默认 dry-run：只打印将执行的每条动作，不改库
- --apply 才执行；执行前自动一致性备份到 backups/
- 幂等：已非 active 的行跳过；纠正事实按 value 去重
- 全部按 id/文本精确匹配，不模糊删除；矛盾清单不自动裁决（那是批 6 的事）
- 不调用 LLM、不文本编辑 notes——画像整体退役 + notes_dirty=1，
  由 bot 重启后首次合成触发重写（handler._synthesize_profile_task 会话内
  计数从 0 起，首次消息即触发）

用法：
  python tools/cleanup_memory_incident_20260816.py            # dry-run
  python tools/cleanup_memory_incident_20260816.py --apply    # 执行
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "memory.db"
OWNER = "2524950083"

# 主人 17:49 的明确纠正（confidence=1.0, origin=corrected, importance=8）
CORRECTIONS = [
    "成展一直是糖糖的开发者——从代码到修bug，不是后来才变成的",
    "蓝发小妹Agent就是糖糖自己",
    "成展和糖糖的关系是主仆关系（主人-猫娘）",
    "成展不爱特摄，不是特摄迷",
    "成展对时政不感兴趣",
]


def ensure_schema(conn):
    """批 2 schema 迁移（bot 未重启时主库还没有这些列）"""
    for stmt in [
        "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'",
        "ALTER TABLE cluster_facts ADD COLUMN status TEXT DEFAULT 'active'",
        "ALTER TABLE people ADD COLUMN notes_dirty INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def plan(conn) -> list[tuple[str, str, str]]:
    """生成动作清单 [(类型, 描述, SQL), ...]；描述里带实际匹配行数"""
    actions = []
    cur = conn.cursor()

    # 1) 撤销错误种子 like 行（按 id，若行已被日常写入影响则按文本兜底）
    for mid, text in [(7736, "自称特摄仙人"), (8289, "对时政方面比较感兴趣")]:
        rows = cur.execute(
            "SELECT id, value FROM memories WHERE qq_id=? AND id=? AND COALESCE(status,'active')='active'",
            (OWNER, mid)).fetchall()
        for rid, value in rows:
            actions.append(("retract_like", f"#{rid} [{value[:40]}]",
                            f"UPDATE memories SET status='retracted' WHERE id={rid}"))
        if not rows:
            fallback = cur.execute(
                "SELECT id, value FROM memories WHERE qq_id=? AND value LIKE ? AND COALESCE(status,'active')='active'",
                (OWNER, f"%{text[:6]}%")).fetchall()
            for rid, value in fallback:
                actions.append(("retract_like", f"#{rid} [{value[:40]}]",
                                f"UPDATE memories SET status='retracted' WHERE id={rid}"))

    # 2) 退役全部合成画像/画像事实（错误结论贯穿 15 画像 + 63 画像事实——整体退役，
    #    notes_dirty 后由重合成产出新世代）
    rows = cur.execute(
        """SELECT id, key, value FROM memories WHERE qq_id=? AND key IN ('profile_synthesis','fact_synthesis')
           AND COALESCE(status,'active')='active'""", (OWNER,)).fetchall()
    for rid, key, value in rows:
        actions.append(("supersede_synth", f"#{rid} [{key}] {value[:40]}",
                        f"UPDATE memories SET status='superseded' WHERE id={rid}"))

    # 3) 蓝发小妹事件行 → superseded（用户纠正：蓝发小妹Agent就是糖糖自己）
    rows = cur.execute(
        "SELECT id, value FROM memories WHERE qq_id=? AND value LIKE '%蓝发小妹%' AND COALESCE(status,'active')='active'",
        (OWNER,)).fetchall()
    for rid, value in rows:
        actions.append(("supersede_event", f"#{rid} [{value[:40]}]",
                        f"UPDATE memories SET status='superseded' WHERE id={rid}"))

    # 4) 时政/特摄簇事实 → retract + 清所属簇摘要
    rows = cur.execute(
        """SELECT id, cluster_id, fact FROM cluster_facts
           WHERE subject_qq=? AND (fact LIKE '%时政%' OR fact LIKE '%特摄%')
             AND COALESCE(status,'active')='active'""", (OWNER,)).fetchall()
    seen_clusters = set()
    for fid, cid, fact in rows:
        actions.append(("retract_cluster_fact", f"CF#{fid} [{fact[:40]}]",
                        f"UPDATE cluster_facts SET status='retracted' WHERE id={fid}"))
        seen_clusters.add(cid)
    for cid in seen_clusters:
        actions.append(("clear_cluster_summary", f"cluster #{cid}",
                        f"UPDATE fact_clusters SET summary='' WHERE id={cid}"))

    # 5) notes 标脏——禁止继续注入旧画像，等重合成
    actions.append(("notes_dirty", f"people {OWNER}",
                    f"UPDATE people SET notes_dirty=1 WHERE qq_id='{OWNER}'"))

    # 6) 写纠正事实（value 去重——幂等）
    for text in CORRECTIONS:
        exists = cur.execute(
            "SELECT id FROM memories WHERE qq_id=? AND value=? AND COALESCE(status,'active')='active'",
            (OWNER, text)).fetchone()
        if exists:
            continue
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        actions.append(("insert_correction", text[:40],
                        "INSERT INTO memories (qq_id, key, value, timestamp, importance, "
                        f"cognitive, confidence, origin, status) VALUES ('{OWNER}', 'fact_correction', "
                        f"?, '{now}', 8, 'semantic', 1.0, 'corrected', 'active')"))
    return actions


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    actions = plan(conn)
    print(f"{'▶ 执行' if apply else '🔍 dry-run'}：共 {len(actions)} 个动作\n")
    for i, (kind, desc, _sql) in enumerate(actions, 1):
        print(f"{i:>3}. [{kind}] {desc}")

    if not apply:
        print("\n未执行任何改动。确认无误后加 --apply 执行。")
        conn.close()
        return 0

    # 执行前一致性备份
    backup = BASE / "backups" / f"memory_{datetime.now().strftime('%Y%m%d_%H%M')}_batch2b.db"
    backup.parent.mkdir(exist_ok=True)
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    print(f"\n✅ 已备份 → {backup.name}")

    # 执行（每个动作独立提交——幂等，中断可重跑）
    for kind, desc, sql in actions:
        try:
            if kind == "insert_correction":
                conn.execute(sql, (desc,))
            else:
                conn.execute(sql)
            conn.commit()
            print(f"  ✅ {desc}")
        except Exception as e:
            print(f"  ❌ {desc}: {e}")

    # 验证（排除 fact_correction——纠正事实本身合法含「对时政」字样）
    wrong = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE qq_id=? AND COALESCE(status,'active')='active' "
        "AND key != 'fact_correction' AND (value LIKE '%特摄仙人%' OR value LIKE '%对时政%')",
        (OWNER,)).fetchone()[0]
    corr = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE qq_id=? AND key='fact_correction' "
        "AND COALESCE(status,'active')='active'", (OWNER,)).fetchone()[0]
    dirty = conn.execute(
        "SELECT notes_dirty FROM people WHERE qq_id=?", (OWNER,)).fetchone()
    print(f"\n验证：active 残留错误={wrong}（应 0）；纠正事实={corr}（应 5）；"
          f"notes_dirty={dirty[0] if dirty else '?'}（应 1）")
    print("下一步：重启 bot → 主人第一条消息触发画像重合成 → 人工验收新画像。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
