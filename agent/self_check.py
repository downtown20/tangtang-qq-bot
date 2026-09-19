"""
🔍 回复自检层 — 在发送前对 LLM 输出做规则化质量检查

从认知自修正闭环理论（RCr/Kr/Ur）出发：
- Kr-core（硬约束）：矛盾检测、信息边界——不可绕过
- Kr-flex（柔性约束）：跑题检测、格式检查——警告但仍发送

设计原则：
1. 纯规则引擎，不调用 LLM，零额外 token 消耗
2. 只拦截明确有问题的回复，不误杀正常回复
3. 所有检查结果记日志，方便后续分析和调优
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger("糖糖.SelfCheck")


@dataclass
class CheckResult:
    """单次自检结果"""
    passed: bool = True
    warnings: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)  # 致命问题，必须拦截
    score: int = 100  # 质量评分 0-100

    @property
    def blocked(self) -> bool:
        return len(self.blocks) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


class ReplySelfCheck:
    """回复自检器：在发送前检查 LLM 输出的质量"""

    # ── 格式问题 ──

    # 截断检测：回复在句子中间突然结束
    _TRUNCATION_PATTERNS = [
        re.compile(r'(?:而且|但是|因为|所以|然后|还有|或者|以及|不过|虽然|如果|只要|除非|无论|不管|即使|哪怕)\s*$'),
        re.compile(r'(?:等[一等]下|稍等|让我|我想|我先|我得|我还要|我还要说|我补充|我继续说|接着|接下来)\s*$'),
        re.compile(r'[，,、]\s*$'),  # 以逗号结尾，大概率没说完
    ]

    # 重复段落检测：同一句话出现 3 次以上
    _REPEATED_LINE_THRESHOLD = 3

    # 最短有效回复长度
    _MIN_REPLY_LENGTH = 3

    # ── 信息边界 ──

    # 糖糖不应该主动提及的信息（除非在上下文中已被提及）
    _INFO_BOUNDARY_PATTERNS = [
        # 不要主动提自己的技术实现细节
        (re.compile(r'(?:我的|糖糖的)?\s*(?:系统提示词|system\s*prompt|prompt|提示词工程|token|上下文窗口|API\s*key)'),
         "技术实现细节泄漏"),
        # 不要假装知道未告知的私密信息
        (re.compile(r'(?:我(?:知道|听说|看到)|据我所知).{0,10}(?:你(?:家|爸爸|妈妈|老婆|老公|女?朋友|男?朋友))'),
         "未经告知的私密信息推测"),
        # 不要编造具体数字（除非上下文中明确有）
        (re.compile(r'(?:你已经连续发了|这是你第)\d+(?:条|次|遍)'),
         "编造具体统计数字"),
    ]

    # ── 表演感 anti-tells（2026-08-17 对话质量审查）──
    # 罐装反应镜头/助手腔（speak-human-tw patterns）——只演情绪、不推进
    # 叙事的句子。用户反馈「少用DeepSeek语言模板」的机器指纹。
    _ANTI_TELL_PATTERNS = [
        (re.compile(r'我(?:不禁|忍不住|不由自主地)?愣了一下'), "罐装镜头「愣了一下」"),
        (re.compile(r'沉默(?:了)?几秒'), "罐装镜头「沉默几秒」"),
        (re.compile(r'(?:很|非常|特别)乐意'), "助手腔「很乐意」"),
        (re.compile(r'作为(?:一个|一名)?(?:AI|人工智能)'), "助手腔「作为AI」"),
        (re.compile(r'让我(?:来)?帮(?:助)?你'), "助手腔「让我帮你」"),
        (re.compile(r'我理解你的(?:感受|心情)'), "助手腔「我理解你的感受」"),
        (re.compile(r'这个问题问得(?:很|真)好'), "助手腔「这个问题问得好」"),
    ]

    # ── 上下文矛盾（简化版：关键词冲突） ──

    def __init__(self):
        # 每个群最近发送的回复历史（用于去重检测）
        self._sent_history: dict[str, deque[str]] = {}
        self._max_history = 10

    # ═══════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════

    def check(self, reply: str, context: str = "", recent_bot_replies: list[str] | None = None,
              hard_turn: bool = False) -> CheckResult:
        """对 LLM 回复执行全部自检规则。

        Args:
            reply: LLM 生成的回复文本
            context: 最近的群聊上下文
            recent_bot_replies: 糖糖最近在该群的回复（用于去重）
            hard_turn: 困难轮次（纠正/元问题）——只在此轮次做复读检查
                       （2026-08-15 Codex：任意长回复同话题都会词级重叠，
                       普通轮次检查会刷日志误导排查）

        Returns:
            CheckResult: 包含是否通过、警告和阻断原因
        """
        result = CheckResult()

        # ── Kr-core（硬约束）──
        self._check_truncation(reply, result)
        self._check_blank(reply, result)
        self._check_repeated_paragraphs(reply, result)
        self._check_info_boundary(reply, context, result)

        # ── Kr-flex（柔性约束）──
        self._check_duplicate_reply(reply, recent_bot_replies or [], result)
        if hard_turn:
            self._check_intent_alignment(reply, recent_bot_replies or [], result)
        self._check_runaway_length(reply, result)
        self._check_anti_tells(reply, result)

        if result.blocked:
            logger.warning(f"🚫 自检拦截: {'; '.join(result.blocks)}")
        elif result.has_warnings:
            logger.info(f"⚠️ 自检警告: {'; '.join(result.warnings)} | 评分:{result.score}")

        return result

    def record_sent(self, group_id: str, reply: str):
        """记录已发送的回复，用于后续的去重检测"""
        if group_id not in self._sent_history:
            self._sent_history[group_id] = deque(maxlen=self._max_history)
        self._sent_history[group_id].append(reply)

    # ═══════════════════════════════════════
    # 单项检查
    # ═══════════════════════════════════════

    def _check_truncation(self, reply: str, result: CheckResult):
        """检测回复是否被截断"""
        for pattern in self._TRUNCATION_PATTERNS:
            if pattern.search(reply):
                result.warnings.append(f"疑似截断: 回复在「{pattern.search(reply).group().strip()[-20:]}」处结束")
                result.score -= 15
                return  # 只报告一次

    def _check_blank(self, reply: str, result: CheckResult):
        """检测空白/无意义回复"""
        stripped = reply.strip()
        if not stripped or len(stripped) < self._MIN_REPLY_LENGTH:
            result.blocks.append("回复过短或为空")
            result.score = 0
            return

        # 纯表情/标点
        if re.match(r'^[\.。…、，,！!？?~～\s\[\]CQ:at,qQqQ=0-9,\s\[\]]+$', stripped):
            result.blocks.append("回复仅含表情/标点/CQ码，无实际内容")
            result.score = 0

    def _check_repeated_paragraphs(self, reply: str, result: CheckResult):
        """检测重复段落（LLM 偶尔会循环输出同一句话）。
        2026-08-16 范式转换：降为警告——系统不替 LLM 决定替换内容
        （罐头兜底违反唯一决策者）；复读由理解协议预防，本检查是诊断。"""
        lines = [l.strip() for l in reply.split('\n') if l.strip() and len(l.strip()) > 5]
        if len(lines) < 3:
            return

        from collections import Counter
        counts = Counter(lines)
        for line, count in counts.items():
            if count >= self._REPEATED_LINE_THRESHOLD:
                result.warnings.append(f"重复段落: 「{line[:40]}...」出现了 {count} 次")
                result.score = max(0, result.score - 60)
                return

    def _check_info_boundary(self, reply: str, context: str, result: CheckResult):
        """检查糖糖是否说出了不该知道的信息"""
        for pattern, desc in self._INFO_BOUNDARY_PATTERNS:
            match = pattern.search(reply)
            if match:
                # 如果上下文中也提到了相同的内容，放行
                matched_text = match.group()
                if matched_text in context:
                    continue
                result.warnings.append(f"信息边界: {desc} (「{matched_text[:30]}」)")
                result.score -= 10

    def _check_intent_alignment(self, reply: str, recent_replies: list[str], result: CheckResult):
        """意图对齐检查（2026-08-15 现场事故）：困难轮次里仍复读上一轮内容。
        与 _check_duplicate_reply 互补——那只看字符级重复，这里看词级重复
        （事故里糖糖把「五层」换措辞成「三层」再答一遍，字符重叠低但语义在复读）。
        门槛：双方都 >60 字、jieba 词重叠率 ≥0.5——短回复（「晚安」）不误伤。
        定位：诊断指标（警告+日志），不阻断——预防靠理解协议，本检查是观测。"""
        if not recent_replies:
            return
        prev = recent_replies[-1].strip()
        cur = reply.strip()
        if len(prev) < 60 or len(cur) < 60:
            return
        try:
            import jieba
            prev_words = {w for w in jieba.cut(prev) if len(w.strip()) >= 2}
            cur_words = {w for w in jieba.cut(cur) if len(w.strip()) >= 2}
            if not prev_words or not cur_words:
                return
            overlap = len(prev_words & cur_words) / min(len(prev_words), len(cur_words))
            if overlap >= 0.5:
                result.warnings.append(f"意图对齐: 与上一条回复词级重叠 {overlap:.0%}——复读风险")
                result.score -= 30
        except ImportError:
            pass

    def _check_duplicate_reply(self, reply: str, recent_replies: list[str], result: CheckResult):
        """检查是否与近期回复高度重复"""
        if not recent_replies:
            return

        reply_stripped = reply.strip()
        for prev in recent_replies[-5:]:  # 只比较最近 5 条
            prev_stripped = prev.strip()
            if not prev_stripped:
                continue

            # 完全相同的回复降为警告（2026-08-16 范式转换：系统不替 LLM 决定
            # 替换内容；复读有现场前科，由理解协议 + 本诊断共同预防）
            if reply_stripped == prev_stripped:
                result.warnings.append("与上一条回复完全相同——复读风险")
                result.score = max(0, result.score - 60)
                return

            # 高度相似（>90% 字符重叠）
            if len(reply_stripped) > 20 and len(prev_stripped) > 20:
                overlap = len(set(reply_stripped) & set(prev_stripped))
                shorter = min(len(reply_stripped), len(prev_stripped))
                if overlap / shorter > 0.9:
                    result.warnings.append("与近期回复高度相似")
                    result.score -= 20

    def _check_runaway_length(self, reply: str, result: CheckResult):
        """检查回复是否过长（LLM 偶尔话痨失控）"""
        if len(reply) > 1500:
            result.warnings.append(f"回复过长({len(reply)}字)，可能话痨失控")
            result.score -= 5
        if len(reply) > 3000:
            result.blocks.append(f"回复超长({len(reply)}字)，疑似 LLM 失控")
            result.score = max(0, result.score - 50)

    def _check_anti_tells(self, reply: str, result: CheckResult):
        """表演感诊断（2026-08-17，只记日志不阻断——观测先行）：
        罐装反应镜头与助手腔是「DeepSeek 语言模板」的机器指纹。
        2026-08-17 Codex 对齐：旧七模式对 983 条基线 0 命中，测不到用户抱怨
        的东西——真实分布是括号动作 11.8%。补括号动作镜头。先收集数据定位
        真实来源，再决定是否加阻断（教训 #18：先取证）。"""
        hits = [desc for pat, desc in self._ANTI_TELL_PATTERNS if pat.search(reply)]
        if re.search(r'（(?:耳朵|尾巴|猫耳|眼睛|蹭|呼噜|炸毛|歪头|小爪|耳朵抖)', reply):
            hits.append("括号动作镜头")
        if hits:
            result.warnings.append(f"表演感 anti-tell: {'、'.join(hits)}")
            result.score -= 5


# ═══════════════════════════════════════
# 话题连贯性跟踪
# ═══════════════════════════════════════

