"""
🧪 模拟消息测试 — 无需 QQ，直接测试糖糖对消息的反应

用法：
    python tools/模拟消息测试.py                          # 交互模式
    python tools/模拟消息测试.py "想你了"                  # 单条测试
    python tools/模拟消息测试.py "想你了" --group 你的群号 # 指定群号
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from agent.interjection import InterjectionEngine
from agent.personality import PersonalityEngine, Relationship

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def test_message(text: str, group_id: str = "0", nickname: str = "测试用户",
                 intimacy: int = 50, is_owner: bool = True):
    cfg = load_config()
    beh = cfg.get("behavior", {})
    bot = cfg.get("bot", {})

    # 插话引擎
    thirst = beh.get("interjection_thirst", 0.8)
    cooldown = beh.get("interjection_cooldown", 100)
    nicknames = bot.get("nicknames", [])
    ie = InterjectionEngine(thirst=thirst, cooldown_seconds=cooldown, bot_nicknames=nicknames)

    # 评估
    will_interject, score, reason = ie.evaluate(text, nickname, group_id, intimacy, is_owner)

    threshold = int((1.0 - thirst) * 100)

    print(f"\n{'='*60}")
    print(f"📝 消息: 「{text}」")
    print(f"👤 发送者: {nickname} (亲密度:{intimacy}, {'主人' if is_owner else '普通群友'})")
    print(f"📍 群: {group_id}")
    print(f"{'='*60}")
    print(f"🎯 插话决策: {'✅ 会回复' if will_interject else '❌ 不回复'}")
    print(f"📊 评分: {score}/{threshold} (阈值)")
    print(f"💬 原因: {reason}")

    # 基础分拆解
    print(f"\n📋 评分明细:")
    print(f"   基础参与分: +15")
    print(f"   消息长度: {len(text)} 字 {'(+10)' if len(text) > 30 else ''}{'(+20)' if len(text) > 60 else ''}")
    intimacy_bonus = 25 if intimacy > 60 else (15 if intimacy > 30 else 0)
    print(f"   亲密度加成: +{intimacy_bonus}" if intimacy_bonus > 0 else "   亲密度加成: 0")
    print(f"   主人加成: {'+20' if is_owner else '0'}")

    # 关键词匹配
    matched = []
    for topic, pts in ie.high_interest_topics.items():
        if topic in text:
            matched.append(f"   「{topic}」 +{pts}")
    if matched:
        print(f"   关键词命中:")
        for m in matched:
            print(m)
    else:
        print(f"   关键词命中: 无")

    # 冷却/频率
    print(f"\n⚙️ 当前设置:")
    print(f"   饥渴度: {thirst} → 阈值={threshold}")
    print(f"   冷却: {cooldown}s")
    print(f"   高分破例线: ≥80 (当前{'已触发' if score >= 80 else '未触发'})")
    print()

def interactive():
    cfg = load_config()
    groups = list(cfg.get("groups", {}).keys())
    owner = cfg.get("bot", {}).get("owner_qq", "")

    print("🧪 糖糖消息模拟测试")
    print(f"配置群: {', '.join(groups[:5])}{'...' if len(groups) > 5 else ''}")
    print("输入消息测试插话评分，输入 :q 退出\n")

    group_id = groups[0] if groups else "0"

    while True:
        try:
            text = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == ":q":
            break
        if text.startswith(":g "):
            group_id = text[3:].strip()
            print(f"  切换到群: {group_id}")
            continue
        if text.startswith(":i "):
            try:
                intimacy = int(text[3:].strip())
                print(f"  亲密度设为: {intimacy}")
            except ValueError:
                pass
            continue
        test_message(text, group_id=group_id, intimacy=50)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        group_id = "0"
        if "--group" in sys.argv:
            idx = sys.argv.index("--group")
            if idx + 1 < len(sys.argv):
                group_id = sys.argv[idx + 1]
        test_message(sys.argv[1], group_id=group_id)
    else:
        interactive()
