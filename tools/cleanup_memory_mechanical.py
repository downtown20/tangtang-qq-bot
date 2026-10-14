#!python3.10
"""全库机械治理（2026-08-16 批 6）——只做可证明安全的规则，绝不智能清库。

规则（全部 dry-run 先行、幂等、可回滚）：
1. 完全重复 active 记忆（同 qq_id+key+value）→ 只留最新一条，其余标 superseded
2. 孤儿 embedding（memory_id 不存在或对应行非 active）→ 物理删除
3. 90 天前的 superseded 合成快照（profile_synthesis/fact_synthesis）→ 物理删除
   （批 2/3 起旧世代只作审计历史，90 天足够回看，之后释放空间）
4. 矛盾/近义事实 + notes 元话术前缀污染 → 不自动改，生成审核清单
   data/审核清单_机械治理_YYYYMMDD.md，人工确认后走纠正工具/重合成

用法：
  python tools/cleanup_memory_mechanical.py            # dry-run
  python tools/cleanup_memory_mechanical.py --apply    # 执行（先自动备份）
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "memory.db"
AUDIT_DIR = BASE / "data" / "审核清单"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def ensure_schema(conn):
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


def plan(conn) -> list[tuple[str, str]]:
    """返回 [(类型, SQL), ...]——SQL 幂等可重跑"""
    actions = []
    cur = conn.cursor()

    # 1) 完全重复：同 (qq_id, key, value) 且 active，保留最新 id（Codex I8：
    # 旧实现 MIN(id) 保留最旧行——最新行可能带更高 confidence/importance，
    # 退役新版是倒退）
    dups = cur.execute(
        """SELECT qq_id, key, value, MAX(id) as keep_id, COUNT(*) as cnt
           FROM memories WHERE COALESCE(status,'active')='active'
           GROUP BY qq_id, key, value HAVING cnt > 1"""
    ).fetchall()
    for qq, key, value, keep, cnt in dups:
        actions.append(("supersede_dups",
                        f"UPDATE memories SET status='superseded' WHERE qq_id=? AND key=? "
                        f"AND value=? AND id != ? AND COALESCE(status,'active')='active'"))
        # 记录参数——SQL 执行时带参（见 main 的执行逻辑）
        actions[-1] = (actions[-1][0], actions[-1][1], (qq, key, value, keep))

    # 2) 孤儿 embedding：无对应行 / 对应行非 active
    actions.append(("delete_orphan_embeddings",
                    """DELETE FROM memory_embeddings WHERE memory_id NOT IN (
                       SELECT id FROM memories WHERE COALESCE(status,'active')='active')"""))

    # 3) 90 天前的 superseded 合成快照
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M")
    actions.append(("delete_old_superseded_synth",
                    "DELETE FROM memories WHERE status='superseded' "
                    "AND key IN ('profile_synthesis','fact_synthesis') "
                    "AND timestamp < ?", (cutoff,)))

    return actions


def audit_report(conn) -> str:
    """矛盾/近义 + 元话术清单——只列不改"""
    cur = conn.cursor()
    lines = [
        f"# 记忆机械治理审核清单（{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
        "> 本清单由 tools/cleanup_memory_mechanical.py 生成——只列问题不自动修改。",
        "> 处理方式：矛盾事实 → 用 correct_memory 工具或直接 DB 标 status；",
        "> 元话术画像 → 标 notes_dirty=1 等下一次重合成。",
        "",
        "## 一、矛盾/近义事实对（同人同 key，一条含否定词）",
    ]
    # 同 qq_id 同 key 的 like/preference/fact 行：一条含「不/没」否定、一条不含，
    # 且共享至少 2 个 jieba 词 → 疑似矛盾对
    try:
        import jieba as _jb
    except ImportError:
        _jb = None
    rows = cur.execute(
        """SELECT id, qq_id, key, value FROM memories
           WHERE COALESCE(status,'active')='active'
             AND key IN ('like','preference','fact','fact_correction')
           ORDER BY qq_id, key, id"""
    ).fetchall()
    by_group = {}
    for mid, qq, key, value in rows:
        by_group.setdefault((qq, key), []).append((mid, value))
    # 停用词——共享的虚词/泛词不算「同一话题」证据（初版共享词≥2 噪声极大）
    STOP = {"不", "没", "是", "的", "了", "我", "你", "他", "她", "它", "这", "那",
            "在", "有", "和", "与", "也", "就", "都", "还", "很", "好", "吗", "呢",
            "啊", "哦", "吧", "着", "过", "看", "说", "想", "觉得", "认为", "有点",
            "一个", "什么", "怎么", "为什么", "就是", "不是", "这样", "那样", "可以",
            "没有", "东西", "内容", "作品", "题材", "喜欢", "感兴趣", "自称", "这个"}
    found_any = False
    for (qq, key), items in by_group.items():
        if key == "fact_correction":
            continue  # 纠正事实互相比对没有意义（都是对的，方向不同而已）
        neg = [(m, v) for m, v in items if any(w in v for w in ("不爱", "不喜", "不迷", "没兴趣", "不喜欢", "不是"))]
        pos = [(m, v) for m, v in items if not any(w in v for w in ("不爱", "不喜", "不迷", "没兴趣", "不喜欢", "不是"))]
        for nm, nv in neg:
            for pm, pv in pos:
                # 共享内容词：jieba 可用则分词，否则字符 2-gram 近似
                if _jb:
                    ws_n, ws_p = set(_jb.cut(nv)), set(_jb.cut(pv))
                else:
                    ws_n, ws_p = {nv[i:i+2] for i in range(len(nv)-1)}, {pv[i:i+2] for i in range(len(pv)-1)}
                common = (ws_n & ws_p) - STOP
                # ≥2 个共享内容词、且总字数 ≥3——滤掉「我喜欢XX」vs「我不喜欢YY」的假对
                if len(common) >= 2 and sum(len(t) for t in common) >= 3:
                    lines.append(f"- QQ{qq} [{key}] #{pm}「{pv[:50]}」 vs #{nm}「{nv[:50]}」")
                    found_any = True
                    break
    if not found_any:
        lines.append("- （未发现）")

    lines += ["", "## 二、notes 元话术前缀污染"]
    bad = cur.execute(
        "SELECT qq_id, nickname, substr(notes,1,40) FROM people WHERE notes LIKE '%增量更新%' "
        "OR notes LIKE '%根据您提供%' OR notes LIKE '%以下是%'").fetchall()
    if bad:
        for qq, nick, head in bad:
            lines.append(f"- QQ{qq} {nick}: 「{head}…」→ 标 notes_dirty=1 等重合成")
    else:
        lines.append("- （未发现）")

    path = AUDIT_DIR / f"审核清单_机械治理_{datetime.now().strftime('%Y%m%d')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    actions = plan(conn)

    n_dups = sum(1 for a in actions if a[0] == "supersede_dups")
    print(f"{'▶ 执行' if apply else '🔍 dry-run'}：{len(actions)} 个机械动作"
          f"（重复组 {n_dups}、孤儿 embedding、90 天前 superseded 快照）\n")

    report_path = audit_report(conn)
    print(f"📋 审核清单已生成 → {report_path}")

    if not apply:
        print("\n未执行任何改动。确认无误后加 --apply 执行。")
        conn.close()
        return 0

    backup = BASE / "backups" / f"memory_{datetime.now().strftime('%Y%m%d_%H%M')}_batch6.db"
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    print(f"\n✅ 已备份 → {backup.name}")

    for action in actions:
        try:
            if len(action) == 3:
                conn.execute(action[1], action[2])
            else:
                conn.execute(action[1])
            conn.commit()
            print(f"  ✅ [{action[0]}]")
        except Exception as e:
            print(f"  ❌ [{action[0]}]: {e}")
    conn.close()

    # 幂等验证：再跑一遍 dry-run 应无重复组
    conn2 = sqlite3.connect(DB_PATH)
    left = plan(conn2)
    conn2.close()
    if any(a[0] == "supersede_dups" for a in left):
        print("\n⚠️ 仍有重复组残留——请检查")
        return 2
    print("\n✅ 幂等验证通过：重复执行结果相同。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
