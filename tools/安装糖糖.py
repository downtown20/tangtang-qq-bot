#!/usr/bin/env python3
"""小糖糖 安装/维护工具（统一入口）

用法：
    python tools/安装糖糖.py                  # 菜单选择
    python tools/安装糖糖.py 安装              # 安装依赖（功能选择 + 环境/目录检测 + 配置调整）
    python tools/安装糖糖.py 核心              # 直接安装一个依赖组（组: 核心 记忆 语音 识图 唱歌 文档 控制台 测试 运维）
    python tools/安装糖糖.py 打包 [--if-newer]  # 导出记忆快照（跑完糖糖后、换机前）
    python tools/安装糖糖.py 解包 [机器名]      # 应用记忆快照（启动糖糖前）
    python tools/安装糖糖.py 检查              # 环境 + 快照状态
    python tools/安装糖糖.py 体检              # 换机功能对比
    python tools/安装糖糖.py --dry-run ...     # 预览，不实际执行

双击入口：项目根目录 安装糖糖.bat（同一程序的壳）；也可直接双击本文件
说明：重量级依赖全部懒加载+降级链——不装的组只会让对应功能不可用，不会崩。
"""

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# 强制 UTF-8 输出（与 同步记忆.py / 体检.py 同一守卫）：
# stdout 被重定向或走管道时 Python 退回 GBK，print 里的符号抛 UnicodeEncodeError
# （同步记忆.py 真出过这个事故：退出码 1 但快照已生成 →「显示失败实际成功」）。
#
# 本文件此前是**被顺带保护**的：模块加载时会 exec 同步记忆.py，而那个模块顶层的
# 守卫改的正是同一个 sys.stdout 对象。那是巧合不是设计——导入顺序一变、或那个模块
# 改成懒加载，保护就无声消失。这里显式兜住，不再依赖别人的副作用。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent

# ── 加载同步记忆模块（打包/解包/检查——单一实现，避免两份逻辑漂移）──
_spec = importlib.util.spec_from_file_location(
    "sync_memory", Path(__file__).resolve().parent / "同步记忆.py")
_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync)

# ─────────────────────────────────────────
# 依赖组定义
# ─────────────────────────────────────────

GROUPS = {
    "核心": {
        "desc": "L0 必装——缺了无法启动",
        # [!] numpy 的上界不能去（2026-09-19 实测）：RVC 依赖链是 NumPy 1.x 编译的
        # （pyworld 报 dtype size changed、numba 0.56 要 numpy<1.24）。装到 2.x 就是
        # 歌唱工作室一路 ImportError。与 requirements.txt 保持一致。
        "pkgs": ["websockets>=12.0", "httpx>=0.27.0", "pyyaml>=6.0",
                 "python-dotenv>=1.0", "jinja2>=3.0", "numpy>=1.23.5,<1.24"],
    },
    "记忆": {
        "desc": "L1 高质量记忆——强烈建议（含 torch CPU 版，另装）",
        "pkgs": ["jieba>=0.42", "transformers==4.57.6", "modelscope>=1.20"],
        "extra": [(["torch"], {"index-url": "https://download.pytorch.org/whl/cpu"})],
    },
    "语音": {
        "desc": "L2 语音输入（ASR + SILK 解码）+ 输出辅助；引擎与模型由模型层自动获取",
        "pkgs": ["soundfile>=0.12", "edge-tts>=6.1", "sherpa-onnx>=1.10", "pilk>=0.2",
                 "matplotlib>=3.7,<4"],
        # soft：失败仅警告不中断——jieba-fast 是 GPT-SoVITS 分词加速（需 MSVC 源码编译），
        # 装不上时 start_api_patched.py 自动回退纯 Python jieba，语音仍可启动
        "soft": ["jieba-fast>=0.39"],
    },
    "识图": {
        "desc": "L3 本地识图——需另装 Ollama+minicpm-v；不装自动用云端 qwen-vl（效果几乎一样）",
        "pkgs": ["Pillow>=10.0"],
    },
    "唱歌": {
        "desc": "L4 唱歌——播放预录成品歌曲（曲库已随包自带）+ 翻唱制作的依赖",
        # faiss-cpu：RVC 音色转换读 .index 用。1.9+ 的 wheel 只认 NumPy 2，
        # 与上面的 numpy<1.24 冲突，所以钉 1.7.4（见 requirements.txt）。
        "pkgs": ["pypinyin>=0.48", "faiss-cpu==1.7.4",
                 "pyworld>=0.3.4", "torchfcpe>=0.0.4", "librosa>=0.10"],
        # soft：失败仅警告不中断。fairseq 只有源码包，Windows 上要 MSVC 编译
        # （与 jieba-fast 同一前置）；装不上时 RVC 的 HuBERT 加载会失败，
        # 由 tools/歌唱组件.py 的检查报出来，而不是让用户对着一个沉默的失败猜。
        "soft": ["fairseq==0.12.2"],
    },
    "文档": {
        "desc": "L5 读 docx/pdf",
        "pkgs": ["python-docx>=0.8", "pdfplumber>=0.10", "PyPDF2>=3.0"],
    },
    "控制台": {
        "desc": "PySide6 图形界面——建议",
        "pkgs": ["pyside6>=6.5"],
    },
    "测试": {
        "desc": "pytest 开发测试 + pyflakes undefined 门禁",
        "pkgs": ["pytest>=8.0", "pytest-asyncio>=0.24", "pyflakes>=3.0"],
    },
    "运维": {
        "desc": "L6 runtime_observer 观察器、体检/语音质检等运维脚本（选装）",
        "pkgs": ["psutil>=5.9", "requests>=2.28"],
    },
}

# 功能 → (名称, 说明, 依赖组列表)
FEATURES = [
    ("聊天+高质量记忆", "核心——LLM、记忆、知识库、BGE 向量（必须）", ["核心", "记忆"]),
    ("发语音+听懂语音消息", "听懂别人的语音消息 + GPT-SoVITS 发声；模型约 6G，自动下载", ["语音"]),
    ("本地识图", "本地 MiniCPM-V 识图（约 2.5G，需 Ollama）；不选则用云端 qwen-vl", ["识图"]),
    # [!] 这句原来只写「已随包自带，本项无需下载」，让用户以为勾了它就有全部唱歌能力。
    #   实际它给的是**播放** 41 首预录成品；「歌唱工作室」（自己做翻唱）还需要
    #   RVC 运行环境 + HuTao 模型，是另一套东西。2026-09-19 主人在笔记本上
    #   正是被这句话误导，去点了「模型」按钮然后来问「是不是我这个模式没有」。
    ("唱歌", "点歌即播 41 首预录成品（已随包自带）；仅播放，不含翻唱制作工具", ["唱歌"]),
    ("读文档", "docx/pdf 解析", ["文档"]),
    ("图形控制台", "PySide6 界面——强烈建议", ["控制台"]),
    ("测试工具", "pytest 开发测试", ["测试"]),
    ("运维工具", "runtime_observer 观察器、体检/语音质检等脚本（选装）", ["运维"]),
]

# 功能 → 需要检查的目录
DIR_CHECKS = {
    0: [("models/BAAI/（BGE 向量模型 + bge-reranker-v2-m3）", BASE / "models" / "BAAI")],
    1: [("gpt-sovits/（语音引擎 + 模型）", BASE / "gpt-sovits"),
        ("asr_models/（语音识别模型，勾了语音就已自动下好）", BASE / "asr_models")],
    # 本地识图走 Ollama（agent/vision_local.py 连 127.0.0.1:11434），
    # 不读 models/minicpm-v/——那个目录是早期路线遗留，已不再使用（2026-09-18 核实）
    2: [],
    3: [("songs/（曲库）", BASE / "songs")],
    4: [],
    5: [],
    6: [],
}


# ─────────────────────────────────────────
# 依赖安装
# ─────────────────────────────────────────

def _plan(groups: list[str]) -> list[tuple[list[str], dict]]:
    plan = []
    for name in groups:
        g = GROUPS[name]
        if g["pkgs"]:
            plan.append((g["pkgs"], {}))
        for extra_pkgs, extra_kw in g.get("extra", []):
            plan.append((extra_pkgs, extra_kw))
    return plan


def _install(pkgs: list[str], kwargs: dict) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", *pkgs]
    for flag, val in kwargs.items():
        cmd.append(f"--{flag}")
        cmd.append(val)
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode == 0


def install_groups(groups: list[str], dry_run: bool = False) -> bool:
    """按组安装依赖。返回是否全部成功。soft 组（jieba-fast）失败仅警告不置失败。"""
    if not groups:
        print("没有选择任何组")
        return False
    plan = _plan(groups)
    soft = [p for g in groups for p in GROUPS[g].get("soft", [])]
    print(f"\n[·] 将安装 {len(groups)} 组、{sum(len(p) for p, _ in plan)} 个包"
          + (f" + {len(soft)} 个加速可选包" if soft else "") + "：")
    for pkgs, _ in plan:
        print(f"  - {' '.join(pkgs)}")
    if soft:
        print(f"  - {' '.join(soft)}（可选——失败仅警告，GPT-SoVITS 自动回退纯 jieba）")
    if dry_run:
        print("\n（--dry-run 预览模式，未实际安装）")
        return True
    ok = True
    for pkgs, kw in plan:
        if not _install(pkgs, kw):
            ok = False
            print(f"  [!] 安装失败: {pkgs}——请手动重试")
    for pkgs in soft:
        if not _install(pkgs, {}):
            print("  [i] jieba-fast 未装成功——如需加速请先装 VS2022 Build Tools（C++ 桌面开发）后重试；"
                  "不装也能用（语音自动回退纯 Python jieba，见 gpt-sovits/start_api_patched.py）")
    print("\n[√] 依赖安装完成" if ok else "\n[!] 部分安装失败，请检查上方输出")
    return ok


# ─────────────────────────────────────────
# 安装向导（环境/选择/目录/配置）
# ─────────────────────────────────────────

def check_environment() -> None:
    print("━━━ 环境检测 ━━━")
    v = sys.version_info
    py_ok = v[:2] == (3, 10)
    print(f"  Python: {v.major}.{v.minor}.{v.micro}{' [√] 符合建议（3.10）' if py_ok else '（建议 3.10，其他版本可能缺依赖兼容性）'}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        print(f"  FFmpeg: [√] {ffmpeg}")
    else:
        print("  FFmpeg: [×] 未找到——请先运行: winget install ffmpeg，重开终端后重试")
    # MSVC cl.exe（jieba-fast 源码编译前置；2026-09-05 M1d——普通终端 cl 不进 PATH，按固定路径扫描）
    cl_paths = sorted(
        p for root in ("C:/Program Files/Microsoft Visual Studio/2022",
                       "C:/Program Files (x86)/Microsoft Visual Studio/2022")
        for p in (Path(root) if Path(root).exists() else Path()).glob(
            "*/VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe"))
    if cl_paths:
        print(f"  MSVC cl.exe: [√] {cl_paths[0]}")
    else:
        print("  MSVC cl.exe: [!] 未找到 VS2022 C++ 工具链——jieba-fast 无预编译 wheel，需源码编译。")
        print("     影响：GPT-SoVITS 语音的分词加速包装不上；可用纯 Python jieba 回退（语音仍可启动，较慢）。")
        print("     如需加速：安装 VS2022 Build Tools → 勾选「使用 C++ 的桌面开发」（含 Windows SDK），然后重开终端再装 jieba-fast。")
    free_gb = shutil.disk_usage(BASE).free / 1e9
    print(f"  磁盘剩余: {free_gb:.1f} GB")
    if free_gb < 10:
        print("  [!] 磁盘空间紧张（<10GB），安装语音/唱歌可能失败")


def check_prerequisites() -> None:
    """必装前置——不能等装完才说。

    SnowLuma 是糖糖连 QQ 所需的协议端，属第三方项目：其 EULA 第 5.4 条禁止
    「将其并入第三方安装包」与「通过自动化脚本部署」，所以安装器不能随包发、
    也不能代下，只能提前说清楚，让用户在我们下模型的这段时间里去准备。
    """
    print("\n━━━ 必装前置 ━━━")
    installed = sorted((BASE / "SnowLuma").glob("SnowLuma-v*"))
    if installed:
        print(f"  [√] SnowLuma: {installed[-1].name}")
    else:
        print("  [×] SnowLuma（QQ 协议端）——糖糖靠它连上 QQ，本机还没有")
        print("     它不属于本项目，许可协议也不允许随包分发或由安装器代下，")
        print("     需要你手动下载一次（一次性，约 100MB）：")
        print("       1. 打开 https://github.com/SnowLuma/SnowLuma/releases")
        print("       2. 下载 Windows x64 版（形如 SnowLuma-vX.Y.Z-win-x64.zip）")
        print("          [!] 别下带 -lite 的那个包——它不带 Node.js，解压了也起不来")
        print(f"       3. 解压到：{BASE / 'SnowLuma'}")
        print("          （解压后应出现 SnowLuma-vX.Y.Z-win-x64/ 文件夹）")
        print("     也可以用任意其他 OneBot 11 反向 WebSocket 实现替代。")
        print("     [i] 现在就可以去下——下面装依赖/下模型要等一会儿，正好并行。")


def choose_features(default: list[int] | None = None) -> list[int]:
    """default：发布版本的推荐组合（回车即采用，用户可另行输入覆盖）"""
    print("\n━━━ 功能选择（可多选）━━━")
    for i, (name, desc, _) in enumerate(FEATURES, 1):
        mark = " *" if default and (i - 1) in default else ""
        print(f"    {i}. {name} —— {desc}{mark}")
    print("    0. 全部安装")
    hint = "回车=采用 * 推荐" if default else "回车=跳过"
    try:
        raw = input(f"  输入编号（逗号分隔，如 1,5,6；{hint}）> ").strip()
    except (EOFError, KeyboardInterrupt):
        return list(default or [])
    if raw in ("0", "全部"):
        return list(range(len(FEATURES)))
    if not raw:
        return list(default or [])
    try:
        sel = [int(x) for x in raw.replace("，", ",").split(",") if x.strip()]
        return [i - 1 for i in sel if 1 <= i <= len(FEATURES)]
    except ValueError:
        print("  [!] 输入无效，按跳过处理")
        return []


def check_directories(choices: list[int]) -> None:
    """在模型获取之后跑——所以「缺失」意味着下载没成功，而不是要用户自己摆"""
    print("\n━━━ 目录/服务检测 ━━━")
    for i in choices:
        for name, path in DIR_CHECKS.get(i, []):
            if path.exists():
                print(f"  [√] {name}")
            else:
                print(f"  [!] {name}——尚未就位（上一步下载可能中断，重跑本工具可续传）")
    if 2 in choices:  # 识图 → 检查 Ollama
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434", timeout=2):
                print("  [√] Ollama 服务（11434 端口）")
        except Exception:
            print("  [×] Ollama 未运行——本地识图不可用；云端 qwen-vl 会兜底（无需处理）")
# ─────────────────────────────────────────
# 模型获取（自动下载 + 自动落位）
# ─────────────────────────────────────────
# 2026-09-18：发布三版本（文字/识图/完整）后新增。此前模型要么手敲命令、
# 要么自己解压到指定子目录——「下载即用」就卡在这一步。本层把落位规则
# 写死在代码里，用户只需双击 安装糖糖.bat。

RELEASE_REPO = "downtown20/tangtang-qq-bot"
# 扫全部 Release 汇总附件——语音分卷可能挂在任意一个 Release 下，
# 只查 /releases/latest 会漏
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page=50"

# 默认预勾选的功能（2026-09-18 主人拍板）：
#   0 聊天+高质量记忆 · 3 唱歌 · 5 图形控制台
# 相当于原来的「文字版」。重活（语音 6G / 识图 2.5G）默认不勾——
# 别替用户决定下几个 G，让他自己选。
DEFAULT_FEATURES = [0, 3, 5]

# 语音识别模型（听懂别人的语音消息）。
# [!] 与 agent/asr.py 的 _SENSE_VOICE_MODEL / _SENSE_VOICE_URL 必须一致——
#   由 tests/test_installer_wiring.py 的契约断言机械保证，改一处会红。
# 为什么要预取：代码里是「首次遇到语音消息才懒加载下载」，可那一刻用户正在
# 对话中被卡住等 233M 下载。安装时先拿掉，才叫「下载的功能直接能用」。
ASR_MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
ASR_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{ASR_MODEL_NAME}.tar.bz2"
)

# 功能下标 → 模型清单
#   (显示名, 目标位置(相对项目根；空=特殊处理), 获取方式, 参数, 体积, 模式)
#   模式: "auto" = 直接下载（失败仅告警）
#         "ask"  = 下载前询问——体积大且缺了只降级不影响可用（如 2.2G 重排序）
MODELS = {
    0: [
        ("BGE 中文语义向量模型——记忆/知识库语义搜索的根基",
         "models/BAAI/bge-small-zh-v1.5", "modelscope",
         {"repo": "BAAI/bge-small-zh-v1.5"}, "184M", "auto"),
        ("BGE 重排序模型——混合检索精排；缺则召回质量下降，不影响聊天",
         "models/BAAI/bge-reranker-v2-m3", "modelscope",
         {"repo": "BAAI/bge-reranker-v2-m3"}, "2.2G", "ask"),
    ],
    1: [
        ("GPT-SoVITS 语音推理集——引擎底模 + 糖糖声线 + 情绪参考音频",
         "gpt-sovits", "release_parts",
         {"prefix": "tangtang-voice"}, "6.4G", "auto"),
        ("语音识别模型——听懂别人发的语音消息",
         "asr_models", "tarbz2",
         {"url": ASR_MODEL_URL, "expect": ASR_MODEL_NAME}, "233M", "auto"),
    ],
    2: [
        ("MiniCPM-V 本地识图模型——经 Ollama 调用（不装则用云端 qwen-vl）",
         "", "ollama", {"model": "minicpm-v"}, "2.5G", "auto"),
    ],
    # 唱歌无下载项（2026-09-18）：41 首预录成品已随版本包自带，播放走标准库
    # （agent/songs.py 只 import random/logging/pathlib），不需要任何模型或额外包。
}

# 子进程执行 modelscope 下载——刚 pip 装上的包当前解释器看不见，必须新起进程
_MODELSCOPE_SNIPPET = (
    "import sys\n"
    "from modelscope import snapshot_download\n"
    "snapshot_download(sys.argv[1], cache_dir=sys.argv[2])\n"
)


def _sizeof(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.2f}GB"


def _download_file(url: str, dest: Path, label: str) -> bool:
    """HTTP 下载（断点续传）。失败返回 False，不抛异常——由调用方决定是否致命。"""
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "tangtang-installer"})
    if done:
        req.add_header("Range", f"bytes={done}-")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if done and getattr(resp, "status", 200) != 206:
                done = 0          # 服务端不支持断点——从头来
            total = done + int(resp.headers.get("Content-Length") or 0)
            print(f"    [↓] {label}" + (f"（{_sizeof(total)}）" if total else ""))
            with open(part, "wb" if done == 0 else "ab") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r      {done * 100 // total}%  ({_sizeof(done)}/{_sizeof(total)})",
                              end="", flush=True)
            print()
    except Exception as e:
        print(f"\n    [!] 下载中断（重跑本工具会自动续传）: {e}")
        return False
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return True


def _fetch_modelscope(repo: str, target: Path, label: str) -> bool:
    """modelscope 下载。cache_dir 的 <命名空间>/<模型名> 布局天然就是代码期望的落点，无需搬运。"""
    if (target / "config.json").exists():
        print(f"    [√] 已就绪，跳过：{label}")
        return True
    print(f"    [↓] {label}（modelscope）")
    proc = subprocess.run(
        [sys.executable, "-c", _MODELSCOPE_SNIPPET, repo, str(BASE / "models")],
        cwd=str(BASE))
    if proc.returncode != 0 or not (target / "config.json").exists():
        # modelscope 某些版本把点号转义成 ___，embeddings.py 两种都认
        alt = target.with_name(target.name.replace(".", "___"))
        if (alt / "config.json").exists():
            print(f"    [√] 已就绪：{alt.relative_to(BASE)}")
            return True
        print("    [!] 下载未完成——请检查网络后重跑；也可稍后手动补装")
        return False
    print(f"    [√] 已放置：{target.relative_to(BASE)}")
    return True


def _release_assets() -> dict:
    """全部 Release 的 附件名 → 下载地址（三个版本的附件合并成一张表）"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(RELEASE_API, timeout=20) as r:
            releases = json.load(r)
    except Exception as e:
        print(f"    [!] 读取 Release 附件列表失败: {e}")
        return {}
    out = {}
    for rel in releases if isinstance(releases, list) else []:
        for a in rel.get("assets", []):
            out[a["name"]] = a["browser_download_url"]
    return out


def _extract_tar_safely(tar, target: Path) -> None:
    """安全解压：拒绝绝对路径、`..` 穿越、符号链接与设备文件。

    [!] Python 3.10 的 `extractall()` **没有 `filter` 参数**（3.12 才加），
    默认完全信任压缩包内容。模型是从 GitHub Releases 拉的 tar.bz2——
    处于中间人环境或上游 Release 被替换时，一个含 `../../启动/x.bat`
    或符号链接条目的小包就能一路写到目标目录之外。
    2026-09-19 独立安全审计把这条列为唯一带本地代码执行潜力的面。

    [!] 与 `agent/asr.py` 的同名函数**必须保持一致**——两处都是同一类压缩包，
    闸门 tests/test_release_sanitizer.py 会比对两份实现（改一处漏一处会红）。
    """
    base = Path(target).resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"压缩包含非法条目（链接/设备文件）：{member.name}")
        dest = (base / member.name).resolve()
        if base != dest and base not in dest.parents:
            raise ValueError(f"压缩包含路径穿越条目：{member.name}")
    tar.extractall(target)


def _unzip_into(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)


def _fetch_release_asset(asset: str, target: Path, label: str) -> bool:
    assets = _release_assets()
    url = assets.get(asset)
    if not url:
        print(f"    [!] Release 里找不到附件 {asset}")
        return False
    tmp = BASE / "temp_files" / asset
    if not _download_file(url, tmp, f"{label}（{asset}）"):
        return False
    print("    [~] 解压…")
    target.mkdir(parents=True, exist_ok=True)
    _unzip_into(tmp, target)
    tmp.unlink(missing_ok=True)
    print(f"    [√] 已放置：{target.relative_to(BASE)}")
    return True


def _fetch_release_parts(prefix: str, target: Path, label: str) -> bool:
    """分卷附件：全部下载后依次解压到同一目录（自动，无需人工排序）"""
    assets = _release_assets()
    parts = sorted(n for n in assets if n.startswith(prefix))
    if not parts:
        print(f"    [!] Release 里找不到 {prefix}* 分卷")
        return False
    print(f"    [i] 共 {len(parts)} 卷，逐卷下载解压（断点续传，中断可重跑）")
    tmpdir = BASE / "temp_files" / "parts"
    for name in parts:
        tmp = tmpdir / name
        if not _download_file(assets[name], tmp, f"{label} {name}"):
            return False
        print("    [~] 解压…")
        target.mkdir(parents=True, exist_ok=True)
        _unzip_into(tmp, target)
        tmp.unlink(missing_ok=True)
    print(f"    [√] 已放置：{target.relative_to(BASE)}")
    return True


def _fetch_tarbz2(url: str, target: Path, expect: str, label: str) -> bool:
    """下 tar.bz2 并解压到 target——与 agent/asr.py 的落位规则一致。

    asr.py 解压时用 `expect`（顶层目录名）来找模型文件，所以这里也必须
    让它落到 target/<expect>/ 下。
    """
    if (target / expect).is_dir():
        print(f"    [√] 已就绪，跳过：{label}")
        return True
    tmp = BASE / "temp_files" / f"{expect}.tar.bz2"
    if not _download_file(url, tmp, label):
        return False
    print("    [~] 解压…")
    target.mkdir(parents=True, exist_ok=True)
    try:
        import tarfile
        with tarfile.open(tmp, "r:bz2") as tar:
            _extract_tar_safely(tar, target)
    except Exception as e:
        print(f"    [!] 解压失败: {e}")
        tmp.unlink(missing_ok=True)
        return False
    tmp.unlink(missing_ok=True)
    if not (target / expect).is_dir():
        print(f"    [!] 解压完成但没找到 {expect}/——请检查压缩包结构")
        return False
    print(f"    [√] 已放置：{target.relative_to(BASE)}/{expect}")
    return True


def _fetch_ollama(model: str, label: str) -> bool:
    if not shutil.which("ollama"):
        print("    [!] 未检测到 Ollama——本地识图靠它跑，装不了就跳过这一步")
        print("      识图有两条路，任选一条即可用：")
        print("        · 云端：控制台把「识图方式」选云端，填阿里云千问 API Key（零下载，推荐先这么用）")
        print("        · 本地：装 Ollama（https://ollama.com/download）后重跑本工具，")
        print("                它会自动 pull 识图模型")
        return False
    print(f"    [↓] {label}（ollama pull {model}）")
    if subprocess.run(["ollama", "pull", model]).returncode != 0:
        print("    [!] Ollama 拉取失败——稍后可手动执行 ollama pull " + model)
        return False
    print("    [√] 本地识图模型就绪")
    return True


def _ask_optional(specs: list) -> set[str]:
    """下载前先问可选大模型——别让用户等完 2.2G 才后悔。

    非交互环境（无 stdin）按「不跳过」处理：宁可多下，也别静默删功能。
    """
    asks = [s for s in specs if s[5] == "ask"]
    if not asks:
        return set()
    print("\n  [!] 以下模型体积较大，但缺了只影响质量、不影响能不能用：")
    for name, _t, _k, _p, size, _m in asks:
        print(f"      · {name}  [{size}]")
    try:
        raw = input("    跳过它们以节省下载量？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if raw in ("y", "yes"):
        print("    → 已跳过（以后想要：重跑本工具即可补装）")
        return {s[0] for s in asks}
    print("    → 一并下载")
    return set()


def fetch_models(choices: list[int], dry_run: bool = False) -> list[str]:
    """按功能选择下载并落位模型。返回未能就位的模型名。"""
    specs = [s for i in choices for s in MODELS.get(i, [])]
    if not specs:
        return []
    print("\n━━━ 模型获取（自动下载并放到位，无需手动解压）━━━")
    skip = set() if dry_run else _ask_optional(specs)
    missing = []
    for name, target_rel, kind, params, size, _mode in specs:
        if name in skip:
            print(f"\n  · {name}  [{size}]  [>] 已跳过")
            continue
        print(f"\n  · {name}  [{size}]")
        if dry_run:
            print("    （--dry-run：未实际下载）")
            continue
        target = BASE / target_rel if target_rel else None
        if kind == "modelscope":
            ok = _fetch_modelscope(params["repo"], target, name)
        elif kind == "release":
            ok = _fetch_release_asset(params["asset"], target, name)
        elif kind == "release_parts":
            ok = _fetch_release_parts(params["prefix"], target, name)
        elif kind == "tarbz2":
            ok = _fetch_tarbz2(params["url"], target, params["expect"], name)
        elif kind == "ollama":
            ok = _fetch_ollama(params["model"], name)
        else:
            ok = False
        if not ok:
            missing.append(name)
    return missing


def bootstrap_config(dry_run: bool) -> bool:
    """首次安装时由 config.example.yaml 生成 config.yaml。

    此前用户必须手动「复制 config.example.yaml 改名」——「下载即用」的又一处手工步骤。
    返回 True 表示本次新建了配置（后续接线无需再征求同意）。
    """
    cfg, example = BASE / "config.yaml", BASE / "config.example.yaml"
    if cfg.exists() or not example.is_file():
        return False
    print("\n━━━ 生成配置 ━━━")
    if dry_run:
        print("  （--dry-run：未实际生成 config.yaml）")
        return True
    try:
        shutil.copy2(example, cfg)
        print("  [√] 已生成 config.yaml")
        return True
    except OSError as e:
        print(f"  [!] 自动生成失败: {e}——请手动复制 config.example.yaml 为 config.yaml")
        return False


# 功能下标 → 需要打开的配置开关（2026-09-18 接线契约）
#   voice.enabled     说（TTS，糖糖发语音条）
#   voice.asr_enabled 听（别人发的语音转文字）
#   voice.provider    决定启动不启动本地 TTS 服务（agent/handler.py: _is_full_mode）
#   llm.vision.enabled 识图总开关
#
# [!] 这张表是「勾了就一定能用」的实现。历史上安装器只会把功能**关掉**、从不会打开，
#   导致用户勾了语音、6G 模型下完、启动后糖糖一声不吭。
VOICE_FEATURE = 1
VISION_FEATURE = 2


def _config_switches(choices: list[int]) -> dict[tuple[str, ...], object]:
    """把功能勾选翻译成配置开关（含「没勾就关掉」的反向接线）。

    键是配置路径元组——`llm.vision.enabled` 三段，不能按点号 split 后两段解包。
    """
    has_voice = VOICE_FEATURE in choices
    has_vision = VISION_FEATURE in choices
    return {
        ("voice", "enabled"): has_voice,
        ("voice", "asr_enabled"): has_voice,   # 听与说同属「语音」功能
        # 没装引擎就切走 provider——否则每次启动都会去拉一个不存在的本地 TTS 服务
        ("voice", "provider"): "gpt-sovits" if has_voice else "edge-tts",
        ("llm", "vision", "enabled"): has_vision,
    }


def _cfg_node(data: dict, path: tuple[str, ...]) -> dict:
    """按路径逐层 setdefault，返回叶子所在的字典"""
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    return node


def wire_config(choices: list[int], fresh: bool, dry_run: bool) -> None:
    """按功能勾选把配置开关拨到位——勾了就一定能用，没勾就干净地关掉。

    刚生成的配置直接改；已有配置先给用户看要改什么再问——不静默改别人的设置。
    """
    cfg = BASE / "config.yaml"
    if not cfg.exists():
        return
    print("\n━━━ 配置接线（勾选的功能开箱即用）━━━")
    try:
        import yaml
    except ImportError:
        print("  [!] 缺少 pyyaml，跳过接线——请自行确认 config.yaml 里的功能开关")
        return
    want = _config_switches(choices)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    changes = []
    for path, value in want.items():
        node = _cfg_node(data, path)
        field = path[-1]
        if node.get(field) != value:
            changes.append((".".join(path), node.get(field), value))
            node[field] = value
    if not changes:
        print("  [√] 开关已与本次选择一致，无需调整")
        return
    print("  将修改：")
    for key, old, new in changes:
        print(f"    · {key}: {old} → {new}")
    if dry_run:
        print("  （--dry-run：未实际写入）")
        return
    if not fresh:
        try:
            ans = input("  是否按本次选择调整配置？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("  [>] 已跳过——配置保持原样（对应功能可能仍不可用）")
            return
    bak = cfg.with_name(f"config.yaml.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cfg, bak)
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    print(f"  [√] 已写入（原配置备份为 {bak.name}）")


def _song_count() -> int:
    """曲库可识别多少首——与 agent/songs.py 的发现口径一致（两个来源），
    只数 audio/ 会漏掉 separation 完还没拷过去的那几首。

    原声要连 .mp3 一起认：开发机上是分离出来的无损 wav，
    发布包里是转码后的 mp3（同目录换后缀）。只认 wav 会在用户机器上少数 40 首。
    """
    songs = BASE / "songs"
    if not songs.is_dir():
        return 0
    stems = {p.stem for p in (songs / "audio").glob("*.wav")}
    stems |= {p.stem.removesuffix("_FINAL")
              for p in (songs / "covers" / "separated").glob("*_FINAL.*")
              if p.suffix.lower() in (".wav", ".mp3")}
    return len(stems)


def report_ready(choices: list[int]) -> None:
    """装完告诉用户「现在能做什么、还差什么」——避免装完一脸茫然"""
    print("\n━━━ 本次装好的功能 ━━━")
    has_voice, has_vision = VOICE_FEATURE in choices, VISION_FEATURE in choices
    print(f"  [√] 聊天 + 记忆{'（含语义检索）' if 0 in choices else ''}")
    if 3 in choices:
        print(f"  [√] 唱歌（曲库 {_song_count()} 首，点歌即播）")
    if 5 in choices:
        print("  [√] 图形控制台")
    print(f"  {'[√]' if has_voice else '[ ]'} 语音：听懂语音消息 + 糖糖发语音条"
          + ("" if has_voice else "（未勾选）"))
    print(f"  {'[√]' if has_vision else '[ ]'} 识图" + ("" if has_vision else "（未勾选）"))
    todo = []
    if not any((BASE / "SnowLuma").glob("SnowLuma-v*")):
        todo.append("SnowLuma 还没装（见开头的「必装前置」）——"
                    "去 github.com/SnowLuma/SnowLuma/releases 下 Windows x64 版解压到 SnowLuma/")
    if has_vision:
        todo.append("识图：控制台选「识图方式」——云端要填千问 API Key；"
                    "本地要装 Ollama（不装会自动走云端，不阻断）")
    if has_voice:
        todo.append("语音：第一次发语音时会启动 GPT-SoVITS 服务，首次启动较慢属正常")
    todo.append("填配置：机器人的 QQ 号、你的 QQ 号、模型 API Key（控制台「设置」页）")
    print("\n  还要做的事：")
    for i, t in enumerate(todo, 1):
        print(f"    {i}. {t}")

    not_chosen = [FEATURES[i][0] for i in range(len(FEATURES))
                  if i not in choices and i not in (6, 7)]
    if not_chosen:
        print(f"\n  本次没勾：{'、'.join(not_chosen)}")
        print("  以后想要 —— 重跑 安装糖糖.bat 勾上即可，模型/依赖会自动补齐，已装的不重来。")


def cmd_install(dry_run: bool) -> int:
    check_environment()
    check_prerequisites()
    choices = choose_features(DEFAULT_FEATURES)
    if not choices:
        print("  未选择任何功能，跳过")
        return 0
    groups = []
    for i in choices:
        for g in FEATURES[i][2]:
            if g not in groups:
                groups.append(g)
    print(f"\n━━━ 安装依赖（{len(groups)} 组）━━━")
    for g in groups:
        print(f"  · {g} — {GROUPS[g]['desc']}")
    if not install_groups(groups, dry_run):
        return 1
    # 顺序有讲究：先建配置 → 下模型 → 接线 → 报"还差什么"。
    # 接线必须在模型之后——模型没就位就打开开关，等于让用户对着报错找原因。
    fresh = bootstrap_config(dry_run)
    missing = fetch_models(choices, dry_run)
    check_directories(choices)
    wire_config(choices, fresh, dry_run)
    if missing:
        print("\n[!]  以下模型未能就位，对应功能会降级或不可用：")
        for m in missing:
            print(f"    · {m}")
        print("   重跑本工具可自动续传（已下好的不会重复下载）")
    if not dry_run:
        report_ready(choices)
    print("\n━━━ 完成 ━━━")
    print("  接下来：")
    print("    1. 双击 启动控制台.bat → 在「设置」页填：机器人的 QQ 号、你的 QQ 号、模型 API Key")
    print("    2. 控制台里启动 SnowLuma → 面板里注入并配好连接 → 启动糖糖")
    print("  （换机搬家：本工具菜单 2/3 打包/解包记忆快照）")
    return 0


# ─────────────────────────────────────────
# 打包项目（生成分享部署包）
# ─────────────────────────────────────────

# 打包时排除的目录名/文件名（任何层级）
_EXCLUDE_DIRS = {"__pycache__", ".git", "logs", "voice_cache", "generated_images",
                 "share_images", "audio", "venv", "cosyvoice3venv"}
_EXCLUDE_SUFFIXES = (".pyc", ".log", ".bak", ".previous")
_EXCLUDE_NAMES = ("memory.db", "memory.db-wal", "memory.db-shm", ".extraction_state.json")


def _collect_include_roots(choices: list[int]) -> list[Path]:
    """按功能选择计算要打包的根级项"""
    roots = ["agent", "main.py", "config.yaml", ".env",
             "role_card.md", "role_card_murasame.md", "role_card_michele.md",
             "scenarios", "knowledge", "SnowLuma", "onebot",
             "stickers", "stickers_cg", "stickers_murasame", "stickers_michele",
             "tools", "安装糖糖.bat", "启动控制台.bat", "requirements.txt", ".stignore"]
    opt_roots = {
        0: ["models/BAAI"],            # 聊天+记忆 → BGE 向量模型
        1: ["gpt-sovits"],             # 语音
        2: ["models/minicpm-v"],       # 识图
        3: ["DiffSinger", "DiffSingerMiniEngine", "Retrieval-based-Voice-Conversion-WebUI",
            "RVC_dataset", "uvr5_models", "venv_demucs"],  # 唱歌
    }
    for i in choices:
        for r in opt_roots.get(i, []):
            if (BASE / r).exists():
                roots.append(r)
    return [BASE / r for r in roots]


def _add_path(zf: zipfile.ZipFile, src: Path):
    """把文件/目录加入 zip（跳过排除项）"""
    if src.is_file():
        if src.name in _EXCLUDE_NAMES or src.name.endswith(_EXCLUDE_SUFFIXES) \
                or ".sync-conflict-" in src.name:
            return
        zf.write(src, src.relative_to(BASE).as_posix())
        return
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(BASE)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.is_dir():
            continue
        if p.name in _EXCLUDE_NAMES or p.name.endswith(_EXCLUDE_SUFFIXES) \
                or ".sync-conflict-" in p.name:
            continue
        zf.write(p, rel.as_posix())


def cmd_package() -> int:
    print("━━━ 打包项目（生成分享部署包）━━━")
    print("  勾选目标机器需要的功能——决定带哪些可选目录（语音/识图/唱歌的模型可以不带）")
    choices = choose_features()
    if not choices:
        print("  未选择功能，跳过")
        return 0

    # 数据库快照：可选携带（不带则目标机解压后自行 打包/解包）
    snap = None
    snaps = sorted((BASE / "memory_sync").glob("memory-*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True) if (BASE / "memory_sync").exists() else []
    if snaps:
        raw = input(f"\n  包含记忆快照 {snaps[0].name}（{snaps[0].stat().st_size / 1e6:.0f}MB）？[Y/n] ").strip().lower()
        if raw not in ("n", "no"):
            snap = snaps[0]
            print("  [√] 将包含记忆快照")

    roots = _collect_include_roots(choices)
    print(f"\n  [·] 收集 {len(roots)} 个根项：")
    for r in roots:
        size = sum(p.stat().st_size for p in r.rglob("*") if p.is_file()) / 1e9 if r.is_dir() \
            else r.stat().st_size / 1e9
        print(f"    · {r.name if r != BASE else BASE}（{size:.1f} GB）" if size > 0.1 else f"    · {r.name}")
    print("  （已排除: __pycache__/日志/冲突文件/运行时产物/voice_cache 等）")

    out = BASE.parent / f"小糖糖部署包-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    print(f"\n  [~] 正在生成 {out.name}（大目录可能需要几分钟）…")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for src in roots:
            _add_path(zf, src)
            count += 1
        if snap:
            zf.write(snap, f"memory_sync/{snap.name}")

    size_gb = out.stat().st_size / 1e9
    print(f"  [√] 部署包已生成: {out}")
    print(f"     大小 {size_gb:.1f} GB，{count} 个根项")
    print()
    print("  目标机器解压后：")
    print("    1. 运行 安装糖糖.bat → 菜单 1 安装依赖")
    if snap:
        print("    2. 菜单 3 解包记忆（应用记忆快照）")
    print("    3. 启动控制台 → 启动 SnowLuma → 面板里配好连接 → 启动糖糖")
    print("  [!] 部署包含 .env（API 密钥），请勿外传")
    return 0


# ─────────────────────────────────────────
# 入口
# ─────────────────────────────────────────

def cmd_release() -> int:
    """发布项目——清理个人数据，生成分享文件夹（tools/准备发布.py）"""
    script = BASE / "tools" / "准备发布.py"
    if not script.exists():
        print("  [×] 未找到 tools/准备发布.py")
        return 1
    print("  [·] 准备发布包（清理个人数据，输出到 项目上级目录/小糖糖-发布/）…")
    return subprocess.run([sys.executable, str(script)], cwd=str(BASE)).returncode


def cmd_pack(if_newer: bool) -> int:
    return _sync.cmd_pack(if_newer)


def cmd_unpack(machine: str | None) -> int:
    return _sync.cmd_unpack(machine)


def cmd_check() -> int:
    check_environment()
    print()
    return _sync.cmd_check()


def cmd_health() -> int:
    """体检——换机功能对比（[×]=缺失 [!]=留意；两台机器各跑一遍逐行对照）"""
    script = BASE / "tools" / "体检.py"
    if not script.exists():
        print(f"  [×] 未找到 {script}")
        return 1
    print("  [·] 运行体检…")
    return subprocess.run([sys.executable, str(script)], cwd=str(BASE)).returncode


def _load_singing_tool():
    """加载 tools/歌唱组件.py——控制台用的是同一份（避免两处实现漂移）。

    [!] 2026-09-20：本模块的 docstring 原先就写着「安装器会用它」，但安装器
    其实从没加载过它（复核时 grep 全文确认：只有一句注释提到）。现在真的接上了，
    由 tests/test_singing_component.py::test_installer_actually_uses_the_shared_module 钉住。
    """
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "singing_tool", Path(__file__).resolve().parent / "歌唱组件.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_singing() -> int:
    """安装翻唱制作组件：模型包（Release 附件）+ demucs 环境（本机现建）。"""
    print("━━━ 翻唱制作组件 ━━━")
    print("  做新翻唱时才需要。点歌播放的 41 首成品已随包自带，不需要它。\n")
    try:
        mod = _load_singing_tool()
    except Exception as e:                                       # noqa: BLE001
        print(f"  [x] 加载 tools/歌唱组件.py 失败：{e}")
        return 1
    st = mod.status()
    if st["ready"]:
        print("  [√] 翻唱组件已经齐了，无需安装")
        return 0
    ok = mod.install_all()
    if ok:
        print("\n  装好了。打开控制台 → 设置 → 曲库 → 歌唱工作室即可使用。")
    return 0 if ok else 1


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args:
        while True:
            print("小糖糖 安装/维护工具")
            print("   第一次用 → 选 1「安装依赖」：勾需要的功能，模型会自动下载并放到正确位置")
            print("   已经装过 → 直接双击 启动控制台.bat")
            print()
            print("  1. 安装依赖 — 功能选择安装（含环境/目录检测、配置调整）")
            print("  2. 打包记忆 — 手动导出快照（自动打包失败/想立即切机时用；平时自动进行）")
            print("  3. 解包记忆 — 手动应用快照（自动解包被分叉拦截/需指定机器时用；平时自动进行）")
            print("  4. 打包项目 — 生成部署 zip（给自己笔记本用，按功能瘦身）")
            print("  5. 发布项目 — 清理个人数据后生成分享文件夹（发给别人）")
            print("  6. 检查状态 — 环境 + 快照总览")
            print("  7. 体检 — 换机功能对比（[×]=缺失 [!]=留意，两台机器各跑一遍对照）")
            print("  8. 翻唱组件 — 做新翻唱用（约 2 G；只听歌不需要）")
            print("  0. 退出")
            try:
                raw = input("\n  选择 > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if raw == "1":
                cmd_install(dry_run)
            elif raw == "2":
                _sync.cmd_pack(True)
            elif raw == "3":
                _sync.cmd_unpack(None)
            elif raw == "4":
                cmd_package()
            elif raw == "5":
                cmd_release()
            elif raw == "6":
                cmd_check()
            elif raw == "7":
                cmd_health()
            elif raw == "8":
                cmd_singing()
            else:
                break
            try:
                input("\n  （回车返回菜单…）")
            except (EOFError, KeyboardInterrupt):
                break
        return 0

    cmd = args[0]
    if cmd in ("安装", "install"):
        return cmd_install(dry_run)
    if cmd in ("打包", "pack"):
        return _sync.cmd_pack("--if-newer" in args or dry_run)
    if cmd in ("解包", "unpack"):
        machine = args[1] if len(args) > 1 else None
        return _sync.cmd_unpack(machine)
    if cmd in ("项目", "package", "share"):
        return cmd_package()
    if cmd in ("发布", "release"):
        return cmd_release()
    if cmd in ("检查", "check"):
        return cmd_check()
    if cmd in ("体检", "health"):
        return cmd_health()
    if cmd in ("翻唱", "singing"):
        return cmd_singing()
    # 兼容旧用法：直接指定依赖组
    return 0 if install_groups([cmd], dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
