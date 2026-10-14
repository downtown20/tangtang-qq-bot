"""构建聊天索引——清洗消息并 BGE 向量化，带进度条"""
import sqlite3, re, time, sys, os

# 强制设置工作目录为项目根目录（兼容双击运行）
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from agent.embeddings import EmbeddingEngine
from agent.store import Store

store = Store("./memory.db")
engine = EmbeddingEngine()
engine.load()

if not engine.ready:
    print("❌ BGE 未就绪")
    sys.exit(1)

conn = sqlite3.connect("./memory.db")
total_all = conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0]
indexed_before = conn.execute("SELECT COUNT(*) FROM chat_index").fetchone()[0]
print(f"总消息: {total_all}, 已索引: {indexed_before}")

# 分批处理——每次取一段连续 ID，检查是否已索引
BATCH = 10000
start_id = 0
indexed = 0; skipped = 0; errors = 0; t0 = time.time()

while True:
    rows = conn.execute(
        "SELECT id, qq_id, message FROM chat_log WHERE id > ? ORDER BY id LIMIT ?",
        (start_id, BATCH)
    ).fetchall()
    if not rows:
        break

    # 预加载本批次已索引的 ID（批量查询比逐条快）
    batch_ids = [r[0] for r in rows]
    placeholders = ','.join('?' * len(batch_ids))
    indexed_set = set(r[0] for r in conn.execute(
        f"SELECT chat_id FROM chat_index WHERE chat_id IN ({placeholders})",
        batch_ids
    ).fetchall())

    for chat_id, qq_id, msg in rows:
        if chat_id in indexed_set:
            start_id = chat_id
            continue

        try:
            clean = re.sub(r'\[CQ:[^\]]+\]', '', (msg or "")).strip()
            if len(clean) < 2:
                skipped += 1; start_id = chat_id; continue
            vec = engine.encode(clean)
            store.index_chat(chat_id, qq_id, clean, vec)
            indexed += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ⚠ 错误: chat_id={chat_id} — {e}")
        start_id = chat_id

    pct = start_id / total_all * 100
    elapsed = time.time() - t0
    processed = indexed + skipped
    speed = processed / elapsed if elapsed > 0 else 0
    remaining = total_all - start_id
    eta = remaining / speed if speed > 0 else 0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"  [{bar}] {pct:.1f}% | id={start_id}/{total_all} | {speed:.0f}条/秒 | 剩余{eta:.0f}秒 | +{indexed}索引 {skipped}跳过 {errors}错误", flush=True)

conn.close()
elapsed = time.time() - t0
print(f"\n完成: {indexed} 新增, {skipped} 跳过, {errors} 错误 | 耗时 {elapsed:.0f}秒 | 总计 {store.count_chat_index()} 条")
