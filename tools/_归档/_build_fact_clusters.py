"""
批量构建事实簇 — 为所有有聊天记录的用户提取结构化记忆
双击运行，处理完所有人后显示汇总。
"""
import sys, os, asyncio, json, sqlite3, re as _re

# 强制工作目录 = 项目根目录（兼容双击运行）
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from agent.store import Store
from agent.embeddings import EmbeddingEngine
from agent.memory import MemorySystem
import httpx


async def main():
    print("=" * 60)
    print("  糖糖 · 批量事实簇构建")
    print("=" * 60)

    # ── 1. 加载配置 ──
    try:
        import yaml
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        llm_cfg = cfg.get("llm", {})
        api_key = llm_cfg.get("api_key", os.getenv("DEEPSEEK_API_KEY", ""))
        base_url = llm_cfg.get("base_url", "https://api.deepseek.com")
        model = llm_cfg.get("model", "deepseek-chat")
        if not api_key:
            print("❌ 未找到 API Key")
            return
        print(f"🔑 {model}")
    except FileNotFoundError:
        print("❌ 找不到 config.yaml")
        return

    # ── 2. 初始化 ──
    store = Store("memory.db")
    engine = EmbeddingEngine()
    engine.load()
    print(f"🧠 BGE: {'✅' if engine.ready else '⚠️ 未就绪'}")
    mem = MemorySystem(store=store)

    # ── 3. 找出所有需要处理的用户（私聊+群聊都算） ──
    conn = sqlite3.connect("memory.db")
    rows = conn.execute("""
        SELECT qq_id, COUNT(*) as cnt
        FROM chat_log
        WHERE is_bot_reply=0
        GROUP BY qq_id
        HAVING cnt >= 10
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("❌ 没有符合条件的用户（≥10条消息）")
        return

    targets = [(qq, cnt) for qq, cnt in rows]
    print(f"👥 {len(targets)} 人待处理\n")

    if not targets:
        print("✅ 没有需要处理的用户")
        return

    # ── 4. LLM 调用器（带重试）──
    async def llm_call(system: str, user: str) -> str:
        last_err = None
        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    resp = await client.post(
                        f"{base_url}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
                    if resp.status_code == 200:
                        return resp.json()["choices"][0]["message"]["content"]
                    if resp.status_code == 429:
                        wait = 5 * (attempt + 1)
                        print(f"\n    ⚠️ 限流，{wait}s后重试...", end=" ", flush=True)
                        await asyncio.sleep(wait)
                    else:
                        last_err = f"API {resp.status_code}: {resp.text[:100]}"
                        await asyncio.sleep(2)
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(3)
        raise RuntimeError(f"API 调用失败(重试5次): {last_err}")

    # ── 5. 逐个用户处理（支持断点续跑）──
    total_clusters = 0
    total_facts = 0
    total_merged = 0
    errors = 0

    for idx, (qq_id, msg_count) in enumerate(targets):
        person = store.get_or_create_person(qq_id)
        nickname = person.get("nickname", qq_id)

        # 断点续跑：已有簇的用户跳过
        existing_clusters = store.count_fact_clusters(qq_id)
        if existing_clusters > 0:
            print(f"[{idx+1}/{len(targets)}] {nickname} ({qq_id}) — {msg_count}条消息 → 已有{existing_clusters}簇，跳过")
            total_clusters += existing_clusters
            continue

        print(f"[{idx+1}/{len(targets)}] {nickname} ({qq_id}) — {msg_count}条消息", end=" ", flush=True)

        try:
            # 先清空旧簇
            for c in store.get_fact_clusters(qq_id):
                store.delete_cluster(c["id"])

            # 获取所有消息（私聊+群聊），按时间排序
            conn = sqlite3.connect("memory.db")
            conn.row_factory = sqlite3.Row
            chat_rows = conn.execute(
                "SELECT message, timestamp FROM chat_log "
                "WHERE qq_id=? AND is_bot_reply=0 ORDER BY id ASC",
                (qq_id,)
            ).fetchall()
            conn.close()

            messages = []
            for r in chat_rows:
                clean = _re.sub(r'\[CQ:[^\]]+\]', '', (r["message"] or "")).strip()
                if clean and len(clean) >= 4:
                    messages.append({"message": clean, "timestamp": r["timestamp"]})

            if len(messages) < 8:
                print("→ 有效消息不足8条，跳过")
                continue

            # 分批提取
            batch_size = 50
            user_clusters = 0
            user_facts = 0
            total_batches = (len(messages) + batch_size - 1) // batch_size

            for i in range(0, len(messages), batch_size):
                batch = messages[i:i + batch_size]
                result = await mem.extract_fact_clusters(
                    qq_id=qq_id, messages=batch, nickname=nickname,
                    llm_call=llm_call, embed_engine=engine,
                )
                user_clusters += result["new_clusters"]
                user_facts += result["new_facts"]
                # 每个 batch 输出进度点，10 个 batch 输出一次统计
                batch_no = i // batch_size + 1
                if total_batches <= 10 or batch_no % 10 == 0 or batch_no == total_batches:
                    print(f"\n    📦 {batch_no}/{total_batches} 批 (+{result['new_clusters']}簇 +{result['new_facts']}条)",
                          end=" " if batch_no < total_batches else "\n    ", flush=True)
                else:
                    print(".", end="", flush=True)

            # 合并相似簇
            merged = await mem.merge_similar_clusters(qq_id, llm_call, engine)

            total_clusters += user_clusters
            total_facts += user_facts
            total_merged += merged

            final_count = store.count_fact_clusters(qq_id)
            print(f"→ {final_count}簇 {user_facts}条" + (f" (合并{merged}对)" if merged else ""))

        except Exception as e:
            errors += 1
            print(f"❌ {e}")

    # ── 6. 汇总 ──
    print("\n" + "=" * 60)
    print(f"📊 完成: {len(targets)}人 | +{total_clusters}簇 | +{total_facts}条 | 合并{total_merged}对")
    if errors:
        print(f"⚠️ {errors} 人失败")
    print(f"📊 全局: {store.count_fact_clusters()} 簇 / {len(store.get_all_fact_cluster_subjects())} 人")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"\n❌ 错误: {e}")
        traceback.print_exc()
    finally:
        print("\n")
        input("按回车退出...")
