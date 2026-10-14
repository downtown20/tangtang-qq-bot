"""
糖糖环境部署工具 — 一键安装所有依赖

双击运行即可，自动检测环境，安装缺失依赖，最后显示报告。
也支持命令行参数：

  python 环境部署工具.py              # 自动检测 + 安装
  python 环境部署工具.py --check       # 仅检查状态
  python 环境部署工具.py --full        # 强制全能模式
  python 环境部署工具.py --minimal     # 仅核心依赖
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 双击运行时 stdout 可能被缓冲——强制实时输出
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

# ─── 配置 ────────────────────────────────────────────
BASE = Path(__file__).resolve().parent.parent
GPT_SOVITS_DIR = BASE / "gpt-sovits"
FFMPEG_INSTALL_DIR = Path.home() / "ffmpeg"

PY310_PATHS = [
    Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python310" / "python.exe",
    Path("C:/Python310/python.exe"),
]

# BGE 模型可能的路径（ModelScope 不同版本不同命名）
BGE_PATHS = [
    BASE / "models/BAAI/bge-small-zh-v1___5/config.json",
    BASE / "models/BAAI/bge-small-zh-v1.5/config.json",
    BASE / "models/bge-small-zh-v1___5/config.json",
    BASE / "models/bge-small-zh-v1.5/config.json",
]
BGE_MODEL_IDS = [
    "BAAI/bge-small-zh-v1.5",
    "iic/bge-small-zh-v1.5",
]

RERANKER_PATHS = [
    BASE / "models/BAAI/bge-reranker-v2-m3/config.json",
    BASE / "models/bge-reranker-v2-m3/config.json",
]

FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)

# ─── 状态追踪 ────────────────────────────────────────
_report = {"ok": [], "warn": [], "fail": [], "skip": []}


def ok(msg):  _report["ok"].append(msg); print(f"  ✅ {msg}", flush=True)
def warn(msg): _report["warn"].append(msg); print(f"  ⚠️ {msg}", flush=True)
def fail(msg): _report["fail"].append(msg); print(f"  ❌ {msg}", flush=True)
def skip(msg): _report["skip"].append(msg); print(f"  ⏭ {msg}", flush=True)
def info(msg): print(f"  🔄 {msg}", flush=True)


# ─── 工具函数 ────────────────────────────────────────

def run(cmd: list, **kwargs):
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def pip_install(pkg: str, py_exe=None) -> bool:
    exe = py_exe or sys.executable
    code, _, err = run([exe, '-m', 'pip', 'install', pkg])
    return code == 0


def find_py310() -> Path | None:
    for p in PY310_PATHS:
        if p.exists():
            return p
    code, out, _ = run(['where', 'python'])
    if code == 0:
        for line in out.split('\n'):
            if 'Python310' in line:
                return Path(line.strip())
    return None


# ─── 安装步骤 ────────────────────────────────────────

# ─── 纯检查（不安装）────────────────────────────────

def check_core_deps():
    print("\n📦 核心依赖")
    for pkg, name in [('httpx', 'httpx'), ('websockets', 'websockets'),
                       ('yaml', 'pyyaml'), ('jieba', 'jieba'),
                       ('PySide6', 'PySide6'), ('PIL', 'Pillow')]:
        try:
            __import__(pkg)
            ok(name)
        except ImportError:
            fail(name)

def check_ffmpeg():
    print("\n🎬 ffmpeg")
    if shutil.which("ffmpeg"):
        ok("ffmpeg 已可用")
    elif (FFMPEG_INSTALL_DIR / "bin" / "ffmpeg.exe").exists():
        ok(f"已安装: {FFMPEG_INSTALL_DIR}")
    else:
        fail("ffmpeg 未安装")

def check_bge():
    print("\n🧠 BGE 语义向量模型")
    for p in BGE_PATHS:
        if p.exists():
            ok(f"模型已存在: {p.parent.name}")
            return
    fail("BGE 模型未找到")

def check_reranker():
    print("\n🎯 Reranker 精排模型")
    for p in RERANKER_PATHS:
        if p.exists():
            ok("模型已存在")
            return
    skip("Reranker 未下载（可选）")

def check_voice():
    print("\n🎤 语音引擎")
    if GPT_SOVITS_DIR.exists():
        ok(f"GPT-SoVITS 目录存在")
    else:
        skip("GPT-SoVITS 目录不存在")
    py310 = find_py310()
    if py310:
        # 快速验证
        c, _, _ = run([str(py310), '-c', 'import torch; print(torch.__version__)'])
        ok("torch 可用") if c == 0 else fail("torch 不可用")
    else:
        warn("未找到 Python 3.10——语音引擎不可用")


# ─── 安装步骤 ────────────────────────────────────────

def step_python():
    print("\n🐍 Python 版本")
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 14):
        warn(f"Python {ver} — 语音引擎不可用（需 3.10）")
    elif sys.version_info >= (3, 10):
        ok(f"Python {ver}")
    else:
        fail(f"Python {ver} — 需要 3.10+")
        return False
    return True


def step_core_deps():
    print("\n📦 核心依赖")
    req = BASE / "requirements.txt"
    if not req.exists():
        fail("requirements.txt 不存在")
        return
    info("安装核心依赖中（可能需要几分钟）...")
    code, _, err = run([sys.executable, '-m', 'pip', 'install', '-r', str(req)])
    if code == 0:
        ok("核心依赖安装完成")
        return
    # 逐个重试
    warn("批量安装部分失败，逐个重试…")
    failed = []
    with open(req, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                pkg = line.split('#')[0].strip()
                if pkg and not pip_install(pkg):
                    failed.append(pkg)
    if failed:
        warn(f"{len(failed)} 个包安装失败: {', '.join(failed)}")
    else:
        ok("核心依赖全部就绪")


def step_ffmpeg():
    print("\n🎬 ffmpeg")
    if shutil.which("ffmpeg"):
        ok("ffmpeg 已可用")
        return
    if FFMPEG_INSTALL_DIR.exists() and (FFMPEG_INSTALL_DIR / "bin" / "ffmpeg.exe").exists():
        ok(f"已安装: {FFMPEG_INSTALL_DIR}")
        return

    print("  📥 下载 ffmpeg (~60MB)...")
    import urllib.request, zipfile, io
    try:
        with urllib.request.urlopen(FFMPEG_URL, timeout=120) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            ffmpeg_bin = next((n for n in zf.namelist() if n.endswith('/bin/ffmpeg.exe')), None)
            if not ffmpeg_bin:
                fail("压缩包中未找到 ffmpeg.exe")
                return
            prefix = ffmpeg_bin.replace('bin/ffmpeg.exe', '')
            FFMPEG_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            for name in zf.namelist():
                if name.startswith(prefix):
                    rel = name[len(prefix):]
                    dest = FFMPEG_INSTALL_DIR / rel
                    if name.endswith('/'):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(name))
        ok(f"ffmpeg 已安装: {FFMPEG_INSTALL_DIR / 'bin' / 'ffmpeg.exe'}")
    except Exception as e:
        fail(f"下载失败: {e}")
        print("  手动: https://github.com/BtbN/FFmpeg-Builds/releases")


def step_bge_model():
    print("\n🧠 BGE 语义向量模型")
    for p in BGE_PATHS:
        if p.exists():
            ok(f"模型已存在: {p.parent}")
            return
    # 尝试下载
    try:
        from modelscope import snapshot_download
    except ImportError:
        warn("modelscope 未安装，尝试 pip install...")
        if not pip_install('modelscope'):
            fail("modelscope 安装失败，请从台式机复制 models/BAAI/ 目录")
            return

    for mid in BGE_MODEL_IDS:
        try:
            print(f"  📥 下载 {mid}...")
            snapshot_download(mid, cache_dir=str(BASE / 'models'))
            # 检查是否成功
            for p in BGE_PATHS:
                if p.exists():
                    ok("BGE 模型下载完成")
                    return
        except Exception as e:
            if hasattr(e, 'args') and 'already exists' in str(e):
                ok("BGE 模型已存在")
                return
    # 所有方法都失败
    warn("自动下载失败。推荐用 Syncthing 从台式机同步（docs/用户手册/文件同步指南.md）")
    warn("或手动复制台式机的 models/BAAI/bge-small-zh-v1___5 到笔记本同一路径")


def step_reranker():
    print("\n🎯 Reranker 精排模型")
    for p in RERANKER_PATHS:
        if p.exists():
            ok(f"模型已存在")
            return
    try:
        from modelscope import snapshot_download
        print("  📥 下载 bge-reranker-v2-m3 (~2GB)...")
        snapshot_download('BAAI/bge-reranker-v2-m3', cache_dir=str(BASE / 'models'))
        if any(p.exists() for p in RERANKER_PATHS):
            ok("Reranker 下载完成")
            return
    except Exception:
        pass
    skip("Reranker 未下载（可选，不影响基本功能）")


def step_voice():
    print("\n🎤 语音引擎")
    if not GPT_SOVITS_DIR.exists():
        skip(f"GPT-SoVITS 目录不存在: {GPT_SOVITS_DIR}")
        return

    py310 = find_py310()
    if not py310:
        py310 = find_py310()  # 重试
    if not py310:
        fail(f"未找到 Python 3.10，请安装到: {PY310_PATHS[0]}")
        fail("语音引擎需要 Python 3.10，核心功能不受影响")
        return

    py_exe = str(py310)
    print(f"  使用 {py_exe}")

    gpt_req = GPT_SOVITS_DIR / "requirements.txt"
    if gpt_req.exists():
        print("  📥 GPT-SoVITS 依赖...")
        code, _, _ = run([py_exe, '-m', 'pip', 'install', '-r', str(gpt_req)])
        if code == 0:
            ok("GPT-SoVITS 依赖就绪")
        else:
            warn("部分包失败，逐个安装...")
            failed = 0
            with open(gpt_req, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and not line.startswith('--'):
                        pkg = line.split('#')[0].strip()
                        if pkg and not pip_install(pkg, py_exe):
                            failed += 1
            if failed:
                warn(f"{failed} 个包失败，可能不影响基本语音功能")
            else:
                ok("GPT-SoVITS 依赖全部就绪")

    # 验证
    checks = ['torch', 'librosa', 'soundfile', 'numpy']
    for pkg in checks:
        c, _, _ = run([py_exe, '-c', f'import {pkg}'])
        ok(pkg) if c == 0 else fail(pkg)


# ─── 报告 ────────────────────────────────────────────

def print_report():
    print("\n" + "═" * 50)
    print("   📋 部署报告")
    print("═" * 50)

    def section(title, items, icon):
        if items:
            print(f"\n  {title} ({len(items)}):")
            for item in items:
                print(f"    {icon} {item}")

    section("通过", _report["ok"], "✅")
    section("警告（不影响基本功能）", _report["warn"], "⚠️")
    section("跳过（可选组件）", _report["skip"], "⏭")
    section("失败（需要处理）", _report["fail"], "❌")

    print("\n" + "═" * 50)
    if _report["fail"]:
        print("  ⚠️ 有失败项，详见上方 ❌ 标记。处理完后重新运行本工具。")
    elif _report["warn"]:
        print("  ✅ 核心功能可用。⚠️ 标记的项可选处理。")
    else:
        print("  🎉 全部就绪！可以启动糖糖了。")
    print("═" * 50)


# ─── 主入口 ──────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="糖糖环境部署工具")
    parser.add_argument('--check', action='store_true', help='仅检查')
    parser.add_argument('--full', action='store_true', help='全能模式')
    parser.add_argument('--minimal', action='store_true', help='仅核心')
    args, _ = parser.parse_known_args()

    os.chdir(BASE)

    print("=" * 50, flush=True)
    print("  🍬 糖糖环境部署工具", flush=True)
    print(f"  项目: {BASE}", flush=True)
    print(f"  Python: {sys.executable}", flush=True)
    print("=" * 50, flush=True)
    print("  正在检测环境，请稍候...\n", flush=True)

    if args.check:
        step_python()
        check_core_deps()
        check_ffmpeg()
        check_bge()
        check_reranker()
        check_voice()
        print_report()
        try: input("\n按 Enter 退出...")
        except (EOFError, OSError): pass
        return

    # 自动安装：检查后安装缺失的
    step_python()

    if args.minimal:
        step_core_deps()
        step_bge_model()
    elif args.full:
        step_core_deps()
        step_ffmpeg()
        step_bge_model()
        step_reranker()
        step_voice()
    else:
        # 自动模式：全部检查 + 安装缺失
        step_core_deps()
        step_ffmpeg()
        step_bge_model()
        step_reranker()
        step_voice()

    print_report()
    input("\n按 Enter 退出...")


if __name__ == "__main__":
    main()
