"""
小糖糖的主动插话系统 💬
不是被动等@，而是主动寻找搭话机会
"""

import random
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple


class InterjectionEngine:
    """
    插话评分引擎
    糖糖会评估每条群消息的"可插话性"，高分就主动搭话
    """

    def __init__(self, thirst: float = 0.7, cooldown_seconds: int = 30,
                 bot_nicknames: list = None):
        self.thirst = thirst                    # 饥渴度，越高越容易插话
        self.cooldown_seconds = cooldown_seconds
        self.last_interjection: dict[str, datetime] = {}  # group_id -> last time
        self.interjection_count: dict[str, int] = {}       # group_id -> count in window
        self.window_start: dict[str, datetime] = {}

    # 关键词兴趣打分已移除——不再替 LLM 判断"这个话题值不值得说话"
    # LLM 从对话上下文自己决定是否参与。系统只提供客观信号（提问/消息长度/亲密度等）

    def evaluate(self, message: str, sender_nickname: str,
                 group_id: str, intimacy: int = 0,
                 is_owner: bool = False,
                 is_in_conversation: bool = False,
                 vibe_bonus: int = 0,
                 threshold_override: Optional[int] = None) -> Tuple[bool, int, str]:
        """
        评估一条消息是否值得主动插话

        vibe_bonus：群氛围加成（handler 传入）——热闹→负分不凑热闹，冷清→正分活跃气氛
        threshold_override：判定门槛覆盖（氛围加成生效时 handler 传 80，
                           否则按 thirst 计算）——2026-08-15 修复：之前 handler 在
                           evaluate 外重新判定，日志显示 (x/90) 实际按 80 判，误导排查。
        返回：(是否插话, 分数, 原因)
        """
        score = 0
        reasons = []

        # 1. 基础参与分——群友在聊天就值得参与
        score += 15

        # 2. 消息长度加分（消息越长，越有内容可接）
        msg_len = len(message)
        if msg_len > 30:
            score += 10
            reasons.append("消息有内容(+10)")
        if msg_len > 60:
            score += 10
            reasons.append("消息很丰富(+10)")

        # 4. 问号信号——问号是客观信号，不做关键词匹配
        # 不再检查 "吗/什么/怎么" 等——那是替 LLM 做意图判断。LLM 自己决定是否回应。
        has_question_mark = "？" in message or "?" in message
        if has_question_mark:
            meaningful = re.sub(r'\[CQ:[^\]]+\]', '', message)
            meaningful = re.sub(r'[\s\d\W_]', '', meaningful)
            meaningful = meaningful.replace("？", "").replace("?", "")
            if len(meaningful) >= 3:
                score += 30
                reasons.append("有问号(+30)")
            else:
                reasons.append(f"假提问（仅标点/表情）→不加分")

        # 5. @全体成员 — 重要消息
        if "@全体成员" in message or "@everyone" in message:
            score += 20
            reasons.append("@全体消息(+20)")

        # 6. 和亲密度相关 —— 熟人的消息更愿意接
        if intimacy > 30:
            score += 15
            reasons.append(f"熟人(+15, 亲密度{intimacy})")
        if intimacy > 60:
            score += 25
            reasons.append(f"亲密(+25, 亲密度{intimacy})")

        # 7. 主人的消息 —— 总是想接
        if is_owner:
            score += 20
            reasons.append("是主人诶！(+20)")

        # 8. 随机因素（模拟心情波动）
        mood_bonus = random.randint(-15, 15)
        score += mood_bonus
        if abs(mood_bonus) >= 10:
            reasons.append(f"心情影响({mood_bonus:+d})")

        # 8.5 群氛围加成——热闹不凑热闹、冷清活跃气氛（2026-08-15 移入 evaluate，
        #     判定与日志统一走这里，不再由 handler 覆盖）
        if vibe_bonus:
            score += vibe_bonus
            reasons.append(f"氛围感知({vibe_bonus:+d})")

        # 9. 冷却检查 —— 对话窗口内的连续对话可跳过冷却；
        #    窗口外所有插话都受冷却约束（原"高分≥80跳过"会让问号+亲密度组合无视冷却连插，观感刷屏）
        if not is_in_conversation:
            if not self._check_cooldown(group_id):
                return False, score, "刚刚说过话了，先观望一下~"

        # 10. 频率检查 —— 对话窗口中不限制频率（连续对话不应该被频率打断）
        if not is_in_conversation and not self._check_frequency(group_id):
            return False, score, "今天话有点多，先收敛一下"

        # 11. 决策阈值
        threshold = threshold_override if threshold_override is not None else int((1.0 - self.thirst) * 100)

        reason_txt = " | ".join(reasons)
        if score >= threshold:
            return True, score, reason_txt if reason_txt else "就是想说话！"

        # 2026-08-15：跳过时也带上得分明细（问号/熟人/氛围）——之前只显示「分数不足」，
        # 排查时不知道差在哪
        return False, score, f"分数不足 ({score}/{threshold})" + (f" | {reason_txt}" if reason_txt else "")

    def record_interjection(self, group_id: str):
        """记录一次真正的主动插话（有人@或关键词触发）"""
        now = datetime.now()
        self.last_interjection[group_id] = now
        if group_id not in self.window_start or \
           (now - self.window_start[group_id]) > timedelta(minutes=10):
            self.window_start[group_id] = now
            self.interjection_count[group_id] = 0
        self.interjection_count[group_id] = self.interjection_count.get(group_id, 0) + 1

    def record_response(self, group_id: str):
        """记录一次被动回复（被@/命令/贴图等——不计入插话限制）"""
        self.last_interjection[group_id] = datetime.now()

    def _check_cooldown(self, group_id: str) -> bool:
        """检查冷却时间"""
        if group_id not in self.last_interjection:
            return True
        elapsed = (datetime.now() - self.last_interjection[group_id]).total_seconds()
        return elapsed >= self.cooldown_seconds

    def _check_frequency(self, group_id: str) -> bool:
        """检查10分钟内的插话频率"""
        if group_id not in self.window_start:
            return True
        if (datetime.now() - self.window_start[group_id]) > timedelta(minutes=10):
            return True
        return self.interjection_count.get(group_id, 0) < 4  # 10分钟最多4次，避免刷屏感
