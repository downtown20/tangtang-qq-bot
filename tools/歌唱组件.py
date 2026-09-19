#!/usr/bin/env python3
"""翻唱制作组件的安装与检查——**单一实现**，控制台与安装器共用。

## 谁调它

两处都走 `importlib.util.spec_from_file_location` 加载本模块（项目既有惯例）：

    · 糖糖控制台_qt.py   歌唱工作室「一键全流程」发现缺件时
    · tools/安装糖糖.py   安装时勾选（以及事后补装）

放一份在这里是因为这两条路都要做同样三件事：查 Release、下模型包、建 demucs 环境。
各写一遍必然漂移——项目为此吃过不止一次亏。

## 三件事互相独立

  1. **模型包**  `tangtang-singing-models.zip`（约 400 M）——RVC 精简推理代码 + HuTao 模型
  2. **demucs 环境**  `venv_demucs/`——人声分离用。**必须另建 venv**：
     audio-separator 拖的 torch 与主环境的 numpy 钉法（<1.24，RVC 要）互相冲突
  3. **分离模型**  `uvr5_models/` 四个 ckpt 约 2.5 G——**不下载**，
     `audio-separator` 的 `load_model()` 自带下载（"downloading it first if necessary"）

## 为什么模型包要自己解压而不用普通解压

包是从网络拉的 zip。CPython 的 `zipfile._extract_member` 自带防穿越，
但这条路径**曾经因为「信任压缩包内容」出过一次事故**（tarfile 在 3.10 没有
`filter` 参数），所以这里仍然显式挡一遍并留痕。

用法：
    python tools/歌唱组件.py 检查        # 只报告缺什么
    python tools/歌唱组件.py 安装        # 下模型包 + 建 demucs 环境
    python tools/歌唱组件.py 装环境      # 只建 demucs 环境
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent

RELEASE_REPO = "downtown20/tangtang-qq-bot"
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page=50"

PACK_NAME = "tangtang-singing-models.zip"

# RVC 在项目里的落点。**包内布局是「RVC 根」**（顶层 `infer/ configs/ assets/ tools/`），
# 所以解压目标必须是这个子目录，不是项目根。
#
# [!] 2026-09-20 踩过：`install_pack` 原来解到项目根，于是
#     `missing_pack_items()` 一项都没满足——用户每点一次「自动安装」就白下 401 MB，
#     文件还散在项目根污染 `tools/` 与 `assets/`。而当时的测试样本是自己编的布局
#     （带前缀），12 条全绿，什么都没拦住。
#     现在由 test_pack_layout_matches_where_it_is_extracted 用**真产物**钉住。
RVC_DIRNAME = "Retrieval-based-Voice-Conversion-WebUI"

# 包内允许出现的顶层名（H3 白名单）。包只该有这四样——
# 没有它，一个被替换的 zip 能覆盖 `agent/handler.py`、`.env`、甚至
# `venv_demucs/Scripts/python.exe`（那个下一步就会被 subprocess 执行）。
PACK_ALLOWED_TOP = {"infer", "configs", "assets", "tools"}
PACK_ALLOWED_EXACT = {"tools/infer_cli.py"}

# 解压上限（压缩炸弹）。真包是 96 条目 / 522 MB，这里留足余量。
# 2026-09-20 复核实测：一个 300 MB 全零条目的 zip 只有 299 KB（放大 1028 倍），
# 原来的实现 0.4 秒就把它解出来落盘——没有任何大小闸门。
PACK_MAX_TOTAL = 2 * 1024 ** 3        # 解压后总量
PACK_MAX_ENTRY = 1 * 1024 ** 3        # 单条目

# 与 糖糖控制台_qt.py 的 _PIPELINE_DOWNLOADS 同口径（那边负责 UI 文案，这边负责动作）
PACK_ITEMS = {
    "HuTao 模型": f"{RVC_DIRNAME}/assets/weights/hutao.pth",
    "HuTao 索引": f"{RVC_DIRNAME}/assets/weights/hutao.index",
    "HuBERT 模型": f"{RVC_DIRNAME}/assets/hubert/hubert_base.pt",
    "RVC infer_cli.py": f"{RVC_DIRNAME}/tools/infer_cli.py",
}
DEMUCS_PY = "venv_demucs/Scripts/python.exe"
# pip 装成功后才写。见 demucs_ready 的注释——venv 的解释器在 pip 之前就存在了。
DEMUCS_SENTINEL = "venv_demucs/.installed"
DEMUCS_PKGS = ["audio-separator==0.44.5"]
# demucs 在 Windows 上要这个 fork；装不上不阻断（audio-separator 自带的那份通常够用）
DEMUCS_SOFT_PKGS = ["diffq-fixed==0.2.4"]


def _say(msg: str) -> None:
    print(msg, flush=True)


# ═══════════════════════════════════════════════════════
# 1. 状态
# ═══════════════════════════════════════════════════════

def missing_pack_items(base: Path | None = None) -> list[str]:
    """模型包里那几项，哪些本机还没有。"""
    b = base or BASE
    return [label for label, rel in PACK_ITEMS.items() if not (b / rel).exists()]


def demucs_ready(base: Path | None = None) -> bool:
    """demucs 环境**装好了**没。

    判据是「解释器在」**且**「哨兵在」，不是只看解释器——**不 import
    audio_separator**（一次要几秒，拖 torch，而控制台是在 GUI 线程里问的）。

    [!] 哨兵不能省。venv 的 `python.exe` 在 `python -m venv` 那一步就落盘了，
    pip 失败它照样在。只看它会把半成品当成成品，而且**再也不会重跑 pip**：
    2026-09-20 实测第二次调用 `install_demucs_env`，0 个子进程直接返回 True，
    日志是「[√] demucs 环境已存在」，而 audio_separator 根本没装上——
    用户只能自己想到去删 venv_demucs。哨兵由 pip 成功后写入。
    """
    b = base or BASE
    return (b / DEMUCS_PY).is_file() and (b / DEMUCS_SENTINEL).is_file()


RVC_DEPS = ["torch", "faiss", "pyworld", "torchfcpe", "fairseq", "librosa"]


def rvc_deps_missing() -> list[str]:
    """主环境里缺哪些 RVC 依赖。

    用 `find_spec` 只查「在不在」，**不 import**——import torch / fairseq 要好几秒，
    而这个函数会被面板状态和「检查」命令调用。

    ⚠ 它查不出「装了但坏了」（比如 faiss 被 NumPy 2 的 ABI 挤掉）——
    那种要用 `probe_rvc_import()` 真跑一次才知道。
    """
    import importlib.util
    return [m for m in RVC_DEPS if importlib.util.find_spec(m) is None]


def probe_rvc_import(base: Path | None = None, *, log=_say) -> bool:
    """真 import 一次 RVC 入口，确认它不是「装了但坏了」。

    这条是有来历的：2026-09-19 实测 `faiss` 与 NumPy 2 的 ABI 冲突时，
    `from infer.modules.vc.modules import VC` 直接 ImportError，
    而 `find_spec("faiss")` 照样返回正常——文件在、动态库加载不了。
    """
    b = base or BASE
    rvc = b / "Retrieval-based-Voice-Conversion-WebUI"
    if not (rvc / "infer").is_dir():
        log("    [!] 模型包还没解压，跳过 import 自检")
        return False
    r = subprocess.run(
        [find_python(), "-c", "from infer.modules.vc.modules import VC; print('OK')"],
        cwd=str(rvc), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    if r.returncode == 0 and "OK" in (r.stdout or ""):
        log("    [√] RVC 入口 import 通过")
        return True
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
    log("    [x] RVC 入口 import 失败：")
    for ln in tail:
        log(f"        {ln[:110]}")
    return False


def status(base: Path | None = None) -> dict:
    b = base or BASE
    miss = missing_pack_items(b)
    deps = rvc_deps_missing()
    return {
        "pack_missing": miss,
        "rvc_deps_missing": deps,
        "demucs_ready": demucs_ready(b),
        "ready": not miss and not deps and demucs_ready(b),
    }


# ═══════════════════════════════════════════════════════
# 2. Release 附件（与 tools/安装糖糖.py 的 _release_assets 同口径）
# ═══════════════════════════════════════════════════════

def release_assets() -> dict[str, dict]:
    """全部 Release 的「附件名 → {url, digest, size}」。

    合并多版本是为了**版本无关的附件**（语音分卷、歌唱模型包）——
    它们挂在某一条 Release 下，后续版本直接复用，不重传。

    `digest` 是 GitHub 自己算好的 `sha256:...`——**白拿的完整性校验**，
    用它比在别处托管一个校验值文件可靠（没有第二处会漂移的东西）。
    """
    try:
        req = urllib.request.Request(RELEASE_API, headers={"User-Agent": "tangtang/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            releases = json.load(r)
    except Exception as e:                                       # noqa: BLE001
        _say(f"    [!] 读取 Release 附件列表失败: {e}")
        return {}
    out: dict[str, dict] = {}
    for rel in releases if isinstance(releases, list) else []:
        for a in rel.get("assets", []):
            out.setdefault(a["name"], {
                "url": a["browser_download_url"],
                "digest": a.get("digest") or "",
                "size": a.get("size") or 0,
            })
    return out


def pack_asset() -> dict | None:
    return release_assets().get(PACK_NAME)


def pack_url() -> str | None:
    a = pack_asset()
    return a["url"] if a else None


# ═══════════════════════════════════════════════════════
# 3. 下载与解压
# ═══════════════════════════════════════════════════════

def download(url: str, dst: Path, on_bytes=None) -> bool:
    """流式下载到 `.part` 再改名——中断不会留下半截文件冒充成品。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tangtang/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_bytes:
                    on_bytes(got, total)
        os.replace(tmp, dst)
        return True
    except Exception as e:                                       # noqa: BLE001
        _say(f"    [!] 下载失败: {e}")
        tmp.unlink(missing_ok=True)
        return False


def extract_zip_safely(zf: zipfile.ZipFile, dest: Path,
                       allowed_top: set[str] | None = None,
                       allowed_exact: set[str] | None = None,
                       max_total: int | None = None,
                       max_entry: int | None = None) -> int:
    """解压并显式挡一遍路径穿越、链接条目、以及**条目名白名单**。

    CPython 的 `_extract_member` 本来就防穿越，这里仍显式挡是因为：
    **这条路径曾经因为「信任压缩包内容」出过事故**（tarfile 在 3.10 没有
    `filter` 参数，默认全信）。

    `allowed_top` 是**条目名白名单**（2026-09-20 补）。没有它时，一个被替换的
    附件能把 `agent/handler.py`、`config.yaml`、`.env` 覆盖掉，甚至能写
    `venv_demucs/Scripts/python.exe`——而那个文件下一步就会被控制台 subprocess
    执行。只挡穿越是不够的：`agent/handler.py` 一点都不「穿越」。

    条目名**允许首段在 `allowed_top` 里**，或全名在 `allowed_exact` 里。
    """
    dest = dest.resolve()
    entries = zf.infolist()

    # **先全量校验，再落盘**。边校验边解压的话，恶意条目排在后面时，
    # 前面的条目已经写进去了——拒绝得不干净。
    for info in entries:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise ValueError(f"压缩包含越界路径，拒绝解压：{name!r}")
        # Unix 模式位里 0xA000 是符号链接——解压出来会指向别处
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise ValueError(f"压缩包含符号链接条目，拒绝解压：{name!r}")
        if allowed_top is not None:
            head = name.split("/")[0]
            if head not in allowed_top and name not in (allowed_exact or set()):
                raise ValueError(
                    f"压缩包含白名单外的条目，拒绝解压：{name!r}"
                    f"（只允许 {sorted(allowed_top)} 下的内容）")
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest)):
            raise ValueError(f"压缩包条目解压后越出目标目录：{name!r}")

    if max_entry is not None:
        big = [i.filename for i in entries if i.file_size > max_entry]
        if big:
            raise ValueError(
                f"压缩包里有超大条目，拒绝解压：{big[:3]}（单条目上限 "
                f"{max_entry / 2**20:.0f} MB）")
    if max_total is not None:
        total = sum(i.file_size for i in entries)
        if total > max_total:
            raise ValueError(
                f"压缩包解压后总量 {total / 2**20:.0f} MB 超过上限 "
                f"{max_total / 2**20:.0f} MB，拒绝解压（压缩炸弹？）")

    for info in entries:
        zf.extract(info, dest)
    return len(entries)



def install_pack(base: Path | None = None, dest_dir: Path | None = None,
                 *, log=_say) -> bool:
    """下载歌唱模型包并解压到 `Retrieval-based-Voice-Conversion-WebUI/`。

    三步，缺一步这条链路就是假的：
      1. 下载后**核对 GitHub 给的 sha256**（`assets[].digest`，白拿的）
      2. 解压时给**条目名白名单**（只放行 infer/ configs/ assets/ 与 tools/infer_cli.py）
      3. 解到 RVC 子目录——包内布局就是「RVC 根」，解到项目根等于没装

    `log` 接收一行文本：控制台把进度接到状态栏与日志，命令行直接 print。
    """
    import hashlib

    b = base or BASE
    # [!] 落点是 RVC 子目录。2026-09-20 这里原先是项目根，于是判据一项都没满足，
    #     用户每点一次就白下 401 MB。见 RVC_DIRNAME 的注释。
    dst = dest_dir or (b / RVC_DIRNAME)

    asset = pack_asset()
    if not asset:
        log(f"    [x] Release 里找不到 {PACK_NAME}——检查网络，或它还没上传")
        return False
    url, digest, size = asset["url"], asset["digest"], asset["size"]

    pkg = b / "temp_files" / PACK_NAME

    def _progress(got: int, total: int) -> None:
        if total and got % (64 * 1024 * 1024) < 1024 * 512:       # 每 ~64M 报一次
            log(f"    [~] 已下载 {got / 2**20:.0f} / {total / 2**20:.0f} MB")

    log(f"    [~] 下载 {PACK_NAME}（约 {size / 2**20:.0f} MB）…")
    if not download(url, pkg, on_bytes=_progress):
        return False
    log(f"    [√] 已下载 {pkg.stat().st_size / 2**20:.1f} MB")

    if digest.startswith("sha256:"):
        want = digest.split(":", 1)[1]
        h = hashlib.sha256()
        with open(pkg, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != want:
            log(f"    [x] 校验失败：sha256 对不上（期望 {want[:16]}…，实际 {got[:16]}…）"
                f"——文件已删除，请重试")
            pkg.unlink(missing_ok=True)
            return False
        log("    [√] sha256 校验通过")
    else:
        log("    [!] Release 附件的 digest 拿不到，跳过校验")

    log("    [~] 解压中…")
    try:
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(pkg) as zf:
            n = extract_zip_safely(zf, dst, PACK_ALLOWED_TOP, PACK_ALLOWED_EXACT,
                                   PACK_MAX_TOTAL, PACK_MAX_ENTRY)
    except Exception as e:                                       # noqa: BLE001
        log(f"    [x] 解压失败：{e}")
        return False
    log(f"    [√] 解压 {n} 个条目到 {dst.relative_to(b)}")
    pkg.unlink(missing_ok=True)

    rest = missing_pack_items(b)
    if rest:
        log(f"    [x] 解压完仍缺：{'、'.join(rest)}——包内容与判据对不上")
        return False
    return True


# ═══════════════════════════════════════════════════════
# 4. demucs 环境
# ═══════════════════════════════════════════════════════

def find_python() -> str:
    """主环境解释器——用它 -m venv 建环境，保证版本与主环境一致（3.10）。"""
    return sys.executable


def install_demucs_env(base: Path | None = None, *, log=_say) -> bool:
    """建 `venv_demucs/` 并装 audio-separator。

    **为什么不随包发这个 venv**：`pyvenv.cfg` 里记的是**建它时那台机器**的
    Python **绝对路径**（`pyvenv.cfg` 里的 `home` 那一行）。发到别人机器上，
    那个路径不存在，venv 直接失效。就地建才可靠。

    代价是用户要下约 1.7 G（torch + onnxruntime）——比随包发多花几分钟，
    但换来的是「在他自己的机器上一定可用」。
    """
    b = base or BASE
    venv_dir = b / "venv_demucs"
    py = venv_dir / "Scripts" / "python.exe"
    if demucs_ready(b):
        log(f"    [√] demucs 环境已就绪：{venv_dir.relative_to(b)}")
        return True

    if py.is_file():
        # 解释器在、哨兵不在 = 上次 pip 没装完。**补装，不要直接返回 True**
        log("    [!] 检测到上次没装完的环境（虚拟环境在、依赖没齐）——补装依赖")
    else:
        log(f"    [~] 创建虚拟环境 {venv_dir.relative_to(b)}（约 1.7 G，视网络需要几分钟）…")
        r = subprocess.run([find_python(), "-m", "venv", str(venv_dir)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0 or not py.is_file():
            log(f"    [x] 创建虚拟环境失败：{(r.stderr or '')[-300:]}")
            return False

    for pkg in DEMUCS_PKGS:
        log(f"    [~] 安装 {pkg} …")
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", pkg],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            log(f"    [x] {pkg} 安装失败：{(r.stderr or '')[-300:]}")
            log("    [!] 环境是半成品，下次点「自动安装」会补装（不会当成已完成）")
            return False
    for pkg in DEMUCS_SOFT_PKGS:
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", pkg],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        log(f"    {'[√]' if r.returncode == 0 else '[!]'} {pkg}"
            f"{'' if r.returncode == 0 else ' 装不上，不阻断'}")

    # 装完了才写哨兵——这是「已完成」的唯一凭据（见 demucs_ready 的注释）
    (b / DEMUCS_SENTINEL).write_text("audio-separator 安装成功\n", encoding="utf-8")
    log("    [√] demucs 环境就绪")
    return True


# ═══════════════════════════════════════════════════════
# 5. 一键
# ═══════════════════════════════════════════════════════

def install_all(base: Path | None = None, *, log=_say) -> bool:
    b = base or BASE
    ok = True
    if missing_pack_items(b):
        ok = install_pack(b, log=log) and ok
    else:
        log("    [√] 模型包内容齐备，跳过下载")
    if not demucs_ready(b):
        ok = install_demucs_env(b, log=log) and ok
    else:
        log("    [√] demucs 环境已就绪")
    deps = rvc_deps_missing()
    if deps:
        log(f"    [!] 主环境还缺这些依赖：{'、'.join(deps)}")
        log(f"        重跑 安装糖糖.bat 勾「唱歌」，或：pip install {' '.join(deps)}")
        ok = False
    # 装了 ≠ 能用：真 import 一次再下结论（faiss × NumPy 2 那次就是「装了但坏了」）
    if not missing_pack_items(b):
        ok = probe_rvc_import(b, log=log) and ok
    log(f"    {'[√] 翻唱组件可用' if ok else '[!] 还有问题，见上面几行'}")
    return ok


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "检查").strip()
    st = status()
    if cmd in ("检查", "status"):
        _say("翻唱制作组件状态：")
        _say(f"  {'[√]' if not st['pack_missing'] else '[ ]'} 模型包"
             f"{'（齐备）' if not st['pack_missing'] else '缺：' + '、'.join(st['pack_missing'])}")
        _say(f"  {'[√]' if not st['rvc_deps_missing'] else '[ ]'} 主环境依赖"
             f"{'' if not st['rvc_deps_missing'] else '缺：' + '、'.join(st['rvc_deps_missing'])}")
        _say(f"  {'[√]' if st['demucs_ready'] else '[ ]'} demucs 环境")
        if not st["pack_missing"]:
            _say("  （下面这一步会真 import 一次，约十几秒）")
            probe_rvc_import()
        _say(f"\n{'[√] 可用' if st['ready'] else '[ ] 未就绪——运行：python tools/歌唱组件.py 安装'}")
        return 0 if st["ready"] else 1
    if cmd in ("安装", "install"):
        _say("安装翻唱制作组件…")
        return 0 if install_all() else 1
    if cmd in ("装环境", "env"):
        return 0 if install_demucs_env() else 1
    if cmd in ("下模型", "pack"):
        return 0 if install_pack() else 1
    if cmd in ("自检", "probe"):
        return 0 if probe_rvc_import() else 1
    _say(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
