# -*- coding: utf-8 -*-
"""特殊模式包：**配置解析的唯一实现**。

## 为什么单独一个模块

两处都要解析 `scenario_reference.packs`：
  · `agent/handler.py`  在线消息路径（决定这轮挂不挂范本）
  · `tools/desktop_bridge.py` 桌面端「特殊模式」区块（显示与编辑）

如果各写一份，改字段时必然漏一处——漏的那次就是「界面说配了、实际不生效」。
所以解析只在这里，两边 import（教训 #38：同一个量只许一个换算点）。

## 形状

    {"id", "scenario", "dir", "label", "budget", "require_marker", "ttl_seconds",
     "allow_groups"}

- `scenario`：哪个 `scenarios/*.yaml` 的 `name` 触发这个包。**中立**——
  架构不判断「哪类场景该配范本」。
- `dir`：范本目录（数字前缀目录 = 注入槽位）
- `require_marker`：要不要 `[进入色色]` 才激活。`False` = 挂载即生效。
- `ttl_seconds`：`0` = **永久**（默认）；`>0` = 激活后多少秒过期。
- `allow_groups`：群聊是否也整篇注入（默认 `False`）。群场景是**群级**设置——
  开了对群里所有人生效，所以这是部署者的判断。**2026-10-26 它被保存链路丢过一次**，
  根因是保存侧另有一份手工白名单；现在两条链路共用 `coerce_pack`。

## 旧格式

`scenario_reference.trigger_scenario` + `.label` → 合成一个包。
主人的配置从单包时代过来的，不能因为升级就静默失效。
"""
from __future__ import annotations

from .intimate_reference import PACKAGE_DIRNAME
from .protocols import INTIMATE_REFERENCE_LABEL

#: 包的默认字段（配置没写就用这些）
PACK_DEFAULTS: dict = {
    "id": "main",
    "label": "场景范本",
    "budget": 13500,
    "require_marker": True,
    # 0 = 永久。2026-10-26 主人定：「TTL 也在前端改，默认永久」。
    "ttl_seconds": 0,
    # 群聊是否也注入（默认 False = 保持改之前的 fail-closed 行为）。
    # 群场景是**群级**设置：开了就对群里所有人生效，不是逐个成员同意的事。
    # 所以这是部署者的判断，不是架构的判断——架构只提供开关。
    "allow_groups": False,
}

#: 旧格式（单触发器）缺省时用的场景名
DEFAULT_TRIGGER = "seductive"


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_field(default, value):
    """按**默认值的类型**把一份脏输入规整好。

    为什么按类型而不是逐个字段手写：以后给包加字段，只要在 `PACK_DEFAULTS` 里
    写上默认值，保存/读取两条链路就自动拥有类型规整——不用再改第二个地方。
    2026-10-26 的 `allow_groups` 事故正是「加字段要手工改两处」的直接后果。
    """
    if isinstance(default, bool):          # 必须排在 int 之前（bool 是 int 子类）
        return bool(value)
    if isinstance(default, int):
        return _int_or(value, default)
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        text = str(value if value is not None else "").strip()
        return text or default
    return default if value is None else value


def coerce_pack(item, index: int = 0) -> dict | None:
    """把一份（可能不完整、带脏值的）包输入规整成完整包；不是包就返回 None。

    **这是包字段表的唯一实现**：读取链路（`normalize_packs`，消息路径和桌面端
    显示都走它）和保存链路（`tools/desktop_bridge.py::api_scenario_packs_save`）
    都必须调它。

    历史（别再犯）：保存链路原本**自己手写了一份 8 键白名单**。加 `allow_groups`
    时只改了读取侧，于是每次保存那个键都静默消失——`handler` 的守卫把缺失当
    `False`，「群聊注入」被悄悄关掉，而界面上还显示着开（配置备份里能查到
    `allow_groups: true` 在之后的备份里没了）。现在字段集恒等于 `PACK_DEFAULTS`，
    「新增字段忘了加白名单」在结构上不可能再发生。
    """
    if not isinstance(item, dict):
        return None
    scenario = str(item.get("scenario") or "").strip()
    if not scenario:
        return None

    pack: dict = {"scenario": scenario}
    for key, default in PACK_DEFAULTS.items():
        raw = item.get(key)
        pack[key] = _coerce_field(default, default if raw is None else raw)

    # id：缺失用默认值；显式空串退化成 pack{i+1}，免得多个包撞名
    raw_id = item.get("id")
    if raw_id is not None:
        pack["id"] = str(raw_id).strip() or "pack%d" % (index + 1)

    pack["dir"] = str(item.get("dir") or "").strip() or ("knowledge/%s" % PACKAGE_DIRNAME)

    # 值域规则（类型规则已经由 _coerce_field 覆盖）
    if pack["budget"] <= 0:
        pack["budget"] = PACK_DEFAULTS["budget"]
    if pack["ttl_seconds"] < 0:
        pack["ttl_seconds"] = 0
    return pack


def normalize_packs(config) -> list[dict]:
    """把 config 里的 `scenario_reference` 归一成包列表（永远返回至少一个包）。

    单包时代的配置也能读；写坏了就退回默认包——**绝不抛异常**：
    这个函数在消息路径上，一个配置手误不该让她不说话。
    """
    raw = (config or {}).get("scenario_reference") or {}
    if not isinstance(raw, dict):
        raw = {}

    packs: list[dict] = []
    items = raw.get("packs")
    if isinstance(items, list):
        for i, item in enumerate(items):
            pack = coerce_pack(item, i)
            if pack is not None:
                packs.append(pack)
    if packs:
        return packs

    # ── 旧格式兜底：trigger_scenario + label → 一个包 ──────────────────
    trigger = str(raw.get("trigger_scenario") or "").strip() or DEFAULT_TRIGGER
    label = str(raw.get("label") or "").strip() or INTIMATE_REFERENCE_LABEL
    return [{
        **PACK_DEFAULTS,
        "id": "main",
        "scenario": trigger,
        "dir": "knowledge/%s" % PACKAGE_DIRNAME,
        "label": label,
        "require_marker": True,
        "ttl_seconds": 0,
    }]


def pack_for(packs: list[dict], scenario_name: str) -> dict | None:
    """当前场景命中哪个包（没有就 None = 这个场景不是特殊模式）。"""
    name = str(scenario_name or "").strip()
    if not name:
        return None
    for pack in packs or []:
        if pack.get("scenario") == name:
            return pack
    return None
