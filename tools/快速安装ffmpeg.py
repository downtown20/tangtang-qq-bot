"""ffmpeg 安装器 —— 装**共享库版**，放进项目内的依赖目录，并挂到系统 PATH。

为什么要共享版，不是 static 版
──────────────────────────────
老版本这个脚本干的事是「把 static ffmpeg.exe 复制进 C:\\Windows\\System32」。
那是个**假安装**，留下三个后遗症：

1. static 版**没有 DLL**，而 torchcodec 要的正是 avcodec / avutil 这些
   **共享库**。于是 torchcodec 永远报 "FFmpeg is not properly installed"。
2. 只复制了 ffmpeg.exe，**ffprobe.exe / ffplay.exe 都没进 System32**——
   而 tools/prepare_michele_refs.py 要调 ffprobe。
3. shutil.which("ffmpeg") 从此**永远返回一个路径**，体检和安装器的
   「FFmpeg 检查」全部显示 [√]。**检查项失去意义**——它报的绿是假的。

所以本脚本只认**共享版**（bin/ 下必须有 .dll），并且**必须有阳性对照**：
装完要能证明 DLL 在位、三个 exe 都能跑。

本脚本做什么
────────────
1. 下载 BtbN 的 ffmpeg-master-latest-win64-gpl-shared.zip（约 85 MB）
2. 解压到 <依赖根>/ffmpeg/（**不是 C 盘**）
3. 把 <依赖根>/ffmpeg/bin 写进**系统 PATH**（HKLM，需要管理员；没有管理员就退到用户 PATH）
4. 把 C:\\Windows\\System32 里那份**残留的 ffmpeg.exe 隔离掉**
   —— System32 在 PATH 里排在最前，不挪走它就会一直遮住新装的
5. 阳性对照：确认 DLL 数量 > 0、ffmpeg/ffprobe/ffplay 三个都能 --version

用法
────
    python tools/快速安装ffmpeg.py            安装
    python tools/快速安装ffmpeg.py --check    只检查现状
    python tools/快速安装ffmpeg.py --path-only 只更新 PATH（已装好时用）
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from 依赖位置 import ensure_deps_root, get_deps_root  # type: ignore
except Exception:  # pragma: no cover
    def get_deps_root() -> Path:  # type: ignore
        return Path(__file__).resolve().parent.parent / "依赖"

    def ensure_deps_root() -> Path:  # type: ignore
        return get_deps_root()


URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
LABEL = "ffmpeg-master-latest-win64-gpl-shared"

#: System32 —— 它在 PATH 里排在最前，里面任何同名 exe 都会遮住新装的
SYSTEM32 = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32"

REQUIRED_EXES = ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")

#: 要在 System32 里找的可疑残留。**必须穷举这三个名字**——
#: 2026-10-14 实测：老脚本只复制了 ffmpeg.exe，可 System32 里还躺着一个
#: ffprobe.exe（143 MB 的 static 全量二进制，来路不明），于是
#: ffmpeg/ffplay 都解析到新装的，唯独 ffprobe 仍然解析到旧的。
#: 只清一个名字的检查，等于给另外两个开了天窗。
STALE_NAMES = REQUIRED_EXES


def stale_system32() -> list[Path]:
    """System32 里存在的、会遮住新装的同名 exe。"""
    return [SYSTEM32 / n for n in STALE_NAMES if (SYSTEM32 / n).exists()]


def ffmpeg_home() -> Path:
    return get_deps_root() / "ffmpeg"


def ffmpeg_bin() -> Path:
    return ffmpeg_home() / "bin"


def _mb(b: int) -> str:
    return f"{b / 2**20:,.1f} MB"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ── 检查 ──────────────────────────────────────────────────────────────
def check(verbose: bool = True, include_stale: bool = True) -> bool:
    """阳性对照：DLL 在位 + 三个 exe 都能跑 + System32 没有残留。**能失败**才算检查。

    include_stale=False 只用于「装之前判断要不要重新下载」——那时残留还没清掉，
    不该因此判定为「没装好」而重下一遍。
    """
    dlls = sorted(p.name for p in ffmpeg_bin().glob("*.dll")) if ffmpeg_bin().is_dir() else []
    ok = True
    if verbose:
        print(f"ffmpeg 目录 : {ffmpeg_home()}")
        print(f"DLL 数量    : {len(dlls)}" + (f"  例: {', '.join(dlls[:3])}" if dlls else ""))
    if not dlls:
        ok = False
        if verbose:
            print("  [×] bin/ 下没有 .dll —— 这是 static 版或压根没装，torchcodec 用不了")
    for exe in REQUIRED_EXES:
        p = ffmpeg_bin() / exe
        if not p.exists():
            ok = False
            if verbose:
                print(f"  [×] 缺 {exe}")
            continue
        rc, out = _run([str(p), "-version"])
        first = out.splitlines()[0][:70] if out else "(无输出)"
        if rc != 0:
            ok = False
        if verbose:
            print(f"  [{'√' if rc == 0 else '×'}] {exe}: {first}")
    if verbose:
        print(f"which 解析  : {shutil.which('ffmpeg') or '（不在 PATH）'}")
        print(f"which ffprobe: {shutil.which('ffprobe') or '（不在 PATH）'}")
        stale = stale_system32()
        print(f"System32 残留: {', '.join(p.name for p in stale) if stale else '已清'}")
        if stale:
            print("  [!] System32 在 PATH 里排在前面，它们会遮住新装的 —— 需要隔离")
            if include_stale:
                ok = False
        print(f"结论        : {'通过' if ok else '**未通过**'}")
    return ok


# ── PATH ──────────────────────────────────────────────────────────────
def _broadcast_env_change() -> None:
    """通知已运行的进程「环境变量变了」——否则新开的记事本也看不到。"""
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None
        )
    except Exception:
        pass


def add_to_path(bin_dir: Path, dry_run: bool = False) -> str:
    """把 bin_dir 写进系统 PATH。返回说明字符串。

    优先 HKLM（系统级，需要管理员）；没权限时退到 HKCU（用户级）并明确告知。
    """
    import winreg

    target = str(bin_dir)
    for hive, sub, name in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "系统级"),
        (winreg.HKEY_CURRENT_USER, "Environment", "用户级"),
    ):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
                try:
                    cur, kind = winreg.QueryValueEx(k, "Path")
                except FileNotFoundError:
                    cur, kind = "", winreg.REG_EXPAND_SZ
                entries = [e for e in str(cur).split(";") if e.strip()]
                if any(os.path.normcase(os.path.expandvars(e).rstrip("\\")) ==
                       os.path.normcase(target.rstrip("\\")) for e in entries):
                    _broadcast_env_change()
                    return f"已在 {name} PATH 中"
                if dry_run:
                    return f"将写入 {name} PATH（dry-run）"
                newv = ";".join(entries + [target])
                winreg.SetValueEx(k, "Path", 0, kind or winreg.REG_EXPAND_SZ, newv)
            _broadcast_env_change()
            return f"已写入 {name} PATH"
        except PermissionError:
            continue
        except OSError:
            continue
    return "**PATH 写入失败**（HKLM 与 HKCU 都被拒）——请手工把目录加进 PATH"


# ── 隔离 System32 残留 ────────────────────────────────────────────────
def quarantine_stale() -> str:
    """把 System32 里那些「只复制了 exe」的旧安装挪走。

    System32 在 PATH 里排在最前，不挪走就会一直遮住新装的。
    **三个名字都要清**——见 STALE_NAMES 上的说明。
    """
    found = stale_system32()
    if not found:
        return "无残留"
    q = ffmpeg_home() / "_quarantine"
    q.mkdir(parents=True, exist_ok=True)
    msgs = []
    for p in found:
        dest = q / f"System32-{p.name}.{os.path.getsize(p)}"
        try:
            shutil.move(str(p), str(dest))
            msgs.append(f"{p.name} → {dest.name}")
        except Exception as e:
            msgs.append(f"**{p.name} 隔离失败**：{type(e).__name__}: {e}")
    return "；".join(msgs)


# ── 安装 ──────────────────────────────────────────────────────────────
def download(dest: Path) -> bool:
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 50 * 2**20:
        print(f"  使用已缓存: {dest} ({_mb(dest.stat().st_size)})")
        return True
    print(f"  下载 {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            got = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if total:
                        print(f"\r  {got * 100 // total}%  {_mb(got)}/{_mb(total)}", end="")
        print()
    except Exception as e:
        print(f"\n  [!] 下载失败：{type(e).__name__}: {e}")
        return False
    return dest.exists() and dest.stat().st_size > 50 * 2**20


def extract(zip_path: Path, home: Path) -> bool:
    tmp = home.parent / "_extract_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"  解压 → {tmp}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
    except Exception as e:
        print(f"  [!] 解压失败：{type(e).__name__}: {e}")
        return False

    # zip 里有一层 <LABEL>/ 包装，把 bin/lib/include/README 提上来
    inner = tmp / LABEL
    if not inner.is_dir():
        cands = [d for d in tmp.iterdir() if d.is_dir()]
        if len(cands) != 1:
            print(f"  [!] 解压结果结构异常：{cands}")
            return False
        inner = cands[0]
    home.mkdir(parents=True, exist_ok=True)
    for child in inner.iterdir():
        dst = home / child.name
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
        shutil.move(str(child), str(dst))
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def install() -> int:
    ensure_deps_root()
    home = ffmpeg_home()
    print("=" * 74)
    print("ffmpeg 安装（共享库版 → 项目内依赖目录）")
    print("=" * 74)
    print(f"目标: {home}")
    print()

    if check(verbose=False, include_stale=False):
        print("[1/4] 已装好，跳过下载解压")
    else:
        dl = get_deps_root() / "_download" / "ffmpeg-shared.zip"
        print("[1/4] 下载")
        if not download(dl):
            return 2
        print("[2/4] 解压")
        if not extract(dl, home):
            return 2

    print("[3/4] 隔离 System32 残留")
    print("     ", quarantine_stale())

    print("[4/4] 写入系统 PATH")
    print("     ", add_to_path(ffmpeg_bin()))

    print()
    print("阳性对照：")
    ok = check(verbose=True)
    if not ok:
        print("\n[!] 安装未通过阳性对照，请不要当成装好了")
        return 1
    print("\n提示：新开的终端才会带上新 PATH；本进程内请重启 shell。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="安装共享库版 ffmpeg 到项目依赖目录并挂 PATH")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="只检查现状")
    g.add_argument("--path-only", action="store_true", help="只更新 PATH")
    a = ap.parse_args()
    if a.check:
        return 0 if check(verbose=True) else 1
    if a.path_only:
        ensure_deps_root()
        print(add_to_path(ffmpeg_bin()))
        return 0 if check(verbose=False) else 1
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
