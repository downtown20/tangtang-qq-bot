import sqlite3
from pathlib import Path

# 项目根由本文件位置算出（tools/_归档/ 的上两级）——2026-10-15 T55：原先写死盘符。
conn = sqlite3.connect(str(Path(__file__).resolve().parents[2] / 'memory.db'))
c = conn.cursor()
r = c.execute('SELECT nickname, notes, intimacy FROM people WHERE qq_id=?', ('3922400700',)).fetchone()
if r:
    print(f"nickname: {r[0]}")
    print(f"intimacy: {r[2]}")
    print(f"notes: {r[1]}")
else:
    print("NOT FOUND")
c.close()
