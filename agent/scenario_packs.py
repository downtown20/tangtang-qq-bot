# -*- coding: utf-8 -*-
"""特殊模式包：**配置解析的唯一实现**。

## 为什么单独一个模块

两处都要解析 `scenario_reference.packs`：
  · `agent/handler.py`  在线消息路径（决定这轮挂不挂范本）
  · `tools/desktop_bridge.py` 桌面端「特殊模式」区块（显示与编辑）

如果各写一份，改字段时必然漏一处——漏的那次就是「界面说配了、实际不生效」。
所以解析只在这里，两边 import（教训 #38：同一个量只许一个换算点）。

## 形状

    {"id", "scenario", "dir", "label", "budget", "require_marker", "ttl_seconds"}

- `scenario`：哪个 `scenarios/*.yaml` 的 `name` 触发这个包。**中立**——
  架构不判断「哪类场景该配范本」。
- `dir`：范本目录（数字前缀目录 = 注入槽位）
- `require_marker`：要不要 `[进入色色]` 才激活。`False` = 挂载即生效。
- `ttl_seconds`：`0` = **永久**（默认）；`>0` = 激活后多少秒过期。

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
            if not isinstance(item, dict):
                continue
            scenario = str(item.get("scenario") or "").strip()
            if not scenario:
                continue
            pack = dict(PACK_DEFAULTS)
            pack.update({k: v for k, v in item.items() if v is not None})
            pack["scenario"] = scenario
            pack["id"] = str(pack.get("id") or "pack%d" % (i + 1))
            pack["dir"] = str(pack.get("dir") or "").strip()
            pack["label"] = str(pack.get("label") or "").strip() or PACK_DEFAULTS["label"]
            pack["budget"] = _int_or(pack.get("budget"), PACK_DEFAULTS["budget"])
            if pack["budget"] <= 0:
                pack["budget"] = PACK_DEFAULTS["budget"]
            pack["require_marker"] = bool(pack.get("require_marker", True))
            pack["ttl_seconds"] = max(0, _int_or(pack.get("ttl_seconds"), 0))
            pack["allow_groups"] = bool(pack.get("allow_groups", False))
            if not pack["dir"]:
                pack["dir"] = "knowledge/%s" % PACKAGE_DIRNAME
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
