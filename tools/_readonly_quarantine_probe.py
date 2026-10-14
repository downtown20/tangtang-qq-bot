"""只读取证：确认生产库 synthetic 合并行格式（任务 A 用，绝不写库）"""
import sqlite3

conn = sqlite3.connect("file:memory.db?mode=ro", uri=True)
cur = conn.cursor()
patterns = [
    "【同时有%个人找你%",
    "【同时有%",
    "【对方连续发了多条消息%",
    "%请在一段回复里自然地回应所有人%",
    "%请只做一次回复%",
]
for pat in patterns:
    n = cur.execute("SELECT COUNT(*) FROM chat_log WHERE message LIKE ?", (pat,)).fetchone()[0]
    print(f"{pat!r:60s} -> {n}")

print("--- 群合并样本（最新3条 + 最老1条）---")
for r in cur.execute(
    "SELECT id, qq_id, group_id, substr(message,1,90), timestamp FROM chat_log "
    "WHERE message LIKE '【同时有%' ORDER BY id DESC LIMIT 3"
).fetchall():
    print(r)
for r in cur.execute(
    "SELECT id, qq_id, group_id, substr(message,1,90), timestamp FROM chat_log "
    "WHERE message LIKE '【同时有%' ORDER BY id ASC LIMIT 1"
).fetchall():
    print(r)
print("--- 私聊合并样本 ---")
for r in cur.execute(
    "SELECT id, qq_id, substr(message,1,90), timestamp FROM chat_log "
    "WHERE message LIKE '【对方连续发了多条消息%' ORDER BY id DESC LIMIT 2"
).fetchall():
    print(r)
conn.close()
