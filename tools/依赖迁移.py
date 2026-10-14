"""把 C 盘的 Python 依赖搬到项目内，再用目录联接（junction）挂回原位。

背景
────
项目此前把依赖装在 C 盘的 Python 安装目录：

* site-packages  —— 7.5 GB
* 用户级 site-packages —— 4.1 GB（其中 4.0 GB 是 pip 中断留下的死目录）
* torch 的 hub 缓存 —— 1.2 GB

C 盘只剩 4 GB。本工具把它们**整体搬到项目内的 依赖/ 目录**，
然后在原位置建一个 **目录联接（junction）** 指过去。

为什么用联接而不是 venv
────────────────────────
项目大量代码用 sys.executable 直接跑主环境，还有 service_manager 用它启动
GPT-SoVITS；换成 venv 要动代码、要重装 371 个包（fairseq / jieba-fast 还要
MSVC）。联接对进程**完全透明**：sys.path 里的路径字符串一个字都没变，
解释器照旧解析，零代码改动。

安全性
──────
* **只读**搬运：先 robocopy 复制 + **逐项核对文件数与总字节**，核对通过才动原目录。
* 原目录**不是删除，是改名**为 <名字>.C盘备份-<时间戳>。核对失败就中止并保留原样。
* 回滚：--rollback 删掉联接（rmdir 对联接只删链接本身，不动目标），再改名搬回。
* 迁移后请自行确认一切正常，再用 --purge-backup 删掉 C 盘的备份。

用法
────
    python tools/依赖迁移.py --dry-run      查看计划（不动任何东西）
    python tools/依赖迁移.py --apply        执行迁移
    python tools/依赖迁移.py --verify       只做验证
    python tools/依赖迁移.py --rollback     回滚
    python tools/依赖迁移.py --purge-backup 删掉 C 盘备份（确认无误后再用）

⚠ 执行前请先停止所有加载了 torch / 正在使用 site-packages 的进程
   （bot 主程序、GPT-SoVITS 服务）。带 .pyd/.dll 的进程会锁住文件，
   robocopy 仍能读，但后续核对与改名会被拒。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from 依赖位置 import get_deps_root, ensure_deps_root, PROJECT_ROOT  # type: ignore
except Exception:  # pragma: no cover - 独立运行时兜底
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    _DEFAULT = PROJECT_ROOT / "依赖"

    def get_deps_root() -> Path:  # type: ignore
        return _DEFAULT

    def ensure_deps_root() -> Path:  # type: ignore
        _DEFAULT.mkdir(parents=True, exist_ok=True)
        return _DEFAULT


PY_PREFIX = Path(sys.base_prefix)
USER_SITE = Path(os.environ.get("APPDATA", str(Path.home()))) / "Python" / "Python310" / "site-packages"
TORCH_HOME = Path.home() / ".cache" / "torch"


@dataclass
class Item:
    """一个待迁移的目录。"""

    key: str
    label: str
    source: Path
    target_rel: str
    must_exist: bool = True

    @property
    def target(self) -> Path:
        return get_deps_root() / self.target_rel

    @property
    def backup(self) -> Path:
        return self.source.with_name(self.source.name + ".C盘备份-__TS__")


def _items() -> list[Item]:
    return [
        Item("site-packages", "主 site-packages", PY_PREFIX / "lib" / "site-packages", "site-packages"),
        Item("user-site", "用户级 site-packages", USER_SITE, "user-site-packages", must_exist=False),
        Item("torch-home", "torch 模型缓存", TORCH_HOME, os.path.join("cache", "torch"), must_exist=False),
    ]


# ── 目录工具 ──────────────────────────────────────────────────────────
def _is_junction(p: Path) -> bool:
    """目录联接 / 符号链接判定（Python 3.10 没有 is_junction，走 os.path.islink + realpath）。"""
    if not p.exists():
        return False
    try:
        if os.path.islink(p):
            return True
        return p.is_dir() and Path(os.path.realpath(p)) != p
    except OSError:
        return False


def _junction_target(p: Path) -> str:
    try:
        return os.path.realpath(p)
    except OSError:
        return "?"


def _stat_tree(p: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)。目录联接不跟随(用 os.walk 默认不跟进 symlink 目录? 需显式控制)。"""
    files = 0
    size = 0
    for root, dirs, names in os.walk(p):
        # 不跟进子目录里的联接/符号链接，避免无限递归
        dirs[:] = [d for d in dirs if not _is_junction(Path(root) / d)]
        for n in names:
            fp = Path(root) / n
            try:
                if fp.is_symlink():
                    continue
                size += fp.stat().st_size
                files += 1
            except OSError:
                pass
    return files, size


def _mb(b: int) -> str:
    return f"{b / 2**20:,.1f} MB"


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ── 各动作 ────────────────────────────────────────────────────────────
def _copy(src: Path, dst: Path) -> bool:
    """robocopy 复制。返回是否成功（robocopy 0-7 都算成功）。"""
    dst.mkdir(parents=True, exist_ok=True)
    rc, out = _run([
        "robocopy", str(src), str(dst), "/E", "/COPY:DAT", "/DCOPY:DAT",
        "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
    ])
    if rc > 7:
        print(f"    [!] robocopy 失败 rc={rc}")
        print("    " + out.strip()[-800:].replace("\n", "\n    "))
        return False
    return True


def _make_junction(link: Path, target: Path) -> bool:
    rc, out = _run(["cmd", "/c", "mklink", "/J", str(link), str(target)])
    if rc != 0:
        print(f"    [!] mklink 失败 rc={rc}: {out.strip()[:300]}")
        return False
    return True


def _remove_junction(link: Path) -> bool:
    """只删联接本身（rmdir 对联接不会递归到目标）。"""
    if not _is_junction(link):
        print(f"    [!] {link} 不是联接，拒绝删除")
        return False
    rc, out = _run(["cmd", "/c", "rmdir", str(link)])
    if rc != 0:
        print(f"    [!] rmdir 失败 rc={rc}: {out.strip()[:200]}")
        return False
    return True


def _backup_name(src: Path, ts: str) -> Path:
    return src.with_name(f"{src.name}.C盘备份-{ts}")


def _find_backup(src: Path) -> Path | None:
    parent = src.parent
    cands = sorted(parent.glob(f"{src.name}.C盘备份-*"))
    return cands[-1] if cands else None


# ── 主流程 ────────────────────────────────────────────────────────────
def cmd_dry_run() -> int:
    print("=" * 78)
    print("依赖迁移 · 计划（dry-run，不会动任何文件）")
    print("=" * 78)
    print(f"项目根   : {PROJECT_ROOT}")
    print(f"依赖根   : {get_deps_root()}")
    print()
    total = 0
    for it in _items():
        exists = it.source.exists()
        state = "联接" if _is_junction(it.source) else ("目录" if exists else "不存在")
        f_n, b_n = _stat_tree(it.source) if exists and state == "目录" else (0, 0)
        total += b_n
        print(f"[{it.key}] {it.label}")
        print(f"    源   : {it.source}  ({state})")
        print(f"    目标 : {it.target}")
        if exists and state == "目录":
            print(f"    体量 : {f_n:,} 文件 / {_mb(b_n)}")
        elif state == "联接":
            print(f"    [跳过] 已经是联接 → {_junction_target(it.source)}")
        print()
    print(f"合计待搬 {_mb(total)}")
    du = shutil.disk_usage(get_deps_root().anchor or "D:\\")
    print(f"目标盘 {get_deps_root().anchor} 可用 {du.free / 2**30:,.1f} GB")
    print()
    print("下一步: python tools/依赖迁移.py --apply")
    return 0


def cmd_apply() -> int:
    ensure_deps_root()
    ts = time.strftime("%Y%m%d-%H%M%S")
    print("=" * 78)
    print(f"依赖迁移 · 执行  时间戳 {ts}")
    print("=" * 78)
    done: list[Item] = []
    for it in _items():
        print(f"\n[{it.key}] {it.label}")
        src, dst = it.source, it.target
        if not src.exists():
            if it.must_exist:
                print(f"    [!] 源不存在：{src} —— 中止（必迁项缺失）")
                return 2
            print(f"    [-] 源不存在，跳过：{src}")
            continue
        if _is_junction(src):
            print(f"    [-] 已经是联接 → {_junction_target(src)}，跳过")
            continue

        f_src, b_src = _stat_tree(src)
        print(f"    源 {f_src:,} 文件 / {_mb(b_src)}")
        free = shutil.disk_usage(dst.anchor).free
        if free < b_src * 1.05:
            print(f"    [!] 目标盘可用 {_mb(free)} < 需要 {_mb(b_src)}，中止")
            return 2

        print(f"    1/5 复制 → {dst}")
        if not _copy(src, dst):
            return 3

        print("    2/5 核对")
        f_dst, b_dst = _stat_tree(dst)
        if (f_dst, b_dst) != (f_src, b_src):
            print(f"    [!] 核对失败：源 {f_src:,}/{_mb(b_src)} vs 目标 {f_dst:,}/{_mb(b_dst)}")
            print("    [!] 保留源目录不动，请人工检查目标目录后重跑")
            return 3
        print(f"    ✓ 一致（{f_dst:,} 文件 / {_mb(b_dst)}）")

        print("    3/5 原目录改名（不是删除）")
        bak = _backup_name(src, ts)
        try:
            src.rename(bak)
        except OSError as e:
            print(f"    [!] 改名失败（可能有进程正在使用）：{e}")
            print("    [!] 请先停止 bot 主程序 / GPT-SoVITS 再重跑；目标目录可保留（复制是幂等的）")
            return 4
        print(f"    {src.name} → {bak.name}")

        print("    4/5 建立目录联接")
        if not _make_junction(src, dst):
            print("    [!] 建联接失败，正在改名回滚")
            bak.rename(src)
            return 5

        print("    5/5 读回验证")
        if not src.exists() or not any(src.iterdir()):
            print("    [!] 联接读不回来，回滚")
            _remove_junction(src)
            bak.rename(src)
            return 5
        print(f"    ✓ {src} → {_junction_target(src)}")
        done.append(it)

    print("\n" + "=" * 78)
    print(f"迁移完成：{len(done)} 项")
    for it in done:
        print(f"  ✓ {it.label}: {it.source} → {it.target}")
    print()
    print("下一步")
    print("  1) 跑 python tools/依赖迁移.py --verify")
    print("  2) 跑 python -c \"import torch, transformers; print(torch.__version__)\"")
    print("  3) 全部正常后：python tools/依赖迁移.py --purge-backup")
    print("=" * 78)
    return 0


def cmd_verify() -> int:
    print("=" * 78)
    print("依赖迁移 · 验证")
    print("=" * 78)
    ok = True
    for it in _items():
        src = it.source
        isj = _is_junction(src)
        mark = "✓" if (isj or not it.must_exist and not src.exists()) else "✗"
        if not isj and src.exists() and not _find_backup(src):
            pass
        print(f"{mark} [{it.key}] {src}")
        print(f"      联接={isj}  指向={_junction_target(src) if isj else '-'}")
        bak = _find_backup(src)
        if bak:
            print(f"      C 盘备份仍在: {bak.name}")
        if it.must_exist and not isj and not bak:
            ok = False
    print()
    print("解释器视角：")
    rc, out = _run([sys.executable, "-X", "utf8", "-c", "import sys;print(sys.path)"])
    print("   ", out.strip()[:400])
    print()
    print("结论:", "通过" if ok else "**未通过** —— 主 site-packages 还不是联接")
    return 0 if ok else 1


def cmd_rollback() -> int:
    print("=" * 78)
    print("依赖迁移 · 回滚")
    print("=" * 78)
    for it in reversed(_items()):
        src = it.source
        bak = _find_backup(src)
        print(f"\n[{it.key}] {src}")
        if not bak:
            print("    [-] 没有找到 C 盘备份，跳过")
            continue
        if _is_junction(src):
            print("    1/2 删除联接（只删链接，不动目标）")
            if not _remove_junction(src):
                return 1
        elif src.exists():
            print(f"    [!] {src} 存在且不是联接，无法自动回滚，请人工处理")
            return 1
        print("    2/2 备份改名回原位")
        bak.rename(src)
        print(f"    ✓ {bak.name} → {src.name}")
    print("\n回滚完成。项目内 依赖/ 下的副本未删除（确认后可手工删）。")
    return 0


def cmd_purge_backup() -> int:
    print("=" * 78)
    print("依赖迁移 · 清理 C 盘备份")
    print("=" * 78)
    for it in _items():
        if not _is_junction(it.source):
            print(f"[-] [{it.key}] {it.source} 不是联接，**拒绝**清理（防止误删真实目录）")
            continue
        bak = _find_backup(it.source)
        if not bak:
            print(f"[-] [{it.key}] 没有备份可清")
            continue
        f_n, b_n = _stat_tree(bak)
        print(f"[{it.key}] 删除 {bak.name}（{f_n:,} 文件 / {_mb(b_n)}）")
        try:
            shutil.rmtree(bak)
            print("    ✓ 已删除")
        except Exception as e:
            print(f"    [!] 删除失败：{type(e).__name__}: {e}")
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把 C 盘依赖搬到项目内并建立目录联接")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只打印计划")
    g.add_argument("--apply", action="store_true", help="执行迁移")
    g.add_argument("--verify", action="store_true", help="验证迁移状态")
    g.add_argument("--rollback", action="store_true", help="回滚")
    g.add_argument("--purge-backup", action="store_true", help="删除 C 盘备份")
    a = ap.parse_args()
    if a.dry_run:
        return cmd_dry_run()
    if a.apply:
        return cmd_apply()
    if a.verify:
        return cmd_verify()
    if a.rollback:
        return cmd_rollback()
    return cmd_purge_backup()


if __name__ == "__main__":
    raise SystemExit(main())
