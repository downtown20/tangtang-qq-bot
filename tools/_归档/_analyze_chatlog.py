import sqlite3
from pathlib import Path

# 项目根由本文件位置算出（tools/_归档/ 的上两级）——2026-10-15 T55：原先写死盘符。
c = sqlite3.connect(str(Path(__file__).resolve().parents[2] / 'memory.db'))

total = c.execute('SELECT COUNT(*) FROM chat_log').fetchone()[0]
private = c.execute('SELECT COUNT(*) FROM chat_log WHERE group_id=""').fetchone()[0]
group = c.execute('SELECT COUNT(*) FROM chat_log WHERE group_id!=""').fetchone()[0]

print(f'总消息: {total}')
print(f'私聊: {private}')
print(f'群聊: {group}')

print('\n按用户统计 (私聊):')
rows = c.execute('SELECT qq_id, COUNT(*), MAX(timestamp), MIN(timestamp) FROM chat_log WHERE group_id="" GROUP BY qq_id ORDER BY COUNT(*) DESC LIMIT 10').fetchall()
for r in rows:
    print(f'  {r[0]}: {r[1]}条, {r[2][:10]} ~ {r[3][:10]}')

print('\n消息样本 (私聊, 随机5条):')
rows = c.execute('SELECT qq_id, is_bot_reply, message FROM chat_log WHERE group_id="" ORDER BY RANDOM() LIMIT 5').fetchall()
for r in rows:
    msg = (r[2] or '')[:100]
    who = '糖糖' if r[1] else '用户'
    print(f'  [{who}→{r[0]}] {msg}')

cq = c.execute('SELECT COUNT(*) FROM chat_log WHERE group_id="" AND message LIKE "%[CQ:%"').fetchone()[0]
print(f'\n含CQ码的私聊: {cq}/{private} ({100*cq/private:.0f}%)')

# 检查米雪儿的数据
print('\n米雪儿(3922400700) 私聊:')
rows = c.execute("SELECT COUNT(*) FROM chat_log WHERE qq_id='3922400700' AND group_id=''").fetchall()
print(f'  私聊: {rows[0][0]}条')

c.close()
