# -*- coding: utf-8 -*-
"""场景卡改名 → 自动同步 config.yaml 的 scenario_targets。

## 为什么必须做

`agent/scenario.py:138-150` 用 **display 名**匹配场景：

    if s.display == name:                       # 匹配 1
    display_no_emoji = s.display.split(" ", 1)[-1]   # 「❤️ 亲密模式」→「亲密模式」
    if display_no_emoji == name:                # 匹配 2

而 `config.yaml` 里 `scenario_targets: {'2524950083': 亲密模式}` 存的就是那个名字。
**改 display = 所有已挂载的群/人静默失效**——没有报错，场景就是不再生效。

## 为什么用文本级替换而不是 yaml.safe_dump

`config.yaml` 有大量注释（每段都在解释「为什么这么配」）。`yaml.safe_dump` 会把它们
全部丢掉——那等于用一次改名换掉整份配置的说明。所以只在 `scenario_targets:` 这个
块的缩进范围内做逐行替换，文件其余字节一动不动。
"""
from __future__ import annotations

import re
from pathlib import Path

CONFIG = "config.yaml"


def display_key(display: str) -> str:
    """复刻 `scenario.py:149` 的取键规则：去掉 emoji 前缀。

    两处必须一致——这里算错了就会改错别人的档，或者改不动。
    """
    text = (display or "").strip()
    return text.split(" ", 1)[-1] if " " in text else text


def _block_range(lines: list[str]) -> tuple[int, int] | None:
    """返回 `scenario_targets:` 块的 [起, 止) 行号（止于下一个顶层键）。"""
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^scenario_targets\s*:", line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 顶层键（无缩进）→ 块结束
        if not lines[j][:1].isspace():
            end = j
            break
    return start, end


def sync_rename(project_root: Path, old_display: str, new_display: str) -> list[str]:
    """把 `scenario_targets` 里挂 old_display 的条目改挂到 new_display。

    返回受影响的键（QQ 号 / 群号）。改不动时返回空列表——调用方据此提示
    「有 N 个挂载点没改成功，请去群管理页手动确认」。
    """
    old_key = display_key(old_display)
    new_key = display_key(new_display)
    if not old_key or old_key == new_key:
        return []

    path = Path(project_root) / CONFIG
    if not path.is_file():
        return []

    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    span = _block_range(lines)
    if span is None:
        return []

    start, end = span
    affected: list[str] = []
    changed = False
    pat = re.compile(r"^(\s+)('?)([^'#:]+)('?)(\s*:\s*)(.+?)(\s*)$")
    for i in range(start, end):
        m = pat.match(lines[i])
        if not m:
            continue
        indent, q1, key, q2, colon, value, tail = m.groups()
        if value.strip().strip("'\"") != old_key:
            continue
        affected.append(key.strip())
        lines[i] = f"{indent}{q1}{key}{q2}{colon}{value.replace(old_key, new_key)}{tail}"
        changed = True

    if changed:
        path.write_text("\n".join(lines), encoding="utf-8", newline="")

    return affected
