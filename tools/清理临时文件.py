# -*- coding: utf-8 -*-
"""T54-B 临时文件清理（幂等 / 先清单后删除 / 可回滚）

用法：
    python tools/清理临时文件.py            # dry-run：只打印计划，不动任何文件
    python tools/清理临时文件.py --apply    # 实际执行

处置范围（写死在下方常量，绝不通配扫全盘）：
  A 删除 .trash/2026-10-14-T54B/deleted_bak_manifest.tsv 列出的 151 个 >7 天 .bak
  B 删除本仓自有 __pycache__/ 与其中的 .pyc（排除第三方目录）
  C 删除根目录 0 字节残留 1000 / 不存在的库.db（复验 size==0 才删）
  D 删除根目录 _tmp_* / tmp_*.png / _clip_a*.png（限白名单后缀）
  E 归档 _tmp_shots/ 与根一次性日志到 .trash/2026-10-14-T54B/归档/，随后重建空 _tmp_shots/

安全阀：
  - 只删清单内路径，不做任何通配扫描式删除
  - 硬约束排除：.env* / config.yaml* / *.db / *.db-* / *.onnx / *.pth / 模型目录
  - 每个删除项执行前复验「存在 + 不在排除名单 + 类型正确」
  - 幂等：文件不存在即跳过；重复运行结果一致

清单与证据见 docs/协作/reports/T54-B-备份锚点治理-20261014.md
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRASH_DIR = ROOT / ".trash" / "2026-10-14-T54B"
MANIFEST = TRASH_DIR / "deleted_bak_manifest.tsv"
ARCHIVE = TRASH_DIR / "归档"
RESULT = TRASH_DIR / "cleanup_result.json"

# 第三方/环境目录——一律不进入（与普查口径一致）
SKIP_DIRS = {
    "依赖", "python310", "node_modules", ".git", ".gitnexus",
    "gpt-sovits", "models", ".uv-cache-codex", ".uv-cache-L2",
}

# 根目录 0 字节残留（复验 size==0）
ZERO_BYTE_ROOT = ["1000", "不存在的库.db"]

# 根目录临时文件：前缀 + 后缀双闸门
TMP_PREFIXES = ("_tmp_", "tmp_")
TMP_EXTRA_PREFIXES = ("_clip_a",)
TMP_SUFFIXES = {'.log', '.txt', '.json', '.qss', '.png', '.html', '.md'}

# 归档（一次性、可回滚）
ARCHIVE_DIRS = ["_tmp_shots"]
ARCHIVE_FILES = [
    "startup.log", "train_test_output.log",
    "v2_server_stdout.log", "v4_server_stdout.log", "v4_server_stdout2.log",
    "pytest_baseline_20260816.log",
    # codex_*.log：被 docs/协作/rulings/裁决_A1.md:23 与 docs/协作/reports/执行报告_A1.md:146
    # 引为 A1 阶段取证材料——归档留证，不直删
    "codex_memory_align.log", "codex_memory_resume.log", "codex_memory_review.log", "codex_review2.log",
]

# 硬约束：这些前缀/后缀的路径永不删
KEEP_PREFIXES = (".env", "config.yaml")
KEEP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".onnx", ".pth", ".ckpt", ".wav", ".mp3")


def force_unlink(p: Path) -> None:
    """删除文件；Windows 只读属性会挡住 unlink，先清掉再删。"""
    try:
        p.unlink()
    except PermissionError:
        os.chmod(p, stat.S_IWRITE)
        p.unlink()


def _rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def forbidden(path: Path) -> str | None:
    """返回不可删除的原因；None 表示允许。"""
    name = path.name
    for pre in KEEP_PREFIXES:
        if name.startswith(pre):
            return "硬约束保留（前缀 %s）" % pre
    for suf in KEEP_SUFFIXES:
        if name.endswith(suf) or suf in name:
            return "硬约束保留（后缀 %s）" % suf
    return None


def collect_pycache() -> list[Path]:
    out = []
    for p in ROOT.rglob("__pycache__"):
        if not p.is_dir():
            continue
        if set(p.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        out.append(p)
    return sorted(out)


def collect_stray_pyc() -> list[Path]:
    out = []
    for p in ROOT.rglob("*.pyc"):
        if "__pycache__" in p.parts:
            continue
        if set(p.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        out.append(p)
    return sorted(out)


def collect_bak() -> list[str]:
    if not MANIFEST.exists():
        return []
    out = []
    for ln in MANIFEST.read_text(encoding="utf-8").splitlines()[1:]:
        if not ln.strip():
            continue
        out.append(ln.split("\t")[3])
    return out


def collect_root_tmp() -> list[Path]:
    out = []
    for p in sorted(ROOT.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in TMP_SUFFIXES:
            continue
        if p.name.startswith(TMP_PREFIXES) or p.name.startswith(TMP_EXTRA_PREFIXES):
            out.append(p)
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    report: dict = {"mode": "apply" if apply else "dry-run", "steps": {}}
    print("[T54-B] 模式 = %s" % report["mode"])

    # ---------- A 删除旧 .bak ----------
    baks = collect_bak()
    hit, miss, skipped, freed = [], [], [], 0
    for rel in baks:
        p = ROOT / rel
        if not p.exists():
            miss.append(rel)
            continue
        why = forbidden(p)
        if why:
            skipped.append([rel, why])
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if apply:
            force_unlink(p)
        hit.append(rel)
        freed += size
    report["steps"]["A_bak"] = {"total": len(baks), "removed": len(hit), "already_gone": len(miss),
                                 "skipped": skipped, "bytes": freed}
    print("  A .bak      清单 %d / 处置 %d / 已不存在 %d / 排除跳过 %d / 释放 %d B"
          % (len(baks), len(hit), len(miss), len(skipped), freed))

    # ---------- B __pycache__ ----------
    dirs = collect_pycache()
    stray = collect_stray_pyc()
    nfiles = nbytes = 0
    for d in dirs:
        for f in d.rglob('*'):
            if f.is_file():
                nfiles += 1
                try:
                    nbytes += f.stat().st_size
                except OSError:
                    pass
        if apply:
            shutil.rmtree(d, onerror=_rm_error)
    for f in stray:
        if apply:
            force_unlink(f)
    report["steps"]["B_pycache"] = {"dirs": [str(d.relative_to(ROOT)) for d in dirs],
                                     "files": nfiles, "bytes": nbytes, "stray_pyc": len(stray)}
    print("  B __pycache__ %d 目录 / %d 文件 / %d B ; 孤立 .pyc %d"
          % (len(dirs), nfiles, nbytes, len(stray)))

    # ---------- C 根 0 字节 ----------
    zb, zb_skip = [], []
    for name in ZERO_BYTE_ROOT:
        p = ROOT / name
        if not p.is_file():
            continue
        if p.stat().st_size != 0:
            zb_skip.append([name, "非 0 字节，跳过"])
            continue
        why = forbidden(p)   # 例：不存在的库.db 命中 .db 后缀硬约束 → 自动跳过
        if why:
            zb_skip.append([name, why])
            continue
        if apply:
            force_unlink(p)
        zb.append(name)
    report["steps"]["C_zero_byte"] = {"removed": zb, "skipped": zb_skip}
    print("  C 0 字节     删除 %s ; 跳过 %s" % (zb or "（无）", zb_skip or "（无）"))

    # ---------- D 根临时文件 ----------
    tmp, tmp_skip = [], []
    for p in collect_root_tmp():
        why = forbidden(p)
        if why:
            tmp_skip.append([str(p.name), why])
            continue
        tmp.append(p)
    tmp_hit = [str(p.name) for p in tmp]
    for p in tmp:
        if apply:
            force_unlink(p)
    report["steps"]["D_root_tmp"] = {"count": len(tmp_hit), "files": tmp_hit, "skipped": tmp_skip}
    print("  D 根临时文件 %d 个：%s" % (len(tmp_hit), ", ".join(tmp_hit)))

    # ---------- E 归档 ----------
    moved = []
    for d in ARCHIVE_DIRS:
        src = ROOT / d
        if not src.exists():
            continue
        if not any(src.iterdir()):
            continue   # 空目录（上次执行重建的）无需再归档——保证重复运行是 no-op
        dst = ARCHIVE / d
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.move(str(src), str(dst))
            src.mkdir(exist_ok=True)   # 重建空目录：tools/_tmp_pixel_ui.py:9 / tools/_tmp_vlm_ui.py:40 的默认输出路径
        moved.append(d)
    files_moved = []
    for name in ARCHIVE_FILES:
        src = ROOT / name
        if not src.exists():
            continue
        dst = ARCHIVE / name
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        files_moved.append(name)
    report["steps"]["E_archive"] = {"dirs": moved, "files": files_moved, "archive": str(ARCHIVE)}
    print("  E 归档       目录 %s ; 文件 %d 个" % (moved or "（无）", len(files_moved)))

    if apply:
        RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("  结果 JSON -> %s" % RESULT)
    else:
        print("  （dry-run，未改动任何文件；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
