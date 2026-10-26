# -*- coding: utf-8 -*-
"""讲解模式（右上角「讲解」开关）—— 必须真的控制**全站**解释性文字。

## 为什么有这份测试

主人 2026-10-26 原话：「右上角的『讲解』按钮需要控制全局的注释显示，
目前只控制『她怎么想』的注释。」

查证据发现是**两个错叠在一起**：

1. `global.css` 只写 `html[data-explain='off'] .explain-only`
   （单类选择器），而 `PromptLedger.tsx` 里写的是
   `className="pl-note pl-explain-only"` —— 前缀命名**永远匹配不到**。
   开关点了它不动，而且是**静默**的：没有任何报错，用户只会觉得
   「这按钮只管一部分」。这正是教训 #53 那一族（失效但不报警）。
2. 只有 4 个文件用了 `explain-only`（回放/状态/账本），资产页、调参页、
   设置页的说明文字一个都没标 —— 那些恰恰是主人天天看的页面。

所以闸门盯两件事：**选择器够宽** + **主要页面确实标了**。
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop" / "src"


def _read(rel: str) -> str:
    p = DESKTOP / rel
    assert p.is_file(), f"找不到 {rel}"
    return p.read_text(encoding="utf-8")


def test_css_selector_covers_prefixed_class_names():
    """CSS 必须用**属性选择器**，不能只写 `.explain-only`。

    单类选择器下 `pl-explain-only` / `fg-explain-only` 这种前缀写法匹配不到，
    而它看起来「写了啊」——静默失效里最贵的一种。
    """
    css = _read("styles/global.css")
    assert "[class*='explain-only']" in css or '[class*="explain-only"]' in css, (
        "global.css 又退回单类选择器了：前缀命名为 xx-explain-only 的块会静默不受控")
    # 关讲解时必须是 !important，否则以后有人写更具体的选择器就把隐藏顶掉了
    marker = "[class*='explain-only']" if "[class*='explain-only']" in css else '[class*="explain-only"]'
    tail = css[css.index(marker):css.index(marker) + 300]
    assert "display: none" in tail and "!important" in tail, tail


def test_prefixed_class_names_exist_and_are_now_controlled():
    """确实存在前缀写法（说明这条闸门不是防御不存在的风险）。"""
    ledger = _read("components/PromptLedger.tsx")
    assert "pl-explain-only" in ledger, (
        "PromptLedger 里的前缀写法没了？如果改名成 explain-only 请同步删掉这条断言")


@pytest.mark.parametrize("rel,needle,why", [
    ("pages/Assets.tsx", "assets-group-desc explain-only",
     "资产页的分组说明（三种进入方式）"),
    ("pages/Assets.tsx", "assets-fdesc explain-only",
     "资产页里每篇协议的一句话作用"),
    ("pages/Assets.tsx", "scenario_channel.trigger",
     "场景触发通道的机制说明"),
    ("pages/Tuning.tsx", "tuning-desc explain-only",
     "调参页的参数作用"),
    ("pages/settings/ModelSettings.tsx", "settings-desc explain-only",
     "模型设置页的整页说明"),
    ("components/SettingsFields.tsx", "dsl-desc explain-only",
     "设置页所有字段的说明（共用组件，一改全覆盖）"),
    ("components/SpecialMode.tsx", "sm-rules explain-only",
     "特殊模式的「生效要同时满足」三条"),
    ("pages/Dashboard.tsx", "row-desc explain-only",
     "概览页每个入口的说明"),
    ("pages/Console.tsx", "cs-gate-desc explain-only",
     "会客厅的入口说明"),
    ("pages/GroupManage.tsx", "gm-desc explain-only",
     "群管理「场景目标」的优先级说明"),
    ("pages/MemoryGraph.tsx", "mg-legend explain-only",
     "记忆图谱图例"),
    ("pages/MemoryProfile.tsx", "mp-tl-legend explain-only",
     "记忆档案时间线图例"),
])
def test_key_explanations_are_controlled(rel: str, needle: str, why: str):
    """主要页面上的解释文字必须带 `explain-only`。

    这里用**源码断言**而不是渲染测试：这些块散在十几个组件里，
    渲染测试要每个都搭一遍 DOM，代价远大于收益；而这里的错误形状是
    「忘了加类」，源码断言正好直接盯住它。
    """
    assert needle in _read(rel), f"{rel} 里的「{why}」没有标 explain-only"


# ── 反向纪律：这些**不许**被藏 ────────────────────────────────

@pytest.mark.parametrize("rel,needle,why", [
    ("pages/Assets.tsx", "assets-apply", "改完怎么生效（apply_hint）——藏了就会白改"),
    ("components/SpecialMode.tsx", "sm-warn", "静默失效的警告——藏了等于藏 bug"),
    ("components/TurnReplayFlow.tsx", "fg-note", "异常站的原因"),
    ("pages/Tuning.tsx", "tuning-err", "参数越界的报错"),
    ("pages/Assets.tsx", "assets-err", "清单读失败的报错"),
])
def test_diagnostics_are_never_hidden(rel: str, needle: str, why: str):
    """错误、警告、操作提示**永远显示**——否则讲解开关就成了藏 bug 的工具。

    这条是纪律的反向锚：以后有人「顺手」把 explain-only 加到这类元素上，
    这里会红。允许它同时出现在别处（例如 apply_hint 的文案里提到 err），
    所以只断言「不是以 explain-only 结尾的那个类」。
    """
    text = _read(rel)
    assert needle in text, f"{rel} 里找不到 {needle}（{why}）"
    bad = 'className="' + needle + ' explain-only"'
    assert bad not in text, f"{rel} 的 {needle}（{why}）被藏起来了——它必须永远显示"
