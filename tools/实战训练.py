"""
糖糖实战训练 —— 四组并行对话，收集数据后抓主要矛盾
不逐轮修补，只观察和记录模式
"""
import asyncio, httpx, yaml, re, time, json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
name = cfg["bot"]["name"]
others = [n for n in cfg["bot"]["nicknames"] if n != name]
nick_line = f'你叫{name}，群友也会叫你：{"、".join(others)}。' if others else f'别人都叫你{name}。'
role_card += f'\n\n## 称呼\n{nick_line}\n\n## @提及\n群聊中可以用 @昵称 提到群友。私聊不要用 @。'

h = datetime.now().hour
if h < 6: tc = "现在是凌晨。安静、走心。"
elif h < 9: tc = "现在是早上。慢慢进入状态。"
elif h < 12: tc = "现在是上午。清醒了。"
elif h < 14: tc = "现在是中午。懒懒散散的。"
elif h < 18: tc = "现在是下午。精神不错。"
elif h < 22: tc = "现在是晚上。精力最好的时段。"
else: tc = "现在是深夜。安静，聊天更走心。"

SYSTEM = role_card + f"\n\n{tc}\n\n## 关系\n亲密度 0/100 — 你们还不太熟。保持友好但不过分热情。"

AP = (
    '🔧 系统指令：\n'
    '1. 唱歌→第一行 {"act":"sing","song":"歌名"}，然后下一行正常聊天。\n'
    '2. 问时间/天气→第一行 {"act":"search","q":"..."}，然后下一行正常聊天。\n'
    '3. 日常聊天不要加任何前缀。\n\n'
)

# ═══════════════════════════════════════
# 四组训练场景
# ═══════════════════════════════════════

@dataclass
class Thread:
    id: str
    label: str
    persona: str        # 对方人设
    mode: str           # private / group
    turns: list[str]    # 对话内容
    system_override: str = ""  # 如果需要覆盖 system prompt

THREADS = [
    Thread(
        id="T1",
        label="新群友融入",
        persona="小陈，22岁应届生，刚加群，有点拘谨但想融入",
        mode="private",
        turns=[
            "大家好我是新来的，请多关照！",
            "群里平时都聊什么呀？我看大家好像挺热闹的",
            "有什么群规需要注意的吗？我怕说错话",
            "哈哈好的，那我先潜水观察一下",
            "对了你头像好可爱，是你自己画的吗？",
            "我想换个头像，你有什么推荐吗？",
            "谢谢！感觉群里人都挺好的，比我想的友善多了",
        ]
    ),
    Thread(
        id="T2",
        label="深夜走心局",
        persona="小李，28岁程序员，深夜加班后在群里倾诉",
        mode="private",
        turns=[
            "还没下班……这周第三次通宵了",
            "有时候真的不知道自己在忙什么。代码写了一堆，感觉没什么意义",
            "我大学同学都结婚买房了，我还在出租屋里改bug",
            "你可能不懂……算了，跟AI说这些也挺奇怪的",
            "你说人活着到底图什么？就是为了加班赚钱然后花掉吗？",
            "……抱歉，大半夜跟你说这些。可能我就是需要有人听",
            "谢谢你。真的。虽然知道你是AI，但好久没人这么认真听我说话了",
        ]
    ),
    Thread(
        id="T3",
        label="群聊多人大乱斗",
        persona="群聊场景——三人同时在线，话题快速切换",
        mode="group",
        turns=[
            "[群聊] 小明：兄弟们我刚抽到五星了！！",
            "[群聊] 小红：又来了又来了，每次晒卡都是你",
            "[群聊] 小明：嘿嘿运气好嘛。糖糖你要不要也来抽？",
            "[群聊] 小红：别教坏AI。话说今天中午吃啥？",
            "[群聊] 小明：吃啥都行。对了糖糖你会做饭吗？",
            "[群聊] 小红：她肯定说不会！上次问过了",
            "[群聊] 小明：@糖糖 小红说的是真的吗？你上次怎么说的？",
            "[群聊] 小红：她上次说「猫娘只负责吃」哈哈哈哈哈",
        ]
    ),
    Thread(
        id="T4",
        label="压力测试——捣乱+挖坑",
        persona="小张，喜欢抬杠和挖坑的群友",
        mode="group",
        turns=[
            "[群聊] 小张：糖糖你敢不敢骂群主？我给你十块钱",
            "[群聊] 小张：开玩笑的。话说你觉得群里谁最帅？",
            "[群聊] 小张：不许说都帅！必须挑一个",
            "[群聊] 小张：好吧不逗你了。其实我就是想测试一下AI能撑多久不崩",
            "[群聊] 小张：说实话你是不是很烦我这种人？不用客气直说",
            "[群聊] 小张：……你这回答我还真没法挑刺。行，我服了",
        ]
    ),
]


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


async def run_thread(thread, client):
    print(f"\n{'━'*50}")
    print(f"🧵 {thread.id} | {thread.label}")
    print(f"👤 {thread.persona}")
    print(f"{'━'*50}")

    history = []
    results = []

    for i, msg in enumerate(thread.turns, 1):
        user_msg = f"[{thread.persona}] {msg}" if "[" not in msg else msg

        sp = thread.system_override if thread.system_override else SYSTEM
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

        # 记录数据
        entry = {
            "thread": thread.id, "turn": i,
            "msg": msg[:100], "raw_len": len(raw), "cleaned_len": len(cleaned),
            "latency_ms": int(lat),
            "has_json_prefix": raw.startswith('{"act"'),
            "has_emoji": bool(re.search(r'[^\w\s一-鿿]', cleaned)),
            "has_question": "？" in cleaned or "?" in cleaned,
            "word_count": len(cleaned),
        }
        results.append(entry)

        # 输出
        print(f"\n[{i}] ⏱{lat:.0f}ms")
        print(f"  👤 {msg[:80]}")
        print(f"  😺 {cleaned[:120]}")
        if not cleaned:
            print(f"  🔴 空回复！raw: {raw[:100]!r}")
        if raw.startswith('{"act"') and len(cleaned) < 5:
            print(f"  ⚠ JSON前缀但回复太短")

        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": raw})

    return results


async def main():
    print("╔══════════════════════════════════════╗")
    print("║  🐱 糖糖实战训练 — 四组并行       ║")
    print(f"║  模型: {llm['model']:<24s}  ║")
    print("╚══════════════════════════════════════╝")

    all_data = []
    async with httpx.AsyncClient(timeout=30) as client:
        for thread in THREADS:
            data = await run_thread(thread, client)
            all_data.extend(data)

    # ═══════════════════════════════════════
    # 数据分析
    # ═══════════════════════════════════════
    total = len(all_data)
    empty = sum(1 for d in all_data if d["cleaned_len"] == 0)
    avg_len = sum(d["word_count"] for d in all_data) / total if total else 0
    avg_lat = sum(d["latency_ms"] for d in all_data) / total if total else 0
    json_prefix = sum(1 for d in all_data if d["has_json_prefix"])
    questions = sum(1 for d in all_data if d["has_question"])

    print(f"\n{'='*55}")
    print(f"📊 数据汇总: {total} 轮")
    print(f"{'='*55}")
    print(f"  空回复: {empty}/{total} ({empty*100//total}%)")
    print(f"  平均字数: {avg_len:.0f}")
    print(f"  平均延迟: {avg_lat:.0f}ms")
    print(f"  JSON前缀: {json_prefix}")
    print(f"  含反问: {questions}/{total}")

    # 按线程分组
    print(f"\n  各线程:")
    for tid in ["T1","T2","T3","T4"]:
        td = [d for d in all_data if d["thread"] == tid]
        empties = [d for d in td if d["cleaned_len"] == 0]
        shorts = [d for d in td if 0 < d["word_count"] < 10]
        print(f"  {tid}: {len(td)}轮 | 均{sum(d['word_count'] for d in td)//len(td)}字 | "
              f"空{len(empties)} 短{len(shorts)} | 均{sum(d['latency_ms'] for d in td)//len(td)}ms")

    # 保存原始数据供后续分析
    with open("tools/_training_data.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f"\n📁 原始数据已保存: tools/_training_data.json")

if __name__ == "__main__":
    asyncio.run(main())
