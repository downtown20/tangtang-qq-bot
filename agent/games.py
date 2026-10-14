"""
🎮 群友互动小游戏 — 猜数字 + 抽签运势
注册为 LLM 可调用的技能，群友说「猜数字」「抽个签」就能玩
"""
from __future__ import annotations

import random


def _register():
    from .skills import register_skill

    # ── 猜数字 ──
    @register_skill(
        "guess_number",
        "群友说「猜数字」或「玩猜数字」时调用。糖糖会想一个1-100的数字，群友猜，猜对为止。"
        "如果群友说「我猜X」并提供了数字，把那个数字作为guess参数传入。"
        "返回提示是高了还是低了，或者猜对了。",
        {"guess": "群友猜的数字（1-100），如果群友只是说开始玩，填0表示新开局"},
    )
    async def _guess_number(guess: int | str = 0) -> str:
        """处理原生工具调用参数。

        技能定义目前将参数描述为 JSON string，LLM 因而可能传入 ``"42"``。
        在技能边界完成窄化，避免把类型错误传播到游戏状态层。
        """
        try:
            normalized_guess = int(str(guess).strip())
        except (TypeError, ValueError):
            return f"[猜数字] 「{guess}」不是有效数字，请猜 1-100 之间的整数喵~"
        return _do_guess(normalized_guess)

    # ── 抽签运势 ──
    @register_skill(
        "fortune",
        "群友说「抽签」「求签」「运势」「占卜」「塔罗」时调用。"
        "从签筒中随机抽取一支签，返回签文和解读。",
        {"type": "签的类型：qian=观音灵签, fortune=运势, tarot=塔罗牌。不指定则随机"},
    )
    async def _fortune(type: str = "") -> str:
        return _do_fortune(type)


# ═══════════════════════════════════════════════════════════
# 猜数字
# ═══════════════════════════════════════════════════════════
_GAME_STATE: dict[str, int] = {}  # context_key -> answer (per-group or per-user)


def _do_guess(guess: int, context_key: str = "") -> str:
    """猜数字逻辑。guess=0 新开局，否则判断大小。
    context_key: 群ID或私聊用户ID，用于隔离不同群的游戏状态。"""
    key = context_key or "_global"
    if guess == 0:
        answer = random.randint(1, 100)
        _GAME_STATE[key] = answer
        return (
            f"[猜数字] 糖糖想好了一个 1-100 之间的数字！\n"
            f"群友可以说「我猜XX」来猜。糖糖会告诉你高了还是低了。\n"
            f"看看几轮能猜中~"
        )

    answer = _GAME_STATE.get(key)
    if answer is None:
        answer = random.randint(1, 100)
        _GAME_STATE[key] = answer

    if guess < 1 or guess > 100:
        return f"[猜数字] 要猜 1-100 之间的数字啦！「{guess}」不算数喵~"

    if guess < answer:
        return f"[猜数字] {guess} —— 太低了！往上猜猜 📈"
    elif guess > answer:
        return f"[猜数字] {guess} —— 太高了！往下猜猜 📉"
    else:
        # 猜对了，重置
        _GAME_STATE.pop(key, None)
        return (
            f"[猜数字] 🎉 {guess} 答对啦！就是 {answer}！\n"
            f"你真厉害喵~ 还想再玩吗？说「猜数字」开始新一局！"
        )


# ═══════════════════════════════════════════════════════════
# 抽签 / 运势 / 塔罗
# ═══════════════════════════════════════════════════════════
_QIAN_SIGNS = [
    ("第一签 上上", "开天辟地", "混沌初开，万物始生。诸事皆宜，一片光明。"),
    ("第二签 中平", "鲲鹏化鹏", "蓄势待发之时。莫急莫躁，时机一到自然成。"),
    ("第三签 下下", "逆水行舟", "不进则退。当慎言慎行，守住本心。"),
    ("第四签 上吉", "龙游浅水", "暂时的困顿，等候风云际会即可一飞冲天。"),
    ("第五签 中吉", "凤凰于飞", "和鸣锵锵，有贵人相助。合作之事大吉。"),
    ("第六签 上上", "花开富贵", "春风得意马蹄疾。付出终有回报，尽情享受。"),
    ("第七签 中平", "曲径通幽", "走一条少有人走的路。虽然辛苦，风景独好。"),
    ("第八签 下下", "塞翁失马", "祸福相依，不必为一时的失意沮丧。"),
    ("第九签 上吉", "鲤鱼跃龙门", "关键时刻到了！放手一搏，一飞冲天。"),
    ("第十签 中吉", "明月清风", "内心平静，诸事顺遂。享受当下的安稳。"),
]

_FORTUNE_RESULTS = [
    ("大吉 🎊", "今天运势爆棚！想做什么都会顺，抓住机会吧~"),
    ("中吉 ✨", "运气不错的一天，适合做些平时不敢尝试的事。"),
    ("小吉 🌸", "会有小惊喜等着你。保持好心情，好事自然来。"),
    ("吉 🍀", "平平淡淡才是真。虽然没有大惊喜，但一切顺利。"),
    ("末吉 ☁️", "今天可能会有小波折，但不会影响大局。放宽心~"),
    ("凶 ⚡", "今天不宜冒险。保守行事，少说话多做事。"),
    ("大凶 💀", "运气暂时不太好…别担心，明天会好起来的！糖糖给你吸吸欧气~"),
]

_TAROT_CARDS = [
    ("愚者", "新的开始，无畏的旅程。相信直觉，大胆前行。"),
    ("魔术师", "你拥有创造奇迹的力量。行动起来，把想法变成现实。"),
    ("女祭司", "倾听内心的声音。有些事不需要外求，答案就在心里。"),
    ("女皇", "丰饶与滋养。享受生活的美好，去爱，被爱。"),
    ("皇帝", "秩序与掌控。该拿出态度了，不要让别人左右你。"),
    ("恋人", "重要的选择。听从内心，选那个让你真正心动的。"),
    ("力量", "温柔的力量胜过蛮力。耐心和坚韧会带你披荆斩棘。"),
    ("隐者", "独处的智慧。退一步，从远处看看，答案会在宁静中出现。"),
    ("命运之轮", "命运在转动。好消息在路上，保持期待。"),
    ("死神", "结束是为了新的开始。放下该放下的，轻装前行。"),
    ("星星", "希望之光。即使黑暗中也有一盏灯为你亮着。"),
    ("月亮", "迷雾之中，不要被表面的幻象迷惑。相信直觉。"),
    ("太阳", "温暖与成功。一切豁然开朗，尽情享受吧。"),
    ("审判", "觉醒的时刻。过去的努力得到回报，新的篇章开启。"),
    ("世界", "圆满。一个周期结束，你已准备好迈入更大的舞台。"),
]


def _do_fortune(t: str) -> str:
    """根据类型返回运势结果"""
    t = t.strip().lower() if t else ""

    if "qian" in t or "签" in t or "灵签" in t or "观音" in t:
        name, title, desc = random.choice(_QIAN_SIGNS)
        return (
            f"🎋 观音灵签 · {name}「{title}」\n\n"
            f"{desc}\n\n"
            f"—— 心诚则灵，仅供参考喵~"
        )

    elif "tarot" in t or "塔罗" in t:
        card, meaning = random.choice(_TAROT_CARDS)
        return (
            f"🔮 塔罗牌 · 「{card}」\n\n"
            f"{meaning}\n\n"
            f"—— 塔罗是镜子，照见你心中所想 ✨"
        )

    else:
        luck, desc = random.choice(_FORTUNE_RESULTS)
        return (
            f"🍀 今日运势 · {luck}\n\n"
            f"{desc}\n\n"
            f"—— 运势仅供娱乐，过好每一天才是真~"
        )


# ── 在 handler.py 中 import 即可自动注册 ──
_register()
