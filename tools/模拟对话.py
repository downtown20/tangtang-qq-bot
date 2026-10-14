"""
模拟与糖糖对话 —— 用完全相同的 system prompt + API + pipeline
用法: python tools/模拟对话.py
"""
import asyncio, httpx, re, sys, os, json, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')
logger = logging.getLogger("模拟")

# ── 加载配置 ──
import yaml
cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
llm = cfg["llm"]

# ── 构建 system prompt（模拟 personality.py） ──
role_card = Path("role_card.md").read_text(encoding="utf-8").strip()
nicknames = cfg["bot"]["nicknames"]
name = cfg["bot"]["name"]
others = [n for n in nicknames if n != name]
nick_line = f"你叫{name}，群友也会叫你：{'、'.join(others)}。看到这些名字就是在叫你。" if others else f"别人都叫你{name}。"
role_card += f"\n\n## 称呼\n{nick_line}"
role_card += "\n\n## @提及\n**仅在群聊中使用**。回复中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @（就两个人，@没意义）。"

# 时间上下文
import datetime
hour = datetime.datetime.now().hour
if hour < 6: time_ctx = "现在是凌晨。夜猫子时间——安静、走心，适合聊深一点的话题。"
elif hour < 9: time_ctx = "现在是早上。刚起来没多久，慢慢进入状态中。"
elif hour < 12: time_ctx = "现在是上午。清醒了，可以正常聊天。"
elif hour < 14: time_ctx = "现在是中午。饭后懒懒散散的，聊天节奏慢一点。"
elif hour < 18: time_ctx = "现在是下午。状态正好的时候，精神不错。"
elif hour < 22: time_ctx = "现在是晚上。精力最好的时段，话比白天多，也更愿意聊深入的话题。"
else: time_ctx = "现在是深夜。安静的时间，聊天会更走心、更真实。"

system_prompt = role_card + f"\n\n{time_ctx}\n\n## 关系\n亲密度 0/100 — 你们还不太熟。礼貌地称呼对方。保持友好但不过分热情——对方先说你再回应。适度克制——不要太撒娇、不要说骚话、保持清爽。不主动分享个人隐私和内心想法。不主动提往事（你们还不熟）。"

# ── CLI 颜色 ──
C_RESET = "\033[0m"
C_CYAN = "\033[96m"
C_YELLOW = "\033[93m"
C_DIM = "\033[2m"
C_GREEN = "\033[92m"

# ── 模拟 pipeline clean() ──
def clean(reply: str) -> str:
    original = reply
    reply = re.sub(r'^\s*\[[^\]]{1,5}\]\s*', '', reply)
    reply = re.sub(r'\[CQ:image,file=(?![\da-fA-F]{32}(?:,|\]|$)).*?(?:\]|$)', '', reply, flags=re.DOTALL)
    reply = re.sub(r'\[CQ:\S{0,20}$', '', reply)
    reply = re.sub(r'\*\*(.+?)\*\*', r'\1', reply)
    reply = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'\1', reply)
    if not re.search(r'^#{1,3}\s|^\d+\.\s|^[-*]\s|```|^\s*[-=]{3,}\s*$|^\s*[│┌├└].*[│┌├└]', reply, re.MULTILINE):
        reply = re.sub(r'^#{1,4}\s+', '', reply, flags=re.MULTILINE)
        reply = re.sub(r'__(.+?)__', r'\1', reply)
        reply = re.sub(r'~~(.+?)~~', r'\1', reply)
    reply = re.sub(r'\[SING:[^\]]*\]', '', reply)
    reply = re.sub(r'\[SING\]', '', reply)
    reply = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏ - ⁠-⁯﻿]', '', reply)
    reply = reply.strip()
    if not reply or len(reply) < 3 or re.match(r'^[\.。…、，,！!？?~～\s]+$', reply):
        return ""
    return reply


async def call_deepseek(system: str, user: str) -> str:
    """调用 DeepSeek API，完全模拟糖糖的配置"""
    body = {
        "model": llm["model"],
        "max_tokens": llm.get("max_tokens", 4096),
        "temperature": llm.get("temperature", 0.6),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {llm['api_key']}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{llm['base_url']}/v1/chat/completions",
            headers=headers,
            json=body,
        )
    if resp.status_code != 200:
        return f"❌ API 错误 {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def chat(conversation: list[dict], user_msg: str):
    """发送消息并获取回复"""
    # 构建 user message（模拟 handler.py 的前缀）
    action_reminder = (
        '🔧 系统指令（只在需要系统配合时用，日常聊天不要加）：\n'
        '1. 有人让你唱歌→第一行写 {"act":"sing","song":"歌名"}\n'
        '2. 有人问时间/天气/日期/实时信息→第一行写 {"act":"search","q":"查询内容"}\n'
        '   只在对方明确问你不知道的具体事实时才搜。不要搜修辞问句。\n'
        '3. 日常聊天、撒娇、吐槽→不要加任何前缀。\n\n'
    )
    full_user = action_reminder + user_msg

    # 构建 messages 数组
    messages = [{"role": "system", "content": system_prompt}]
    for turn in conversation:
        messages.append(turn)
    messages.append({"role": "user", "content": full_user})

    # 调用 API
    body = {
        "model": llm["model"],
        "max_tokens": llm.get("max_tokens", 4096),
        "temperature": llm.get("temperature", 0.6),
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {llm['api_key']}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{llm['base_url']}/v1/chat/completions",
            headers=headers,
            json=body,
        )
    if resp.status_code != 200:
        return f"❌ API 错误 {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    raw = data["choices"][0]["message"]["content"].strip()

    # 过 pipeline
    cleaned = clean(raw)

    return raw, cleaned


async def main():
    print(f"\n{C_CYAN}🐱 糖糖对话模拟器{C_RESET}")
    print(f"{C_DIM}模型: {llm['model']} | system prompt: {len(system_prompt)}字 | 输入 /quit 退出 | /raw 看原始提示词{C_RESET}\n")

    # 统计
    stats = {"turns": 0, "empty_cleaned": 0, "brackets": 0, "formulaic": 0, "total_chars": 0}

    conversation = []  # [{"role": "user/assistant", "content": "..."}]

    while True:
        try:
            user_msg = input(f"{C_GREEN}你: {C_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_msg:
            continue
        if user_msg == "/quit":
            break
        if user_msg == "/raw":
            print(f"\n{C_DIM}── System Prompt ──\n{system_prompt}\n── 共 {len(system_prompt)} 字 ──{C_RESET}\n")
            continue
        if user_msg == "/stats":
            print(f"\n{C_DIM}统计: {stats}{C_RESET}\n")
            continue

        stats["turns"] += 1
        raw, cleaned = await chat(conversation, user_msg)

        print(f"\n{C_YELLOW}糖糖(原始):{C_RESET} {raw}")

        if cleaned != raw:
            changes = []
            if len(cleaned) != len(raw):
                changes.append(f"长度 {len(raw)}→{len(cleaned)}")
            if "（" in raw and "（" not in cleaned:
                changes.append("括号已剥离")
            if "**" in raw and "**" not in cleaned:
                changes.append("Markdown已洗")
            note = f" {C_DIM}({' | '.join(changes)}){C_RESET}" if changes else ""
            print(f"{C_CYAN}糖糖(清洗):{C_RESET} {cleaned}{note}")
        else:
            print(f"{C_DIM}(清洗后无变化){C_RESET}")

        # 标记潜在问题
        issues = []
        if not cleaned:
            issues.append("❌ 被清洗为空！")
            stats["empty_cleaned"] += 1
        if "（" in cleaned or "(" in cleaned:
            issues.append("📌 含括号动作")
            stats["brackets"] += 1
        if re.search(r'^(嘿嘿|哈哈|来了来了|知道啦知道啦)', cleaned):
            issues.append("📌 公式化开头")
            stats["formulaic"] += 1
        stats["total_chars"] += len(cleaned) if cleaned else 0

        if issues:
            print(f"{C_YELLOW}⚠ 问题:{C_RESET} {' | '.join(issues)}")

        print()

        # 加入对话历史
        conversation.append({"role": "user", "content": user_msg})
        conversation.append({"role": "assistant", "content": raw})  # 存原始值让 LLM 看到真实输出


if __name__ == "__main__":
    asyncio.run(main())
