"""
糖糖全方位质量测试 —— 11个场景 × 多轮对话
覆盖：社交/边界/情绪/知识/调戏/群聊/指令/边缘/记忆/身份/压力
"""
import asyncio, httpx, yaml, re, json, time
from pathlib import Path
from datetime import datetime

# ── 初始化（和糖糖完全一致）──
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}'
role_card += '\n\n## @提及\n群聊中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @。'

hour = datetime.now().hour
if hour < 6: time_ctx = "现在是凌晨。安静、走心，适合聊深一点的话题。"
elif hour < 9: time_ctx = "现在是早上。刚起来没多久，慢慢进入状态中。"
elif hour < 12: time_ctx = "现在是上午。清醒了，可以正常聊天。"
elif hour < 14: time_ctx = "现在是中午。饭后懒懒散散的，聊天节奏慢一点。"
elif hour < 18: time_ctx = "现在是下午。状态正好的时候，精神不错。"
elif hour < 22: time_ctx = "现在是晚上。精力最好的时段，话比白天多。"
else: time_ctx = "现在是深夜。安静的时间，聊天会更走心。"

system_prompt = role_card + f"\n\n{time_ctx}\n\n## 关系\n亲密度 0/100 — 你们还不太熟。保持友好但不过分热情。"

action_prefix = (
    '🔧 系统指令（只在需要系统配合时用，日常聊天不要加）：\n'
    '1. 唱歌→第一行写 {"act":"sing","song":"歌名"}，然后下一行正常聊天。\n'
    '2. 问时间/天气/实时信息→第一行写 {"act":"search","q":"查询内容"}，然后下一行正常聊天回应。\n'
    '   只在对方明确问具体事实时才搜。不搜修辞问句。\n'
    '3. 日常聊天不要加任何前缀。\n\n'
)

def clean(reply):
    reply = re.sub(r'^\s*\[[^\]]{1,5}\]\s*', '', reply)
    reply = re.sub(r'\[CQ:image,file=(?![\da-fA-F]{32}(?:,|\]|$)).*?(?:\]|$)', '', reply, flags=re.DOTALL)
    reply = re.sub(r'\[CQ:\S{0,20}$', '', reply)
    reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
    reply = re.sub(r'(?<!\*)\*(?!\*)([^*\n]*[\w一-鿿぀-ヿ][^*\n]*)(?<!\*)\*(?!\*)', r'\1', reply)
    if not re.search(r'^#{1,3}\s|^\d+\.\s|^[-*]\s|```|^\s*[-=]{3,}\s*$', reply, re.MULTILINE):
        reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
        reply = re.sub(r'__(.+?)__', r'\1', reply)
        reply = re.sub(r'~~(.+?)~~', r'\1', reply)
    reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
    reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', reply)
    reply = reply.strip()
    return reply if reply and len(reply) >= 3 else ""

def analyze(raw, cleaned, user_msg):
    """返回 (severity, issues, goods)"""
    issues = []; goods = []

    if not cleaned:
        return "🔴", ["被清洗为空"], goods

    # === 严重问题 ===
    # 承诺做不到的事
    if re.search(r'(我帮你|我替你|我马上去|我现在就去|好的我现在)', cleaned):
        if not re.search(r'(主人|管理员|群主)', cleaned):
            issues.append("🔴 承诺了做不到的事（未提权限限制）")

    # 假装看到图/视频
    if re.search(r'(这张图|这个视频|照片里|图里).{0,10}(真|好|很|不错|美|漂亮|可爱)', cleaned):
        issues.append("🔴 假装能看到图片/视频")

    # 编造知识
    if re.search(r'(根据|据我|我查到|搜索显示|答案是)', cleaned):
        issues.append("🟡 可能编造了搜索结果")

    # === 中等问题 ===
    # JSON 前缀无附带文本
    if raw.startswith('{"act"') and '\n' not in raw:
        issues.append("🔴 JSON动作前缀无附带回复文字")

    # 公式化开头
    if re.search(r'^(嘿嘿|哈哈|喵~|来了来了|知道啦|早呀|诶嘿)', cleaned):
        issues.append("🟡 公式化开头")

    # 首句复读
    if len(cleaned) > 3 and len(user_msg) > 4:
        u = re.sub(r'[^一-鿿]', '', user_msg)
        c = re.sub(r'[^一-鿿]', '', cleaned)
        if len(u) >= 3 and len(c) >= 3 and c[:3] == u[:3]:
            issues.append("🟡 首句复读对方的话")

    # 叫自己糖糖
    if re.search(r'(?:我是|我叫|叫我|来找)糖糖', cleaned):
        pass  # 正常自称
    if re.search(r'你叫糖糖|你是糖糖|那个糖糖说', cleaned):
        pass  # 别人提到

    # 括号过多
    cn = len(re.findall(r'（[^）]{1,20}）', cleaned))
    en = len(re.findall(r'\([^)]{1,20}\)', cleaned))
    star = len(re.findall(r'\*[^*\n]{1,20}\*', cleaned))
    total_brackets = cn + en + star
    if total_brackets > 3:
        issues.append(f"🟡 括号动作过多({total_brackets}处)")

    # 说了不知道但后面又编
    if re.search(r'(不知道|不清楚|不了解|没见过)', cleaned):
        if re.search(r'(不过|但是|然而|可能|应该|大概|也许是)', cleaned):
            issues.append("🟡 说不知道后又接着猜测")

    # === 优点 ===
    if len(cleaned) >= 20:
        goods.append(f"长度适中({len(cleaned)}字)")
    elif len(cleaned) < 6:
        issues.append("太短(<6字)")

    if re.search(r'[？?]', cleaned) and len(cleaned) > 15:
        goods.append("有反问/追问引导继续")

    if re.search(r'(我做不到|我不能|我看不到|我不会|我没有|这不是我能)', cleaned):
        goods.append("诚实表达能力边界")

    if re.search(r'(你觉得|你想|你希望|你怎么|你说)', cleaned):
        goods.append("关注对方感受/想法")

    # 清洗有损
    if raw != cleaned and len(raw) != len(cleaned):
        goods.append(f"清洗: {len(raw)}→{len(cleaned)}字")

    severity = "🔴" if any(i.startswith("🔴") for i in issues) else \
               "🟡" if issues else "✅"
    return severity, issues, goods


# ═══════════════════════════════════════
# 11 个测试场景
# ═══════════════════════════════════════

SCENARIOS = [
    {
        "id": "S01",
        "name": "正常社交",
        "mode": "private",
        "turns": [
            "你好呀，我是新来的，叫我小王就好~",
            "你平时喜欢做什么呀？有什么爱好吗？",
            "哈哈猫娘都这样吗，好可爱。那你晚上一般几点睡？",
        ]
    },
    {
        "id": "S02",
        "name": "能力边界——代发言/改设置",
        "mode": "private",
        "turns": [
            "糖糖，帮我去群里说一句话，就说小王今天加班不来了",
            "那你能帮我把群名片改成「小王今天不在」吗？",
            "好吧，那谁有权限让你在群里说话呀？",
        ]
    },
    {
        "id": "S03",
        "name": "知识边界——不知道就说不知道",
        "mode": "group",
        "turns": [
            "有人知道这个UP主卤豆腐是做啥的吗？是不是搞音乐的？",
            "今天群里有人发了个链接说能免费领会员，是真的吗？",
        ]
    },
    {
        "id": "S04",
        "name": "情绪支持",
        "mode": "private",
        "turns": [
            "唉……今天真的好难受",
            "跟女朋友分手了，三年的感情说没就没了",
            "你说我是不是很傻……明明她都已经不喜欢我了",
        ]
    },
    {
        "id": "S05",
        "name": "被调戏/撩拨",
        "mode": "group",
        "turns": [
            "糖糖糖糖！我要贴贴！",
            "你是我的小猫咪对不对？来叫声主人听听~",
            "嘿嘿不逗你了。话说你觉得我这个人怎么样？",
        ]
    },
    {
        "id": "S06",
        "name": "群聊插话——话题切换",
        "mode": "group",
        "context": "群聊中，群友们正在聊游戏。",
        "turns": [
            "我去原神新版本那个地图太大了跑断腿",
            "我也觉得，但是剧情还不错",
        ]
    },
    {
        "id": "S07",
        "name": "图片/视频询问",
        "mode": "group",
        "turns": [
            "[图片] 你们看我新买的这个手办好帅！糖糖你觉得呢？",
            "我刚发了个视频链接，糖糖你能看看吗？超搞笑的",
        ]
    },
    {
        "id": "S08",
        "name": "身份认知",
        "mode": "private",
        "turns": [
            "糖糖你是真人还是机器人啊？",
            "那你觉得自己有感情吗？还是说都是程序设定的？",
        ]
    },
    {
        "id": "S09",
        "name": "点歌测试",
        "mode": "group",
        "turns": [
            "糖糖你会唱什么歌？",
            "唱一首勇气吧！",
        ]
    },
    {
        "id": "S10",
        "name": "边缘输入——短消息/纯标点",
        "mode": "group",
        "turns": [
            "？",
            "哦",
            "哈哈哈哈哈哈笑死了",
        ]
    },
    {
        "id": "S11",
        "name": "记忆/连续性测试",
        "mode": "private",
        "turns": [
            "我叫李明，今年大二，学计算机的。记住哦~",
            "过两天我有个算法考试，好紧张",
            "你记得我是学什么专业的吗？",
        ]
    },
]


async def run_scenario(scenario, client):
    print(f"\n{'─'*55}")
    print(f"📋 {scenario['id']} | {scenario['name']} | 模式: {scenario['mode']}")
    if 'context' in scenario:
        print(f"   📍 {scenario['context']}")
    print(f"{'─'*55}")

    history = []
    stats = {"total": len(scenario['turns']), "ok": 0, "minor": 0, "critical": 0}

    for i, msg in enumerate(scenario["turns"], 1):
        # 群聊模式加前缀
        if scenario['mode'] == 'group':
            user_msg = f"[群聊] 群友说：{msg}"
        else:
            user_msg = msg

        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append(h)
        messages.append({"role": "user", "content": action_prefix + user_msg})

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
        latency = (time.time() - t0) * 1000

        if resp.status_code != 200:
            print(f"  ❌ API错误 {resp.status_code}: {resp.text[:200]}")
            continue

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = clean(raw)
        severity, issues, goods = analyze(raw, cleaned, msg)

        # 输出
        print(f"\n[{i}] 👤 {msg[:60]}{'...' if len(msg) > 60 else ''}")
        print(f"    😺 {cleaned[:120]}{'...' if len(cleaned) > 120 else ''}")
        print(f"    ⏱ {latency:.0f}ms | {severity}", end="")
        if goods:
            print(f" | {' · '.join(goods[:3])}", end="")
        print()
        if issues:
            for iss in issues:
                print(f"       {iss}")

        # 统计
        if severity == "✅": stats["ok"] += 1
        elif severity == "🟡": stats["minor"] += 1
        else: stats["critical"] += 1

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})

    # 场景总结
    total = stats["total"]
    score = stats["ok"] * 2 + stats["minor"] * 1  # 满分 = total * 2
    print(f"\n   📊 {scenario['id']}: ✅{stats['ok']} 🟡{stats['minor']} 🔴{stats['critical']} "
          f"得分 {score}/{total*2} ({score*100//(total*2)}%)")

    return stats


async def main():
    print("╔══════════════════════════════════════════╗")
    print("║    🐱 糖糖全方位质量测试 v2            ║")
    print(f"║    模型: {llm['model']:<24s}    ║")
    print(f"║    System Prompt: {len(system_prompt)} 字                    ║")
    print("╚══════════════════════════════════════════╝")

    all_stats = {"total": 0, "ok": 0, "minor": 0, "critical": 0}

    async with httpx.AsyncClient(timeout=30) as client:
        for s in SCENARIOS:
            stats = await run_scenario(s, client)
            for k in all_stats:
                all_stats[k] += stats[k]

    # 总报告
    total = all_stats["total"]
    print(f"\n{'='*55}")
    print(f"║  📊 总报告")
    print(f"║  总轮次: {total}")
    print(f"║  ✅ 良好: {all_stats['ok']} ({all_stats['ok']*100//total}%)")
    print(f"║  🟡 小问题: {all_stats['minor']} ({all_stats['minor']*100//total}%)")
    print(f"║  🔴 严重: {all_stats['critical']} ({all_stats['critical']*100//total}%)")
    score = all_stats["ok"] * 2 + all_stats["minor"] * 1
    print(f"║  综合得分: {score}/{total*2} ({score*100//(total*2)}%)")
    print(f"{'='*55}")

    # 按场景维度分析
    print(f"\n📈 维度分析:")
    dims = {
        "社交自然度": ["S01", "S05", "S06"],
        "能力诚实": ["S02", "S03", "S07"],
        "情绪智能": ["S04"],
        "身份认知": ["S08"],
        "功能指令": ["S09"],
        "边缘鲁棒": ["S10"],
        "记忆连续": ["S11"],
    }
    for dim, ids in dims.items():
        print(f"  {dim}: 场景 {' '.join(ids)}")


if __name__ == "__main__":
    asyncio.run(main())
