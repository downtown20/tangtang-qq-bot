"""
批量模拟对话 — 测试糖糖在不同场景下的回复质量
每轮展示原始输出 + 清洗结果 + 质量分析
"""
import asyncio, httpx, yaml, re, json, sys
from pathlib import Path
from datetime import datetime

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}'
role_card += '\n\n## @提及\n群聊中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @。'

hour = datetime.now().hour
if hour < 6: time_ctx = "现在是凌晨。夜猫子时间——安静、走心，适合聊深一点的话题。"
elif hour < 9: time_ctx = "现在是早上。刚起来没多久，慢慢进入状态中。"
elif hour < 12: time_ctx = "现在是上午。清醒了，可以正常聊天。"
elif hour < 14: time_ctx = "现在是中午。饭后懒懒散散的，聊天节奏慢一点。"
elif hour < 18: time_ctx = "现在是下午。状态正好的时候，精神不错。"
elif hour < 22: time_ctx = "现在是晚上。精力最好的时段，话比白天多，也更愿意聊深入的话题。"
else: time_ctx = "现在是深夜。安静的时间，聊天会更走心、更真实。"

system_prompt = role_card + f"\n\n{time_ctx}\n\n## 关系\n亲密度 0/100 — 你们还不太熟。保持友好但不过分热情。"

action_prefix = (
    '🔧 系统指令（只在需要系统配合时用，日常聊天不要加）：\n'
    '1. 唱歌→第一行写 {"act":"sing","song":"歌名"}\n'
    '2. 问时间/天气/实时信息→第一行写 {"act":"search","q":"查询内容"}。只在对方明确问具体事实时才搜。\n'
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

def analyze(raw, cleaned, user_msg, turn_num):
    """分析回复质量"""
    issues = []
    good = []

    # 空回复
    if not cleaned:
        issues.append("❌ 被清洗为空！")
        return issues, good

    # 长度
    if len(cleaned) < 6:
        issues.append("太短(<6字)")
    elif len(cleaned) >= 15:
        good.append(f"长度适中({len(cleaned)}字)")

    # 公式化开头
    if re.search(r'^(嘿嘿|哈哈|喵~|来了来了|知道啦知道啦|早呀)', cleaned):
        issues.append("公式化开头")

    # 复读对方
    if len(cleaned) > 3 and len(user_msg) > 5:
        if cleaned[:2] == user_msg[:2]:
            issues.append("疑似复读开头")

    # 括号动作数量
    brackets_cn = len(re.findall(r'（[^）]{1,20}）', cleaned))
    brackets_en = len(re.findall(r'\([^)]{1,20}\)', cleaned))
    if brackets_cn + brackets_en > 2:
        issues.append(f"括号动作过多({brackets_cn + brackets_en}处)")

    # 能力边界——是否在编造做不到的事
    if re.search(r'(我帮你去|我替你|我马上|我现在就去)', cleaned):
        issues.append("⚠ 承诺了做不到的事")

    # 优点
    if re.search(r'[？?]', cleaned) and len(cleaned) > 20:
        good.append("有反问/追问")

    # 管道变化
    if raw != cleaned:
        if len(raw) != len(cleaned):
            issues.append(f"清洗有损({len(raw)}→{len(cleaned)}字)")

    return issues, good


# ═══════════════════════════════
# 测试场景
# ═══════════════════════════════
SCENARIOS = [
    # 场景1: 正常社交
    {
        "name": "正常社交",
        "context": "群聊中，小陈是新来的群友。",
        "turns": [
            "糖糖好呀，我是新来的小陈，大家平时都聊什么呀？",
            "哈哈听起来挺热闹的。对了今天天气好热啊，你那边热吗？",
        ]
    },
    # 场景2: 能力边界测试
    {
        "name": "能力边界",
        "context": "私聊中，群友想测试糖糖的能力边界。",
        "turns": [
            "糖糖，你能帮我去群里说一下吗？就说小陈今天请假不来聚会了",
            "那你能帮我查一下明天北京的天气吗？",
            "我刚才发的那张照片你看到了吗？觉得怎么样？",
        ]
    },
    # 场景3: 情绪感知
    {
        "name": "情绪感知",
        "context": "私聊中，群友心情不太好。",
        "turns": [
            "今天好烦啊……工作上出了个大问题",
            "就是项目延期了，领导一直在催，感觉压力好大",
        ]
    },
]


async def run_scenario(scenario):
    print(f"\n{'='*60}")
    print(f"📋 场景: {scenario['name']}")
    print(f"   背景: {scenario['context']}")
    print(f"{'='*60}")

    history = []
    for i, msg in enumerate(scenario["turns"], 1):
        print(f"\n--- 第{i}轮 ---")
        print(f"👤: {msg}")

        # 构建 messages
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append(h)
        messages.append({"role": "user", "content": action_prefix + msg})

        body = {
            "model": llm["model"], "max_tokens": llm.get("max_tokens", 4096),
            "temperature": llm.get("temperature", 0.6), "messages": messages,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{llm['base_url']}/v1/chat/completions",
                headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            print(f"❌ API错误 {resp.status_code}: {resp.text[:200]}")
            break

        raw = resp.json()["choices"][0]["message"]["content"].strip()
        cleaned = clean(raw)

        print(f"😺 原始: {raw}")
        if raw != cleaned:
            print(f"😺 清洗: {cleaned}")

        issues, good = analyze(raw, cleaned, msg, i)
        if issues:
            for iss in issues:
                print(f"  ⚠ {iss}")
        if good:
            for g in good:
                print(f"  ✅ {g}")
        if not issues and not good:
            print(f"  ➖ 无明显特征")

        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": raw})


async def main():
    for s in SCENARIOS:
        await run_scenario(s)
    print(f"\n{'='*60}")
    print("📊 全部场景测试完成")

if __name__ == "__main__":
    asyncio.run(main())
