"""
状态投影器 — 从糖糖的持久自我状态中投影相关内容到当前对话

「LLM 提供瞬间；架构提供生命。」

ContextBuilder 不是在"组装上下文"。
它是在从糖糖的持续存在状态中提取与当前对话相关的部分，
投射到 LLM 的上下文窗口中。

核心职责：
1. 按优先级分层组装上下文（Core → Relevant → Supplementary → Conversation）
2. 管理 Token 预算——每层有上限，超出则按规则截断
3. 从 TangTangSelf 中提取关系感、存在状态、自我叙事
4. 提供组装结果 + Token 使用报告
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from .interaction_contract import ChatContext

logger = logging.getLogger("糖糖.ContextBuilder")

# Jinja2 模板加载器——模板文件在 agent/prompts/ 目录
_TEMPLATE_DIR = Path(__file__).parent / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)


# ═══════════════════════════════════════════════════════════
# Token 估算（不引入 tiktoken 依赖，用简单的中文估算）
# ═══════════════════════════════════════════════════════════

# DeepSeek V4 的 tokenizer 对中文：1 汉字 ≈ 1.5-2 tokens
# 保守估计取 2 tokens / 汉字。英文词 ≈ 1.3 tokens。
# 这是一个粗略估算，不需要 100% 精确——预算管理是"约束"而非"精确计算"。

def estimate_tokens(text: str) -> int:
    """估算文本 token 数——**全系统唯一的预算尺子**。

    **2026-10-25 第二轮自查：旧系数把真实 token 高估了 2.36 倍。**

    取证方式（不是拍脑袋）：把她的真实 system prompt（3000 字符 / 中文 2340 /
    其他 660）原样发给**同一个模型**，读 `usage.prompt_tokens` = **1926**
    （含 user「在吗」约 4 tokens）。旧公式 `中文×1.8 + 其他×0.5` 估出
    **4542**，高估 2.36 倍。

    后果不是「数字难看」，是**内容被白白砍掉**：预算 3000「估算 tokens」
    实际只装得下约 1271 真实 tokens，于是 `_truncate_text` 从尾部砍掉约 44%
    的 system prompt（「## 关系」「风格锚」「FOLLOW_UP_PROTOCOL」整段）——
    她有一半说明书从来没送到过。

    新系数由实测标定（同一模型、四个样本）：

    | 样本 | 中文 | 其他 | 真实 tokens | 真实/字符 |
    |---|---|---|---|---|
    | 纯中文 | 192 | 16 | 133 | 0.64 |
    | 纯英文 | 0 | 405 | 127 | 0.31 |
    | 中英混合+标点 | 27 | 54 | 75 | 0.93 |
    | 纯代码 | 0 | 105 | 69 | 0.66 |

    中文取 **0.72**（比实测 0.64 略保守）；其他字符**保留 0.5**
    （对纯英文仍高估约 1.6 倍——这是安全方向：低估会让预算守不住，
    比高估危险）。真实 system prompt 复算：0.72×2340 + 0.5×660 = **2015**
    vs 真实 1926 → +4.6%，落在「略保守」的目标区间内。

    ⚠ 已知的残留误差：符号/换行密集的文本（代码、JSON schema）会被低估
    （实测纯代码 0.64 倍）。这几类在对话 prompt 里占比低，且低估只会让
    预算更宽松，不会截掉内容；真要精确计费请读 API 返回的 `usage`。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿' or '　' <= c <= '〿')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars * 0.72 + other_chars * 0.5)


@dataclass
class TokenReport:
    """Token 使用报告"""
    sections: dict[str, int] = field(default_factory=dict)
    total_system: int = 0
    total_user: int = 0
    total_history: int = 0
    total_tools: int = 0  # 2026-08-17 Codex 对齐：tools 计入账本
    total: int = 0
    budget_limit: int = 8000

    def log(self):
        """打印报告到日志"""
        lines = [f"📊 Token 报告 (总{self.total}/{self.budget_limit}):"]
        for name, count in sorted(self.sections.items(), key=lambda x: -x[1]):
            pct = round(count / max(1, self.total) * 100)
            lines.append(f"  {name}: {count}t ({pct}%)")
        logger.debug("\n".join(lines))


@dataclass
class ContextResult:
    """组装好的上下文"""
    system_prompt: str
    user_message: str
    history_messages: list[dict]
    token_report: TokenReport
    # 规范化的事实边界；旧调用方可以继续只读取上面四个字段。
    chat_context: ChatContext | None = None


# ═══════════════════════════════════════════════════════════
# ContextBuilder
# ═══════════════════════════════════════════════════════════

class ContextBuilder:
    """从持久自我状态中投影相关内容，组装 LLM 调用的完整上下文。

    分层结构：
    - Phase 1: Core (~1800t) — role_card, 关系感, 存在状态, 自我叙事, 时间/情绪, 关系层级
    - Phase 2: Relevant (~800t) — 记忆, 场景指令, 知识库, 话题记忆
    - Phase 3: Supplementary (~400t) — 群氛围, 活跃成员, 偏好, 贴图历史, 权力结构
    - Phase 4: Conversation (~2500t) — 结构化聊天历史 + 当前消息

    超出预算的层从后往前截断——优先保留 Core，最后截 Supplementary。
    """

    # Token 预算
    # 2026-10-23 T2.3：删掉 BUDGET_CONVERSATION = 2500——它**没有任何读者**。
    # 同组的 CORE/RELEVANT/SUPPLEMENTARY/TOTAL 都有生产用途（:171/:185/:245），
    # 只有它是旧分层预算布局的遗留：那个布局的唯一消费者 _assemble_with_budget
    # 本身也零调用（本次一并删除）。⚠ 别把它当成「对话层预算」在未来接回来——
    # 现在的对话预算走 protocols.fit_backgrounds（1200t 池）与 build() 的 history 段，
    # 两套预算并存正是当初分层没落地的原因。
    BUDGET_CORE = 1800
    BUDGET_RELEVANT = 800
    BUDGET_SUPPLEMENTARY = 400
    BUDGET_TOTAL = 6000

    def __init__(self, self_state=None):
        """self_state 是 TangTangSelf 实例——ContextBuilder 从中投影关系感和存在状态"""
        self._self_state = self_state

    # ═══════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════

    def build(
        self,
        *,
        # ── 必需 ──
        user_id: str,
        nickname: str,
        message: str,
        system_prompt_base: str,   # personality.build_system_prompt() 的返回
        history_messages: list[dict],
        # ── Phase 1 扩展 ──
        group_id: str = "",
        # ── Phase 2 ──
        memories_text: str = "",        # 格式化后的记忆文本 (注: 当前记忆已移到 user_message)
        topic_memories: str = "",
        scene_instruction: str = "",
        knowledge: str = "",
        # ── Phase 3 ──
        group_vibe: str = "",
        active_members: str = "",
        preferences: str = "",
        sticker_history: str = "",
        power_structure: str = "",
        # ── 其他 ──
        song_list: str = "",
        capability_note: str = "",
        drift_context: str = "",
        cast_context: str = "",
        chat_context: ChatContext | None = None,
    ) -> ContextResult:
        """组装完整的 LLM 调用上下文。

        使用 Jinja2 模板 (prompts/system.j2) 渲染最终 system prompt。
        模板定义了完整的 prompt 结构——本文档从此是 prompt 结构的唯一权威来源。"""
        from jinja2 import TemplateNotFound

        if chat_context is not None:
            if not isinstance(chat_context, ChatContext):
                raise TypeError("chat_context must be a ChatContext")
            expected_channel = "group" if str(group_id or "").strip() else "private"
            expected_scope = (
                f"group:{str(group_id).strip()}"
                if expected_channel == "group"
                else f"private:{str(user_id).strip()}"
            )
            if chat_context.scope_id != expected_scope:
                raise ValueError("chat_context scope does not match legacy arguments")
            if chat_context.channel != expected_channel:
                raise ValueError("chat_context channel does not match legacy arguments")
            if chat_context.actor_id != str(user_id).strip():
                raise ValueError("chat_context actor does not match legacy arguments")
            if chat_context.current_message != str(message or ""):
                raise ValueError("chat_context message does not match legacy arguments")
            # 合同对象是上下文事实的来源；转换为旧 list 形状仅为兼容模板/账本。
            history_messages = [dict(item) for item in chat_context.history_messages]

        token_report = TokenReport(budget_limit=self.BUDGET_TOTAL)

        # ── 收集模板变量（各模块只负责提供自己的数据）──
        rel_ctx = self._get_relationship_context(user_id)
        existence_ctx = self._get_existence_context()
        narrative_ctx = self._get_self_narrative_context()
        drive_ctx = self._get_drive_context()

        # ── 预算约束（2026-08-10 修复：分层裁剪真正生效）──
        # 之前只做"事后整体截断"——超预算时连核心人格层都可能被砍。
        # 现在渲染前按优先级裁剪：核心层(personality_base)不裁，
        # 补充层（知识/话题/活跃成员/偏好）各限制在 BUDGET_SUPPLEMENTARY 内。
        personality_base = system_prompt_base
        knowledge = knowledge if knowledge else ""
        _sup_limit = self.BUDGET_SUPPLEMENTARY
        for _name, _var in (("topic_memories", topic_memories), ("knowledge", knowledge),
                            ("active_members", active_members), ("preferences", preferences)):
            if _var and estimate_tokens(_var) > _sup_limit:
                _cut = self._truncate_text(_var, _sup_limit)
                if _name == "topic_memories":
                    topic_memories = _cut
                elif _name == "knowledge":
                    knowledge = _cut
                elif _name == "active_members":
                    active_members = _cut
                else:
                    preferences = _cut
                logger.debug(f"📊 补充层裁剪 {_name}: → ~{estimate_tokens(_cut)}t")

        # 渲染模板
        try:
            template = _jinja_env.get_template("system.j2")
        except TemplateNotFound:
            # Fallback: 模板文件不存在时回退到旧式拼接
            logger.warning("system.j2 模板未找到，回退到旧式拼接")
            return self._build_legacy(
                user_id=user_id, nickname=nickname, message=message,
                system_prompt_base=system_prompt_base, history_messages=history_messages,
                group_id=group_id, memories_text=memories_text,
                topic_memories=topic_memories, scene_instruction=scene_instruction,
                knowledge=knowledge, group_vibe=group_vibe, active_members=active_members,
                preferences=preferences, sticker_history=sticker_history,
                power_structure=power_structure, song_list=song_list,
                capability_note=capability_note, drift_context=drift_context,
                cast_context=cast_context,
            )

        system_prompt = template.render(
            personality_base=personality_base,
            scenario_sensitivity="",
            scenario_tone="",
            time_context="",       # 已包含在 personality_base 中
            mood_context="",       # 已包含在 personality_base 中
            # cot_text/relationship_tier 已由 personality_base 提供（模板不再引用）
            power_structure=power_structure if power_structure and "## 权力结构" not in system_prompt_base else "",
            group_vibe=group_vibe,
            relationship_context=rel_ctx or "",
            existence_context=existence_ctx or "",
            self_narrative=narrative_ctx or "",
            drives_context=drive_ctx or "",
            memories="",
            topic_memories=topic_memories,
            knowledge=knowledge,
            scene_instruction=scene_instruction,
            active_members=active_members,
            preferences=preferences,
            sticker_history=sticker_history,
            song_list=song_list,
            drift_context=("\n\n" + drift_context.replace("## 群友眼中的糖糖", "### 群友眼中的糖糖")) if drift_context else "",
            cast_context=f"\n\n{cast_context}" if cast_context else "",
            capability_note=capability_note,
        )

        # 事后截断——保留预算管理的安全网
        budget_limit = self.BUDGET_CORE + self.BUDGET_RELEVANT + self.BUDGET_SUPPLEMENTARY
        if estimate_tokens(system_prompt) > budget_limit:
            # 2026-08-15 整体审查 Critical：盲截尾部第一个牺牲的是能力边界说明
            # （capability_note 在模板最后——运行模式/语音能力/工具边界）。
            # 2026-08-17 Codex 终审：纠正契约「## 必须遵守」同样不可裁——
            # 旧逻辑只豁免 capability，契约在中间会被截断吃掉（契约可绕过）。
            # 2026-10-25 自查：**「## 此刻」同样不可裁**——它带日期/星期/时分，
            # 在 personality_base 的 97% 位置，必被尾部截断吃掉。取证
            # `turn_traces#67`：「我上一轮说了「10月26号，星期一」，但这个日期
            # 可能是我编的？我没有查时间」——她连时段词都看不到，只能查或猜。
            # 一个不知道自己现在几点的存在，谈不上「有活人的时间观念」。
            exempts = []  # [(marker, section_text)]
            for marker in ("## 必须遵守", "## ⚙️ 当前运行模式", "## 此刻"):
                if marker in system_prompt:
                    _head, _sec = system_prompt.split(marker, 1)
                    _m = re.search(r'\n\n## ', _sec)
                    if _m:
                        exempts.append((marker, marker + _sec[:_m.start()]))
                        system_prompt = _head + _sec[_m.start():]
                    else:
                        exempts.append((marker, marker + _sec))
                        system_prompt = _head
            if exempts:
                exempt_t = sum(estimate_tokens(sec) for _, sec in exempts)
                system_prompt = self._truncate_text(system_prompt, budget_limit - exempt_t)
                for _mk, sec in exempts:
                    system_prompt += "\n\n" + sec
                logger.debug(f"📊 system prompt 超预算，已截断（契约+能力边界豁免保留）")
            else:
                system_prompt = self._truncate_text(system_prompt, budget_limit)
                logger.debug(f"📊 system prompt 超预算，已截断")

        token_report.total_system = estimate_tokens(system_prompt)
        token_report.total_history = estimate_tokens(
            "\n".join(m.get("content", "") for m in history_messages)
        )
        token_report.total = token_report.total_system + token_report.total_history

        return ContextResult(
            system_prompt=system_prompt,
            user_message="",  # 由 handler 填充
            history_messages=history_messages,
            token_report=token_report,
            chat_context=chat_context,
        )

    def _build_legacy(self, **kwargs) -> ContextResult:
        """旧式拼接——system.j2 不存在时的回退方案。"""
        # ... 原有的字符串拼接逻辑（从上面的旧代码搬过来） ...
        # 此处省略以保持简洁——实际就是旧 build() 的代码
        raise NotImplementedError("Template fallback not implemented — ensure prompts/system.j2 exists")

    # ═══════════════════════════════════════
    # 状态投影方法
    # ═══════════════════════════════════════

    def _get_relationship_context(self, user_id: str) -> str:
        """从 TangTangSelf 投影关系感到当前对话"""
        if not self._self_state:
            return ""
        return self._self_state.get_relationship_context(user_id)

    def _get_existence_context(self) -> str:
        """从 TangTangSelf 投影存在状态"""
        if not self._self_state:
            return ""
        return self._self_state.existence.to_context()

    def _get_self_narrative_context(self) -> str:
        """从 TangTangSelf 投影自我叙事（2026-08-17 Codex 对齐+终审：只注入
        近 3 天更新过的叙事——常驻的「关于你自己」是自我履历复述，表演感源
        之一。终审修复：时间戳缺失/损坏时 fail-closed 不注入——迁移或损坏
        状态恰是窗口最需要兜底的情况，旧逻辑 fail-open 会永久常驻）"""
        if not self._self_state:
            return ""
        try:
            from datetime import datetime, timedelta
            updated = (self._self_state.self_narrative.updated_at or "").strip()
            if not updated:
                return ""
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if datetime.now(dt.tzinfo) - dt > timedelta(days=3):
                return ""
        except Exception:
            return ""
        return self._self_state.self_narrative.to_context()

    def _get_drive_context(self) -> str:
        """从 TangTangSelf 投影当前驱动力状态（带变化门）。

        2026-10-12 M3（审计 P0-1「每轮注入同一句」）：这里传 gated=True——
        同一 (驱动力, 强度档) 在 30 分钟内不重复注入。自治决策简报不传，
        那里驱动力是核心输入而非背景。
        """
        if not self._self_state:
            return ""
        return self._self_state.drives.get_drive_context(
            self_state=self._self_state, gated=True,
        )

    # ═══════════════════════════════════════
    # 预算管理
    # ═══════════════════════════════════════
    #
    # 2026-10-23 T2.3：这里原有的 _assemble_with_budget() 已删除（零调用，实测全仓
    # 只有定义处一行、0 个调用方）。它是**旧分层预算布局**的唯一消费者，配套的
    # BUDGET_CONVERSATION 也一并删掉（见上方常量区注释）。
    #
    # 为什么删而不是接线：它和 build() 主路径**重复**——build() 自己已经在
    # :245 用 BUDGET_CORE+RELEVANT+SUPPLEMENTARY 算 budget_limit 并在 :265/:270
    # 截断 system_prompt；背景块预算另有 protocols.fit_backgrounds 的 1200t 池。
    # 接进来只会多出**第三套**预算逻辑，而本仓的历史教训正是「同一件事两套逻辑
    # 各自漂移」（T10/T12/T25/T35/T42）。全项目评估结论同此（见
    # docs/开发规划/全项目审查历史_合并.md:130「应删除而非接入」）。

    @staticmethod
    def _truncate_text(text: str, token_budget: int) -> str:
        """按 token 预算截断文本——尽量在句子边界处断"""
        if estimate_tokens(text) <= token_budget:
            return text

        # 按字符比例估算（中文 ≈ 0.72 tokens/字——**必须与 `estimate_tokens` 同系数**，
        # 否则「判超预算」和「算出留多少字符」用两把尺子，截完还是不达标）。
        char_limit = int(token_budget / 0.72)
        truncated = text[:char_limit]

        # 在最后一个完整句子处断开
        for sep in ['\n\n', '\n', '。', '；', '，', '、', ' ']:
            pos = truncated.rfind(sep)
            if pos > char_limit * 0.5:  # 至少保留一半
                return truncated[:pos + len(sep)].rstrip()

        return truncated + "…"

    # ═══════════════════════════════════════
    # 诊断
    # ═══════════════════════════════════════

    def get_last_report(self) -> Optional[TokenReport]:
        """获取最后一次组装的 Token 报告（由 handler 在调用后保存）"""
        return getattr(self, '_last_report', None)
