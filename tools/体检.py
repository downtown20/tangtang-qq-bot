#!/usr/bin/env python3
"""小糖糖 换机体检 [·]

项目文件经 Syncthing 同步后，在两台机器各自运行本脚本，
逐行对比输出——差异就是「功能缺失」或「状态不一致」的地方。

    python tools/体检.py

检查维度：
  1. Python 3.10 解释器自动发现与关键路径（壁纸）
  2. Python 环境（版本 + 关键依赖 import）
  3. 模型与资源（BGE/情绪模型/ASR/GPT-SoVITS/CosyVoice/曲库/贴图/知识库）
  4. 外部服务端口（NapCat 3000 / GPT-SoVITS 9880 / DiffSinger 9266 / CosyVoice 9267 / Ollama 11434）
  5. 数据状态（memory.db 完整性 / memory_sync 快照新旧 / 状态跟随文件）
  6. GPU（CosyVoice 3 需要 NVIDIA 独显；gpt-sovits 可 CPU）
  7. 密钥（.env 变量是否覆盖 config.yaml 的 ${...} 引用）
  8. GitNexus 索引新鲜度（索引落后源码时 impact/context 分析会漏新代码）

说明：
  - 端口项是运行时检查——糖糖未启动时端口关闭是正常现象（main.py 会自动拉起语音服务）
  - 本脚本不读取、不打印任何密钥值，只检查变量名是否存在
  - 换机纪律见 tools/同步记忆.py：打包 → Syncthing 同步 → 解包 → 体检 → 启动
"""

import importlib
import re
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 强制 UTF-8 输出：控制台重定向时 Python 退回 GBK，print 的 emoji 会炸
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))  # 保证任意 cwd 下能 import agent 包
from agent.paths import find_python310
from agent.knowledge import discover_document_files

# 关键依赖 → 功能映射
CRITICAL_DEPS = {
    "torch": "模型推理（BGE/情绪/GPU）",
    "jieba": "中文分词（知识库/插话/记忆提取）",
    "websockets": "SnowLuma WebSocket 接入",
    "httpx": "HTTP 客户端（语音/搜索/图片）",
    "yaml": "config.yaml 解析",
    "numpy": "向量运算",
    "transformers": "BGE/reranker/情绪模型加载",
    "jinja2": "提示词模板",
    "dotenv": ".env 密钥加载",
}

# 可选依赖——缺失时对应功能降级，不影响启动（用 [!] 不用 [×]）
OPTIONAL_DEPS = {
    "sherpa_onnx": "语音识别（语音消息转文字）",
    "pilk": "SILK v3 解码（QQ 语音转文字，2026-08-24 接入）",
    "edge_tts": "Edge-TTS 语音输出辅助",
    "soundfile": "语音音频读写辅助",
    "psutil": "runtime_observer 运行观察器",
    "PySide6": "糖糖图形控制台（不影响机器人核心运行）",
    "jieba_fast": "GPT-SoVITS 分词加速（C 扩展，需 MSVC 编译；缺失自动回退纯 Python jieba，见 start_api_patched.py）",
    "matplotlib": "GPT-SoVITS 启动依赖（lr_schedulers.py 顶层导入；缺失则语音服务无法启动）",
    "pyflakes": "开发检查工具（pyflakes undefined 门禁）",
}

PORTS = {
    3000: "NapCat（QQ 接入）",
    9880: "GPT-SoVITS（语音）",
    9266: "DiffSinger MiniEngine（点歌）",
    9267: "CosyVoice 3（备用语音）",
    11434: "Ollama（本地识图，可选）",
}

# 经 Syncthing 同步的「状态跟随」文件——.stignore 未排除（2026-08-14 盘点；
# 2026-08-15 移除 moments.json——动态功能已废弃，文件从未被写过）
FOLLOW_STATE_FILES = [
    ".conversation_state.json", ".reflection_state.json", ".fact_extraction_state.json",
    ".birthday_sent.json", ".catch_up_sent.json", ".greeting_state.json",
    ".preferences.json",
]


def _ts(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
    except OSError:
        return "?"


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _env_keys() -> set[str]:
    env = BASE / ".env"
    keys: set[str] = set()
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line.strip())
            if m:
                keys.add(m.group(1))
    return keys


def _sec(title: str):
    print(f"\n{'=' * 58}\n {title}\n{'=' * 58}")


def check_paths():
    _sec("[1] 路径与 Python 3.10 解释器")
    print(f"  本机项目路径: {BASE}")
    py310 = find_python310()
    if py310:
        print(f"  [√] Python 3.10 解释器（自动发现）: {py310}")
    else:
        print("  [!] 未找到 Python 3.10 解释器 → CosyVoice 不可用（可设 PYTHON310 环境变量指定）")

    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        bgimg = (cfg.get("appearance") or {}).get("background_image", "")
        if bgimg:
            # 2026-08-15：壁纸支持相对路径——按项目根解析
            bg_path = Path(bgimg) if Path(bgimg).is_absolute() else BASE / bgimg
            print(f"  {'[√]' if bg_path.exists() else '[!]'} 控制台壁纸: {bgimg}")
    except Exception as e:
        print(f"  [!] config.yaml 解析失败: {e}")


def check_python():
    _sec("[2] Python 环境")
    print(f"  解释器: {sys.executable}")
    print(f"  版本: {sys.version.split()[0]}")
    for mod, why in CRITICAL_DEPS.items():
        try:
            importlib.import_module(mod)
            print(f"  [√] {mod:22s} {why}")
        except Exception:
            print(f"  [×] {mod:22s} {why} ← 缺失，相关功能不可用")
    for mod, why in OPTIONAL_DEPS.items():
        try:
            importlib.import_module(mod)
            print(f"  [√] {mod:22s} {why}（可选）")
        except Exception:
            print(f"  [!] {mod:22s} {why}（可选）← 缺失，功能降级")


def check_resources():
    _sec("[3] 模型与资源")
    checks = [
        ("BGE 向量模型", ["models/BAAI/bge-small-zh-v1.5", "models/BAAI/bge-small-zh-v1___5",
                          "models/BAAI/bge-reranker-v2-m3"]),
        ("情绪模型 StructBERT", ["models/iic/nlp_structbert_sentiment-classification_chinese-base"]),
        ("语音识别模型", ["asr_models"]),
        ("GPT-SoVITS 引擎", ["gpt-sovits/start_api_patched.py"]),
        ("CosyVoice3 引擎", ["cosyvoice3/tts_server.py"]),
        ("曲库", ["songs"]),
        ("贴图库", ["Imag"]),
        ("知识库", ["knowledge"]),
        ("SnowLuma 接入", ["SnowLuma"]),
        ("NapCat", ["napcat"]),
    ]
    for label, paths in checks:
        p = next((BASE / x for x in paths if (BASE / x).exists()), None)
        if p is None:
            print(f"  [×] {label:20s} 缺失")
            continue
        extra = ""
        if label == "曲库":
            extra = f"（{len(list(p.rglob('*.wav')))} 个 wav）"
        elif label == "知识库":
            # 与生产索引共用唯一文档发现边界；不能把敏感目录或非索引
            # 文件计入容量，否则体检数字会与实际 KnowledgeBase 不一致。
            extra = f"（{len(discover_document_files(p))} 个可索引文档）"
        elif label == "语音识别模型":
            extra = "（onnx: " + ("有" if list(p.rglob("*.onnx")) else "无") + "）"
        print(f"  [√] {label:20s} {p} {extra}")
    spk = BASE / "gpt-sovits" / "speakers"
    if spk.exists():
        print(f"  [i] GPT-SoVITS 音色目录: {len(list(spk.iterdir()))} 个条目")


def check_ports():
    _sec("[4] 外部服务端口（运行时检查）")
    print("  [i] 糖糖未运行时端口关闭是正常的——main.py 启动会自动拉起语音服务")
    print("  [i] 本机若配置为不使用本地语音/本地识图（如笔记本），语音与 Ollama 端口项可忽略")
    for port, label in PORTS.items():
        up = _port_open(port)
        print(f"  {'[√]' if up else '[ ]'} :{port:<5} {label}" + ("" if up else "（未运行）"))


def check_data():
    _sec("[5] 数据状态")
    db = BASE / "memory.db"
    if db.exists():
        ok = False
        try:
            conn = sqlite3.connect(str(db), timeout=30)
            ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            conn.close()
        except Exception:
            pass
        print(f"  {'[√]' if ok else '[×]'} memory.db {db.stat().st_size / 1e6:.0f} MB"
              f"（完整性 {'ok' if ok else '失败'}，{_ts(db)}）")
    else:
        print("  [!] memory.db 不存在——先在另一台机器 打包 并 解包（tools/同步记忆.py）")

    self_state = BASE / ".tangtang_self.json"
    if self_state.exists():
        print(f"  {'[√]'} .tangtang_self.json {self_state.stat().st_size / 1e3:.0f} KB（{_ts(self_state)}）")
    else:
        print("  [!] .tangtang_self.json 不存在（关系场/自我叙事会重新积累）")

    snaps = sorted((BASE / "memory_sync").glob("memory-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if snaps:
        print("  memory_sync 快照：")
        for i, p in enumerate(snaps):
            print(f"    {'← 最新' if i == 0 else '      '} {p.name}  {p.stat().st_size / 1e6:.0f} MB（{_ts(p)}）")
    else:
        print("  [!] memory_sync/ 无快照")

    present = sum((BASE / f).exists() for f in FOLLOW_STATE_FILES)
    print(f"  状态跟随文件（Syncthing 同步）: {present}/{len(FOLLOW_STATE_FILES)} 个存在")


def check_gpu():
    _sec("[6] GPU（CosyVoice 3 需要）")
    try:
        import torch
        cuda = torch.cuda.is_available()
        if cuda:
            name = torch.cuda.get_device_name(0)
            print(f"  [√] CUDA 可用: {name}")
        else:
            print("  [!] 无 CUDA → CosyVoice 不可用；GPT-SoVITS 可 CPU 但合成慢")
    except Exception:
        nv = shutil.which("nvidia-smi")
        print("  [!] torch 未安装，无法检测 CUDA" + ("" if nv else "；nvidia-smi 也不在 PATH"))
    ff = shutil.which("ffmpeg")
    print(f"  {'[√]' if ff else '[!]'} ffmpeg: {ff or '不在 PATH（tools/快速安装ffmpeg.py）'}")


def check_secrets():
    _sec("[7] 密钥与配置")
    keys = _env_keys()
    env_file = BASE / ".env"
    print(f"  {'[√]' if env_file.exists() else '[×]'} .env 存在（{len(keys)} 个变量，值不显示）")
    try:
        text = (BASE / "config.yaml").read_text(encoding="utf-8")
        refs = sorted(set(re.findall(r'\$\{(\w+)\}', text)))
        for r in refs:
            print(f"    {'[√]' if r in keys else '[×]'} ${{{r}}}")
    except Exception as e:
        print(f"  [!] config.yaml 读取失败: {e}")


def check_gitnexus_index():
    _sec("[8] GitNexus 索引新鲜度（代码智能）")
    idx = BASE / ".gitnexus" / "meta.json"
    if not idx.exists():
        print("  [!] 无 .gitnexus 索引——impact/context 代码分析不可用（可运行 npx gitnexus analyze 建立）")
        return
    src = [BASE / "main.py", BASE / "糖糖控制台_qt.py"]
    for d in ("agent", "napcat", "tools", "tests"):
        src += [p for p in (BASE / d).rglob("*.py") if "__pycache__" not in p.parts]
    newest = max((p.stat().st_mtime for p in src if p.exists()), default=0)
    days = (newest - idx.stat().st_mtime) / 86400
    if days > 0.5:
        print(f"  [!] GitNexus 索引落后源码约 {days:.0f} 天（索引 {_ts(idx)}）——impact/context 结论会漏新代码")
        print("    → 重建：node .gitnexus/run.cjs analyze")
    else:
        print(f"  [√] GitNexus 索引新鲜（{_ts(idx)}）")


def main():
    print(f"[·] 小糖糖换机体检 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    check_paths()
    check_python()
    check_resources()
    check_ports()
    check_data()
    check_gpu()
    check_secrets()
    check_gitnexus_index()
    print(f"\n{'=' * 58}")
    print("[i] 在两台机器各跑一遍，逐行对比。[×] 即缺失功能，[!] 即需要留意。")


if __name__ == "__main__":
    main()
    # 双击 .py 直接运行时保持窗口不闪退；管道/自动化调用（stdin 非终端）不阻塞
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n（按任意键退出…）")
        except (EOFError, KeyboardInterrupt):
            pass
