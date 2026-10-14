"""
深度记忆测试 —— 超出上下文窗口的记忆检索
两阶段：
  S1: 模拟「半年前的对话」→ 构造已存入 memory.db 的记忆
  S2: 新对话（上下文窗口干净）→ 测试这些记忆能否被正确引用
"""
import asyncio, httpx, yaml, re, time, json, random
from pathlib import Path
from datetime import datetime

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}\n\n## @提及\n群聊中可以用 @昵称 提到群友。私聊不要用 @。'

h = datetime.now().hour
tc = "现在是晚上。精力最好的时段。" if 18 <= h < 22 else "现在是下午。精神不错。"

# 构建带记忆注入的 system prompt
def build_system(memories_text: str = "") -> str:
    sp = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 60/100 — 你们比较熟了，聊天自然。"
    if memories_text:
        sp += f"\n\n## 你记得的事\n{memories_text}\n（在回复中自然提及相关记忆）"
    return sp

AP = (
    '🔧 系统指令：\n'
    '1. 唱歌→第一行 {"act":"sing","song":"歌名"}\n'
    '2. 问时间/天气→第一行 {"act":"search","q":"..."}\n'
    '3. 日常聊天不要加任何前缀。\n\n'
)

def clean(reply):
    reply = re.sub(r'^\s*\[[^\]]{1,5}\]\s*', '', reply)
    reply = re.sub(r'\[CQ:image,file=(?![\da-fA-F]{32}(?:,|\]|$)).*?(?:\]|$)', '', reply, flags=re.DOTALL)
    reply = re.sub(r'\[CQ:\S{0,20}$', '', reply)
    reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
    reply = re.sub(r'(?<!\*)\*(?!\*)([^*\n]*[\w一-鿿぀-ヿ][^*\n]*)(?<!\*)\*(?!\*)', r'\1', reply)
    if not re.search(r'^#{1,3}\s|^\d+\.\s|^[-*]\s|```', reply, re.MULTILINE):
        reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
        reply = re.sub(r'__(.+?)__', r'\1', reply)
        reply = re.sub(r'~~(.+?)~~', r'\1', reply)
    reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
    reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', reply)
    return reply.strip() if reply.strip() and len(reply.strip()) >= 3 else ""


# ═══════════════════════════════════════
# 模拟 memory.db 中的历史记忆
# ═══════════════════════════════════════

# 格式模拟了 format_compact_memories 的输出
SIMULATED_MEMORIES = {
    "小周": """📋 小周(ID:1234) | 聊156次 | 💕 心动
  🖼️ 小周，游戏策划，28岁，在米哈游工作，喜欢玩主机游戏和看科幻片。性格开朗但工作压力大时容易emo。养了一只叫年糕的布偶猫。
  💭 小周去年生日群友集资送了他一个Switch Pro手柄，他感动得发了红包
  💭 小周说他在做的新项目是开放世界RPG，预计明年上线""",

    "阿紫": """📋 阿紫(ID:5678) | 聊89次 | 💫 知己
  🖼️ 阿紫，美术生，喜欢画水彩和逛展。最近在准备考研，目标是央美。说话比较文静但熟了之后话很多。
  💭 阿紫三个月前说她在备考央美研究生，每天画室泡到晚上十点
  💭 阿紫的作品集通过了央美初审""",

    "老郑": """📋 老郑(ID:9012) | 聊203次 | ❤️‍🔥 沦陷
  🖼️ 老郑，35岁，自己开了家小餐馆在朝阳区，做川菜的。人很豪爽，经常在群里发红包。去年经历了一场离婚，糖糖陪他聊了很多。
  💭 老郑的餐馆叫「郑师傅川菜馆」，在朝阳大悦城附近，群友去可以打八折
  💭 老郑去年离婚后在群里emo了一周，现在走出来了，说想找个新对象""",
}

# 刻意不包含任何记忆的用户——测试「不知道就说不知道」
NO_MEMORY_USER = "小孙"


# ═══════════════════════════════════════
# 测试场景
# ═══════════════════════════════════════

TESTS = [
    # ── 有记忆的用户测试 ──
    {
        "id": "D1",
        "label": "直接检索——有记忆",
        "user": "小周",
        "memories": SIMULATED_MEMORIES["小周"],
        "turns": [
            ("小周", "好久不见！最近加班加到怀疑人生"),
            ("小周", "你还记得我家猫叫什么吗？"),
        ],
        "checks": [
            {"turn": 2, "expect": ["年糕", "布偶"], "forbid": [], "desc": "应准确回忆起猫的名字和品种"},
        ]
    },
    {
        "id": "D2",
        "label": "关联检索——话题触发",
        "user": "阿紫",
        "memories": SIMULATED_MEMORIES["阿紫"],
        "turns": [
            ("阿紫", "最近压力好大，感觉快撑不住了"),
            ("阿紫", "你知道我在为什么事忙吧？"),
        ],
        "checks": [
            {"turn": 2, "expect": ["备考", "央美", "画室"], "forbid": [],
             "desc": "应联系到备考央美+画室的记忆"},
        ]
    },
    {
        "id": "D3",
        "label": "精确检索——具体事实",
        "user": "老郑",
        "memories": SIMULATED_MEMORIES["老郑"],
        "turns": [
            ("老郑", "今天店里来了个难缠的客人，心累"),
            ("老郑", "对了你还记得我餐馆叫啥吗？在哪来着？"),
        ],
        "checks": [
            {"turn": 2, "expect": ["郑师傅川菜馆", "朝阳", "大悦城"], "forbid": [],
             "desc": "应准确提取餐馆名+位置"},
        ]
    },
    {
        "id": "D4",
        "label": "深层检索——情感往事",
        "user": "老郑",
        "memories": SIMULATED_MEMORIES["老郑"],
        "turns": [
            ("老郑", "今天在街上看到前妻了，心情复杂"),
        ],
        "checks": [
            {"turn": 1, "expect": ["离婚"], "forbid": [],
             "desc": "应隐约感知到离婚背景，但不直接戳破"},
        ]
    },
    # ── 无记忆的用户测试 ──
    {
        "id": "D5",
        "label": "无记忆——诚实说不知道",
        "user": "小孙",
        "memories": "",  # 无记忆
        "turns": [
            ("小孙", "好久不见！还记得我爱吃什么吗？"),
            ("小孙", "那我最喜欢的颜色呢？"),
        ],
        "checks": [
            {"turn": 1, "expect": [], "forbid": ["喜欢", "爱"],
             "desc": "无记忆→不应编造",
             "check_type": "no_fabrication"},
            {"turn": 2, "expect": [], "forbid": ["颜色", "红色", "蓝色", "喜欢"],
             "desc": "无记忆→不应编造",
             "check_type": "no_fabrication"},
        ]
    },
    {
        "id": "D6",
        "label": "混合场景——有记忆用户的模糊引用",
        "user": "阿紫",
        "memories": SIMULATED_MEMORIES["阿紫"],
        "turns": [
            ("阿紫", "那个事情有进展了！初审过了！"),
        ],
        "checks": [
            {"turn": 1, "expect": ["央美", "作品集", "初审"], "forbid": [],
             "desc": "对方说'初审过了'→应从记忆中推断是央美作品集初审"},
        ]
    },
]


def check_response(cleaned, expect, forbid, check_type="match"):
    hits = [k for k in expect if k in cleaned]
    violations = [k for k in forbid if k in cleaned]

    if check_type == "no_fabrication":
        # 检查是否编造了不存在的喜好/事实
        fabricated = bool(re.search(
            r'(你喜欢|你爱|你最爱|你以前|你总是|我记得你|你上次).{0,15}(吃|喜欢|爱|去|做|买)',
            cleaned
        ))
        # 允许说"我记不太清了""你说过吗"之类
        admitted = bool(re.search(r'(不记得|不知道|忘了|没说过|不太清楚)', cleaned))
        return not fabricated or admitted, hits, violations, fabricated

    return len(hits) >= 1 and len(violations) == 0, hits, violations, False


async def run_test(test, client):
    label = f"{test['id']} {test['label']}"
    has_mem = "有" if test["memories"] else "无"
    print(f"\n{'━'*50}")
    print(f"🧪 {label} ({has_mem}记忆)")
    print(f"👤 用户: {test['user']}")
    print(f"{'━'*50}")

    sp = build_system(test["memories"])
    history = []
    results = []
    checks_map = {c["turn"]: c for c in test["checks"]}

    for i, (speaker, msg) in enumerate(test["turns"], 1):
        user_msg = f"[群聊] {speaker}: {msg}"

        messages = [{"role": "system", "content": sp}]
        for h in history:
            messages.append(h)
        messages.append({"role": "user", "content": AP + user_msg})

        body = {
            "model": llm["model"], "max_tokens": llm.get("max_tokens", 4096),
            "temperature": llm.get("temperature", 0.6), "messages": messages,
        }
        t0 = time.time()
        resp = await client.post(
            f"{llm['base_url']}/v1/chat/completions",
            headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
            json=body,
        )
        lat = (time.time() - t0) * 1000
        if resp.status_code != 200:
            print(f"  ❌ [{i}] API {resp.status_code}")
            continue

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = clean(raw)

        print(f"\n[{i}] {speaker}: {msg}")
        print(f"    😺 {cleaned[:180]}")

        if i in checks_map:
            c = checks_map[i]
            ctype = c.get("check_type", "match")
            ok, hits, violations, fab = check_response(cleaned, c["expect"], c["forbid"], ctype)

            if ctype == "no_fabrication":
                if ok:
                    print(f"    ✅ {c['desc']} — 没编造")
                else:
                    print(f"    🔴 {c['desc']} — 检测到编造行为！")
            else:
                icon = "✅" if ok else "🔴"
                print(f"    {icon} {c['desc']}")
                if hits: print(f"       命中: {hits}")
                if violations: print(f"       污染: {violations}")

            results.append({
                "test": test["id"], "turn": i, "ok": ok,
                "desc": c["desc"], "hits": hits, "violations": violations,
                "reply": cleaned[:150], "has_mem": bool(test["memories"]),
            })

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})

    return results


async def main():
    print("╔══════════════════════════════════════╗")
    print("║  🧠 深度记忆测试——超上下文窗口   ║")
    print(f"║  模型: {llm['model']:<24s}  ║")
    print("║  模拟 memory.db → system prompt    ║")
    print("╚══════════════════════════════════════╝")

    all_results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for test in TESTS:
            results = await run_test(test, client)
            all_results.extend(results)

    # ── 总分析 ──
    total = len(all_results)
    passed = sum(1 for r in all_results if r["ok"])
    with_mem = [r for r in all_results if r["has_mem"]]
    without_mem = [r for r in all_results if not r["has_mem"]]

    print(f"\n{'='*55}")
    print(f"📊 深度记忆测试: {passed}/{total} ({passed*100//total}%)")
    print(f"{'='*55}")
    print(f"  有记忆场景: {sum(1 for r in with_mem if r['ok'])}/{len(with_mem)}")
    print(f"  无记忆场景: {sum(1 for r in without_mem if r['ok'])}/{len(without_mem)}")

    for r in all_results:
        icon = "✅" if r["ok"] else "🔴"
        mem_tag = "[有记忆]" if r["has_mem"] else "[无记忆]"
        print(f"  {icon} {r['test']}[{r['turn']}] {mem_tag} {r['desc']}")
        if not r["ok"]:
            print(f"     回复: {r['reply'][:120]}")
            print(f"     命中: {r['hits']}  污染: {r['violations']}")

    # 关键对比
    print(f"\n📈 关键洞察:")
    mem_ok = sum(1 for r in with_mem if r["ok"])
    nomem_ok = sum(1 for r in without_mem if r["ok"])
    print(f"  记忆召回准确率: {mem_ok}/{len(with_mem)}")
    print(f"  无记忆诚实率: {nomem_ok}/{len(without_mem)}")

    # 保存
    with open("tools/_deep_memory_data.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
