"""
🔍 糖糖对话质量分析器

用法：
    python tools/分析对话质量.py                          # 分析所有群最近 500 条
    python tools/分析对话质量.py --group 928384102         # 只分析指定群
    python tools/分析对话质量.py --group 928384102 --hours 24  # 最近24小时
    python tools/分析对话质量.py --private 2524950083       # 分析私聊

输出：
    1. 回复率统计
    2. 响应速度
    3. 开头多样性（检测公式化/重复开头）
    4. 重复内容检测
    5. 上下文衔接质量
    6. 消息长度分析
    7. 具体问题列表（可直接交给开发者修）
"""
import sqlite3
import re
import sys
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "memory.db"


def load_messages(group_id: str = None, private_qq: str = None,
                  hours: int = None, limit: int = 500) -> list[dict]:
    """从数据库加载消息对（用户消息 + 糖糖回复）"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    conditions = []
    params = []

    if group_id:
        conditions.append("group_id = ?")
        params.append(group_id)
    elif private_qq:
        conditions.append("group_id = '' AND qq_id = ?")
        params.append(private_qq)

    if hours:
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        conditions.append("timestamp >= ?")
        params.append(since)

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"""
        SELECT * FROM chat_log
        WHERE {where}
        ORDER BY id DESC
        LIMIT ?
    """
    params.append(limit * 2)  # 多取一些，包括 bot reply
    cur.execute(query, params)
    rows = cur.fetchall()
    db.close()

    # 按时间正序
    rows = list(reversed(rows))

    # 配对：用户消息 → 糖糖回复
    pairs = []
    i = 0
    while i < len(rows):
        user = rows[i]
        if user["is_bot_reply"]:
            i += 1
            continue
        # 找下一条 bot reply
        bot = rows[i + 1] if i + 1 < len(rows) else None
        if bot and bot["is_bot_reply"]:
            pairs.append({
                "user": dict(user),
                "bot": dict(bot),
                "delay_sec": _parse_delay(user["timestamp"], bot["timestamp"]),
            })
            i += 2
        else:
            pairs.append({
                "user": dict(user),
                "bot": None,
                "delay_sec": None,
            })
            i += 1
    return pairs


def _parse_delay(t1: str, t2: str) -> float:
    """计算两条消息之间的秒数差"""
    try:
        d1 = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
        d2 = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
        return (d2 - d1).total_seconds()
    except Exception:
        return 0


# ─── 分析器 ────────────────────────────────────────────

class ReplyQualityAnalyzer:
    """分析糖糖的回复质量"""

    def __init__(self, pairs: list[dict], label: str = ""):
        self.pairs = pairs
        self.label = label
        self.issues: list[dict] = []  # 收集所有问题

    def analyze(self) -> str:
        if not self.pairs:
            return "📭 没有找到消息记录"

        lines = []
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🔍 糖糖对话质量分析报告")
        if self.label:
            lines.append(f"📍 {self.label}")
        lines.append(f"📊 分析 {len(self.pairs)} 组对话")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # ── 1. 回复率 ──
        replied = [p for p in self.pairs if p["bot"] is not None]
        missed = [p for p in self.pairs if p["bot"] is None]
        reply_rate = len(replied) / len(self.pairs) * 100 if self.pairs else 0

        lines.append(f"\n📡 回复率: {len(replied)}/{len(self.pairs)} = {reply_rate:.1f}%")
        if reply_rate < 30:
            self.issues.append({
                "level": "🔴",
                "title": f"回复率过低 ({reply_rate:.0f}%)",
                "detail": "超过 70% 的消息没有回复。可能原因：插话饥渴度太低、冷却太长、话题不匹配",
                "fix": "提高 interjection_thirst 到 0.85+，缩短 cooldown 到 60s 以内"
            })
        elif reply_rate < 50:
            self.issues.append({
                "level": "🟡",
                "title": f"回复率偏低 ({reply_rate:.0f}%)",
                "detail": "一半以上的消息被跳过",
                "fix": "适当提高 interjection_thirst 或降低冷却时间"
            })

        # ── 2. 响应速度 ──
        delays = [p["delay_sec"] for p in replied if p["delay_sec"] is not None]
        if delays:
            avg_delay = sum(delays) / len(delays)
            fast = sum(1 for d in delays if d < 10)
            slow = sum(1 for d in delays if d > 60)
            lines.append(f"\n⏱ 响应速度: 平均 {avg_delay:.1f}s | <10s: {fast}次 | >60s: {slow}次")
            if avg_delay > 30:
                self.issues.append({
                    "level": "🟡",
                    "title": f"平均响应时间过长 ({avg_delay:.0f}s)",
                    "detail": "可能是 LLM 调用太慢或 typing delay 设置太长",
                    "fix": "检查 LLM provider 速度，缩短 private_chat 的 typing delay"
                })

        # ── 3. 开头多样性 ──
        openings = []
        for p in replied:
            text = p["bot"]["message"]
            # 去掉 CQ 码
            text = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
            # 取前 15 个字作为开头
            opening = text[:15] if len(text) >= 10 else text[:10]
            if opening:
                openings.append(opening)

        if openings:
            counter = Counter(openings)
            repeated = [(k, v) for k, v in counter.most_common(10) if v >= 2]
            unique_rate = len(set(openings)) / len(openings) * 100

            lines.append(f"\n📝 开头多样性: {len(set(openings))}/{len(openings)} = {unique_rate:.0f}%")
            if repeated:
                lines.append(f"  重复开头 (≥2次):")
                for k, v in repeated[:5]:
                    lines.append(f"    {v}× 「{k}...」")

            if unique_rate < 50:
                self.issues.append({
                    "level": "🔴",
                    "title": f"开头严重重复 (多样性{unique_rate:.0f}%)",
                    "detail": "几乎每条回复都用同样的方式开头，显得很机器人",
                    "fix": "在 role_card.md 的「最高优先级规则」中强调开头多样化，给更多反面示例"
                })
            elif unique_rate < 70:
                self.issues.append({
                    "level": "🟡",
                    "title": f"开头多样性一般 ({unique_rate:.0f}%)",
                    "detail": "有一定重复，还可以改进",
                    "fix": "检查 role_card.md，补充更多开头示例"
                })

            # 检测公式化开头
            formulaic = ["嘿嘿", "早呀", "哈哈", "唔…", "诶~", "云层", "耳朵", "尾巴", "瞳孔", "蜷", "呼噜"]
            formulaic_count = 0
            for o in openings:
                if any(f in o for f in formulaic):
                    formulaic_count += 1
            if formulaic_count > len(openings) * 0.2:
                self.issues.append({
                    "level": "🔴",
                    "title": f"公式化开头过多 ({formulaic_count}次)",
                    "detail": "使用了禁止的肢体动作/问候语开头，role_card 规则未生效",
                    "fix": "强化 role_card 顶部规则，在 config.yaml personality.core 中加粗警告"
                })

        # ── 4. 重复回复检测 ──
        bot_texts = [re.sub(r'\[CQ:[^\]]+\]', '', p["bot"]["message"]).strip() for p in replied]
        dup_counter = Counter(bot_texts)
        exact_dups = [(k, v) for k, v in dup_counter.most_common(20) if v >= 2]
        if exact_dups:
            lines.append(f"\n🔄 完全重复回复:")
            for k, v in exact_dups[:5]:
                lines.append(f"    {v}× 「{k[:60]}...」")
            self.issues.append({
                "level": "🔴",
                "title": f"存在 {len(exact_dups)} 组完全重复的回复",
                "detail": "对不同的消息回复了相同的内容，说明 LLM 缺乏多样性",
                "fix": "提高 LLM temperature 到 1.0+，在 role_card 中强调「每次回复都要不一样」"
            })

        # ── 5. 长度分析 ──
        lengths = [len(re.sub(r'\[CQ:[^\]]+\]', '', p["bot"]["message"])) for p in replied]
        if lengths:
            avg_len = sum(lengths) / len(lengths)
            too_short = sum(1 for l in lengths if l < 15)
            too_long = sum(1 for l in lengths if l > 300)
            lines.append(f"\n📏 回复长度: 平均 {avg_len:.0f}字 | 过短(<15): {too_short}次 | 过长(>300): {too_long}次")
            if avg_len > 250:
                self.issues.append({
                    "level": "🟡",
                    "title": "回复平均过长，可能显得啰嗦",
                    "detail": "群聊回复 100-200 字最合适",
                    "fix": "在 role_card 中强调「一句话能说清楚的就一句话」，降低 max_tokens"
                })

        # ── 6. 上下文衔接 ──
        # 检测：用户问了问题，糖糖回了无关内容
        bad_continuity = 0
        question_signs = ["？", "?", "吗", "呢", "吧", "什么", "怎么", "哪", "谁", "几", "多少"]
        for p in replied:
            user_text = p["user"]["message"]
            bot_text = p["bot"]["message"]
            user_text = re.sub(r'\[CQ:[^\]]+\]', '', user_text).strip()
            bot_text = re.sub(r'\[CQ:[^\]]+\]', '', bot_text).strip()

            # 只在用户明显在提问时检测——问题没得到回应才是真跑题
            is_question = any(q in user_text for q in question_signs)
            if is_question and len(user_text) > 6:
                user_words = set(re.findall(r'[一-鿿]{2,}', user_text))
                bot_words = set(re.findall(r'[一-鿿]{2,}', bot_text))
                user_words.discard("什么")  # 虚词不算
                user_words.discard("怎么")
                if user_words and not user_words.intersection(bot_words):
                    bad_continuity += 1

        question_count = sum(1 for p in replied if any(q in re.sub(r'\[CQ:[^\]]+\]', '', p["user"]["message"]) for q in question_signs) and len(re.sub(r'\[CQ:[^\]]+\]', '', p["user"]["message"])) > 6)
        if question_count > 0:
            bad_rate = bad_continuity / question_count * 100
            lines.append(f"\n🔗 上下文衔接: {bad_continuity}/{question_count} 个问题未回应 = {bad_rate:.0f}%")
            if bad_rate > 40:
                self.issues.append({
                    "level": "🔴",
                    "title": f"对提问的跑题率过高 ({bad_rate:.0f}%)",
                    "detail": f"{bad_continuity} 个问题被糖糖忽略或答非所问",
                    "fix": "role_card 中加强对「正面回答问题」的要求，检查 knowledge 是否干扰了 LLM 判断"
                })

        # ── 7. 漏回原因分析 ──
        if missed:
            lines.append(f"\n❌ 未回复的消息 ({len(missed)}条):")
            for m in missed[-5:]:
                text = m["user"]["message"]
                text = re.sub(r'\[CQ:[^\]]+\]', '', text).strip()
                lines.append(f"    {m['user']['timestamp'][:16]} 「{text[:60]}」")

        # ── 汇总问题 ──
        lines.append(f"\n{'='*60}")
        lines.append("🚨 需要修复的问题")
        lines.append(f"{'='*60}")

        if not self.issues:
            lines.append("✅ 未发现明显问题，对话质量良好！")
        else:
            for i, issue in enumerate(self.issues, 1):
                lines.append(f"\n{i}. {issue['level']} {issue['title']}")
                lines.append(f"   原因: {issue['detail']}")
                lines.append(f"   建议: {issue['fix']}")

        lines.append(f"\n{'='*60}")
        lines.append("💡 把上面的「需要修复的问题」发给 Claude，说「修复这些问题」即可")
        lines.append(f"{'='*60}")

        return "\n".join(lines)


# ─── 入口 ──────────────────────────────────────────────

def main():
    group_id = None
    private_qq = None
    hours = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--group" and i + 1 < len(args):
            group_id = args[i + 1]
            i += 2
        elif args[i] == "--private" and i + 1 < len(args):
            private_qq = args[i + 1]
            i += 2
        elif args[i] == "--hours" and i + 1 < len(args):
            hours = int(args[i + 1])
            i += 2
        else:
            i += 1

    # 标签
    if group_id:
        label = f"群 {group_id}"
    elif private_qq:
        label = f"私聊 {private_qq}"
    else:
        label = "所有群聊 + 私聊"

    if hours:
        label += f"（最近 {hours}h）"

    pairs = load_messages(group_id=group_id, private_qq=private_qq, hours=hours)
    analyzer = ReplyQualityAnalyzer(pairs, label)
    report = analyzer.analyze()
    print(report)


if __name__ == "__main__":
    main()
