#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T54 — Syncthing 冲突副本清理（幂等、可重跑、单个失败不中断）

背景
----
本仓由 Syncthing 双机同步（memory_sync / SnowLuma / tasks / .trash 等）。历史冲突累积出
64 个 '*.sync-conflict-*' 文件，共 1787.12 MiB。'.stignore' 里已有 '*.sync-conflict-*'
规则（注释写着「已有冲突文件不再扩散」），所以这批是**历史遗留、不会再增长**。

用法
----
    python tools/清理冲突副本.py                      # 预演（默认；只列不删）
    python tools/清理冲突副本.py --apply              # 真正删除
    python tools/清理冲突副本.py --manifest 路径.md    # 只写 Markdown 清单（不删除）

删除边界（T54 硬约束，不可绕过）
------------------------------
本脚本只删「文件名含 '.sync-conflict-' 的常规文件」，并额外把两类挡在门外：

1. '.db' / '.db-wal' / '.db-shm' —— 硬约束原文「数据库文件一个都不许删、不许移 ——
   那是糖糖的记忆，删了不可逆」。命中的冲突库（含 memory_sync 里那个 802 MB 的）只盘点，
   转主人拍板。
2. '.py' —— 硬约束原文「任何 .py 源码（除你自己新建的 tools/清理冲突副本.py）绝对不要动」。

被挡住的文件打印 'HOLD' 行并在末尾汇总，可原样搬进「需主人拍板」清单。确实要删它们时，
必须**同时**给 --allow-db / --allow-py 和 --owner-approved（双重闸门，防手滑）。

幂等性
------
可重复运行：第二次跑应报「待删 0」。删除失败只记录不抛异常，最后汇总「失败 n」。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

MARK = ".sync-conflict-"
DB_SUFFIXES = (".db", ".db-wal", ".db-shm")
ROOT = Path(__file__).resolve().parent.parent
BQ = chr(96)  # 反引号——Markdown 清单生成用，避免在源码里写字面量


def _u8(stream):
    """Windows 控制台默认 GBK，中文路径/摘要会炸——尽量切 UTF-8。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def human(n) -> str:
    return "%.2f MiB" % (n / 1048576.0)


def rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def find_candidates(root: Path):
    """返回 (候选文件列表, 扫描异常列表)。不跟随符号链接。"""
    found, broken = [], []
    for p in sorted(root.rglob("*")):
        try:
            if p.is_symlink():
                continue
            if not p.is_file():
                continue
            if MARK not in p.name:
                continue
            found.append(p)
        except OSError as exc:            # 权限 / 路径过长 / 被占用
            broken.append((p, str(exc)))
    return found, broken


def classify(p: Path) -> str:
    """'db' | 'py' | 'ok'"""
    name = p.name.lower()
    if name.endswith(DB_SUFFIXES):
        return "db"
    if name.endswith(".py"):
        return "py"
    return "ok"


def build_manifest(rows, path: Path, mode: str) -> None:
    import datetime as _dt

    def ts(t):
        return _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")

    label = {"ok": "可清（零风险）",
             "db": "**不可删（数据库）→ 主人拍板**",
             "py": "**不可删（.py 源码）→ 主人拍板**"}
    q = BQ
    lines = [
        "# T54 · Syncthing 冲突副本清理清单（2026-10-14）",
        "",
        "扫描根：" + q + ROOT.as_posix() + q,
        "判定规则：文件名含 " + q + MARK + q + " 的常规文件。",
        "本次运行模式：" + q + mode + q,
        "",
        "| # | 相对路径 | 字节数 | MiB | 最后修改时间 | 处置 |",
        "|---|----------|-------:|----:|--------------|------|",
    ]
    total = 0
    for i, (p, size, mt, kind) in enumerate(rows, 1):
        total += size
        lines.append("| %d | %s%s%s | %d | %.2f | %s | %s |"
                     % (i, q, rel(p), q, size, size / 1048576.0, ts(mt), label[kind]))
    n_ok = sum(1 for r in rows if r[3] == "ok")
    n_db = sum(1 for r in rows if r[3] == "db")
    n_py = sum(1 for r in rows if r[3] == "py")
    lines += [
        "",
        "**合计 %d 条 / %d 字节 / %s** —— 其中可清 %d 条、数据库类 %d 条、.py 类 %d 条。"
        % (len(rows), total, human(total), n_ok, n_db, n_py),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    _u8(sys.stdout)
    _u8(sys.stderr)

    ap = argparse.ArgumentParser(description="T54 Syncthing 冲突副本清理（默认预演）")
    ap.add_argument("--apply", action="store_true", help="真正删除；缺省只预演")
    ap.add_argument("--manifest", metavar="PATH", help="把清单写成 Markdown（不删除）")
    ap.add_argument("--root", metavar="DIR", help="覆盖扫描根（默认仓库根）")
    ap.add_argument("--allow-db", action="store_true",
                    help="解除 .db/.db-wal/.db-shm 保护（需配合 --owner-approved）")
    ap.add_argument("--allow-py", action="store_true",
                    help="解除 .py 保护（需配合 --owner-approved）")
    ap.add_argument("--owner-approved", action="store_true",
                    help="主人已就解除保护拍板")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else ROOT
    if not root.is_dir():
        print("扫描根不存在：" + str(root))
        return 2

    candidates, broken = find_candidates(root)
    rows = []
    for p in candidates:
        try:
            st = p.stat()
        except OSError as exc:
            broken.append((p, str(exc)))
            continue
        rows.append((p, st.st_size, st.st_mtime, classify(p)))

    n_db = sum(1 for r in rows if r[3] == "db")
    n_py = sum(1 for r in rows if r[3] == "py")
    skip_db = bool(n_db) and not (args.allow_db and args.owner_approved)
    skip_py = bool(n_py) and not (args.allow_py and args.owner_approved)
    targets = [r for r in rows
               if (r[3] == "ok")
               or (r[3] == "db" and not skip_db)
               or (r[3] == "py" and not skip_py)]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("=" * 78)
    print("T54 清理冲突副本  根=%s  模式=%s" % (root.as_posix(), mode))
    print("=" * 78)

    free_before = shutil.disk_usage(root).free
    print("候选（文件名含 %s）：%d 条 / %s"
          % (MARK, len(rows), human(sum(r[1] for r in rows))))
    print("  其中本次会删 %d 条 / %s"
          % (len(targets), human(sum(r[1] for r in targets))))
    print("  HOLD 数据库类 %d 条 / %s"
          % (n_db, human(sum(r[1] for r in rows if r[3] == 'db'))))
    print("  HOLD .py 类  %d 条 / %s"
          % (n_py, human(sum(r[1] for r in rows if r[3] == 'py'))))
    if broken:
        print("  扫描异常 %d 条" % len(broken))
    print("-" * 78)

    if args.manifest:
        build_manifest(rows, Path(args.manifest), mode)
        print("清单已写入：%s" % Path(args.manifest).as_posix())
        print("-" * 78)

    deleted, failed, freed = [], [], 0
    for p, size, mt, kind in rows:
        if kind == "db" and skip_db:
            print("HOLD  db  %s  (%s)" % (rel(p), human(size)))
            continue
        if kind == "py" and skip_py:
            print("HOLD  py  %s  (%s)" % (rel(p), human(size)))
            continue
        if not args.apply:
            print("PLAN  %s  (%s)" % (rel(p), human(size)))
            continue
        try:
            if MARK not in p.name:            # 最后一道自检
                raise RuntimeError("文件名不含标记，拒绝删除")
            os.remove(p)
            deleted.append((p, size))
            freed += size
            print("DEL   %s  (%s)" % (rel(p), human(size)))
        except OSError as exc:
            failed.append((p, str(exc)))
            print("FAIL  %s  (%s)" % (rel(p), exc))

    free_after = shutil.disk_usage(root).free
    print("-" * 78)
    if args.apply:
        print("实际删除 %d 条 / %s" % (len(deleted), human(freed)))
        print("失败     %d 条" % len(failed))
        for p, why in failed:
            print("   FAIL %s -> %s" % (rel(p), why))
    else:
        print("（预演，未改动任何文件；加 --apply 才真删）")
    print("保留待拍板：数据库类 %d 条 / %s ；.py 类 %d 条 / %s"
          % (n_db, human(sum(r[1] for r in rows if r[3] == 'db')),
             n_py, human(sum(r[1] for r in rows if r[3] == 'py'))))
    print("磁盘空闲（%s）：%.2f GiB -> %.2f GiB"
          % (root.anchor or "?", free_before / 1073741824.0, free_after / 1073741824.0))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
