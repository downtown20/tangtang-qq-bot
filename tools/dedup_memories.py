"""一次性脚本：删除重复记忆（相同 qq_id + key + value）"""
import sqlite3, sys, os

# 自动找项目根目录（脚本在 tools/ 下，memory.db 在上一级）
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)  # tools/ 的父目录
db_path = os.path.join(project_root, "memory.db")

# 如果手动传了路径，优先用传的
if len(sys.argv) > 1:
    db_path = sys.argv[1]

print(f"数据库路径: {db_path}")
if not os.path.exists(db_path):
    print(f"❌ 找不到数据库！请把 memory.db 放到 {project_root} 下")
    input("按回车退出...")
    sys.exit(1)

try:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    before = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    print(f"去重前: {before} 条记忆")

    cur.execute("""
        DELETE FROM memories WHERE id NOT IN (
            SELECT MIN(id) FROM memories
            GROUP BY qq_id, key, value
        )
    """)
    deleted = cur.rowcount
    conn.commit()

    after = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    print(f"删除了 {deleted} 条重复")
    print(f"去重后: {after} 条记忆")
    conn.close()
    print("✅ 完成！")
except Exception as e:
    print(f"❌ 出错: {e}")
    import traceback
    traceback.print_exc()

input("按回车退出...")
