# -*- coding: utf-8 -*-
"""控制台设置页必须**成对出现**：建了 section 就必须在导航里登记（2026-10-15）。

## 事故事实

`糖糖控制台_qt.py` 里 `_add_section("llm_seductive", ...)` 建了一个「亲密模式
专用模型」设置页，但 `llm_seductive` **既不在 `groups`（导航分组）也不在
`labels`**。而导航按钮是**按 groups 生成**的——于是这一页永远切不到：
控制台里根本看不到它，改配置只能手改 yaml。

它当初是 2026-09-22 为了修「教训 #4：改了 config 没改控制台」才补的，
却漏了登记这一步——**空转近一个月**（教训 #35 同族：文档/界面说了但代码没生效，
而且没人发现，因为它不报错）。

## 本文件守什么

1. 每个 `_add_section(key, ...)` 的 key 都必须出现在 `groups` 的某个分组里；
2. 每个 `_add_section` 的 key 都要有 `labels` 条目（否则导航按钮没有名字）；
3. 阳性对照：解析出来的 section/group 数必须合理（解析器坏掉时不能让断言恒真）。
"""

import ast
from pathlib import Path

import pytest

_CONSOLE = Path(__file__).resolve().parent.parent / "运维" / "糖糖控制台_qt.py"


def _tree():
    return ast.parse(_CONSOLE.read_text(encoding="utf-8"), filename=str(_CONSOLE))


def _add_section_keys(tree):
    """所有 `_add_section("key", ...)` 的第一个参数。"""
    keys = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_add_section"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            keys.append(node.args[0].value)
    return keys


def _nav_groups(tree):
    """`groups = [(标题, [key, ...]), ...]` 里登记的全部 key。"""
    keys = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "groups" for t in node.targets)
                and isinstance(node.value, ast.List)):
            continue
        for item in node.value.elts:
            if not (isinstance(item, ast.Tuple) and len(item.elts) >= 2):
                continue
            names = item.elts[1]
            if isinstance(names, ast.List):
                for elt in names.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        keys.add(elt.value)
    return keys


def _labels(tree):
    """`labels = {...}` 的全部 key。"""
    out = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "labels" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.add(k.value)
    return out


def test_parser_actually_found_the_lists():
    """阳性对照：解析器必须真的读到东西，否则下面两条断言是恒真的假绿。"""
    tree = _tree()
    sections = _add_section_keys(tree)
    groups = _nav_groups(tree)
    labels = _labels(tree)
    assert len(sections) >= 15, f"只解析出 {len(sections)} 个 _add_section —— 解析器多半坏了"
    assert len(groups) >= 15, f"只解析出 {len(groups)} 个导航 key —— 解析器多半坏了"
    assert len(labels) >= 15, f"只解析出 {len(labels)} 个标签 —— 解析器多半坏了"


def test_every_settings_section_is_reachable_from_the_nav():
    """建了 section 就必须在 groups 里登记，否则它是一页永远切不到的死页。"""
    tree = _tree()
    sections = set(_add_section_keys(tree))
    groups = _nav_groups(tree)
    orphan = sorted(sections - groups)
    assert not orphan, (
        "这些设置页建了但没登记进导航 groups —— 用户在控制台里永远切不到：\n  "
        + "\n  ".join(orphan)
        + "\n（_add_section 只负责把页建出来，导航按钮是按 groups 生成的）"
    )


def test_every_nav_entry_has_a_label():
    """导航里的每个 key 都要有中文标签，否则按钮没有名字。"""
    tree = _tree()
    groups = _nav_groups(tree)
    labels = _labels(tree)
    missing = sorted(groups - labels)
    assert not missing, f"这些导航 key 没有 labels 条目：{missing}"
