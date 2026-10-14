"""
米雪儿事实簇提取测试 — 从历史聊天记录中提取结构化记忆
双击运行，结果会保留在窗口里。
"""
import sys, os, asyncio, traceback

# 强制工作目录 = 项目根目录（兼容双击运行）
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from agent.store import Store
from agent.embeddings import EmbeddingEngine
from agent.memory import MemorySystem
import httpx


async def main():
    print("=" * 60)
    print("  糖糖 · 事实簇提取测试")
    print("  目标: 米雪儿 (3922400700)")
    print("=" * 60)

    # ── 1. 加载配置 ──
    try:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        llm = cfg.get("llm", {})
        api_key = llm.get("api_key", os.getenv("DEEPSEEK_API_KEY", ""))
        base_url = llm.get("base_url", "https://api.deepseek.com")
        model = llm.get("model", "deepseek-chat")
        print(f"\n🔑 模型: {model}")
        print(f"   端点: {base_url}")
        if not api_key:
            print("❌ 未找到 API Key！请在 config.yaml 的 llm.api_key 中设置")
            return
    except FileNotFoundError:
        print("❌ 找不到 config.yaml，请确认脚本在项目根目录下运行")
        print(f"   当前目录: {os.getcwd()}")
        return

    # ── 2. 初始化 ──
    print("\n📦 初始化...")
    store = Store("memory.db")
    engine = EmbeddingEngine()
    engine.load()
    print(f"   BGE 语义向量: {'✅ 就绪' if engine.ready else '⚠️ 未就绪（将跳过语义匹配）'}")

    mem = MemorySystem(store=store)

    qq_id = "3922400700"
    person = store.get_or_create_person(qq_id)
    nickname = person.get("nickname", qq_id)
    print(f"   目标用户: {nickname} ({qq_id})")

    # ── 3. 获取聊天记录 ──
    import sqlite3, re as _re
    conn = sqlite3.connect("memory.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT message, timestamp FROM chat_log "
        "WHERE qq_id=? AND group_id='' AND is_bot_reply=0 "
        "ORDER BY id ASC",
        (qq_id,)
    ).fetchall()
    conn.close()

    messages = []
    for r in rows:
        msg = r["message"] or ""
        clean = _re.sub(r'\[CQ:[^\]]+\]', '', msg).strip()
        if clean and len(clean) >= 4:
            messages.append({"message": clean, "timestamp": r["timestamp"]})

    print(f"\n📨 消息统计: {len(messages)} 条有效 / {len(rows)} 条原始")

    if len(messages) < 5:
        print("⚠️ 消息太少，跳过提取")
        return

    # ── 4. LLM 调用器 ──
    async def llm_call(system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": 4096,
                    "temperature": 0.3,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"API 返回 {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            if "choices" not in data:
                raise RuntimeError(f"API 响应无 choices: {json.dumps(data, ensure_ascii=False)[:300]}")
            return data["choices"][0]["message"]["content"]

    # ── 5. 分批提取 ──
    batch_size = 50
    total_new_clusters = 0
    total_new_facts = 0
    total_batches = (len(messages) + batch_size - 1) // batch_size

    print(f"\n🔄 开始分批提取 (共 {total_batches} 批，每批 {batch_size} 条)...\n")
    # 先清空已有数据，避免旧批次结果干扰
    existing_ids = [c["id"] for c in store.get_fact_clusters(qq_id)]
    for cid in existing_ids:
        store.delete_cluster(cid)
    if existing_ids:
        print(f"   🧹 已清理 {len(existing_ids)} 个旧簇，重新提取...\n")

    for i in range(0, len(messages), batch_size):
        batch = messages[i:i + batch_size]
        batch_num = i // batch_size + 1
        print(f"📦 批次 {batch_num}/{total_batches}: {len(batch)} 条消息...", end=" ", flush=True)

        try:
            result = await mem.extract_fact_clusters(
                qq_id=qq_id,
                messages=batch,
                nickname=nickname,
                llm_call=llm_call,
                embed_engine=engine,
            )
            total_new_clusters += result["new_clusters"]
            total_new_facts += result["new_facts"]
            print(f"+{result['new_clusters']}簇 +{result['new_facts']}条")
        except Exception as e:
            print(f"❌ 失败: {e}")

    # ── 5.5 合并相似簇 ──
    print(f"\n🔀 检查相似簇并合并...", end=" ", flush=True)
    try:
        merged = await mem.merge_similar_clusters(qq_id, llm_call, engine)
        print(f"合并了 {merged} 对相似簇")
    except Exception as e:
        print(f"❌ 合并失败: {e}")

    # ── 6. 展示结果 ──
    print("\n" + "=" * 60)
    print(f"📊 总计: {total_new_clusters}个新簇, {total_new_facts}条新事实")
    print("=" * 60)

    clusters = store.get_fact_clusters(qq_id)
    print(f"\n当前 {nickname} 的事实簇 ({len(clusters)} 个):\n")

    if not clusters:
        print("  (暂无事实簇——可能所有批次提取都失败了)")
    else:
        for c in clusters:
            facts = store.get_cluster_facts(c["id"])
            perm_label = {"permanent": "🔒", "stable": "📌", "normal": "📝", "transient": "⏳"}.get(
                c.get("permanence", ""), ""
            )
            print(f"  {perm_label} [{c['category']}] {c['title']} ({c['fact_count']}条)")
            print(f"     {c.get('summary', '')[:120]}")
            for f in facts[:5]:
                print(f"     · {f['fact']} (重要性:{f['importance']}, 置信度:{f['confidence']:.0%})")
            print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\n❌ 未预期的错误:")
        traceback.print_exc()
    finally:
        print("\n" + "=" * 60)
        input("按回车退出...")
