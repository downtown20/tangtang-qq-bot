"""
场景引擎 — 让糖糖在不同群担任不同角色。

支持两种场景类型：
- replace: 替换 role_card.md 中"性格+说话"章节，换入场景 role（活动管理者、程序专家）
- overlay: 不删任何猫娘内容，只追加一层敏感度提示（心理陪伴）

设计约束（硬编码）：
- overlay 敏感层上限 2000 字（加载时 warn）
- replace role 上限 500 字（加载时 warn）
- 不重复 comfort.py 已有内容（共情/倾听/不说教——那些已经由 comfort.py 实时处理）
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("糖糖.Scenario")

# ---------------------------------------------------------------------------
# 硬约束
# ---------------------------------------------------------------------------

MAX_OVERLAY_CHARS = 2000  # 敏感层超过此值 warn
MAX_REPLACE_CHARS = 500  # 替换型 role 超过此值 warn

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """一个场景定义"""
    name: str
    type: str = "replace"          # "replace" | "overlay"
    display: str = ""
    description: str = ""
    role: str = ""                  # replace 类型：替换性格+说话的角色定义
    sensitivity: str = ""           # overlay 类型：追加的敏感度提示
    tone: str = ""                  # 语调指引
    behavior: dict = field(default_factory=dict)
    scheduled_actions: dict = field(default_factory=dict)

    @property
    def is_overlay(self) -> bool:
        return self.type == "overlay"

    @property
    def is_replace(self) -> bool:
        return self.type == "replace"


# ---------------------------------------------------------------------------
# 场景管理器
# ---------------------------------------------------------------------------


class ScenarioManager:
    """加载和管理所有场景定义"""

    def __init__(self, scenarios_dir: str = "scenarios"):
        self._dir = Path(scenarios_dir)
        self._scenarios: dict[str, Scenario] = {}
        # ``/场景 重载`` runs in a worker; readers must see either the old or
        # the fully rebuilt map, never the clear/load gap in between.
        self._state_lock = threading.RLock()
        self._load_all()

    # ---- 加载 ----

    def _load_all(self) -> None:
        """扫描 scenarios/ 下所有 .yaml 文件并加载"""
        if not self._dir.exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"📁 场景目录已创建: {self._dir.resolve()}")
            return

        import yaml

        for f in sorted(self._dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"⚠️ 场景文件 {f.name} YAML 解析失败: {e}")
                continue

            if not isinstance(data, dict) or "name" not in data:
                logger.warning(f"⚠️ 场景文件 {f.name} 缺少 name 字段，跳过")
                continue

            scenario = Scenario(
                name=data["name"],
                type=data.get("type", "replace"),
                display=data.get("display", ""),
                description=data.get("description", ""),
                role=(data.get("role") or "").strip(),
                sensitivity=(data.get("sensitivity") or "").strip(),
                tone=(data.get("tone") or "").strip(),
                behavior=data.get("behavior") or {},
                scheduled_actions=data.get("scheduled_actions") or {},
            )

            # ---- 硬约束校验 ----
            if scenario.is_overlay and scenario.sensitivity:
                if len(scenario.sensitivity) > MAX_OVERLAY_CHARS:
                    logger.warning(
                        f"⚠️ 场景「{scenario.display}」敏感层 {len(scenario.sensitivity)} 字，"
                        f"超出上限 {MAX_OVERLAY_CHARS}。建议精简。"
                    )
            elif scenario.is_replace and scenario.role:
                if len(scenario.role) > MAX_REPLACE_CHARS:
                    logger.warning(
                        f"⚠️ 场景「{scenario.display}」role {len(scenario.role)} 字，"
                        f"超出上限 {MAX_REPLACE_CHARS}。建议精简。"
                    )

            self._scenarios[scenario.name] = scenario
            logger.info(
                f"📋 场景已加载: {scenario.display} "
                f"({scenario.name}, type={scenario.type}, "
                f"{'sensitivity' if scenario.is_overlay else 'role'}={len(scenario.sensitivity or scenario.role)}字)"
            )

        if not self._scenarios:
            logger.info("📋 未发现场景文件，所有群使用默认闲聊模式")

    # ---- 查询 ----

    def get(self, name: str) -> Optional[Scenario]:
        """获取场景。空字符串或 None → None（默认闲聊）。
        先用 ID 精确匹配，再用 display name 模糊匹配。"""
        with self._state_lock:
            if not name:
                return None
            if name in self._scenarios:
                return self._scenarios[name]
            # 尝试用 display name 匹配（去掉 emoji 前缀）
            for s in self._scenarios.values():
                if s.display == name:
                    return s
                # "🧠 心理陪伴" → 匹配 "心理陪伴"
                display_no_emoji = s.display.split(" ", 1)[-1] if " " in s.display else s.display
                if display_no_emoji == name:
                    return s
            return None

    # ---- 动态场景检测 ----
    # 关键词列表已移除。判断对话是否暧昧/正经——
    # 这是 LLM 的本职工作，不是关键词词典能做的事。
    # 显式配置的场景（per-user scenario_targets / per-group scenario）仍然生效。

    def detect_dynamic(self, text: str = "", _extra_context: str = "", is_private: bool = False) -> Optional[Scenario]:
        """动态场景检测已禁用——语气判断交给 LLM 的自然理解。
        保留接口兼容性，始终返回 None（使用默认 role_card 基线）。"""
        return None

    def list_all(self) -> list[Scenario]:
        """列出所有已加载的场景"""
        with self._state_lock:
            return list(self._scenarios.values())

    def reload(self) -> None:
        """热重载所有场景定义"""
        with self._state_lock:
            self._scenarios.clear()
            self._load_all()

    @property
    def count(self) -> int:
        with self._state_lock:
            return len(self._scenarios)

    # ---- 私聊场景查找 ----

    def resolve_private_scenario(
        self,
        user_id: str,
        group_configs: dict,
        store=None,
    ) -> Optional[Scenario]:
        """查找私聊时应该使用的场景：取该用户最近互动群的场景。

        Args:
            user_id: 私聊者的 QQ 号
            group_configs: config.yaml 的 groups 字典
            store: Store 实例（用于查最近互动群）

        Returns:
            Scenario 或 None（默认闲聊）
        """
        # 尝试从数据库查该用户最近发言的群
        last_group = None
        if store:
            last_group = store.find_last_group(user_id)

        # 如果数据库有记录，用那个群的场景
        if last_group and last_group in group_configs:
            scenario_name = group_configs[last_group].get("scenario", "")
            if scenario_name:
                return self.get(scenario_name)

        # 没有真实发言群可验证成员关系时，不能套用任意群的场景。
        return self.detect_dynamic("", "")
