#!/usr/bin/env python3
"""小糖糖 换机体检 [·]

项目文件经 Syncthing 同步后，在两台机器各自运行本脚本，
逐行对比输出——差异就是「功能缺失」或「状态不一致」的地方。

    python tools/体检.py

检查维度：
  1. Python 3.10 解释器自动发现与关键路径（壁纸）
  2. Python 环境（版本 + 关键依赖 import）
  3. 模型与资源（BGE/情绪模型/ASR/GPT-SoVITS/曲库/贴图/知识库）
  4. 外部服务端口（NapCat 3000 / GPT-SoVITS 9880 / Ollama 11434）
  5. 数据状态（memory.db 完整性 / memory_sync 快照新旧 / 状态跟随文件）
  6. 算力（CUDA / Intel XPU / CPU 三档；
     GPT-SoVITS 无卡也能跑但**慢得多**——100 字实测 CPU 198 秒 vs GPU 5.4 秒）
  7. 密钥（.env 变量是否覆盖 config.yaml 的 ${...} 引用）
  8. GitNexus 索引新鲜度（索引落后源码时 impact/context 分析会漏新代码）

说明：
  - 端口项是运行时检查——糖糖未启动时端口关闭是正常现象（main.py 会自动拉起语音服务）
  - 本脚本不读取、不打印任何密钥值，只检查变量名是否存在
  - 换机纪律见 tools/同步记忆.py：打包 → Syncthing 同步 → 解包 → 体检 → 启动
"""

import importlib
import os
import re
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

# 强制 UTF-8 输出：控制台重定向时 Python 退回 GBK，print 的 emoji 会炸
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))  # 保证任意 cwd 下能 import agent 包
from agent.paths import (STATE_DIR, ensure_ffmpeg_on_path,  # noqa: E402  路径单一事实源
                          find_python310, find_msvc_cl,
                          has_jieba_fast_c_extension, state_path)
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
    "jieba_fast": "GPT-SoVITS 分词加速（C 扩展，需 MSVC 编译；缺失自动回退纯 Python jieba，见 start_api_patched.py）",
    "matplotlib": "GPT-SoVITS 启动依赖（lr_schedulers.py 顶层导入；缺失则语音服务无法启动）",
    "pyflakes": "开发检查工具（pyflakes undefined 门禁）",
}

PORTS = {
    3000: "NapCat（QQ 接入）",
    9880: "GPT-SoVITS（语音）",
    11434: "Ollama（本地识图，可选）",
}

# 经 Syncthing 同步的「状态跟随」文件——.stignore 未排除（2026-08-14 盘点；
# 2026-08-15 移除 moments.json——动态功能已废弃，文件从未被写过）
# 2026-10-15 T55-F：这批文件移进 状态/ 目录，名字不变、.stignore 的规则按新路径改写。
# **不能把 状态/ 整体忽略**——下面这 7 个正是「特意要同步」的那一批（私聊/群聊
# 活跃窗口、反思游标、已发记录、偏好），目录级忽略会让笔记本上的糖糖丢状态。
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


def _rel(p: Path) -> str:
    """相对项目根展示——状态/日志收进子目录后，输出要能看出文件到底在哪
    （2026-10-15 T55-F 根目录规范化）。"""
    try:
        return p.relative_to(BASE).as_posix()
    except ValueError:
        return str(p)


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


def check_installed_features():
    """这台机器装了哪几个功能（2026-09-20 加）。

    体检是**两台机器逐行对比**用的——而「A 机有语音、B 机没有」这种差异，
    在下面那张资源表里表现为一堆 `[×] 缺失`，看的人分不清那是**没装**
    还是**装坏了**。先把安装清单摆出来，后面每一行的「缺失」才有意义：
    装过却是 [×] 才是真问题，没勾过的 [×] 是预期。

    清单由安装器写（`tools/安装糖糖.py:write_manifest`）。
    """
    _sec("[0] 本次安装勾了哪些功能")
    try:
        from agent.installed import choices, installed_summary
        print(f"  {installed_summary()}")
        ch = choices()
        if ch is None:
            print("  [!] 没有 installed_features.json——老安装或直接拷贝的源码树。")
            print("      下面的「缺失」无法区分「没装」和「装坏了」。")
            print("      重跑一次 安装糖糖.bat 即可补上这份记录（不会重下已装的东西）。")
    except Exception as e:
        print(f"  [!] 读安装清单失败: {e}")


def check_paths():
    _sec("[1] 路径与 Python 3.10 解释器")
    print(f"  本机项目路径: {BASE}")
    py310 = find_python310()
    if py310:
        print(f"  [√] Python 3.10 解释器（自动发现）: {py310}")
    else:
        print("  [!] 未找到 Python 3.10 解释器 → 语音（GPT-SoVITS）不可用（可设 PYTHON310 环境变量指定）")

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

    # ── 编译工具链 + 「加速包是不是真的在加速」────────────────────────
    # 2026-10-14 补。换机时最容易漏的就是这两条：
    #   · 原来体检**一个字都不提 MSVC** —— 换到没装 VS 的电脑上，这件事在体检里
    #     完全不可见（安装器会报，体检不会），而它决定 jieba-fast 的 C 扩展能不能编出来。
    #   · 原来只判 import jieba_fast 能否成功 —— 但它的包体是纯 Python 外壳，
    #     真正的 C 扩展是顶层模块。只判前者会把「只有外壳」误报成「装好了」。
    cl = find_msvc_cl()
    if cl:
        print(f"  [√] {'MSVC cl.exe':22s} {cl}")
    else:
        print(f"  [!] {'MSVC cl.exe':22s} 未找到 VS2022 C++ 工具链（安装期前置，非运行时依赖）")
        print("        jieba-fast 装不上 → 回退纯 Python jieba"
              "（实测 450 字 0.21ms → 0.56ms，对合成总耗时无感）")
        print("        装法：VS2022 Build Tools → 勾选「使用 C++ 的桌面开发」（含 Windows SDK），重开终端再装")
    if has_jieba_fast_c_extension():
        print(f"  [√] {'jieba-fast C 扩展':22s} 在（真的在加速）")
    else:
        print(f"  [!] {'jieba-fast C 扩展':22s} 不在 —— 只有纯 Python 外壳，并不比普通 jieba 快")


def check_resources():
    _sec("[3] 模型与资源")
    # [!] 2026-09-20 拆开：原先「BGE 向量模型」和 reranker 挤在**一条**检查里，
    #   判据是 `next((x for x in paths if exists))` —— 三个路径任一存在就报 √。
    #   于是「只有 reranker、没有真正的 BGE」也会打 `[√] BGE 向量模型`，
    #   而实测能力正好相反：向量模型是根基（没有它语义检索全废），
    #   reranker 只是精排（缺了降级、不影响能跑）。（Code Review A-5）
    #   两个模型、两件事，就该是两行。
    # 第三列 = 「这是不是一个**模型**目录」。
    # 模型目录要判 config.json 才算就位（modelscope 下到一半会留下只有几个文件的目录）；
    # 资源目录（知识库/曲库/贴图/SnowLuma…）只要求存在——它们本来就没有 config.json。
    #
    # [!] 2026-09-20 我第一版把这一列漏了，于是 `knowledge/` 这种普通目录也被要求
    #   "有 config.json"，体检直接把知识库报成「缺失」——
    #   被 tests/test_health_diagnostics.py 当场抓住。两个不同的判据不能共用一个谓词。
    checks = [
        ("BGE 向量模型", ["models/BAAI/bge-small-zh-v1.5", "models/BAAI/bge-small-zh-v1___5"], True),
        ("BGE 重排序（可选）",
         ["models/BAAI/bge-reranker-v2-m3", "models/bge-reranker-v2-m3"], True),
        ("情绪模型 StructBERT（可选）",
         ["models/iic/nlp_structbert_sentiment-classification_chinese-base"], True),
        ("语音识别模型", ["asr_models"], False),
        ("GPT-SoVITS 引擎", ["gpt-sovits/start_api_patched.py"], False),
        ("曲库", ["songs"], False),
        ("贴图库", ["Imag"], False),
        ("知识库", ["knowledge"], False),
        ("SnowLuma 接入", ["SnowLuma"], False),
        # [!] 原先这里还有一行 ("NapCat", ["napcat"])。项目早已改用 SnowLuma
        #   （`配置NapCat.py` 都归档了，`配置SnowLuma.py` 才是活的），
        #   这个目录从来就没有过 → 每次体检稳定输出一行 `[×] NapCat 缺失`。
        #   删掉它不是因为难看，是因为**它会教用户忽略 `[×]`**——
        #   一个永远存在的假缺失，会让真的那几行也失去分量。
    ]

    def _usable(rel, is_model):
        p = BASE / rel
        if not p.exists():
            return False
        if not is_model:
            return True
        return p.is_file() or (p / "config.json").exists() or any(p.glob("*/config.json"))

    for label, paths, is_model in checks:
        p = next((BASE / x for x in paths if _usable(x, is_model)), None)
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

    self_state = state_path(".tangtang_self.json")
    if self_state.exists():
        print(f"  {'[√]'} {_rel(self_state)} {self_state.stat().st_size / 1e3:.0f} KB（{_ts(self_state)}）")
    else:
        print(f"  [!] {_rel(self_state)} 不存在（关系场/自我叙事会重新积累）")

    snaps = sorted((BASE / "memory_sync").glob("memory-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    if snaps:
        print("  memory_sync 快照：")
        for i, p in enumerate(snaps):
            print(f"    {'← 最新' if i == 0 else '      '} {p.name}  {p.stat().st_size / 1e6:.0f} MB（{_ts(p)}）")
    else:
        print("  [!] memory_sync/ 无快照")

    present = sum(state_path(f).exists() for f in FOLLOW_STATE_FILES)
    print(f"  状态跟随文件（Syncthing 同步）: {present}/{len(FOLLOW_STATE_FILES)} 个存在"
          f"（{_rel(STATE_DIR)}/）")


def _torch_tier() -> tuple[str, str]:
    """→ (档位名, torch 版本)。**轮子后缀是「装的到底是哪一档」的唯一硬证据。**

    [!] devices.best_device() 答的是「现在能不能用加速」，答不了「我装的是哪一档」：
    有卡却装了 +cpu 轮子时它照样报 CPU。用户手册 §0.5 承诺体检第 6 节能看出自己是
    哪一档（「装错 torch 不会崩，只会慢」说的就是这个形态），所以这里必须把轮子
    后缀单独报出来，不能只说「设备 = CPU」让用户以为机器不行。
    """
    from agent import devices as _devices
    tm = _devices._torch()
    if tm is None:
        return "未安装", "torch 导不进来"
    ver = getattr(tm, "__version__", "?")
    low = ver.lower()
    if "+cu" in low:
        return "CUDA", ver
    if "+xpu" in low:
        return "XPU", ver
    if "+cpu" in low:
        return "CPU", ver
    return "未知（轮子无后缀）", ver


def check_gpu():
    _sec("[6] 算力（CUDA / Intel XPU / CPU）")
    try:
        from agent import devices as _devices
        tier, ver = _torch_tier()
        print(f"  torch 档位：{tier}（{ver}）")
        dev = _devices.best_device()
        for _line in _devices.summary().splitlines():
            print(f"  {_line}")
        if dev == "cpu":
            # 2026-10-14 T52 实测（同一台机器、同一批中文文本，GPU 与 CPU 各跑一遍）：
            #   GPU  2 / 18 / 50 / 100 字 = 1.2 / 1.5 / 2.7 / 5.4 秒
            #   CPU  2 / 18 / 50 / 100 字 = 42.8 / 40.2 / 80.9 / 198.2 秒
            # 笔记本（Intel Arc 130T 核显）就属于这一档 —— 不是「不可用」，是「慢」。
            print("      · GPT-SoVITS 可用但慢：100 字实测 198 秒（有卡时 5.4 秒）")
            # 换档命令写在体检里，是因为「装错档」的用户最先跑的就是体检
            # （用户手册 §0.5「怎么知道自己是哪一档」指的就是这里）。
            print("      · 换档：python tools/安装糖糖.py 安装 --torch-variant auto"
                  "（自动探测本机设备选 cu / xpu / cpu；判定表见 requirements.txt 开头）")
            print("      · Intel 核显机器装 xpu 档后本项会自动变成 XPU（判据见 requirements.txt）")
    except Exception as e:
        nv = shutil.which("nvidia-smi")
        print(f"  [!] 无法探测算力（{type(e).__name__}: {e}）"
              + ("" if nv else "；nvidia-smi 也不在 PATH"))
    # 用 ensure_ffmpeg_on_path 而不是裸 shutil.which：机器级/用户级 PATH 只对**之后
    # 新启动**的进程生效，而体检往往跑在一个早期就启动的终端里——那时 PATH 还是旧的，
    # 会把「项目自带 ffmpeg 就位」误报成「不在 PATH」，甚至报出搬家前的旧盘符
    # （2026-10-30 迁移后实测：项目在 F:，体检却打印 D: 的 ffmpeg）。
    ff = ensure_ffmpeg_on_path()
    print(f"  {'[√]' if ff else '[!]'} ffmpeg: {ff or '不在 PATH（tools/快速安装ffmpeg.py）'}")


def check_secrets():
    """密钥与配置。

    [!] 2026-09-20 重写。原实现只做一件事：从 config.yaml 里 `re.findall(r'\\$\\{(\\w+)\\}')`
    把**已经是 `${VAR}` 形态**的引用抠出来，逐个问「.env 里有同名变量吗」。

    它的判据**恰好反了**——能看见的只有"形态正确"的那一种，
    而形态错的那一种（本次事故的形态）一个字都不报：

        老模板原样（`api_key: 你的DeepSeek或Claude的API密钥`）→ 0 条报告
        config.yaml 里物化成明文（`api_key: sk-realkey-123`）→ 0 条报告
        正常 `${DEEPSEEK_KEY}`（阳性对照）                     → 才报

    也就是说**越坏越安静**。体检是主人用来判断「我哪里缺」的工具，
    这一项恰恰看不见最该看见的那种坏法——CLAUDE.md 反模式 #30 的活标本。

    现在改成拿 `agent/secret_refs.py` 的判据问**解析后的配置**：
    「这四个密钥字段，运行期真的拿得到可用值吗」——形态对不对由那一层负责，
    这里只关心结果。同时保留原来的 `.env` 变量清单（它对排错有用）。
    """
    _sec("[7] 密钥与配置")
    keys = _env_keys()
    env_file = BASE / ".env"
    print(f"  {'[√]' if env_file.exists() else '[×]'} .env 存在（{len(keys)} 个变量，值不显示）")
    cfg_path = BASE / "config.yaml"
    try:
        from agent.secret_refs import (collapse_unresolved_secrets, normalize_secret_fields,
                                       secret_paths, unfilled_secrets)
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        # 与运行期同一套：先归一（老占位串 → ${VAR}），再解析，再判有没有值。
        # 不这么做就会重蹈覆辙——体检说"没问题"而糖糖说"没 Key"。
        #
        # [!] 解析要自己喂 env：体检**刻意不调 `load_dotenv()`**（它不该污染
        #   os.environ，也可能与正在跑的糖糖互相干扰）。取值口径与运行期对齐：
        #   `load_dotenv()` 默认不覆盖已有变量 → 等价于「.env 兜底，os.environ 优先」。
        env = {**(_read_env_file(BASE / ".env")), **os.environ}
        normalize_secret_fields(raw, env)
        resolved = _resolve_env_vars(raw, env)
        missing = {p: r for p, _, r in unfilled_secrets(resolved, env)}
        collapse_unresolved_secrets(resolved)
        for path, env_name in secret_paths(resolved):
            dotted = ".".join(path)
            if dotted in missing:
                print(f"    [×] {dotted:24s} → ${{{env_name}}}  {missing[dotted]}")
            else:
                print(f"    [√] {dotted:24s} → ${{{env_name}}}")
        if "llm.api_key" in missing:
            print("        -> 没有它糖糖一句也回不了。填法（任选）：")
            print("          ① 控制台 → 设置 →「密钥管理」→ 填「LLM API Key」→ 保存")
            print("          ② 直接编辑 .env，加一行 DEEPSEEK_KEY=你的Key")
    except Exception as e:
        print(f"  [!] 密钥检查失败: {e}")
    # 其余 ${VAR} 引用（web_search 的 key 之类）仍按老办法列一遍——
    # 它们不在 secret_paths 里，但填错一样会静默失效。
    try:
        text = cfg_path.read_text(encoding="utf-8")
        refs = sorted(set(re.findall(r"\$\{(\w+)\}", text)))
        others = [r for r in refs if r not in {n for _, n in _secret_names()}]
        for r in others:
            print(f"    {'[√]' if r in keys else '[×]'} ${{{r}}}")
    except Exception:
        pass


def _secret_names():
    """secret_paths 覆盖的那些变量名（上面已单独报过，别重复列）"""
    try:
        from agent.secret_refs import FIXED_SECRET_PATHS, PROVIDER_KEY_ENV
        return ([("llm.api_key", n) for n in PROVIDER_KEY_ENV.values()]
                + [(f"{'.'.join(p)}", n) for p, n in FIXED_SECRET_PATHS])
    except Exception:
        return []


def _read_env_file(path: Path) -> dict:
    """读 .env 成 {名: 值}。**不写 os.environ**（体检的既有纪律，见文件头）。"""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return out


def _resolve_env_vars(obj, env: dict | None = None):
    """`main._resolve_env_vars` 的本地副本（体检不 import main——那会拉起整条运行栈）。

    [!] 与 main.py **同一条规则**：只有**整串**形如 `${VAR}` 才替换，
    取不到就原样返回（正是这条"原样返回"让下游能把"变量没设"认出来）。
    两份实现是个漂移点，但那比体检把一个 QQ 机器人主程序 import 进来划算；
    `tests/test_health_diagnostics.py` 钉住两边规则一致。
    """
    env = os.environ if env is None else env
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v, env) for v in obj]
    if isinstance(obj, str):
        m = re.match(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", obj)
        if m:
            return env.get(m.group(1), obj)
    return obj


def check_gitnexus_index():
    _sec("[8] GitNexus 索引新鲜度（代码智能）")
    idx = BASE / ".gitnexus" / "meta.json"
    if not idx.exists():
        print("  [!] 无 .gitnexus 索引——impact/context 代码分析不可用（可运行 npx gitnexus analyze 建立）")
        return
    src = [BASE / "main.py", BASE / "运维" / "糖糖控制台_qt.py"]
    for d in ("agent", "napcat", "tools", "tests"):
        src += [p for p in (BASE / d).rglob("*.py") if "__pycache__" not in p.parts]
    newest = max((p.stat().st_mtime for p in src if p.exists()), default=0)
    days = (newest - idx.stat().st_mtime) / 86400
    if days > 0.5:
        print(f"  [!] GitNexus 索引落后源码约 {days:.0f} 天（索引 {_ts(idx)}）——impact/context 结论会漏新代码")
        print("    → 重建：node .gitnexus/run.cjs analyze")
    else:
        print(f"  [√] GitNexus 索引新鲜（{_ts(idx)}）")


def check_connection():
    """链路自检：QQ ← SnowLuma ← 糖糖 这一条线到底接上没有。

    复用的 `检查连接.py` 与控制台「连接自检」按钮**同一个实现**——
    新装/换机后最常见的失败是「协议端配好了但糖糖收不到」，而这里能把
    是雪糖哪一段断了一行一行列出来（含该填什么）。
    """
    _sec("[9] 链路自检（QQ ← SnowLuma ← 糖糖）")
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "conn_check", Path(__file__).resolve().parent / "检查连接.py")
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"  [!] 自检模块没加载起来（{type(e).__name__}: {e}）")
        return
    try:
        results = mod.check_all(BASE)
    except Exception as e:
        print(f"  [!] 自检跑失败（{type(e).__name__}: {e}）")
        return
    for r in results:
        mark = mod.GLYPH.get(r.status, "[?]")
        print(f"  {mark} {r.title}")
        # 汇总行只说结论——它的 next_step 与上面第一条 [×] 是同一句，重复打印反而看不清
        if r.status is not mod.Status.OK and r.next_step and r.key != mod.KEY_SUMMARY:
            print(f"        → {r.next_step}")


def main():
    print(f"[·] 小糖糖换机体检 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    check_installed_features()   # 放最前：后面每一行的「缺失」都要靠它才有意义
    check_paths()
    check_python()
    check_resources()
    check_ports()
    check_data()
    check_gpu()
    check_secrets()
    check_gitnexus_index()
    check_connection()
    print(f"\n{'=' * 58}")
    print("[i] 在两台机器各跑一遍，逐行对比。")
    print("    [×] = 这一项不在这台机器上。**结合 [0] 的安装清单看**：")
    print("        没勾过的功能缺了是预期；勾过却 [×] 才是要处理的。")
    print("    [!] = 就位了但有需要留意的地方。")


if __name__ == "__main__":
    main()
    # 双击 .py 直接运行时保持窗口不闪退；管道/自动化调用（stdin 非终端）不阻塞
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n（按任意键退出…）")
        except (EOFError, KeyboardInterrupt):
            pass
