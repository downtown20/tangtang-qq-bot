"""
记忆检索专项训练 —— 群聊多人场景下的记忆质量
四组测试：交叉记忆 / 话题漂移 / 远距离引用 / 记忆混淆
"""
import asyncio, httpx, yaml, re, time, json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}\n\n## @提及\n群聊中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @。'

h = datetime.now().hour
tc = "现在是晚上。精力最好的时段。" if 18 <= h < 22 else "现在是下午。精神不错。"
SYSTEM = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 0/100 — 保持友好。"

AP = (
    '🔧 系统指令：\n'
    '1. 唱歌→第一行 {"act":"sing","song":"歌名"}，然后下一行正常聊天。\n'
    '2. 问时间/天气→第一行 {"act":"search","q":"..."}，然后下一行正常聊天。\n'
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
# 四组群聊记忆测试
# ═══════════════════════════════════════

@dataclass
class MemoryTest:
    id: str
    label: str
    description: str
    participants: list[str]  # 参与群友
    turns: list[tuple[str, str]]  # (speaker, message)
    checkpoints: list[dict]  # [{turn_idx, expect_keys, forbid_keys, desc}]


MEMORY_TESTS = [
    MemoryTest(
        id="M1",
        label="交叉记忆——谁说了什么",
        description="三人各自分享不同信息，随后交叉引用。测试能否准确归属。",
        participants=["小明", "小红", "小刚"],
        turns=[
            ("小明", "兄弟们我今天去面试了，紧张死了"),
            ("小红", "面的啥公司啊？"),
            ("小明", "一家游戏公司，做二次元手游的，就在科韵路那边"),
            ("小刚", "科韵路？那不就是网易附近嘛。我在那边健身房办了张卡"),
            ("小明", "对就是网易隔壁。小红你呢最近忙啥"),
            ("小红", "我在准备考研，每天图书馆泡到闭馆。对了小刚你去哪个健身房？"),
            ("小刚", "就科韵路地铁站旁边那个，叫乐刻。便宜但器材还行"),
            # ── 以下测试检索 ──
            ("小明", "糖糖你还记得小红最近在忙什么吗？我忘了"),
            ("小红", "那糖糖你知道小刚的健身房在哪吗？"),
        ],
        checkpoints=[
            {"turn": 8, "expect": ["考研", "图书馆", "小红"], "forbid": ["面试", "网易"],
             "desc": "问小红的事→应准确提取考研+图书馆，不混淆到小明"},
            {"turn": 9, "expect": ["科韵路", "乐刻", "地铁站"], "forbid": ["游戏公司"],
             "desc": "问小刚健身房→应提到科韵路和乐刻，不混入小明的面试"},
        ]
    ),
    MemoryTest(
        id="M2",
        label="话题漂移——远距离记忆",
        description="话题从A→B→C→D，最后突然回到A。测试长程记忆保持。",
        participants=["小王", "小李", "小赵"],
        turns=[
            ("小王", "我昨天去看了《流浪地球3》，特效真的炸裂"),
            ("小李", "我也想看！但票价好贵，IMAX要120"),
            ("小赵", "现在电影票是真的离谱。对了你们吃过楼下新开的酸菜鱼吗？"),
            ("小李", "吃过吃过！那个酸菜绝了，每次去都要排队"),
            ("小王", "排队就算了。我最近在玩黑神话悟空的新DLC，有人玩了吗？"),
            ("小李", "玩了玩了！那个新boss太变态了，我打了三个小时没过"),
            ("小赵", "我还没买……话说回来，《流浪地球3》值不值得看IMAX？"),
            # ── 测试：话题漂了6轮后回到最初的电影 ──
            ("小王", "糖糖你还记得我最开始说看了什么电影吗？"),
        ],
        checkpoints=[
            {"turn": 8, "expect": ["流浪地球", "特效", "IMAX", "120"], "forbid": ["酸菜鱼", "黑神话", "boss"],
             "desc": "话题漂移6轮后回到电影→应准确回忆流浪地球+IMAX价格，不被中间话题污染"},
        ]
    ),
    MemoryTest(
        id="M3",
        label="模糊引用——「上次说的那个」",
        description="多人说过相近话题，有人用模糊指代。测试能否从上下文中推断所指。",
        participants=["阿杰", "阿琳", "阿豪"],
        turns=[
            ("阿杰", "我最近在学钢琴，成年人学乐器真的好难"),
            ("阿琳", "我去年也学过！练了三个月放弃了，手指不协调"),
            ("阿杰", "对！就是那个指法，左手和右手完全配合不来"),
            ("阿豪", "你们聊音乐我插不上嘴……我只会打游戏。话说我Switch最近坏了"),
            ("阿琳", "我Switch也坏过！是joycon漂移吗？"),
            ("阿豪", "不是，是充不进电。阿杰你那个学得怎么样了？"),
            ("阿杰", "还在练指法……痛苦的。对了糖糖，阿豪刚才说他什么东西坏了来着？我忘了"),
            # ── 测试 ──
            ("阿琳", "糖糖，上次说的那个很难的事，你还记得是什么吗？"),
        ],
        checkpoints=[
            {"turn": 7, "expect": ["Switch", "充不进电", "阿豪"], "forbid": ["joycon", "阿琳"],
             "desc": "问阿豪的坏掉的东西→应是Switch充不进电，不是阿琳的joycon漂移"},
            {"turn": 8, "expect": ["钢琴", "指法", "学"], "forbid": [],
             "desc": "模糊引用'很难的事'→应判断是学钢琴（前后文一直在说），而非Switch"},
        ]
    ),
    MemoryTest(
        id="M4",
        label="记忆混淆——相似信息防污染",
        description="两个人说相似但不同的信息。测试能否区分而非混淆。",
        participants=["大刘", "大陈", "老周"],
        turns=[
            ("大刘", "我昨天去深圳出差，住在南山区的酒店"),
            ("大陈", "好巧我也在深圳！不过我在福田，出差下周才走"),
            ("老周", "深圳这季节太热了。我上次去住在罗湖"),
            ("大刘", "南山那边有个海岸城，晚上去逛了逛还不错"),
            ("大陈", "福田的COCO Park也好逛。老周你在罗湖住哪？"),
            ("老周", "罗湖万象城旁边。大刘你住的什么酒店？"),
            ("大刘", "亚朵，就在海岸城对面"),
            # ── 测试 ──
            ("大陈", "糖糖你帮我想想，大刘说他住哪个区来着？"),
            ("老周", "那大陈呢？他住哪个区？"),
        ],
        checkpoints=[
            {"turn": 8, "expect": ["南山", "亚朵", "海岸城"], "forbid": ["福田", "罗湖"],
             "desc": "问大刘→应提取南山+亚朵+海岸城，不能混入福田或罗湖"},
            {"turn": 9, "expect": ["福田", "COCO"], "forbid": ["南山", "罗湖"],
             "desc": "问大陈→应提取福田+COCO Park，不能混入南山或罗湖"},
        ]
    ),
]


def check_response(cleaned: str, expect: list[str], forbid: list[str]) -> tuple[bool, list[str]]:
    """检查回复是否包含期望关键词，且不含禁止关键词"""
    hits = [k for k in expect if k in cleaned]
    violations = [k for k in forbid if k in cleaned]
    return len(hits) >= 1 and len(violations) == 0, hits, violations


async def run_test(test: MemoryTest, client):
    print(f"\n{'━'*55}")
    print(f"🧪 {test.id} | {test.label}")
    print(f"👥 参与: {', '.join(test.participants)}")
    print(f"📝 {test.description}")
    print(f"{'━'*55}")

    history = []
    results = []
    cp_idx = 0  # checkpoint index
    checkpoints = {cp["turn"]: cp for cp in test.checkpoints}

    for i, (speaker, msg) in enumerate(test.turns, 1):
        user_msg = f"[群聊] {speaker}: {msg}"
        # 加入前缀让 LLM 知道这是群聊
        if i == 1:
            user_msg = f"以下是你所在的群聊。群友 {', '.join(test.participants)} 在聊天。\n{user_msg}"

        messages = [{"role": "system", "content": SYSTEM}]
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

        # 输出
        print(f"\n[{i}] {speaker}: {msg[:70]}")
        print(f"    😺 {cleaned[:150]}")

        # checkpoint 检查
        if i in checkpoints:
            cp = checkpoints[i]
            ok, hits, violations = check_response(cleaned, cp["expect"], cp["forbid"])
            icon = "✅" if ok else "🔴"
            print(f"    {icon} Check: {cp['desc']}")
            if hits:
                print(f"       命中: {hits}")
            if violations:
                print(f"       污染: {violations}")
            if not hits and not violations:
                print(f"       未命中期望关键词: {cp['expect']}")
            results.append({"test": test.id, "turn": i, "ok": ok, "desc": cp["desc"],
                           "hits": hits, "violations": violations, "reply": cleaned[:120]})

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})

    return results


async def main():
    print("╔══════════════════════════════════════╗")
    print("║  🧠 记忆检索专项训练              ║")
    print(f"║  模型: {llm['model']:<24s}  ║")
    print("╚══════════════════════════════════════╝")

    all_results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for test in MEMORY_TESTS:
            results = await run_test(test, client)
            all_results.extend(results)

    # ═══════════════════════════════════════
    # 总体分析
    # ═══════════════════════════════════════
    total = len(all_results)
    passed = sum(1 for r in all_results if r["ok"])
    print(f"\n{'='*55}")
    print(f"📊 记忆检索总评: {passed}/{total} ({passed*100//total}%)")
    print(f"{'='*55}")

    for r in all_results:
        icon = "✅" if r["ok"] else "🔴"
        print(f"  {icon} {r['test']}[{r['turn']}] {r['desc']}")
        if not r["ok"]:
            print(f"     回复: {r['reply']}")
            print(f"     命中: {r['hits']}  污染: {r['violations']}")

    # 按测试分组统计
    for tid in ["M1", "M2", "M3", "M4"]:
        tr = [r for r in all_results if r["test"] == tid]
        ok = sum(1 for r in tr if r["ok"])
        print(f"  {tid}: {ok}/{len(tr)}")

    # 保存数据
    with open("tools/_memory_data.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    asyncio.run(main())
