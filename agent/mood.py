"""
💓 情绪引擎 — 糖糖有自己的心情了

三维情绪模型：精力 / 心情 / 耐心
- 精力 (energy):   随对话消耗，空闲恢复。没精力时回复变短、犯困
- 心情 (mood):     被夸涨、被骂跌。影响回复的积极/消极程度
- 耐心 (patience): 连续追问下降。低时炸毛、带刺

状态持久化到 SQLite（kv_store 表），重启恢复。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

logger = logging.getLogger("糖糖.Mood")

STATE_KEY = "mood_state"


class MoodEvent(Enum):
    """情绪事件——每种事件对应一套数值变化"""
    # 正面
    BEING_AT = ("被@", 2, 2, 0)
    BEING_PRAISED = ("被夸", 0, 5, 2)
    BEING_THANKED = ("被感谢", 0, 3, 1)
    BEING_MISSED = ("被想念", 0, 4, 1)
    INTIMATE_TALK = ("亲密聊天", 1, 3, 1)
    HELPED_SOMEONE = ("帮到别人", 1, 4, 0)

    # 中性（能耗大幅降低——糖糖不该说几句就累）
    SENT_REPLY = ("发回复", -1, 0, 0)        # was: -2, 0, -1
    SENT_LONG_REPLY = ("发长回复", -2, 1, 0)  # was: -4, 1, -2
    INTERJECTED = ("主动插话", 0, 0, 0)        # was: -1, 0, -1
    IDLE_RECOVERY = ("空闲恢复", 5, 0, 3)       # was: 10, 0, 5

    # 负面
    BEING_IGNORED = ("被无视", -1, -3, -2)
    BEING_SCOLDED = ("被骂", -3, -8, -5)
    REPEATED_QUESTIONS = ("连续追问", -2, -2, -8)
    LATE_NIGHT = ("深夜", -3, -1, 0)            # was: -5, -1, 0
    TALKING_TOO_MUCH = ("说话太多", -2, -1, -2)  # was: -3, -1, -3


@dataclass
class MoodState:
    """当前情绪快照"""
    energy: int = 85       # 0-100（起始精力提高，不轻易累）
    mood: int = 70         # 0-100
    patience: int = 75     # 0-100
    updated: str = ""      # ISO timestamp

    def clamp(self):
        """所有值限制在 0-100"""
        self.energy = max(0, min(100, self.energy))
        self.mood = max(0, min(100, self.mood))
        self.patience = max(0, min(100, self.patience))
        return self


class MoodEngine:
    """情绪引擎"""

    def __init__(self, config: dict | None = None, store=None):
        cfg = config or {}
        self._store = store  # Store 实例，用于持久化（None=降级到 JSON）
        self._state = MoodState()
        # 衰减速率（每小时）
        self._decay_energy = cfg.get("decay_energy_per_hour", 1)
        self._decay_mood_toward = cfg.get("mood_neutral", 70)  # 心情趋近的中性值
        self._decay_mood_rate = cfg.get("decay_mood_per_hour", 5)
        self._decay_patience = cfg.get("decay_patience_per_hour", 10)
        # 精力阈值——更宽容，不轻易喊累
        self._energy_high = cfg.get("energy_high", 50)    # 50+ 精力充沛
        self._energy_normal = cfg.get("energy_normal", 25)  # 25-50 正常
        self._energy_low = cfg.get("energy_low", 10)       # <10 真的累了
        self._last_update: datetime | None = None
        self._reply_count_since_rest: int = 0  # 本次会话回复数

    # ════════════════════════════════════════════════════════════
    # 公开接口
    # ════════════════════════════════════════════════════════════

    @property
    def energy(self) -> int:
        return self._state.energy

    @property
    def mood(self) -> int:
        return self._state.mood

    @property
    def patience(self) -> int:
        return self._state.patience

    def load(self):
        """从 SQLite 恢复状态。如果状态太旧（>12小时）或精力太低（<20），
        刷新为新会话初始值——每次重启都是精力充沛的开始。"""
        raw = None
        if self._store:
            raw = self._store.kv_get(STATE_KEY)

        data = None
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None

        if not data:
            self._state.updated = datetime.now().isoformat()
            return

        updated_str = data.get("updated", "")
        stale = True
        if updated_str:
            try:
                age = (datetime.now() - datetime.fromisoformat(updated_str)).total_seconds()
                stale = age > 12 * 3600
            except ValueError:
                pass
        energy_val = data.get("energy", 75)
        if stale or energy_val < 20:
            self._state.energy = 85
            self._state.mood = 70
            self._state.patience = 75
            self._state.updated = datetime.now().isoformat()
            self._last_update = datetime.now()
            logger.info(f"💓 状态过期或精力过低({energy_val})，已重置为 {self._state.energy}")
            return
        self._state.energy = energy_val
        self._state.mood = data.get("mood", 65)
        self._state.patience = data.get("patience", 70)
        self._state.updated = updated_str
        self._last_update = datetime.fromisoformat(self._state.updated) if updated_str else datetime.now()
        logger.debug(
            f"💓 情绪恢复: 精力{self._state.energy} "
            f"心情{self._state.mood} 耐心{self._state.patience}"
        )

    def save(self):
        """写入 SQLite（kv_store 表）"""
        self._state.updated = datetime.now().isoformat()
        data = json.dumps({
            "energy": self._state.energy,
            "mood": self._state.mood,
            "patience": self._state.patience,
            "updated": self._state.updated,
        }, ensure_ascii=False)
        if self._store:
            self._store.kv_set(STATE_KEY, data)

    def update(self, event: MoodEvent):
        """处理一个情绪事件"""
        self._apply_decay()  # 先应用时间衰减
        e_delta, m_delta, p_delta = event.value[1], event.value[2], event.value[3]
        self._state.energy += e_delta
        self._state.mood += m_delta
        self._state.patience += p_delta
        self._state.clamp()
        self._last_update = datetime.now()
        self.save()

    def build_context(self) -> str:
        """生成注入 LLM 提示词的情绪状态（2026-08-17 对话质量审查重写）——
        只在明显偏离中性时出现，一行感受口吻。数值是给系统看的：常驻数值块
        让模型「汇报状态」而非「带着状态说话」（表演感主源之一）。"""
        self._apply_decay()
        e, m, _p = self._state.energy, self._state.mood, self._state.patience
        parts = []
        if e <= 30:
            parts.append("你现在有点累了，回复可以短一点、软一点")
        elif e >= 80:
            parts.append("你现在精力很好，可以活泼一点")
        if m <= 45:
            parts.append("心情有点低，不想太热闹")
        elif m >= 85:
            parts.append("心情很好，想多聊几句")
        if not parts:
            return ""
        return "（" + "；".join(parts) + "）"

    # ════════════════════════════════════════════════════════════
    # 内部
    # ════════════════════════════════════════════════════════════

    def _apply_decay(self):
        """根据经过的时间应用衰减"""
        if not self._last_update:
            self._last_update = datetime.now()
            return

        elapsed = (datetime.now() - self._last_update).total_seconds() / 3600.0
        if elapsed <= 0:
            return

        # 精力：随时间衰减
        self._state.energy -= int(elapsed * self._decay_energy)

        # 心情：趋近中性
        neutral = self._decay_mood_toward
        steps = int(elapsed / 0.5)  # 每半小时一步
        for _ in range(min(steps, 20)):  # 最多算 10 小时
            if self._state.mood < neutral:
                self._state.mood += self._decay_mood_rate
            elif self._state.mood > neutral:
                self._state.mood -= self._decay_mood_rate

        # 耐心：缓慢恢复
        self._state.patience += int(elapsed * self._decay_patience)

        self._state.clamp()
        self._last_update = datetime.now()


# 情绪事件检测已移除——不再用关键词猜用户态度。
# LLM 自己理解消息中的情感，系统只记录客观事件（被@、发了回复等）。
