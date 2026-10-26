# -*- coding: utf-8 -*-
"""把 `protocols.py` 里的协议条文导出成 `prompts/*.md`（2026-10-26 第 49 轮）。

## 为什么需要「导出」而不是手写这些 md

`protocols.py` 里那 22 条条文是**默认值本身**，有几百到上千字，含大量
「」、——、`**`、换行。手抄必然走样（本项目手抄清单必腐烂，教训 #35）。
这个脚本用 AST 从源码里读**字面量**，逐字节写到 md——
**导出物与源码默认值保证一致**，而且有闸门盯着（`tests/test_prompt_overrides.py`）。

## 用法

    python tools/export_prompts.py            # 导出缺失的（已存在的跳过）
    python tools/export_prompts.py --force    # 覆盖已存在的（会把你的修改冲掉，慎用）
    python tools/export_prompts.py --list      # 只看状态，不写文件

## 为什么「已存在的跳过」

`prompts/*.md` 是**用户的编辑成果**。导出脚本的职责是「补齐缺失」，
不是「还原出厂设置」——后者是界面上的「恢复默认」按钮（它只删文件，不动别的）。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.prompt_overrides import OVERRIDABLE, PROMPT_DIR, read_override  # noqa: E402

PROTOCOLS = ROOT / "agent" / "protocols.py"


def load_defaults() -> dict[str, str]:
    """用 AST 从 `protocols.py` 读出内置默认值（`{常量名: 文本}`）。

    **不能 import**：import 之后 `protocols.X` 已经是覆盖后的值了，
    拿它当默认值会把用户的修改固化进「默认」，删 md 也回不去。
    """
    tree = ast.parse(PROTOCOLS.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        try:
            val = ast.literal_eval(node.value)
        except Exception:
            continue
        if isinstance(val, str) and val.strip():
            out[tgt.id] = val
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出协议条文到 prompts/*.md")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的（会冲掉你的修改）")
    ap.add_argument("--list", action="store_true", help="只看状态")
    args = ap.parse_args(argv)

    defaults = load_defaults()
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)

    missing_const = [c for c in OVERRIDABLE.values() if c not in defaults]
    if missing_const:
        print("⚠ 这些常量在 protocols.py 里找不到（白名单过期了？）：", missing_const)
        return 2

    wrote = skipped = 0
    for name, const in OVERRIDABLE.items():
        path = PROMPT_DIR / f"{name}.md"
        default = defaults[const]
        active = read_override(name)
        if args.list:
            state = ("已覆盖 %d 字" % len(active)) if active else "用默认 %d 字" % len(default)
            print("  %-24s %-28s %s" % (name, const, state))
            continue
        if path.exists() and not args.force:
            skipped += 1
            continue
        # 2026-10-26：文件头承载「协议名/分组/说明/来源」——主人要求
        # 「每条协议的标签和注释都不要明文，和场景卡一样在文档内可改」。
        # 这里用 safe_dump 生成，避免手拼 YAML 的引号陷阱
        # （第一次手拼时 `desc: `~` 标记的含义…` 里的反引号直接 ScannerError）。
        import yaml as _yaml
        import sys as _sys
        _sys.path.insert(0, str(PROMPT_DIR.parent))
        from agent import prompt_overrides as _po
        group, desc = _po.PROMPT_META.get(name, ("", ""))
        group_title = next((t for k, t, _ in _po.PROMPT_GROUPS if k == group), "")
        head = _yaml.safe_dump({
            "name": name,
            "group": group,
            "group_title": group_title,
            "desc": desc,
            "origin": _po.PROMPT_ORIGIN.get(name, ""),
        }, allow_unicode=True, sort_keys=False).rstrip("\n")
        path.write_text(f"---\n{head}\n---\n\n{default}", encoding="utf-8", newline="")
        wrote += 1

    if not args.list:
        print("导出完成：写入 %d 个、跳过 %d 个（已存在）→ %s"
              % (wrote, skipped, PROMPT_DIR))
        print("⚠ 这些是**当前生效的文本**。改完重启糖糖生效；删掉某个文件 = 回落到源码默认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
