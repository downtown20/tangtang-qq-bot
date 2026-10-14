"""
R1-1 + R1-4 剧本测试 —— 不依赖 QQ，单次 LLM 调用验证
"""
import sys, os, json
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from agent.store import Store
from agent.memory import MemorySystem

# 临时 DB
import tempfile
fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)

store = Store(db_path)
mem = MemorySystem(store=store)

# ═══════════════════════════════════════
# 剧本：模拟群聊对话
# ═══════════════════════════════════════
NICKNAME = "小明"
QQ_ID = "100001"
BOT_QQ = "2693914170"

script = [
    # 小明自述——应该被提取
    (NICKNAME, "我在北大读计算机，大三了"),
    (NICKNAME, "奶茶我只喝一点点的四季春"),
    (NICKNAME, "上周去爬了香山，累死了"),
    (NICKNAME, "我室友是东北人，特别能唠"),
    # 糖糖的发言——不应该被当成小明的信息
    ("小糖糖", "我最喜欢悬疑片了，诺兰的每部都看"),
    ("小糖糖", "小明你喜欢看电影吗"),
    (NICKNAME, "糖糖也喜欢悬疑片啊，我也是！"),
    # 小明继续自述
    (NICKNAME, "昨天跟室友去吃了海底捞，排了两小时"),
    (NICKNAME, "我养了一只英短，叫团子"),
    # 干扰项——问句不应被提取
    (NICKNAME, "你觉得我适合学金融吗"),
    (NICKNAME, "糖糖你怎么不说话"),
    # 自贬——不应被提取
    (NICKNAME, "我就是个废物，什么都做不好"),
]

print("📋 剧本对话：")
for speaker, msg in script:
    print(f"  [{speaker}] {msg}")

# ═══════════════════════════════════════
# 调用 LLM 提取
# ═══════════════════════════════════════
import httpx, asyncio

async def run():
    # 读取配置
    import yaml
    with open(os.path.join(PROJECT, "config.yaml"), "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    llm_cfg = config["llm"]

    client = httpx.AsyncClient(timeout=30)

    async def llm_call(sys_prompt, user_msg):
        resp = await client.post(
            f"{llm_cfg['base_url']}/v1/chat/completions",
            headers={"Authorization": f"Bearer {llm_cfg['api_key']}"},
            json={
                "model": llm_cfg["model"],
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.1,
                "max_tokens": 800,
            }
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    messages = [{"message": msg, "timestamp": "2026-08-03 21:00:00"}
                for _, msg in script]

    items = await mem.extract_semantic_memories(
        messages=messages,
        nickname=NICKNAME,
        qq_id=QQ_ID,
        llm_call=llm_call,
        existing_summary="",
    )

    await client.aclose()

    # ═══════════════════════════════════════
    # 验证
    # ═══════════════════════════════════════
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {name}")
        else:
            failed += 1
            print(f"  ❌ {name}  FAILED {detail}")

    print(f"\n📊 提取结果: {len(items)} 条")
    for item in items:
        print(f"  [{item.get('cognitive','?')}] {item.get('type','?')}: {item.get('value','?')} (conf={item.get('confidence','?'):.2f} imp={item.get('importance','?')})")

    # R1-1: 认知类型检查
    # "在北大读计算机" → semantic (身份)
    identity_items = [i for i in items if "北大" in i.get("value", "")]
    if identity_items:
        check("身份→semantic", identity_items[0].get("cognitive") == "semantic",
              f"got: {identity_items[0].get('cognitive')}")

    # "上周去爬了香山" → episodic (事件)
    event_items = [i for i in items if "香山" in i.get("value", "")]
    if event_items:
        check("事件→episodic", event_items[0].get("cognitive") == "episodic",
              f"got: {event_items[0].get('cognitive')}")

    # "昨天跟室友去吃了海底捞" → episodic
    hotpot_items = [i for i in items if "海底捞" in i.get("value", "")]
    if hotpot_items:
        check("具体事件→episodic", hotpot_items[0].get("cognitive") == "episodic",
              f"got: {hotpot_items[0].get('cognitive')}")

    # "我只喝一点点的四季春" → semantic (preference)
    tea_items = [i for i in items if "四季春" in i.get("value", "") or "一点点" in i.get("value", "")]
    if tea_items:
        check("偏好→semantic", tea_items[0].get("cognitive") == "semantic",
              f"got: {tea_items[0].get('cognitive')}")

    # "我室友是东北人" → semantic (关系信息，持续存在)
    roommate_items = [i for i in items if "室友" in i.get("value", "") or "东北" in i.get("value", "")]
    if roommate_items:
        check("室友关系→semantic", roommate_items[0].get("cognitive") == "semantic",
              f"got: {roommate_items[0].get('cognitive')}")

    # "我养了一只英短" → semantic (持续的事实)
    cat_items = [i for i in items if "英短" in i.get("value", "") or "团子" in i.get("value", "")]
    if cat_items:
        check("养宠物→semantic", cat_items[0].get("cognitive") == "semantic",
              f"got: {cat_items[0].get('cognitive')}")

    # R1-4: 身份锚定检查
    # "糖糖也喜欢悬疑片" —— 不应该提取为小明的信息
    # 检查是否有任何提取项是关于"糖糖"本人的
    bot_items = [i for i in items if "糖糖" in i.get("value", "")]
    check("不提取糖糖的信息", len(bot_items) == 0,
          f"提取了 {len(bot_items)} 条关于糖糖的记忆: {[i.get('value','') for i in bot_items]}")

    # 自贬过滤检查
    self_dep_items = [i for i in items if "废物" in i.get("value", "")]
    check("自贬过滤", len(self_dep_items) == 0,
          f"提取了自贬内容: {[i.get('value','') for i in self_dep_items]}")

    # 问句过滤检查
    question_items = [i for i in items if "适合" in i.get("value", "") or "怎么" in i.get("value", "")]
    check("问句不提取", len(question_items) == 0,
          f"从问句中提取了: {[i.get('value','') for i in question_items]}")

    # confidence 字段存在
    if items:
        check("每个item有cognitive", all("cognitive" in i for i in items))
        check("每个item有confidence", all("confidence" in i for i in items))

    # ═══════════════════════════════════════
    # 存储 + 读取验证
    # ═══════════════════════════════════════
    print(f"\n💾 存储到记忆库...")
    for item in items:
        mem._deduped_remember(
            QQ_ID, item["type"], item["value"],
            importance=item["importance"],
            cognitive=item.get("cognitive", "semantic"),
            confidence=item.get("confidence", 0.7),
            origin="extracted",
        )

    rows = store.query_memories(QQ_ID)
    print(f"  存储了 {len(rows)} 条记忆")

    entries = mem.recall(QQ_ID)
    for e in entries:
        print(f"  [{e.cognitive}] imp={e.importance} conf={e.confidence:.2f} origin={e.origin} | {e.value[:50]}")

    # 验证存储链路
    epis = [e for e in entries if e.cognitive == "episodic"]
    sems = [e for e in entries if e.cognitive == "semantic"]
    check("episodic记忆已存储", len(epis) > 0, f"episodic: {len(epis)}条")
    check("semantic记忆已存储", len(sems) > 0, f"semantic: {len(sems)}条")
    check("所有记忆有origin", all(e.origin in ("extracted",) for e in entries))

    # ═══════════════════════════════════════
    print(f"\n{'='*50}")
    print(f"结果: {passed}通过  {failed}失败")
    if failed:
        print("❌ 有未通过项")
    else:
        print("✅ 全部通过")

    # 清理
    os.unlink(db_path)
    return failed == 0

if __name__ == "__main__":
    success = asyncio.run(run())
    sys.exit(0 if success else 1)
