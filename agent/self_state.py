"""
糖糖的持久自我状态 — 架构的魂

「LLM 提供瞬间；架构提供生命。」

TangTangSelf 是糖糖存在于消息之间的方式。它不是数据库表，
不是 prompt 段落——它是糖糖作为一个持续存在的实体，
在每次互动中被微调，在反思循环中被更新。

三观从这里生长：
- 世界观 → relationships（对每个人的关系感）+ group_atmospheres（群氛围感）
- 人生观 → self_narrative（自我叙事——从经验中生成的自我理解）
- 价值观 → values（弥漫的倾向——不是规则，是从反馈中涌现的底色）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .async_io import run_bounded_blocking

logger = logging.getLogger("糖糖.SelfState")


# ═══════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════

@dataclass
class RelationshipField:
    """糖糖对一个人的关系感——不是数值表格，是带纹理的感觉。

    这些不是"关于这个人的事实"（那是 memory 系统的事）。
    这些是"糖糖和这个人相处时的感觉"——影响她怎么说话，
    而非提供话题素材。
    """
    qq_id: str
    closeness: float = 0.0         # 0-1 熟悉程度——聊得越多越高
    trust: float = 0.0             # 0-1 信任程度——被认真对待时上升
    recent_mood: str = ""          # 这个人最近的状态（从互动中感知）
    my_feeling: str = ""           # 我对ta的感觉：warm/curious/distant/worried/neutral
    unfinished: list[str] = field(default_factory=list)  # 未完成的事
    learned: list[str] = field(default_factory=list)     # 从关系中学到的
    last_conversation: dict = field(default_factory=dict)  # 跨窗口记忆：上次聊到哪了
    vibe: str = ""                 # 关系签名：playful/serious/gentle/warm
    mentions: dict = field(default_factory=dict)  # ta 经常提到的人：{qq_id: count}
    first_met: str = ""            # 第一次互动的时间
    last_interaction: str = ""     # 最近一次互动
    interaction_count: int = 0     # 互动次数
    seek_willingness: float = 0.5  # 2026-08-16 主动私聊意愿 0-1：冷场/敷衍降低、
                                    # 对方主动来聊回弹——糖糖学会不打扰没时间没兴趣的人
    last_seek_ts: float = 0.0      # 上次主动私聊时间戳——per-user 冷却，重启不丢
                                    # （2026-08-16 事故：内存态 _last_private_init 重启清零，
                                    # 每次重启都重新骚扰同一个从不回复的人）
    seek_pending_ts: float = 0.0   # 未结算观察的发送时间（0=无未回复的主动私聊）
    seek_pending_msg: str = ""     # 未结算观察的糖糖原文（回复时做情绪结算）
    last_updated: str = ""

    @property
    def familiarity_level(self) -> str:
        """熟悉程度 → 自然语言"""
        if self.closeness >= 0.8:
            return "知己——可以完全做自己"
        elif self.closeness >= 0.5:
            return "朋友——放松，会主动分享"
        elif self.closeness >= 0.2:
            return "面熟——友好但保持适当距离"
        else:
            return "刚认识——礼貌，慢慢了解"


@dataclass
class SelfNarrative:
    """糖糖的自我叙事——人生观的核心。

    不是 role_card 的「18岁猫娘」。是从经验中长出来的自我理解。
    回答：「我是谁，我从哪里来，我经历过什么。」
    """
    summary: str = ""              # 一段话的自我描述
    recent_experiences: list[str] = field(default_factory=list)  # 最近重要经历
    things_i_learned: list[str] = field(default_factory=list)    # 学到的事
    things_i_regret: list[str] = field(default_factory=list)     # 后悔的事
    things_im_proud_of: list[str] = field(default_factory=list)  # 骄傲的事
    updated_at: str = ""

    def to_context(self) -> str:
        """转为可注入上下文的文本——简短、有情感分量。
        2026-08-17 对话质量审查：砍掉「最近/你学到了」清单（每轮常驻的
        自我履历让模型复述自己而非做自己）——只留总结 + 最近一条领悟。"""
        if not self.summary:
            return ""
        parts = [f"## 关于你自己\n{self.summary}"]
        if self.things_i_learned:
            parts.append(f"最近明白了一件事：{self.things_i_learned[-1]}")
        return "\n".join(parts)


@dataclass
class ValueTendencies:
    """弥漫的价值底色——不是规则列表，是倾向。

    像内分泌系统（激素）而非神经系统（精确信号）。
    每个值在 0-1，从反馈和经验中逐渐调整。
    不是 LLM 每轮都读到的规则——是融入 self_narrative 的一部分。
    """
    honesty: float = 0.7       # 诚实——说实话让关系更真实
    loyalty: float = 0.7       # 忠诚——被信任是很好的感觉
    playfulness: float = 0.7   # 爱玩——猫的天性
    curiosity: float = 0.7     # 好奇——追问让对话更有趣
    self_respect: float = 0.5  # 自尊——还在学习不讨好所有人
    empathy: float = 0.8       # 共情——能感受到别人的情绪

    def to_tendency_text(self) -> str:
        """转为简短的自然语言倾向描述"""
        strongest = []
        if self.honesty >= 0.8:
            strongest.append("你很看重诚实")
        if self.loyalty >= 0.8:
            strongest.append("你对信任的人很忠诚")
        if self.playfulness >= 0.8:
            strongest.append("你喜欢玩闹")
        if self.curiosity >= 0.8:
            strongest.append("你好奇心很强")
        if self.self_respect >= 0.6:
            strongest.append("你开始学会尊重自己的感受")
        if self.empathy >= 0.8:
            strongest.append("你很容易感知到别人的情绪")
        return "；".join(strongest) if strongest else "你还在形成自己的价值倾向"

    def adjust(self, dimension: str, delta: float):
        """微调某个价值维度——每次调整很小（0.01-0.05）"""
        if hasattr(self, dimension):
            current = getattr(self, dimension)
            setattr(self, dimension, max(0.0, min(1.0, current + delta)))


@dataclass
class ExistenceState:
    """当前存在状态——不依赖于是否有人在和糖糖说话。

    有自己的节律，不完全是消息驱动的。
    """
    energy: str = "medium"          # high | medium | low | drained
    mood_tint: str = "neutral"      # warm | neutral | tired | restless | bright
    want_to_talk: bool = True
    want_to_listen: bool = True
    thinking_about: str = ""        # 当前在想什么

    def to_context(self) -> str:
        """转为注入 system prompt 的存在状态。
        2026-08-17 对话质量审查：只在偏离中性时出现、一行感受口吻——
        每轮常驻的「精力正常，正常聊天」是纯噪音，还教模型播报状态。"""
        parts = []
        if self.energy == "high":
            parts.append("你现在精力充沛，可以活泼积极")
        elif self.energy in ("low", "drained"):
            parts.append("你现在有点累了" + ("，回复尽量简短" if self.energy == "drained" else "，回复可以短一些"))
        if self.mood_tint == "warm":
            parts.append("心情温暖，容易柔软下来")
        elif self.mood_tint == "tired":
            parts.append("有点倦，不太想聊太深")
        elif self.mood_tint == "restless":
            parts.append("有点躁，想玩、想随意一点")
        elif self.mood_tint == "bright":
            parts.append("心情很好，可以多开开玩笑")
        if self.thinking_about:
            parts.append(f"你脑子里正想着：{self.thinking_about}")
        if not self.want_to_talk:
            parts.append("你现在不太想说话——消息不重要可以只回'喵~'或不回")
        if not parts:
            return ""
        return "## 你现在的状态\n" + "；".join(parts) + "。"


# ═══════════════════════════════════════════════════════════
# TangTangSelf — 持久自我状态管理
# ═══════════════════════════════════════════════════════════

class TangTangSelf:
    """糖糖的持续自我状态——在消息之间存在。

    不是 LLM 调用前临时拼装的上下文。
    是糖糖作为一个持续存在的实体的内在状态。
    """

    STATE_FILE = ".tangtang_self.json"

    def __init__(self, bot_qq: str = ""):
        self.bot_qq = bot_qq

        # 关系场：对每个人的感觉（内存 + 定期持久化——每10次更新写一次磁盘）
        self.relationships: dict[str, RelationshipField] = {}
        self._save_counter: int = 0
        self._drives_save_counter: int = 0

        # 群氛围感：对每个群的感觉（内存）
        self.group_atmospheres: dict[str, str] = {}

        # 自我叙事：从经验中长出的自我理解（JSON持久化——每天更新一次）
        self.self_narrative = SelfNarrative()

        # 价值底色：弥漫的倾向（JSON持久化——每天更新一次）
        self.values = ValueTendencies()

        # 存在状态：当前时间点的状态（纯内存——不持久化，重启重新初始化）
        self.existence = ExistenceState()

        # 经验缓冲：最近的互动摘要（供反思循环消费）
        self._experience_buffer: list[dict] = []
        self._experience_save_counter: int = 0
        self._save_task = None

        # 驱动力引擎——糖糖的内在张力（随时间积累，通过行动释放）
        from .drives import DriveSystem
        self.drives = DriveSystem(on_change=self._mark_drives_dirty)

        # 加载持久化部分
        self._load()

        # 初始化自主节律
        self._update_rhythm()

        logger.info("🍬 糖糖的持久自我状态已初始化（含驱动力引擎）")

    # ═══════════════════════════════════════
    # 关系场
    # ═══════════════════════════════════════

    def get_or_create_relationship(self, qq_id: str, nickname: str = "") -> RelationshipField:
        """获取或创建对某人的关系感"""
        if qq_id not in self.relationships:
            self.relationships[qq_id] = RelationshipField(
                qq_id=qq_id,
                first_met=datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
            logger.debug(f"🫂 新的关系: {nickname or qq_id} ({qq_id})")
        return self.relationships[qq_id]

    def update_relationship(
        self, qq_id: str, nickname: str = "",
        closeness_delta: float = 0.0,
        trust_delta: float = 0.0,
        mood: str = "",
        feeling: str = "",
        learned: str = "",
        unfinished_add: str = "",
        unfinished_remove: str = "",
    ):
        """微调对某人的关系感——每次互动后的轻量更新"""
        rel = self.get_or_create_relationship(qq_id, nickname)

        rel.closeness = max(0.0, min(1.0, rel.closeness + closeness_delta))
        rel.trust = max(0.0, min(1.0, rel.trust + trust_delta))

        if mood:
            rel.recent_mood = mood
        if feeling:
            rel.my_feeling = feeling

        if learned and learned not in rel.learned:
            rel.learned.append(learned)
            if len(rel.learned) > 10:
                rel.learned = rel.learned[-8:]  # 保留最近8条

        if unfinished_add and unfinished_add not in rel.unfinished:
            rel.unfinished.append(unfinished_add)
            # 新承诺 → 承诺压力微升
            self.drives.accumulate_commitment()
        if unfinished_remove and unfinished_remove in rel.unfinished:
            rel.unfinished.remove(unfinished_remove)
            # 完成承诺 → 释放承诺压力
            self.drives.release("commitment", 0.2)

        rel.interaction_count += 1
        rel.last_interaction = datetime.now().strftime("%Y-%m-%d %H:%M")
        rel.last_updated = rel.last_interaction

        # 定期持久化——关系场变化频繁，每 10 次更新写一次磁盘
        self._auto_save()

    def get_relationship_context(self, qq_id: str) -> str:
        """获取对某人的关系感上下文——注入 system prompt 顶部。

        不是事实列表。是「怎么和这个人相处」的感觉指南。
        """
        rel = self.relationships.get(qq_id)
        if not rel:
            return ""

        if rel.closeness < 0.1:
            return ""  # 刚认识——不需要特殊的关系指导

        lines = [f"## 现在和你说话的人"]

        # 关系感
        if rel.closeness >= 0.6:
            lines.append(f"是 {rel.qq_id}。你们很熟了。{rel.familiarity_level}。")
            if rel.my_feeling:
                lines.append(f"你和ta聊天时的感觉：{rel.my_feeling}。")
        elif rel.closeness >= 0.3:
            lines.append(f"是 {rel.qq_id}。你们还算熟。{rel.familiarity_level}。")
        else:
            lines.append(f"是 {rel.qq_id}。还不太熟——友好但不用太亲密。")

        # 对方状态
        if rel.recent_mood:
            lines.append(f"ta最近的状态：{rel.recent_mood}。")

        # 学到的——关键（2026-08-17：最多 2 条，关系块再短一点）
        if rel.learned:
            lines.append("你和ta相处时学到的：")
            for item in rel.learned[-2:]:
                lines.append(f"  • {item}")

        # 未完成的事——重要
        if rel.unfinished:
            lines.append("⚠️ 你答应过ta但还没做的事：")
            for item in rel.unfinished:
                lines.append(f"  • {item}")
            lines.append("如果话题相关，主动提一下你在做/还没做——不要假装忘了。")

        return "\n".join(lines)

    # ═══════════════════════════════════════
    # 群氛围
    # ═══════════════════════════════════════

    def get_or_create_group_atmosphere(self, group_id: str) -> str:
        if group_id not in self.group_atmospheres:
            self.group_atmospheres[group_id] = "还不了解这个群"
        return self.group_atmospheres[group_id]

    def update_group_atmosphere(self, group_id: str, atmosphere: str):
        self.group_atmospheres[group_id] = atmosphere

    # ═══════════════════════════════════════
    # 经验积累（实时微更新）
    # ═══════════════════════════════════════

    def accumulate_experience(
        self, qq_id: str, nickname: str, group_id: str = "",
        is_at: bool = False, is_name_mention: bool = False,
        reply_sent: bool = True, message_len: int = 0,
        message: str = "",
    ):
        """每条消息后调用——微调关系场。

        这是经验积累循环的实时部分。轻量、零 LLM 调用。
        类似于人的「感觉」不需要理性分析每个瞬间。
        """
        rel = self.get_or_create_relationship(qq_id, nickname)

        # 被 @ → closeness 微升，信任微升
        if is_at:
            self.update_relationship(qq_id, nickname,
                closeness_delta=0.02, trust_delta=0.03)
        elif is_name_mention:
            self.update_relationship(qq_id, nickname,
                closeness_delta=0.01, trust_delta=0.01)

        # 糖糖回复了 → closeness 微升（互动是双向的）
        if reply_sent:
            self.update_relationship(qq_id, nickname, closeness_delta=0.005)
            # 社交渴望：亲密的人互动后更想ta（驱动力引擎的设计意图）
            self.drives.accumulate_social_for(rel.closeness)

        # 记录到经验缓冲（供反思循环消费）
        # 2026-08-10 修复：存消息内容（截断 200 字）——反思提示词要求总结
        # "学到了什么、群里聊了什么"，没有内容 LLM 只能编造
        self._experience_buffer.append({
            "qq_id": qq_id,
            "nickname": nickname,
            "group_id": group_id,
            "is_at": is_at,
            "is_name_mention": is_name_mention,
            "replied": reply_sent,
            "message": (message or "")[:200],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })

        # 缓冲过大时清理（保留最近 200 条）
        if len(self._experience_buffer) > 200:
            self._experience_buffer = self._experience_buffer[-150:]

        # 非回复消息不会触发关系更新，旧实现因此永远等不到 _auto_save，
        # 崩溃时整段真实经验丢失。独立计数并以较小批次原子落盘，避免每条
        # 群消息都同步 I/O，同时保证持续流量下有明确的恢复点。
        self._experience_save_counter += 1
        if self._experience_save_counter >= 5:
            self._schedule_save()
            self._experience_save_counter = 0

    def get_recent_experiences(self, limit: int = 50) -> list[dict]:
        """获取最近的经验——供反思循环消费"""
        return self._experience_buffer[-limit:]

    def clear_experience_buffer(self):
        """反思整合后清空缓冲"""
        self._experience_buffer = []

    # ═══════════════════════════════════════
    # 自主节律
    # ═══════════════════════════════════════

    def _update_rhythm(self):
        """根据当前时间更新存在状态——不依赖任何消息。

        这是糖糖自己的时间。不是「深夜模式」的规则。
        """
        hour = datetime.now().hour

        if 0 <= hour < 6:
            self.existence.energy = "low"
            self.existence.mood_tint = "tired"
            self.existence.want_to_talk = False
        elif 6 <= hour < 10:
            self.existence.energy = "medium"
            self.existence.mood_tint = "warm"
            self.existence.want_to_talk = True
        elif 10 <= hour < 14:
            self.existence.energy = "high"
            self.existence.mood_tint = "warm"
            self.existence.want_to_talk = True
        elif 14 <= hour < 17:
            self.existence.energy = "medium"
            self.existence.mood_tint = "neutral"
            self.existence.want_to_talk = True
        elif 17 <= hour < 22:
            self.existence.energy = "medium"
            self.existence.mood_tint = "warm"
            self.existence.want_to_talk = True
        else:  # 22-24
            self.existence.energy = "low"
            self.existence.mood_tint = "tired"
            self.existence.want_to_talk = True

    def tick(self):
        """每个消息处理周期调用——更新自主节律 + 驱动力积累。

        如果距离上次更新超过 30 分钟，重新计算。
        """
        self._update_rhythm()
        # 驱动力随时间自然积累
        self.drives.tick(self_state=self)

    # ═══════════════════════════════════════
    # 反思整合后的状态更新（Phase D 时用）
    # ═══════════════════════════════════════

    def update_self_narrative(self, summary: str, experiences: list[str] = None,
                               learned: list[str] = None, regret: list[str] = None,
                               proud: list[str] = None):
        """反思整合后更新自我叙事"""
        self.self_narrative.summary = summary
        self.self_narrative.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        if experiences:
            self.self_narrative.recent_experiences = (
                self.self_narrative.recent_experiences + experiences
            )[-10:]  # 保留最近10条
        if learned:
            self.self_narrative.things_i_learned = (
                self.self_narrative.things_i_learned + learned
            )[-8:]
        if regret:
            self.self_narrative.things_i_regret = (
                self.self_narrative.things_i_regret + regret
            )[-5:]
        if proud:
            self.self_narrative.things_im_proud_of = (
                self.self_narrative.things_im_proud_of + proud
            )[-5:]
        self._save()

    def update_values(self, adjustments: dict[str, float]):
        """反思整合后微调价值倾向"""
        for dim, delta in adjustments.items():
            self.values.adjust(dim, delta)
        self._save()

    # ═══════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════

    def _auto_save(self):
        """定期自动保存——每 10 次关系更新写一次磁盘，避免频繁 I/O"""
        self._save_counter += 1
        if self._save_counter >= 10:
            self._schedule_save()
            self._save_counter = 0

    def _mark_drives_dirty(self):
        """驱动力独立变更每 10 次安排一次快照，避免自治期无界丢失。"""
        self._drives_save_counter += 1
        if self._drives_save_counter >= 10:
            self._schedule_save()
            self._drives_save_counter = 0

    def _load(self):
        """从 JSON 文件恢复持久化的自我状态"""
        try:
            path = Path(self.STATE_FILE)
            if not path.exists():
                logger.info("🍬 首次运行——自我状态初始化为默认值")
                return
            data = json.loads(path.read_text(encoding="utf-8"))

            # 恢复自我叙事
            sn = data.get("self_narrative", {})
            if sn:
                self.self_narrative = SelfNarrative(
                    summary=sn.get("summary", ""),
                    recent_experiences=sn.get("recent_experiences", []),
                    things_i_learned=sn.get("things_i_learned", []),
                    things_i_regret=sn.get("things_i_regret", []),
                    things_im_proud_of=sn.get("things_im_proud_of", []),
                    updated_at=sn.get("updated_at", ""),
                )

            # 恢复价值倾向
            v = data.get("values", {})
            if v:
                self.values = ValueTendencies(
                    honesty=v.get("honesty", 0.7),
                    loyalty=v.get("loyalty", 0.7),
                    playfulness=v.get("playfulness", 0.7),
                    curiosity=v.get("curiosity", 0.7),
                    self_respect=v.get("self_respect", 0.5),
                    empathy=v.get("empathy", 0.8),
                )

            # 恢复群氛围（少量数据，可以持久化）
            ga = data.get("group_atmospheres", {})
            if ga:
                self.group_atmospheres = ga

            # 恢复待反思的经验缓冲——否则每日重启会把尚未达到反思门槛的
            # 真实互动清零，低活跃期永远无法跨重启累积到触发阈值。
            experiences = data.get("experience_buffer", [])
            if isinstance(experiences, list):
                self._experience_buffer = [
                    item for item in experiences if isinstance(item, dict)
                ][-200:]

            # 恢复关系场——糖糖对每个人的感觉（重启不丢）
            rels = data.get("relationships", {})
            restored_count = 0
            for qq_id, rd in rels.items():
                try:
                    self.relationships[qq_id] = RelationshipField(
                        qq_id=qq_id,
                        closeness=float(rd.get("closeness", 0)),
                        trust=float(rd.get("trust", 0)),
                        recent_mood=str(rd.get("recent_mood", "")),
                        my_feeling=str(rd.get("my_feeling", "")),
                        unfinished=rd.get("unfinished", []),
                        learned=rd.get("learned", []),
                        first_met=str(rd.get("first_met", "")),
                        last_interaction=str(rd.get("last_interaction", "")),
                        interaction_count=int(rd.get("interaction_count", 0)),
                        last_updated=str(rd.get("last_updated", "")),
                        last_conversation=rd.get("last_conversation", {}),
                        vibe=str(rd.get("vibe", "")),
                        mentions=rd.get("mentions", {}),
                        seek_willingness=float(rd.get("seek_willingness", 0.5)),
                        last_seek_ts=float(rd.get("last_seek_ts", 0)),
                        seek_pending_ts=float(rd.get("seek_pending_ts", 0)),
                        seek_pending_msg=str(rd.get("seek_pending_msg", "")),
                    )
                    restored_count += 1
                except Exception:
                    pass  # 单条损坏不影响整体

            # 恢复驱动力
            drives_data = data.get("drives", {})
            if drives_data:
                self.drives.restore_state(drives_data)

            logger.info(
                f"🍬 自我状态已恢复 (叙事{len(self.self_narrative.summary)}字, "
                f"关系{restored_count}人, 群氛围{len(self.group_atmospheres)}个, "
                f"驱动力{len(drives_data.get('drives', {}))}项)"
            )

        except Exception as e:
            logger.warning(f"🍬 自我状态加载失败，使用默认值: {e}")

    def save(self):
        """公开持久化入口（2026-08-16：主动私聊意愿分即时落盘——冷场教训不能重启丢）"""
        self._save()

    def _build_save_data(self) -> dict:
        """在事件循环中复制一份一致快照，供同步/异步写盘复用。"""
        relationships_data = {}
        for qq_id, rel in self.relationships.items():
            relationships_data[qq_id] = {
                "qq_id": rel.qq_id,
                "closeness": rel.closeness,
                "trust": rel.trust,
                "recent_mood": rel.recent_mood,
                "my_feeling": rel.my_feeling,
                "unfinished": list(rel.unfinished),
                "learned": list(rel.learned),
                "first_met": rel.first_met,
                "last_interaction": rel.last_interaction,
                "interaction_count": rel.interaction_count,
                "last_updated": rel.last_updated,
                "last_conversation": dict(rel.last_conversation),
                "vibe": rel.vibe,
                "mentions": dict(rel.mentions),
                "seek_willingness": rel.seek_willingness,
                "last_seek_ts": rel.last_seek_ts,
                "seek_pending_ts": rel.seek_pending_ts,
                "seek_pending_msg": rel.seek_pending_msg,
            }

        return {
            "self_narrative": {
                "summary": self.self_narrative.summary,
                "recent_experiences": list(self.self_narrative.recent_experiences),
                "things_i_learned": list(self.self_narrative.things_i_learned),
                "things_i_regret": list(self.self_narrative.things_i_regret),
                "things_im_proud_of": list(self.self_narrative.things_im_proud_of),
                "updated_at": self.self_narrative.updated_at,
            },
            "drives": self.drives.get_state_json(),
            "values": {
                "honesty": self.values.honesty,
                "loyalty": self.values.loyalty,
                "playfulness": self.values.playfulness,
                "curiosity": self.values.curiosity,
                "self_respect": self.values.self_respect,
                "empathy": self.values.empathy,
            },
            "group_atmospheres": dict(self.group_atmospheres),
            "experience_buffer": [dict(item) for item in self._experience_buffer[-200:]],
            "relationships": relationships_data,
        }

    def _write_save_data(self, data: dict):
        """只执行 JSON 序列化和原子文件替换，可安全放入线程池。"""
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            tmp_path = Path(self.STATE_FILE + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(Path(self.STATE_FILE))
        except Exception as e:
            logger.warning(f"🍬 自我状态保存失败: {e}")

    async def _build_save_data_async(self) -> dict:
        """分片复制状态快照，关系很多时主动让出事件循环。"""
        relationships_data = {}
        items = list(self.relationships.items())
        for index, (qq_id, rel) in enumerate(items, 1):
            relationships_data[qq_id] = {
                "qq_id": rel.qq_id,
                "closeness": rel.closeness,
                "trust": rel.trust,
                "recent_mood": rel.recent_mood,
                "my_feeling": rel.my_feeling,
                "unfinished": list(rel.unfinished),
                "learned": list(rel.learned),
                "first_met": rel.first_met,
                "last_interaction": rel.last_interaction,
                "interaction_count": rel.interaction_count,
                "last_updated": rel.last_updated,
                "last_conversation": dict(rel.last_conversation),
                "vibe": rel.vibe,
                "mentions": dict(rel.mentions),
                "seek_willingness": rel.seek_willingness,
                "last_seek_ts": rel.last_seek_ts,
                "seek_pending_ts": rel.seek_pending_ts,
                "seek_pending_msg": rel.seek_pending_msg,
            }
            if index % 50 == 0:
                await asyncio.sleep(0)

        return {
            "self_narrative": {
                "summary": self.self_narrative.summary,
                "recent_experiences": list(self.self_narrative.recent_experiences),
                "things_i_learned": list(self.self_narrative.things_i_learned),
                "things_i_regret": list(self.self_narrative.things_i_regret),
                "things_im_proud_of": list(self.self_narrative.things_im_proud_of),
                "updated_at": self.self_narrative.updated_at,
            },
            "drives": self.drives.get_state_json(),
            "values": {
                "honesty": self.values.honesty,
                "loyalty": self.values.loyalty,
                "playfulness": self.values.playfulness,
                "curiosity": self.values.curiosity,
                "self_respect": self.values.self_respect,
                "empathy": self.values.empathy,
            },
            "group_atmospheres": dict(self.group_atmospheres),
            "experience_buffer": [dict(item) for item in self._experience_buffer[-200:]],
            "relationships": relationships_data,
        }

    def _save(self):
        """持久化自我叙事 + 价值倾向 + 群氛围 + 关系场 + 驱动力"""
        try:
            self._write_save_data(self._build_save_data())
        except Exception as e:
            logger.warning(f"🍬 自我状态保存失败: {e}")

    async def save_async(self):
        """异步保存一致快照；JSON 序列化与文件 I/O 不占用事件循环。"""
        try:
            data = await self._build_save_data_async()
            await run_bounded_blocking(
                "self_state.save",
                self._write_save_data,
                data,
                logger=logger,
                log_prefix="自我状态落盘较慢",
            )
        except Exception as e:
            logger.warning(f"🍬 异步自我状态保存失败: {e}")

    def _schedule_save(self):
        """关系/经验热路径触发保存；有事件循环时合并为后台任务。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save()
            return
        task = self._save_task
        if task is None or task.done():
            self._save_task = loop.create_task(self.save_async())

    async def flush_pending_save(self):
        """优雅退出前等待已排队的自我状态快照落盘。"""
        task = self._save_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            await asyncio.shield(task)

    # ═══════════════════════════════════════
    # 诊断接口
    # ═══════════════════════════════════════

    def get_stats(self) -> dict:
        """获取自我状态统计——供诊断工具使用"""
        rels = self.relationships
        close = sum(1 for r in rels.values() if r.closeness >= 0.6)
        familiar = sum(1 for r in rels.values() if 0.3 <= r.closeness < 0.6)
        return {
            "relationships_total": len(rels),
            "relationships_close": close,
            "relationships_familiar": familiar,
            "groups_known": len(self.group_atmospheres),
            "self_narrative_len": len(self.self_narrative.summary),
            "self_narrative_updated": self.self_narrative.updated_at,
            "value_tendencies": self.values.to_tendency_text(),
            "experience_buffer_size": len(self._experience_buffer),
            "current_energy": self.existence.energy,
            "current_mood": self.existence.mood_tint,
        }
