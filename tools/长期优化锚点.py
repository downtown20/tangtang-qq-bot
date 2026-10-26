"""长期优化 · 回滚锚点管理 🪝

本仓不是 git 仓库（提交仓在 D:\小糖糖-发布），所以"改动前的可回滚快照"只能靠锚点文件。
本工具把这件事规范化：打锚点 / 列锚点 / 逐字节对比 / 回滚。

用法：
    python tools/长期优化锚点.py create T1                       # 给关键文件打锚点 .bak-<日期>-T1
    python tools/长期优化锚点.py create T1 --files agent/handler.py
    python tools/长期优化锚点.py list                            # 列出所有锚点
    python tools/长期优化锚点.py diff T1                         # 逐字节对比当前 vs 锚点
    python tools/长期优化锚点.py restore T1 --files agent/handler.py   # 从锚点回滚（先自动备份现状）

设计约束：
    - 锚点**留在原目录**（与文件同级），这样 `git diff --no-index` 也能用
    - 全程 bytes I/O（铁律 E3：read_text/write_text 会把 CRLF 静默转 LF）
    - restore 前先把现状另存为 .bak-<日期>-<tag>-preRestore，绝不静默覆盖
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 默认锚点集：改动最频繁、影响面最大的核心文件
DEFAULT_FILES = [
    "agent/handler.py",
    "agent/reply_pipeline.py",
    "agent/store.py",
    "agent/memory.py",
    "agent/protocols.py",
    "agent/personality.py",
    "agent/handler_commands.py",
    "agent/handler_autonomy.py",
    "main.py",
    "运维/糖糖控制台_qt.py",
    "config.yaml",
]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _anchor_path(rel: str, tag: str) -> Path:
    p = ROOT / rel
    return p.with_name(p.name + f".bak-{tag}")


def cmd_create(tag: str, files: list[str]) -> int:
    stamp = datetime.now().strftime("%Y%m%d")
    full_tag = f"{stamp}-{tag}"
    made = []
    for rel in files:
        src = ROOT / rel
        if not src.exists():
            print(f"  [跳过] {rel} 不存在")
            continue
        dst = _anchor_path(rel, full_tag)
        shutil.copy2(src, dst)          # copy2 保留 mtime，且是字节级复制
        made.append((rel, str(dst.relative_to(ROOT)), _sha(src), src.stat().st_size))
    print(f"[锚点] {full_tag}  共 {len(made)} 个文件")
    for rel, dst, sha, size in made:
        print(f"  {rel}  →  {dst}   {size}B  sha={sha}")
    return 0


def cmd_list() -> int:
    found: dict[str, list[str]] = {}
    for p in sorted(ROOT.glob("*/*.bak-*")) + sorted(ROOT.glob("*.bak-*")):
        name = p.name
        if ".bak-" not in name:
            continue
        tag = name.split(".bak-", 1)[1]
        found.setdefault(tag, []).append(str(p.relative_to(ROOT)))
    if not found:
        print("（无锚点）")
        return 0
    for tag in sorted(found):
        print(f"{tag}  ({len(found[tag])} 个文件)")
        for f in found[tag]:
            print(f"    {f}")
    return 0


def cmd_diff(tag: str) -> int:
    """逐字节对比当前文件 vs 锚点；只用 bytes，不用 read_text"""
    paths = sorted(set(ROOT.glob(f"*/*.bak-{tag}")) | set(ROOT.glob(f"*.bak-{tag}")))
    if not paths:
        print(f"[错误] 找不到锚点 tag={tag}")
        return 1
    for bak in paths:
        orig = ROOT / bak.name.split(".bak-", 1)[0]
        # 若带目录，需要把目录拼回去
        if not orig.exists():
            orig = bak.parent / bak.name.split(".bak-", 1)[0]
        if not orig.exists():
            print(f"  [缺失] 原文件不存在：{orig}")
            continue
        a, b = orig.read_bytes(), bak.read_bytes()
        if a == b:
            print(f"  相同  {orig.relative_to(ROOT)}  ({len(a)}B)")
        else:
            print(f"  ★ 有差异  {orig.relative_to(ROOT)}  当前 {len(a)}B vs 锚点 {len(b)}B")
            print(f"      git diff --no-index -- \"{orig}\" \"{bak}\"")
    return 0


def cmd_restore(tag: str, files: list[str]) -> int:
    stamp = datetime.now().strftime("%Y%m%d")
    for rel in files:
        bak = _anchor_path(rel, tag)
        if not bak.exists():
            print(f"  [跳过] 锚点不存在：{bak}")
            continue
        cur = ROOT / rel
        pre = cur.with_name(cur.name + f".bak-{stamp}-{tag}-preRestore")
        shutil.copy2(cur, pre)                      # 先把现状存下来
        shutil.copy2(bak, cur)                      # 再从锚点恢复（字节级）
        same = _sha(cur) == _sha(bak)
        print(f"  回滚 {rel}  ← {bak.name}   字节一致={same}   现状已存 {pre.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("tag"); c.add_argument("--files", nargs="*")
    sub.add_parser("list")
    d = sub.add_parser("diff"); d.add_argument("tag")
    r = sub.add_parser("restore"); r.add_argument("tag"); r.add_argument("--files", nargs="*", required=True)
    args = ap.parse_args()

    if args.cmd == "create":
        return cmd_create(args.tag, args.files or DEFAULT_FILES)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "diff":
        return cmd_diff(args.tag)
    if args.cmd == "restore":
        return cmd_restore(args.tag, args.files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
