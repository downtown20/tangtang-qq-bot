"""桌面端数字输入框契约（2026-10-27 立）。

## 为什么需要这条闸门

主人 10-27 反馈：「两个饥渴度…输入框不允许输入 0.0 几的数，只允许输入 0.几的数，
你注意核对」。核对结果是一个**双层 bug**，缺一层都修不好：

1. **`<input type="number">` 的 DOM 根本存不下小数/负数的中间态**。实测（DevTools
   里直接设 value 再读回）：

       "0"  → "0"     "0."  → ""      "0.0" → "0.0"   "0.08" → "0.08"
       ".5" → ".5"    "-"   → ""

   也就是说用户敲「0」再敲「.」的那一刻，`input.value` 返回**空串**。

2. **受控渲染把空串又写回配置**。旧实现 `value={String(value)}` +
   `onChange={coerce}` 拿到空串就走 `coerce("") = 0`，然后把 value 重置成 "0"。
   于是想输默认的 `0.08` 时，用户看到的是：

       打「0」→ 0 ／ 打「.」→ **小数点消失** ／ 打「0」→ 0 ／ 打「8」→ 8

   最终只能得到 `0.8` 或 `8`，永远输不进 `0.08`。

后果不只是「不方便」：`config.yaml` 里的 `behavior.interjection_thirst` 实测就是
`0`（默认值应为 `0.08`）——**想调低频率，结果被静默写成了「完全关掉」**。

## 闸门做两件事

1. `desktop/src/**/*.tsx` 里不允许出现**真实的** `type="number"` 属性
   （注释里的说明文字不算）；
2. 共用的 `NumberField` 必须保留「本地 draft + 失焦兜底提交」结构
   ——防止有人出于「简化」把它改回直连 value。

两件事都配了变异对照（`变异/数字输入.json`）：把 `type="text"` 改回
`type="number"`、或删掉 onBlur 兜底，本文件必须红。**测试自己能红才算闸门。**
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "desktop" / "src"
SETTINGS_FIELDS = SRC / "components" / "SettingsFields.tsx"

# 真实属性形态（含 JSX 表达式写法）；注释里的反引号说明不匹配
NUMBER_ATTR = re.compile(r"""\btype\s*=\s*(?:"number"|'number'|\{\s*['"]number['"]\s*\})""")

# 注释行的判定：TS 块注释续行 / 单行注释 / JSX 注释
COMMENT_PREFIXES = ("*", "//", "{/*", "*/")


def _tsx_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.tsx") if p.is_file())


def test_src_has_tsx_files() -> None:
    """阳性对照的底座：目录扫不到文件时，下面的断言会「全绿通过」但什么也没查。"""
    files = _tsx_files()
    assert len(files) >= 10, f"只扫到 {len(files)} 个 tsx，路径可能错了：{SRC}"
    assert SETTINGS_FIELDS.is_file(), f"找不到共用数字输入组件：{SETTINGS_FIELDS}"


def test_no_number_type_inputs() -> None:
    """不允许 `<input type="number">` —— 它对小数/负数中间态无解（见模块 docstring）。"""
    offenders: list[str] = []
    for path in _tsx_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not NUMBER_ATTR.search(line):
                continue
            if line.strip().startswith(COMMENT_PREFIXES):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}  {line.strip()}")
    assert not offenders, (
        "发现 type=\"number\" 输入框（DOM 存不下 `0.` / `-`，会让小数和负数输不进去）。"
        "请改用 components/SettingsFields.tsx 导出的 NumberField：\n  " + "\n  ".join(offenders)
    )


def test_number_field_keeps_draft_and_blur_commit() -> None:
    """NumberField 必须是「text + 本地 draft + 失焦兜底」，不能简化回直连 value。"""
    src = SETTINGS_FIELDS.read_text(encoding="utf-8")
    assert "export function NumberField" in src, "NumberField 没导出（其他页面要复用它）"

    start = src.index("export function NumberField")
    body = src[start:]
    # 只取这个组件的范围：到下一个顶层 export/function 之前
    tail = re.search(r"\n(?:export )?(?:function|const) ", body[1:])
    if tail:
        body = body[: tail.start() + 1]

    assert 'type="text"' in body, (
        "NumberField 必须用 type=\"text\"——type=\"number\" 的 DOM 会把 `0.` 存成空串，"
        "这正是 0.08 输不进去的根因"
    )
    assert "inputMode" in body, "NumberField 缺 inputMode（移动端会弹字母键盘）"
    assert "useState" in body, "NumberField 缺本地 draft state"
    # 用 `onBlur=` 而不是 `onBlur`：后者会被 `onBlurX=` 这类改名蒙过去
    assert "onBlur=" in body, "NumberField 缺失焦兜底提交（清空/只按回车时会漏存）"
    assert "COMPLETE_NUMBER" in body, "NumberField 缺「完整数字才提交」的判据"


def test_thirst_fields_are_float_type() -> None:
    """两个饥渴度字段必须是 float —— 类型写成 int 会先被 UI 取整，再谈不上精度。"""
    import json

    schema = json.loads((SRC / "lib" / "settingsSchema.json").read_text(encoding="utf-8"))
    flat: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            path, kind = node.get("path"), node.get("type")
            if isinstance(path, str) and isinstance(kind, str):
                flat[path] = kind
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    for key in ("behavior.interjection_thirst", "behavior.private_interjection_thirst"):
        assert flat.get(key) == "float", f"{key} 的类型是 {flat.get(key)!r}，必须是 float"
