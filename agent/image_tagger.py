# -*- coding: utf-8 -*-
"""图库打标 —— 把 `stickers_cg/_tag/tag_cg_ollama.py` 的能力搬进桌面端。

## 为什么要有这个模块（2026-10-26 第 63 轮）

主人原话：「图库没有调用模型打标签功能，该功能需要提供**本地、API 两种方式**，
本地模式我们只提供接口和必要的文件夹位置……然后将开关整合到桌面端
而不是用 `D:\\qq-小糖糖\\打标色图.bat`。」

翻译成三件事：

1. **界面上一键跑**：不用再双击 bat，也不用记 `--resume` / `--limit` 参数；
2. **两种 provider**：本机 Ollama（免费、离线）/ 云端 OpenAI 兼容视觉 API
   （不装模型也能用，但要 key）；
3. **参数与方式分家**（2026-10-27 主人反馈：「设置中的图库打标仅仅是调整背后参数、
   信息、模型，而非决定打标方式」）：

   | 在哪 | 管什么 |
   |---|---|
   | 设置 → 图库打标 | 用哪个模型、端点、key、缩图边长、超时（`provider` 也在那里） |
   | 图库页 | **打哪些图**（哪个库、全部/未打标/选中的）+ 改名 + 覆盖已有标签 |

   所以 `run()` 的三个关键参数（`dir_path` / `rename` / `overwrite`）一律
   **调用时传入**，不再只从 config 读；config 里那份只作为兜底默认值
   （老配置不写这三个键也能跑，向后兼容）。

## 为什么不重写「模型输出 → 标签」的解析

那段（`tag_cg_ollama.py:74-252`：`persons_of` / `strip_neg` / `ACT_RULES` /
`SOLO_RULES` / `EXPOSE_TAGS` / `extract_v2` / `make_rec_v2`）**就是打标质量的本体**——
单人不许挂双人体位、否定词要先剥、体位只认中文动作词……重写一遍必然两套真相漂移
（教训 #34：去重前先确认是不是同一个东西；这里是同一个东西，就该只有一份）。

所以本模块**复用**它的三件东西：

| 复用 | 作用 |
|---|---|
| `_encode_image(path, max_side)` | 缩到 768 再送模型——视觉 token 数直接决定解码耗时 |
| `PROMPT` | 质问方式（决定了模型吐出什么格式的描述） |
| `make_rec_v2(name, desc, old)` | 解析 + 生成 metadata 记录 + 规范文件名 |

副作用要如实写出来：那个脚本 import 时会把 `sys.stdout` 重配成 UTF-8、
并往 `sys.path` 塞项目根（`tag_cg_ollama.py:19-25`）。两者对本进程都**无害**
（UTF-8 正是 bridge 要的），所以不去改那个脚本——改它等于让 bat 那条路也冒风险。

## 边界

- **不改模型**：本地是 Ollama（原生 `/api/generate`），云端是 OpenAI 兼容
  `/chat/completions`（`image_url` + data URL）。别的协议不支持，要支持就加一个
  `_ask_xxx`，不要在这两个里塞分支。
- **不并发**：本地模型串行反而快（单卡排队），云端也就几百毫秒一张。
  并发要解决「metadata.json 的读改写竞态」，现在每张都落盘，串行最省心。
- **结果写在 `metadata.json`**，和现有脚本同一个文件、同一套字段——
  她（`agent/sticker.py`）读的就是这个，不能另开一份。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 项目根（`agent/` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: `config.yaml` 里这一节的默认值。**每一个都能在设置页改**，改完不用重启（每次读）。
#:
#: ⚠ 2026-10-27 起 `dir` / `rename` 的归属是**图库页**（「打哪些图」「打完怎么处理」），
#: 这里保留它们只是因为老配置里可能写着：调用时传参优先，不传才回落到这两条
#: （向后兼容，不是第二套真相——`run()` 的注释写清了优先级）。
DEFAULTS: dict = {
    # off = 不启用（图库页的按钮变灰）；ollama = 本机；api = 云端
    "provider": "off",
    # 图库目录，相对项目根。**兜底值**：图库页会按它当前选中的库传入 dir_path，
    # 只有在没传（老前端 / 直接调 API）时才用这里
    "dir": "stickers_cg",
    # —— 本地（Ollama）——
    "ollama_host": "http://127.0.0.1:11434",
    "ollama_model": "qwen3-vl:8b-instruct-q4_K_M",
    # —— 云端（OpenAI 兼容视觉 API）——
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_model": "qwen-vl-max",
    # 档位引用（2026-10-27）：填了就用「设置 → AI 模型」里那个档位的
    # base_url / model / key，覆盖上面三个扁平字段。留空 = 走老路（兼容）。
    "model_ref": "",
    # key 留空时读环境变量 IMAGE_TAG_API_KEY（推荐放 .env，别把 key 写进 config.yaml）
    "api_key": "",
    # —— 通用 ——
    "max_side": 768,
    "timeout_s": 300,
    # 兜底的改名默认值（图库页有独立的勾选框）。
    # 文件名参与她的语义索引，所以默认开；不想动文件名就在图库页关掉（标签照样落库）
    "rename": True,
}

#: 解析层脚本的**回落位置**（2026-10-27）。
#:
#: 只有 `stickers_cg` 自带 `_tag/tag_cg_ollama.py`，贴图库没有。但那段解析规则
#: （`persons_of` / `ACT_RULES` / `make_rec_v2`…）是**打标质量的本体**，跟「打哪个库」
#: 无关——所以目标目录里没有脚本时回落到这里，**仍然只有一份解析层**，
#: 不是「偷偷用简化版」（教训 #34：同一个东西就该只有一份）。
FALLBACK_SCRIPT_ROOT = PROJECT_ROOT / "stickers_cg"


#: 建索引时认的图片后缀（与脚本 `EXTS` 一致）
EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"})

_LOCK = threading.Lock()
_STATE: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "current": "",
    "error": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "cancel": False,
}
_THREAD: threading.Thread | None = None
#: 脚本路径 → 已加载的模块。按路径缓存：切库（CG / 贴图）不会互踩
_TAGGERS: dict[str, object] = {}


# ── 配置与路径 ────────────────────────────────────────────────

#: `${VAR}` 形态的密钥引用（与 `agent/secret_refs.py`、设置页的正则一致）
_REF_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


def _env_file_value(name: str) -> str:
    """从项目根的 `.env` 读一个键（不依赖进程环境）。

    **为什么不用 `os.environ`**：bridge 是独立进程，它有自己的 `_load_dotenv()`
    返回 dict、**不写 `os.environ`**。所以「密钥放 `.env`」这条既定做法在
    bridge 侧必须自己读文件，否则同一个 `.env` 在 bot 进程能读到、
    在 bridge 进程读不到——两套口径（教训 #38）。
    """
    try:
        raw = (PROJECT_ROOT / ".env").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return ""


def _resolve_secret(value) -> str:
    """`${VAR}` → 实际值（先进程环境，再 `.env`）；字面量原样返回。

    只有**整个字段**是一个引用时才替换（`${A}-${B}` 这种混合形态不支持，
    也没有任何配置这么写过）——与设置页/`normalize_secret_fields()` 同一条判据。
    """
    text = str(value or "").strip()
    match = _REF_PATTERN.match(text)
    if not match:
        return text
    name = match.group(1)
    return (os.environ.get(name) or "").strip() or _env_file_value(name)


def load_config() -> dict:
    """读 `config.yaml` 的 `image_tagging` 节，缺的用 `DEFAULTS` 补。

    **每次调用都重读**：设置页改完立刻生效比「改完要重启」友好得多，
    而且这个函数只在按钮点击/列表刷新时跑，开销可以忽略。
    读失败一律回落默认值——打标是可选功能，配置坏了不能拖垮图库页。

    2026-10-27 起多做两件事（都为了「统一到模型档位 / 统一到 .env」）：

    1. `model_ref` 存在时，用那个档位的 base_url / model / key **覆盖**
       `api_base` / `api_model` / `api_key`——图库打标不再各填一套连接信息。
    2. `api_key` 若是 `${VAR}` 引用，解析成实际值（先进程环境，再 `.env`）。
       bridge 进程不 load_dotenv，不解析就会把字面量 `${IMAGE_TAG_API_KEY}`
       当 key 发出去（401）。
    """
    cfg = dict(DEFAULTS)
    try:
        import yaml

        raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        section = raw.get("image_tagging")
        if isinstance(section, dict):
            for key, value in section.items():
                if key in cfg and value is not None:
                    cfg[key] = value
            # YAML 1.1 会把裸 `off` / `on` / `yes` / `no` 解析成布尔——
            # `provider: off` 到了这里就是 `False`，`str(False)` 会变成 "False"，
            # 于是界面报「未知的打标方式：False」。配置里已经要求加引号，
            # 但用户手改总会漏，所以这里再兜一层：非字符串一律当不启用（fail-safe）。
            if not isinstance(cfg.get("provider"), str):
                cfg["provider"] = "off"

        # ① 档位引用：只在非本地（ollama）时生效——本地那条路没有 base_url 概念
        ref = str(cfg.get("model_ref") or "").strip()
        if ref and str(cfg.get("provider") or "") != "ollama":
            from .model_refs import resolve_model_ref

            hit = resolve_model_ref(raw, ref)
            if hit is None:
                cfg["model_ref_error"] = f"找不到档位「{ref}」（设置 → AI 模型）"
            else:
                if hit["base_url"]:
                    cfg["api_base"] = hit["base_url"]
                if hit["model"]:
                    cfg["api_model"] = hit["model"]
                if hit["api_key"]:
                    cfg["api_key"] = hit["api_key"]
                # 档位是云端连接：显式选它 = 走 api 这条路
                cfg["provider"] = "api"
                cfg["model_ref_label"] = hit["label"]
                # 2026-10-28：打标必须能看图。档位选了聊天档（`input` 只有 text）
                # 时要在**开跑之前**拦住——否则几十张图一路失败，还看不出原因
                # （实测：`model_ref=default` 把 api_model 换成了 deepseek-v4.1-flash）。
                cfg["model_input"] = hit.get("input") or []

        # ② 密钥：`${VAR}` 引用 → 实际值；留空时直接找 .env 的标准变量名。
        #    为什么留空也要找：设置页把 key 改成「写 .env」之后，config 里就是空串，
        #    而 bridge 进程不 load_dotenv（os.environ 里没有），不兜这一层就会
        #    在界面报「API key 没配」——明明 .env 里写着（两套口径，教训 #38）。
        resolved = _resolve_secret(cfg.get("api_key"))
        if not resolved:
            resolved = (os.environ.get("IMAGE_TAG_API_KEY") or "").strip() or _env_file_value(
                "IMAGE_TAG_API_KEY"
            )
        cfg["api_key"] = resolved
    except Exception:  # 文件不在 / YAML 坏 / 没装 yaml：全部回落默认，不抛
        pass
    return cfg


def _resolve_dir(cfg: dict, dir_path=None) -> Path:
    """目录的**唯一解析点**：调用时传入 > config 的 `dir` > DEFAULTS。

    优先级写在这里一处，`gallery_dir` / `metadata_path` / `scan` / `run` 全走它，
    避免「界面说打这个库、实际写那个库」的两套口径（教训 #38/#51）。
    """
    raw = dir_path if dir_path not in (None, "") else cfg.get("dir")
    rel = str(raw or DEFAULTS["dir"]).strip() or DEFAULTS["dir"]
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def gallery_dir(cfg: dict | None = None, dir_path=None) -> Path:
    """这个图库的绝对路径。`dir_path` 由调用方（图库页当前选中的库）传入。"""
    cfg = cfg or load_config()
    return _resolve_dir(cfg, dir_path)


def metadata_path(cfg: dict | None = None, dir_path=None) -> Path:
    return gallery_dir(cfg, dir_path) / "metadata.json"


def _load_meta(cfg: dict, dir_path=None) -> dict:
    p = metadata_path(cfg, dir_path)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_meta(cfg: dict, meta: dict, dir_path=None) -> None:
    """整份重写 `metadata.json`。

    不原子替换：现有脚本（`tag_cg_ollama.py`）也是直接 write，而它每张都写，
    中断时最坏情况是这一张丢失。加原子替换需要临时文件 + `os.replace`，
    会给「她自己也在读这个文件」的进程多一个中间态——收益不值这个复杂度。
    """
    metadata_path(cfg, dir_path).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _has_tags(meta: dict, name: str) -> bool:
    """「已打标」的**唯一判据** = metadata 里有非空 `persons`（与脚本 `--resume` 同口径）。

    扫描、增量过滤、覆盖开关全调这一个函数——两处各写一遍必然漂移（教训 #38）。
    """
    return bool(str((meta.get(name) or {}).get("persons") or "").strip())



# ── 复用打标脚本的解析层 ──────────────────────────────────────

def _script_for(d: Path) -> Path | None:
    """这个图库用哪份解析脚本：先看目录自带，再回落到 `FALLBACK_SCRIPT_ROOT`。

    返回 `None` = 两处都没有 → 调用方报错（**不许**退回一个自写的简化解析器）。
    """
    cand = []
    for root in (d, FALLBACK_SCRIPT_ROOT):
        cand += [root / "_tag" / "tag_cg_ollama.py", root / "_tag" / "tag_cg.py"]
    return next((p for p in cand if p.is_file()), None)


def _tagger(dir_path=None):
    """懒加载目标图库的解析脚本（**只取函数，不跑它的 main**）。

    它有一堆正则和优先级规则（单人不许双人体位、否定词先剥、体位只认动作词），
    那是打标质量的本体。重写＝两套真相迟早漂移（教训 #34）。
    贴图库没有 `_tag/`，回落到 `stickers_cg` 的同一份脚本——解析层只有一份。
    """
    d = gallery_dir(dir_path=dir_path)
    script = _script_for(d)
    if script is None:
        raise RuntimeError(
            f"找不到打标脚本：{d}/_tag/tag_cg_ollama.py（也没有回落到"
            f"{FALLBACK_SCRIPT_ROOT}/_tag/）。这个功能复用现有脚本的解析规则，"
            f"缺了它只能报错，不会偷偷用简化版")
    key = str(script)
    mod = _TAGGERS.get(key)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location("_tangtang_tagger", str(script))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"打标脚本加载失败：{script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for need in ("_encode_image", "PROMPT", "make_rec_v2"):
        if not hasattr(mod, need):
            raise RuntimeError(f"{script.name} 里没有 {need}——打标脚本结构变了，先对齐再跑")
    _TAGGERS[key] = mod
    return mod


# ── 扫描 / 状态 ──────────────────────────────────────────────

def scan(cfg: dict | None = None, dir_path=None) -> dict:
    """图库现状：多少张图、多少张已打标、待打标几张。

    「已打标」判据走 `_has_tags`——**和脚本 `--resume` 同一口径**，
    否则界面说「还剩 3 张」、跑起来却重打 30 张（两种口径必然对不上，教训 #38）。
    """
    cfg = cfg or load_config()
    d = gallery_dir(cfg, dir_path)
    meta = _load_meta(cfg, dir_path)
    files: list[Path] = []
    if d.is_dir():
        try:
            files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS)
        except OSError:
            files = []
    tagged = [p for p in files if _has_tags(meta, p.name)]
    return {
        "dir": str(d),
        "dir_exists": d.is_dir(),
        "files": len(files),
        "tagged": len(tagged),
        "pending": len(files) - len(tagged),
        "metadata_entries": len(meta),
    }


def scan_many(cfg: dict, dir_paths) -> dict:
    """多个库合计（图库页「贴图」是三个库的合集，只读汇总用）。

    只在 image_tagger 里累加——**不要再在 bridge 里写一遍加法**（教训 #51）。
    """
    parts = [scan(cfg, d) for d in (dir_paths or [])]
    return {
        "dir": None,
        "dir_exists": any(p["dir_exists"] for p in parts),
        "files": sum(p["files"] for p in parts),
        "tagged": sum(p["tagged"] for p in parts),
        "pending": sum(p["pending"] for p in parts),
        "metadata_entries": sum(p["metadata_entries"] for p in parts),
        "dirs": [p["dir"] for p in parts],
    }


def _dir_ready(cfg: dict, dir_path=None) -> str:
    """空串 = 这个目录能打标；非空 = **为什么不能**（直接显示，不让人猜）。"""
    d = gallery_dir(cfg, dir_path)
    if not d.is_dir():
        return f"图库目录不存在：{d}"
    if _script_for(d) is None:
        return f"这个图库没有打标脚本（{d}/_tag/tag_cg_ollama.py）"
    return ""


def status(dir_path=None, source: str = "", dir_reason: str = "",
           scan_dirs=None) -> dict:
    """给界面的**唯一口径**：配置（隐去 key）+ 扫描 + 进度 + 能不能跑。

    进度和扫描分两个字段：`progress` 是「这一轮跑到哪」，`scan` 是「还剩多少张」。
    合成一个字段的话，跑完那一刻两个数字会互相打架（教训 #51）。

    `dir_path`    —— 图库页当前选中的库（不传 = config 兜底）
    `source`      —— 桥那边的库 key，原样带回给前端（界面显示「在打哪个库」）
    `dir_reason`  —— 桥判定「这个范围不能直接打标」的原因（如合集不可写），
                     空 = 由本模块按目录自己判
    `scan_dirs`   —— 多库合计扫描（合集时用；`scan` 走 `scan_many`）
    """
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "api_key"}
    safe["api_key_configured"] = bool(
        os.environ.get("IMAGE_TAG_API_KEY") or str(cfg.get("api_key") or "").strip())
    with _LOCK:
        progress = dict(_STATE)
        progress.pop("cancel", None)
    d = gallery_dir(cfg, dir_path)
    return {
        "ok": True,
        "source": source,
        # 合集（scan_dirs）没有「单一目录」；其余情况给实际扫描的目录，界面要显示它
        "dir": None if scan_dirs else str(d),
        "config": safe,
        "provider_ready": _provider_ready(cfg),
        "dir_ready": dir_reason or _dir_ready(cfg, dir_path),
        "scan": scan_many(cfg, scan_dirs) if scan_dirs else scan(cfg, dir_path),
        "progress": progress,
    }


def _provider_ready(cfg: dict) -> str:
    """空串 = 可以开跑；非空 = **为什么不能**（直接显示给用户，不让他猜）。"""
    provider = str(cfg.get("provider") or "off").strip()
    if provider == "off":
        return "还没选打标方式：设置 → 图库打标 → 打标方式"
    if provider == "ollama":
        if not str(cfg.get("ollama_model") or "").strip():
            return "Ollama 模型名是空的"
        if not str(cfg.get("ollama_host") or "").strip():
            return "Ollama 地址是空的（默认 http://127.0.0.1:11434）"
        return ""
    if provider == "api":
        # 档位引用写错了要**明说哪个档位找不到**，不能退化成「模型名是空的」
        if cfg.get("model_ref_error"):
            return str(cfg["model_ref_error"])
        if not str(cfg.get("api_model") or "").strip():
            return "API 模型名是空的"
        # 2026-10-28：选了档位还要确认它**能看图**。打标是给图片打标签，
        # 聊天档（`input` 只有 text）会一路打到模型那边才失败——代价是一轮
        # API 调用加一句看不懂的报错。实测：model_ref=default 把 api_model
        # 换成了 deepseek-v4.1-flash，key 也从 115 变成 51 字符。
        inputs = cfg.get("model_input") or []
        # 只在「明确标注了能力」且不含 image 时拦；`[]` = 这个档位没标过能力，
        # 不猜（老档位全部属于这一类，猜错会把人挡在门外）。
        if inputs and "image" not in inputs:
            label = cfg.get("model_ref_label") or cfg.get("model_ref") or "这个档位"
            return f"档位「{label}」不能看图——打标要选标注了「能看图」的档位，或改用本机 Ollama"
        if not (os.environ.get("IMAGE_TAG_API_KEY") or str(cfg.get("api_key") or "").strip()):
            return "API key 没配：设置里填，或在 .env 里写 IMAGE_TAG_API_KEY"
        return ""
    return f"未知的打标方式：{provider}（可选 off / ollama / api）"


# ── 两种 provider ────────────────────────────────────────────

def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            pass
        raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上 {url}：{exc.reason}") from None


def _ask_ollama(cfg: dict, b64: str, prompt: str) -> str:
    host = str(cfg.get("ollama_host") or "").rstrip("/")
    out = _post_json(
        f"{host}/api/generate",
        {
            "model": str(cfg.get("ollama_model") or ""),
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            # 与现有脚本同参数：温度压低（要稳定标签）、num_predict 限长（描述不需要长）
            "options": {"num_ctx": 8192, "num_predict": 260,
                        "temperature": 0.1, "top_p": 0.8},
        },
        {},
        int(cfg.get("timeout_s") or DEFAULTS["timeout_s"]),
    )
    return str(out.get("response") or "").strip()


def _ask_api(cfg: dict, b64: str, prompt: str) -> str:
    key = (os.environ.get("IMAGE_TAG_API_KEY")
           or str(cfg.get("api_key") or "").strip())
    if not key:
        raise RuntimeError("API key 没配：设置里填，或在 .env 里写 IMAGE_TAG_API_KEY")
    base = str(cfg.get("api_base") or "").rstrip("/")
    out = _post_json(
        f"{base}/chat/completions",
        {
            "model": str(cfg.get("api_model") or ""),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "max_tokens": 260,
            "temperature": 0.1,
        },
        {"Authorization": f"Bearer {key}"},
        int(cfg.get("timeout_s") or DEFAULTS["timeout_s"]),
    )
    try:
        return str(out["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"返回格式不是 OpenAI chat/completions：{str(out)[:200]}") from None


def _ask(cfg: dict, b64: str, prompt: str) -> str:
    provider = str(cfg.get("provider") or "").strip()
    if provider == "ollama":
        return _ask_ollama(cfg, b64, prompt)
    if provider == "api":
        return _ask_api(cfg, b64, prompt)
    raise RuntimeError(f"未知的打标方式：{provider}")


# ── 跑一轮 ──────────────────────────────────────────────────

#: `run()` 接受的范围（图库页「选范围」的三选一）
SCOPES = ("pending", "all", "selected")


def _select_todo(cfg: dict, dir_path, scope: str, names, overwrite: bool,
                 limit: int) -> list[Path]:
    """这一轮要打哪些图——**筛选的唯一实现**。

    两个维度（与图库页的控件一一对应）：
      `scope`     —— 候选集：pending=未打标的 / all=整个图库 / selected=传入的 names
      `overwrite` —— 候选集里已经打过标的要不要重打（False = 跳过）
    `pending` 已经排除了已打标的，所以 `overwrite` 在它上面不生效（由图库页说明）。
    """
    d = gallery_dir(cfg, dir_path)
    if not d.is_dir():
        return []
    meta = _load_meta(cfg, dir_path)
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS)
    if scope == "selected":
        wanted = {str(n).strip() for n in (names or []) if str(n).strip()}
        files = [p for p in files if p.name in wanted]
    if not overwrite:
        files = [p for p in files if not _has_tags(meta, p.name)]
    return files[:limit] if limit and limit > 0 else files


def run(dir_path=None, scope: str = "pending", names=None,
        rename=None, overwrite=None, limit: int = 0) -> dict:
    """启动一轮打标（后台线程，立刻返回）。

    **「打哪些图 / 怎么处理」全部由调用方传入**（图库页的控件），不再只从 config 读：

    - `dir_path` —— 打哪个库的目录（图库页当前选中的库；不传才回落 config 的 dir）
    - `scope`    —— `pending`（默认，只打没 persons 的）/ `all`（整个图库）/
                    `selected`（只打 `names` 里的）
    - `names`    —— `scope="selected"` 时的文件名白名单（只认纯文件名，由桥护栏）
    - `rename`   —— 打完是否按规范改名（不传 = config 的 rename，老配置照样生效）
    - `overwrite`—— 已有标签的要不要重打（不传 = `scope=="all"` 时为 True，
                    与老 `full=True` 语义一致；`scope=="pending"` 时强制 False）
    """
    global _THREAD
    cfg = load_config()
    why = _provider_ready(cfg)
    if why:
        return {"ok": False, "error": why}
    why = _dir_ready(cfg, dir_path)
    if why:
        return {"ok": False, "error": why}
    scope = str(scope or "pending").strip()
    if scope not in SCOPES:
        return {"ok": False,
                "error": f"未知的打标范围：{scope}（可选 {' / '.join(SCOPES)}）"}
    if overwrite is None:
        overwrite = scope == "all"
    overwrite = bool(overwrite) and scope != "pending"
    d = gallery_dir(cfg, dir_path)
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "error": "已经在跑了"}
    todo = _select_todo(cfg, dir_path, scope, names, overwrite, int(limit or 0))
    if not todo:
        return {"ok": True, "started": False,
                "message": "图库里没有图片" if (scope == "all" and overwrite)
                else "没有需要打标的图（全部已有标签）"}
    with _LOCK:
        _STATE.update({
            "running": True, "total": len(todo), "done": 0, "ok": 0, "fail": 0,
            "current": "", "error": "", "started_at": time.time(),
            "finished_at": 0.0, "cancel": False,
        })
    _THREAD = threading.Thread(target=_worker, args=(cfg, todo, dir_path, rename),
                               daemon=True, name="image-tagger")
    _THREAD.start()
    return {"ok": True, "started": True, "total": len(todo),
            "provider": cfg["provider"], "dir": str(d),
            "scope": scope, "overwrite": overwrite}


def cancel() -> dict:
    """请求停止。**不会打断正在进行的那一张**（要等它返回或超时）。"""
    with _LOCK:
        if not _STATE["running"]:
            return {"ok": True, "running": False, "message": "没在跑"}
        _STATE["cancel"] = True
    return {"ok": True, "running": True, "message": "已请求停止，当前这张跑完就停"}


def _worker(cfg: dict, todo: list[Path], dir_path=None, rename=None) -> None:
    """后台线程：逐张跑模型 → 解析 → 落盘 metadata（`rename` / 目录都由调用时传入）。

    `rename=None` 时回落 cfg 的 rename——这样老配置（`image_tagging.rename`）仍然生效，
    图库页不传也不会把「改名」默默变成关闭。
    """
    meta = _load_meta(cfg, dir_path)
    do_rename = bool(cfg.get("rename") if rename is None else rename)
    ok = fail = 0
    try:
        tagger = _tagger(dir_path)
        for index, path in enumerate(todo, start=1):
            with _LOCK:
                if _STATE["cancel"]:
                    break
                _STATE["current"] = path.name
            try:
                b64 = tagger._encode_image(path, int(cfg.get("max_side")
                                                    or DEFAULTS["max_side"]))
                desc = _ask(cfg, b64, tagger.PROMPT)
                if not desc:
                    raise RuntimeError("模型返回了空描述")
                rec, new_name = tagger.make_rec_v2(path.name, desc, meta.get(path.name) or {})
                rec["tag_provider"] = str(cfg.get("provider") or "")
                if do_rename and new_name and new_name != path.name:
                    target = path.parent / new_name
                    if target.exists():
                        # 撞名不动：宁可这次不改名，也不覆盖另一张图
                        raise RuntimeError(f"改名目标已存在：{new_name}")
                    path.rename(target)
                    meta.pop(path.name, None)
                    meta[new_name] = rec
                else:
                    meta[path.name] = rec
                _save_meta(cfg, meta, dir_path)   # 每张落盘：中断最多丢当前这张
                ok += 1
            except Exception as exc:  # 单张失败不终止整轮
                fail += 1
                with _LOCK:
                    _STATE["error"] = f"{path.name}：{exc}"
            with _LOCK:
                _STATE["done"] = index
                _STATE["ok"] = ok
                _STATE["fail"] = fail
    except Exception as exc:   # 脚本加载失败等启动级错误
        with _LOCK:
            _STATE["error"] = str(exc)
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["current"] = ""
            _STATE["cancel"] = False
            _STATE["finished_at"] = time.time()


def reset_state_for_tests() -> None:
    """测试用：把进度和缓存清干净（生产代码不要调）。"""
    global _THREAD
    with _LOCK:
        _STATE.update({
            "running": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
            "current": "", "error": "", "started_at": 0.0, "finished_at": 0.0,
            "cancel": False,
        })
    _THREAD = None
    _TAGGERS.clear()


def describe_one(name: str, dir_path=None) -> dict:
    """只跑模型、**不落盘**：设置页「试一张」用。

    存在的意义：配好 Ollama / API 之后要先知道「这条路通不通、模型吐什么」，
    否则第一次跑批量就是几十张失败。所以这里刻意不写 metadata。
    """
    cfg = load_config()
    why = _provider_ready(cfg)
    if why:
        return {"ok": False, "error": why}
    d = gallery_dir(cfg, dir_path).resolve()
    # ⚠ 必须 `resolve()` 之后再比：`Path.relative_to` 是纯字符串操作，
    # `gallery/../secret.txt`.relative_to(`gallery`) 会**成功**返回 `../secret.txt`
    # ——不做这一步就是一条真实的路径穿越（2026-10-26 单测当场抓到）。
    path = (d / str(name or "").strip()).resolve()
    try:
        path.relative_to(d)
    except ValueError:
        return {"ok": False, "error": "路径不在这图库里"}
    if not path.is_file():
        return {"ok": False, "error": f"没有这张图：{name}"}
    try:
        tagger = _tagger(dir_path)
        b64 = tagger._encode_image(path, int(cfg.get("max_side") or DEFAULTS["max_side"]))
        desc = _ask(cfg, b64, tagger.PROMPT)
        rec, new_name = tagger.make_rec_v2(path.name, desc, {})
        return {
            "ok": True,
            "name": path.name,
            "provider": cfg.get("provider"),
            "model": (cfg.get("ollama_model") if cfg.get("provider") == "ollama"
                      else cfg.get("api_model")),
            # 模型原话要原样给出来：标签提取错了，得先看它是怎么描述的
            "raw": desc,
            "tags": {k: rec.get(k) for k in
                     ("persons", "act", "emotions", "scene_tags", "desc")},
            "rename_to": new_name,
            "note": "只是试打，没有写入 metadata.json",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
