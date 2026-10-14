"""
一次性垃圾记忆清理脚本
用法：python tools/清理垃圾记忆.py [--dry-run]

清理内容：
1. 推理泄露：value 含 SKIP/前者/后者 的记忆
2. 重复记忆：同 qq_id + 同 value 的保留最早一条，删其余
3. 自贬碎片：value 含"我是废物"等已被校验层拦截的旧数据（可选，默认不执行）

安全：
- 默认 dry-run 模式，只预览不删除
- 清理前自动备份到 memories_backup 表
- 每项清理输出具体删了什么
"""

import sqlite3, sys, os

# 双击运行时确保工作目录正确
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "memory.db"
DRY_RUN = "--dry-run" in sys.argv or "--no-dry-run" not in sys.argv


def main():
    conn = sqlite3.connect(DB_PATH)
    total_deleted = 0

    # ── 1. 推理泄露 ──
    leaks = conn.execute(
        "SELECT id, qq_id, value FROM memories WHERE "
        "value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
    ).fetchall()

    print(f"\n=== 1. 推理泄露 ({len(leaks)} 条) ===")
    for r in leaks:
        print(f"  id={r[0]} qq={r[1]}: \"{r[2][:100]}\"")

    if not DRY_RUN and leaks:
        ids = [str(r[0]) for r in leaks]
        # 备份
        conn.execute(
            f"INSERT INTO memories_backup SELECT * FROM memories WHERE id IN ({','.join(ids)})"
        )
        conn.execute(f"DELETE FROM memories WHERE id IN ({','.join(ids)})")
        total_deleted += len(leaks)

    # ── 2. 重复记忆 ──
    dups = conn.execute(
        "SELECT qq_id, value, COUNT(*) as cnt, MIN(id) as keep_id FROM memories "
        "GROUP BY qq_id, value HAVING cnt > 1 ORDER BY cnt DESC"
    ).fetchall()

    print(f"\n=== 2. 重复记忆 ({len(dups)} 组) ===")
    dup_total = 0
    for qq, val, cnt, keep_id in dups:
        del_count = cnt - 1
        dup_total += del_count
        if del_count >= 3:
            print(f"  {qq}: \"{val[:60]}...\" x{cnt} → 保留{keep_id}, 删{del_count}条")

    if not DRY_RUN and dups:
        for qq, val, cnt, keep_id in dups:
            conn.execute(
                "INSERT INTO memories_backup SELECT * FROM memories "
                "WHERE qq_id=? AND value=? AND id != ?", (qq, val, keep_id)
            )
            conn.execute(
                "DELETE FROM memories WHERE qq_id=? AND value=? AND id != ?",
                (qq, val, keep_id)
            )
        total_deleted += dup_total

    # ── 提交 ──
    if DRY_RUN:
        print(f"\n🔍 DRY RUN —— 将删除 {len(leaks) + dup_total} 条")
        print("    执行: python tools/清理垃圾记忆.py --no-dry-run")
    else:
        conn.commit()
        print(f"\n✅ 已删除 {total_deleted} 条垃圾记忆（原数据备份到 memories_backup）")

    # ── 验证 ──
    remaining_leaks = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE "
        "value LIKE '%SKIP%' OR value LIKE '%前者%' OR value LIKE '%后者%'"
    ).fetchone()[0]
    remaining_dups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT qq_id, value, COUNT(*) as cnt FROM memories "
        "GROUP BY qq_id, value HAVING cnt > 1)"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    print(f"验证: 泄露{remaining_leaks}条 | 重复{remaining_dups}组 | 总记忆{total}条")

    conn.close()


if __name__ == "__main__":
    try:
        if not os.path.exists(DB_PATH):
            print(f"❌ 找不到数据库文件: {os.path.abspath(DB_PATH)}")
            print(f"   当前目录: {os.getcwd()}")
            input("\n按回车退出...")
            sys.exit(1)
        main()
    except Exception as e:
        print(f"\n❌ 出错: {e}")
        import traceback
        traceback.print_exc()
    print()
    input("按回车退出...")
if __name__ == "__main__":
    main()
