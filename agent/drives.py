"""
驱动力引擎 — 糖糖的内在张力系统

「有内在生命的存在不是"有状态"——是有冲突。
  互相矛盾的需求必须在选择中解决，这个解决过程就是意识。」

六种驱动力随时间自然积累，通过行动释放，相互竞争。
LLM 是最终决策者——驱动力系统只提供"糖糖现在最想要什么"。

科学基础：
- 预测加工理论（Friston）：预测误差驱动行动
- 对手过程理论（Solomon & Corbit）：释放后反向微调
- 异ostatic 调节（Sterling）：预测需求，主动行动

数值设计（2026-08-10 调优）：

  驱动力        积累/小时  到阈值需   回复释放  主动发起释放  触发后回落至
  ───────────── ───────── ─────────  ─────────  ───────────  ──────────
  社交渴望        0.08      ~9h       0.12       0.35          ~0.35
  承诺压力        0.10      ~6.5h     0.30       0.40          ~0.30
  信息饥渴        0.06      ~12h      0.10       0.35          ~0.35
  表达冲动        0.07      ~10h      0.15       0.35          ~0.35
  好奇心          0.04      ~19h      0.08       0.30          ~0.40
  回避欲          0.03      ~23h      0.10       （不主动发起）  —

  解读：「回复释放」= 被动回复一次扣多少（部分满足，实际含 10% 过冲，
  压 1.7-3.7 小时积累——承诺/回避压力重，压得久是设计）。
  「主动发起释放」= 自治循环发消息后按主导驱动力扣多少（行动级满足，压 4-6 小时积累）。
  「触发后回落至」= 0.7+ 触发 → 行动级释放后掉到的区间——欲望有涨有落，
  数小时后重新接近阈值再触发 = 稀缺感与节奏感（2026-07-31 版释放量远小于积累，
  全部驱动力永久饱和顶格 1.00，无张力——已修正）。
  对手过程：释放后再过冲 10%（满足后的淡漠期），由 tick 缓慢回升。
  负面反馈：release(负数) = 反向强化（被冷落 → 回避欲上升）。

  调参指南：
  - 积累速度 → __init__ 的 alpha 参数 + tick() 的能量调制硬编码（两处须同步）
  - 回复释放 → release_by_action() 的 mapping 字典
  - 主动发起释放 → handler_autonomy.py 的 ACTION_RELEASES
  - 阈值     → __init__ 的 threshold 参数

  精力影响：高能量时社交渴望加速 2×、回避欲减速；疲惫则相反。

  4 小时离线 cap 防止长时间离线后暴涨。安静半天后驱动力自然接近阈值，
  自治循环（每 10 分钟检查）触发糖糖主动找人说话。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger("糖糖.Drives")


@dataclass
class Drive:
    """一种驱动力——随时间积累，通过行动释放。

    动力学方程：D(t+1) = D(t) + α·Δt - β·A(t) + γ·P(t)
    - α: 自然积累速率
    - β: 释放系数
    - γ: 预测误差敏感度
    - P(t): 期望 vs 现实的偏差
    """

    name: str
    label: str                    # 中文标签
    value: float = 0.0           # 当前值 [0, 1]
    alpha: float = 0.01          # 自然积累速率 (/小时)
    beta: float = 0.35           # 释放系数（每次行动释放多少）
    gamma: float = 0.1           # 预测加工敏感度
    threshold: float = 0.7       # 触发阈值——超过此值产生行动冲动
    last_tick: str = ""          # 上次 tick 时间
    last_release: str = ""       # 上次释放时间
    history: list[float] = field(default_factory=list)  # 最近 20 次的值历史

    def tick(self, dt_hours: float):
        """时间流逝——驱动力自然积累"""
        self.value = min(1.0, self.value + self.alpha * dt_hours)
        self.last_tick = datetime.now().strftime("%Y-%m-%d %H:%M")

    def release(self, amount: float | None = None):
        """通过行动释放驱动力。

        负数 = 反向：负面反馈强化驱动力（被冷落 → 回避欲上升）。
        释放后过冲——满足感的余波让欲望短暂低于基线，由 tick 缓慢回升。
        实际释放量 = amount × 1.1（含 10% 过冲）。
        """
        # amount=None → 使用默认释放量 beta；0 → 明确表示不释放（无变化）
        if amount is None or not math.isfinite(amount):
            amount = self.beta
        if amount < 0:
            # 反向：不是释放而是强化——负面体验让欲望更强
            self.value = min(1.0, self.value + abs(amount))
            self.last_release = datetime.now().strftime("%Y-%m-%d %H:%M")
            return
        # 对手过程（Solomon）：释放后短暂过冲——欲望消解后的淡漠期。
        # 之前实现是 +10% 反弹（助燃），方向反了；改为再压低 10%，
        # 由 tick 的自然积累缓慢恢复——「吃饱了不想再吃」的感觉。
        self.value = max(0.0, self.value - amount * 1.1)
        self.last_release = datetime.now().strftime("%Y-%m-%d %H:%M")

    def prediction_error(self, expected: float, actual: float):
        """预测加工：期望与现实的偏差加剧驱动力"""
        error = abs(expected - actual)
        self.value = min(1.0, self.value + self.gamma * error)

    @property
    def is_urgent(self) -> bool:
        return self.value >= self.threshold

    @property
    def is_active(self) -> bool:
        return self.value >= 0.3

    @property
    def intensity_text(self) -> str:
        """驱动力强度的自然语言描述"""
        if self.value >= 0.9:
            return "非常强烈——几乎无法忽视"
        elif self.value >= self.threshold:
            return "强烈——想要采取行动"
        elif self.value >= 0.5:
            return "明显——能感受到但可以控制"
        elif self.value >= 0.3:
            return "隐约——微弱但存在"
        else:
            return "微弱——几乎感觉不到"


class DriveSystem:
    """六种驱动力组成的竞争系统。

    不是规则引擎。驱动力提供"糖糖想要什么"——LLM 决定"怎么做"。
    """

    def __init__(self, on_change: Callable[[], None] | None = None):
        self.drives: dict[str, Drive] = {
            "social": Drive(
                name="social", label="社交渴望",
                alpha=0.08, beta=0.35, threshold=0.7,
            ),
            "commitment": Drive(
                name="commitment", label="承诺压力",
                alpha=0.10, beta=0.4, threshold=0.65,
            ),
            "curiosity_info": Drive(
                name="curiosity_info", label="信息饥渴",
                alpha=0.06, beta=0.3, threshold=0.7,
            ),
            "express": Drive(
                name="express", label="表达冲动",
                alpha=0.07, beta=0.35, threshold=0.7,
            ),
            "curiosity_explore": Drive(
                name="curiosity_explore", label="好奇心",
                alpha=0.04, beta=0.3, threshold=0.75,
            ),
            "avoid": Drive(
                name="avoid", label="回避欲",
                alpha=0.03, beta=0.4, threshold=0.7,
            ),
        }
        self._last_tick_time: Optional[datetime] = None
        self._on_change = on_change
        # 注入变化门的内存态（2026-10-12 M3）。窗口不长（30 分钟），重启后最多
        # 多注入一次——按持久化规矩 #15，只约束「重启后必须记得」的状态。
        self._last_injection: tuple[str, str] | None = None
        self._last_injection_at: float = 0.0

    # ═══════════════════════════════════════
    # 核心循环
    # ═══════════════════════════════════════

    def tick(self, self_state=None, dt_hours: float = None):
        """时间流逝——所有驱动力自然积累。

        每 N 分钟调用一次（由 TangTangSelf.tick() 或 handler 循环触发）。
        self_state 用于自适应调整积累速率。
        """
        now = datetime.now()

        # 计算时间间隔
        if dt_hours is None:
            if self._last_tick_time:
                dt_hours = (now - self._last_tick_time).total_seconds() / 3600
            else:
                dt_hours = 0.05  # 首次 tick，假设 3 分钟
        self._last_tick_time = now

        # 限制单次积累上限（防止长时间离线后暴涨）
        dt_hours = min(dt_hours, 4.0)

        # ── 自适应调整 ──
        # 从 self_state 读取当前状态来调整积累速率
        energy = "medium"
        if self_state:
            energy = self_state.existence.energy

        # 精力修正：精力低 → 回避欲加速，社交渴望减速
        energy_mod = {"high": 0.8, "medium": 1.0, "low": 1.3, "drained": 1.8}

        # ── 逐驱动力积累 ──
        # 精力修正系数（不永久修改 drive.alpha——用局部变量）
        for name, drive in self.drives.items():
            effective_alpha = drive.alpha
            if name == "avoid":
                # 回避欲：精力越低积累越快
                mod = energy_mod.get(energy, 1.0)
                effective_alpha = 0.03 * mod
            elif name == "social":
                # 社交渴望：精力低时不想社交
                mod = 2.0 - energy_mod.get(energy, 1.0)
                effective_alpha = 0.08 * mod

            # 用有效 alpha 积累（不修改 drive.alpha 本身）
            drive.value = min(1.0, drive.value + effective_alpha * dt_hours)
            drive.last_tick = datetime.now().strftime("%Y-%m-%d %H:%M")

            # 记录历史（最多 20 个数据点）
            drive.history.append(drive.value)
            if len(drive.history) > 20:
                drive.history = drive.history[-20:]

        if self._on_change:
            self._on_change()

    # ═══════════════════════════════════════
    # 内部消化
    # ═══════════════════════════════════════

    # ── 高饱和钝化（2026-10-12 主人拍板，审计 P0-1）──
    # 取证：旧口径「>0.85 才衰减、地板 0.85、速率 0.01/h」在数学上是**惰性的**——
    # 0.01/h 小于任何 alpha（0.03~0.10/h），净速率恒为正，驱动力照样升到 1.00 停住
    # （实测 4/6 长期 0.94~0.99、每 30 分钟注入同一句）。只降地板不改变这一点：
    # 地板决定"最低能降到哪"，不决定"会不会贴顶"。所以改成按**超出地板的幅度**
    # 成比例松弛：净速率 = alpha − k·(v−floor)，平衡点 v* = floor + alpha/k。
    # k=0.5、floor=0.60 时六个驱动力的平衡点是 0.65~0.80——没有行动消费时谁也压不到
    # 0.80 以上；而行动级释放仍能把它打到 0.3~0.4 再爬回来，张力有上有下。
    SATURATION_FLOOR = 0.60
    SATURATION_RELAXATION_PER_HOUR = 0.5

    def natural_decay(self, dt_hours: float):
        """高饱和滞留时的松弛——压抑久了会钝化，越饱和越容易被时间冲淡。

        只在地板（SATURATION_FLOOR）之上生效，且速率与超出地板的幅度成正比。
        地板之上**没有硬停顿**：驱动力仍可靠 tick 的积累重新越过阈值——
        否则"钝化"会变成"永远安静"，那比贴顶更糟。

        2026-10-13 T1.1（审计 G3 观测缺口）：**每周期必留一行 info，含"无块可衰减"
        的情况**。这一行不是装饰——M3 的验收口径是「值是否收敛到 floor+α/k」，而在它
        存在之前，「衰减没生效」与「衰减生效了但值本来就低于地板」在日志里长得一模
        一样（RC5：失败路径不产生可观测事实）。2026-10-13 那次「M3 无效」的误判正是
        踩在这里：读到的 0.998 是**重启时从盘里载入的旧值**，衰减尚未跑满一个周期。
        汇总成一行而非每块一行：6 行/周期 × 144 周期 = 864 行/天太吵；一行/周期 =
        144 行/天（规划已按这个量级评估）。α 取标称值——social/avoid 在 tick() 里另
        有精力调制，故这两项的 v* 只能用标称 α 近似。
        """
        parts = []
        for name, drive in self.drives.items():
            before = drive.value
            excess = before - self.SATURATION_FLOOR
            if excess <= 0:
                continue
            after = max(
                self.SATURATION_FLOOR,
                before - self.SATURATION_RELAXATION_PER_HOUR * excess * dt_hours,
            )
            drive.value = after
            parts.append(
                f"{drive.label} {before:.3f}→{after:.3f}(Δ{after - before:+.3f} "
                f"excess={excess:.3f} α={drive.alpha:.3f})"
            )
        logger.info(
            "🔥 自然衰减 dt=%.3fh k=%.2f floor=%.2f | %s",
            dt_hours,
            self.SATURATION_RELAXATION_PER_HOUR,
            self.SATURATION_FLOOR,
            " ; ".join(parts) if parts else "无（各驱动力均未超地板）",
        )

    # ═══════════════════════════════════════
    # 查询接口
    # ═══════════════════════════════════════

    def get_dominant(self) -> Optional[Drive]:
        """返回当前最迫切的驱动力（超过阈值的最高值），或 None"""
        urgent = [d for d in self.drives.values() if d.is_urgent]
        if not urgent:
            return None
        urgent.sort(key=lambda d: d.value, reverse=True)
        return urgent[0]

    def get_all_active(self) -> list[Drive]:
        """返回所有活跃的驱动力（> 0.3），按强度降序"""
        active = [d for d in self.drives.values() if d.is_active]
        active.sort(key=lambda d: d.value, reverse=True)
        return active

    # 注入变化门（2026-10-12 M3，审计 P0-1「4/6 长期贴顶、每轮注入同一句」）
    # 同一 (驱动力, 强度档) 在冷却窗口内不重复注入——一句常驻的同一句话会退化成
    # 背景噪声；而**常驻背景注入**在 2026-08-17 瘦身时被实测有害。跨档位立刻放行。
    # 只作用于每轮的对话上下文（context_builder 传 gated=True）；自治决策简报
    # 不门控——那里驱动力是核心输入，不是背景。
    DRIVE_INJECT_COOLDOWN_SECONDS = 1800

    def get_drive_context(self, self_state=None, *, gated: bool = False) -> str:
        """生成注入 system_prompt 的驱动力上下文。

        这不是指令——是让 LLM 感知到糖糖当前的内在状态，
        像人感知到自己的情绪和欲望一样。
        2026-08-17 对话质量审查：只在主导驱动力足够强（≥0.6）时出现，
        一行感受口吻——次要清单/百分比/精力字段是给系统看的报告格式，
        常驻会诱导模型「汇报状态」而非「带着状态说话」。
        """
        dominant = self.get_dominant()

        # 注意：get_dominant() 已经要求 >= threshold（0.65~0.75），所以下面这个
        # 0.6 判定实际恒不成立——审计 P0-1 说的「注入门恒真」指的就是它。保留为
        # 防御性下界；真正的节流交给变化门。
        if not dominant or not dominant.is_active or dominant.value < 0.6:
            return ""

        # ── 变化门：换档立刻放行，同档在冷却窗口内不重复 ──
        # 档位用 Drive.intensity_text（既有的"强度"语义），不新增报告字段。
        band = dominant.intensity_text
        now = time.monotonic()
        if gated and self._last_injection == (dominant.name, band):
            if now - self._last_injection_at < self.DRIVE_INJECT_COOLDOWN_SECONDS:
                return ""
        self._last_injection = (dominant.name, band)
        self._last_injection_at = now

        desire_texts = {
            "social": "你想和人亲近——特别是那些好久没说话的人。如果ta们出现，你可能会比平时更主动。",
            "commitment": "你有点在意那些没做完的事——答应过但还没兑现的承诺让你不太舒服。如果有人提到相关的事，你会想解释或跟进。",
            "curiosity_info": "你感觉自己错过了不少事——群里发生了什么你不知道。如果有人提到你不知道的事，你会更想追问。",
            "express": "你有很多话想说、很多感受想表达。唱歌、撒娇、认真聊天——你特别想找个人倾诉。",
            "curiosity_explore": "你对世界充满好奇——有什么新鲜事、新话题，你特别想了解。如果有人提到你不太懂的事，你会追问。",
            "avoid": "你有点累了，不太想营业。如果有人找你聊复杂的话题，你可能会简短回应或委婉地说现在脑子转不太动。",
        }
        text = desire_texts.get(dominant.name, "你有一种强烈的内在冲动")
        # 语气随强度走（M3 验收：「注入句随强度变化」）——**不加百分比/清单**：
        # 那种报告格式正是 2026-08-17 瘦身删掉的，它会诱导模型「汇报状态」
        # 而不是「带着状态说话」。所以只改语气，不加字段。
        if dominant.value >= 0.9:
            text = "这股冲动有点压不住了。" + text
        elif dominant.value < dominant.threshold + 0.05:
            # 刚过阈值那一档。下界不能直接用 threshold——get_dominant() 已经保证
            # value >= threshold，那样写会变成**永远不执行**的死分支（正是审计
            # 一直在批的「写了不生效」同形）。
            text = "隐约有一点——" + text
        return f"## 你现在内在的状态\n{text}"

    # ═══════════════════════════════════════
    # 行动接口
    # ═══════════════════════════════════════

    def release(self, drive_name: str, amount: float = 0.0):
        """行动后释放指定的驱动力"""
        if drive_name in self.drives:
            self.drives[drive_name].release(amount)
            logger.debug(
                f"🔥 驱动力释放: {self.drives[drive_name].label} "
                f"→ {self.drives[drive_name].value:.2f}"
            )
            if self._on_change:
                self._on_change()

    def release_by_action(self, action_type: str):
        """根据行动类型自动释放对应的驱动力。

        action_type:
        - "reply_social": 回复了某人的消息 → 释放社交渴望
        - "reply_commitment": 兑现了一条承诺（或划掉未完成项）→ 释放承诺压力
        - "asked_question": 追问了某事 → 释放好奇心/信息饥渴
        - "sang_or_played": 唱歌/撒娇/玩耍 → 释放表达冲动
        - "avoided": 选择不回复或简短回复 → 释放回避欲
        - "initiated": 主动发起对话 → 释放社交渴望

        2026-10-13 P1（审计 P0-1「`reply_commitment` 零调用」）：本表是这些数值的
        **唯一来源**。此前兑现承诺走的是散在 handler/self_state 两处的
        `release("commitment", 0.2)` 魔法数，而命名动作 `reply_commitment`
        在生产代码里**零调用**——表与实际行为分叉，审计据此判为「写了没接线」。
        现在两个消费点都改走本表，数值取本表原design 值 0.30（净 0.33）。
        行为增量已实测知情：单次结算的净释放从 0.22 → 0.33（+50%），
        按 commitment alpha 0.10/h 计算，压制时间 2.2h → 3.3h；方向与审计
        「消费点要压得住积累」（P0-1：承诺压力长期 0.976 贴顶）一致。
        """
        # 数值（2026-08-10 调优）：回复 = 部分满足。实际释放量 = amount × 1.1（含 10% 过冲），
        # 恢复时间 ≈ amount×1.1/alpha：social 1.7h、commitment 3.3h、curiosity_info 1.8h、
        # curiosity_explore 2.2h、express 2.4h、avoid 3.7h——承诺/回避压力重，压得久是设计。
        # 主动发起（initiated）走自治循环的 ACTION_RELEASES 行动级释放。
        # 原则：释放量必须压得住积累——否则全部驱动力永久饱和（曾全部顶格 1.00，无张力）。
        mapping = {
            "reply_social": [("social", 0.12), ("avoid", 0.02)],
            "reply_commitment": [("commitment", 0.3)],
            "asked_question": [("curiosity_info", 0.1), ("curiosity_explore", 0.08)],
            "sang_or_played": [("express", 0.15)],
            "avoided": [("avoid", 0.1)],
            "initiated": [("social", 0.35), ("express", 0.15)],
        }
        for drive_name, amount in mapping.get(action_type, []):
            self.release(drive_name, amount)

    def accumulate_social_for(self, closeness: float):
        """根据与某人的亲密度加速社交渴望的积累。
        在 accumulate_experience 中调用——每次和某人互动后，
        对 ta 的想念会随着关系深度而增加。
        """
        drive = self.drives["social"]
        # 亲密的人 → 更快想念（0.005 ~ 0.02 额外积累）
        extra = closeness * 0.02
        drive.value = min(1.0, drive.value + extra)

    def accumulate_commitment(self, overdue_days: float = 0):
        """有新承诺或承诺逾期 → 加速承诺压力积累"""
        drive = self.drives["commitment"]
        extra = 0.03 + overdue_days * 0.01
        drive.value = min(1.0, drive.value + extra)

    # ═══════════════════════════════════════
    # 诊断
    # ═══════════════════════════════════════

    def get_stats(self) -> dict:
        """获取驱动力统计——供诊断工具使用"""
        return {
            name: {
                "value": round(drive.value, 3),
                "label": drive.label,
                "is_urgent": drive.is_urgent,
                "intensity": drive.intensity_text,
            }
            for name, drive in self.drives.items()
        }

    def get_state_json(self) -> dict:
        """导出可持久化的驱动力状态"""
        return {
            "drives": {
                name: round(drive.value, 3)
                for name, drive in self.drives.items()
            },
            "last_tick": self._last_tick_time.strftime("%Y-%m-%d %H:%M")
                if self._last_tick_time else "",
        }

    def restore_state(self, data: dict):
        """从持久化数据恢复驱动力状态"""
        drives_data = data.get("drives", {})
        for name, value in drives_data.items():
            if name in self.drives:
                v = float(value)
                # 防御：损坏/手工编辑的持久化数据可能带 NaN/Inf/负数/超界值——
                # NaN 会通过动力学传播污染所有驱动力，必须拒绝非有限数并夹紧
                if not math.isfinite(v):
                    v = 0.0
                self.drives[name].value = min(1.0, max(0.0, v))
        # 恢复上次 tick 时间（如果持久化中有）
        last_tick = data.get("last_tick", "")
        if last_tick:
            try:
                self._last_tick_time = datetime.strptime(last_tick, "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                pass
