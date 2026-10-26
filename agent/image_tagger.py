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
3. **配置集中**：设置页配好就完事，图库页只留一个「打标」按钮 + 进度。

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

import base64
import importlib.util
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 项目根（`agent/` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: `config.yaml` 里这一节的默认值。**每一个都能在设置页改**，改完不用重启（每次读）。
DEFAULTS: dict = {
    # off = 不启用（图库页的按钮变灰）；ollama = 本机；api = 云端
    "provider": "off",
    # 图库目录，相对项目根。**换个目录就是换一套图**，所以做成配置而不是常量
    "dir": "stickers_cg",
    # —— 本地（Ollama）——
    "ollama_host": "http://127.0.0.1:11434",
    "ollama_model": "qwen3-vl:8b-instruct-q4_K_M",
    # —— 云端（OpenAI 兼容视觉 API）——
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_model": "qwen-vl-max",
    # key 留空时读环境变量 IMAGE_TAG_API_KEY（推荐放 .env，别把 key 写进 config.yaml）
    "api_key": "",
    # —— 通用 ——
    "max_side": 768,
    "timeout_s": 300,
    # 打标后按规范改名（`体位_暴露_hash.jpg`）。文件名参与她的语义索引，所以默认开；
    # 不想动文件名就在设置里关掉（标签照样写进 metadata.json）
    "rename": True,
}

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
_TAGGER = None


# ── 配置与路径 ────────────────────────────────────────────────

def load_config() -> dict:
    """读 `config.yaml` 的 `image_tagging` 节，缺的用 `DEFAULTS` 补。

    **每次调用都重读**：设置页改完立刻生效比「改完要重启」友好得多，
    而且这个函数只在按钮点击/列表刷新时跑，开销可以忽略。
    读失败一律回落默认值——打标是可选功能，配置坏了不能拖垮图库页。
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
    except Exception:  # 文件不在 / YAML 坏 / 没装 yaml：全部回落默认，不抛
        pass
    return cfg


def gallery_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    rel = str(cfg.get("dir") or DEFAULTS["dir"]).strip() or DEFAULTS["dir"]
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def metadata_path(cfg: dict | None = None) -> Path:
    return gallery_dir(cfg) / "metadata.json"


def _load_meta(cfg: dict) -> dict:
    p = metadata_path(cfg)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_meta(cfg: dict, meta: dict) -> None:
    """整份重写 `metadata.json`。

    不原子替换：现有脚本（`tag_cg_ollama.py`）也是直接 write，而它每张都写，
    中断时最坏情况是这一张丢失。加原子替换需要临时文件 + `os.replace`，
    会给「她自己也在读这个文件」的进程多一个中间态——收益不值这个复杂度。
    """
    metadata_path(cfg).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 复用打标脚本的解析层 ──────────────────────────────────────

def _tagger():
    """懒加载 `stickers_cg/_tag/tag_cg_ollama.py`（**只取函数，不跑它的 main**）。

    它有一堆正则和优先级规则（单人不许双人体位、否定词先剥、体位只认动作词），
    那是打标质量的本体。重写＝两套真相迟早漂移（教训 #34）。
    """
    global _TAGGER
    if _TAGGER is not None:
        return _TAGGER
    root = gallery_dir()
    cand = [root / "_tag" / "tag_cg_ollama.py", root / "_tag" / "tag_cg.py"]
    script = next((p for p in cand if p.is_file()), None)
    if script is None:
        raise RuntimeError(
            f"找不到打标脚本：{root}/_tag/tag_cg_ollama.py。"
            f"这个功能复用现有脚本的解析规则，缺了它只能报错，不会偷偷用简化版")
    spec = importlib.util.spec_from_file_location("_tangtang_tagger", str(script))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"打标脚本加载失败：{script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for need in ("_encode_image", "PROMPT", "make_rec_v2"):
        if not hasattr(mod, need):
            raise RuntimeError(f"{script.name} 里没有 {need}——打标脚本结构变了，先对齐再跑")
    _TAGGER = mod
    return mod


# ── 扫描 / 状态 ──────────────────────────────────────────────

def scan(cfg: dict | None = None) -> dict:
    """图库现状：多少张图、多少张已打标、待打标几张。

    「已打标」判据 = metadata 里有非空 `persons` 字段——**和脚本 `--resume` 同一口径**，
    否则界面说「还剩 3 张」、跑起来却重打 30 张（两种口径必然对不上，教训 #38）。
    """
    cfg = cfg or load_config()
    d = gallery_dir(cfg)
    meta = _load_meta(cfg)
    files: list[Path] = []
    if d.is_dir():
        try:
            files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS)
        except OSError:
            files = []
    tagged = [p for p in files if str((meta.get(p.name) or {}).get("persons") or "").strip()]
    return {
        "dir": str(d),
        "dir_exists": d.is_dir(),
        "files": len(files),
        "tagged": len(tagged),
        "pending": len(files) - len(tagged),
        "metadata_entries": len(meta),
    }


def status() -> dict:
    """给界面的**唯一口径**：配置（隐去 key）+ 扫描 + 进度。

    进度和扫描分两个字段：`progress` 是「这一轮跑到哪」，`scan` 是「还剩多少张」。
    合成一个字段的话，跑完那一刻两个数字会互相打架（教训 #51）。
    """
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "api_key"}
    safe["api_key_configured"] = bool(
        os.environ.get("IMAGE_TAG_API_KEY") or str(cfg.get("api_key") or "").strip())
    with _LOCK:
        progress = dict(_STATE)
        progress.pop("cancel", None)
    return {
        "ok": True,
        "config": safe,
        "provider_ready": _provider_ready(cfg),
        "scan": scan(cfg),
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
        if not str(cfg.get("api_model") or "").strip():
            return "API 模型名是空的"
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

def _todo(cfg: dict, full: bool, limit: int) -> list[Path]:
    d = gallery_dir(cfg)
    if not d.is_dir():
        return []
    meta = _load_meta(cfg)
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS)
    if not full:
        files = [p for p in files
                 if not str((meta.get(p.name) or {}).get("persons") or "").strip()]
    return files[:limit] if limit and limit > 0 else files


def run(full: bool = False, limit: int = 0) -> dict:
    """启动一轮打标（后台线程，立刻返回）。

    `full=False`（默认）= 只打**没有 persons 字段**的（增量），与脚本 `--resume` 同义。
    `full=True` = 全部重打（会覆盖已有标签）。
    """
    global _THREAD
    cfg = load_config()
    why = _provider_ready(cfg)
    if why:
        return {"ok": False, "error": why}
    with _LOCK:
        if _STATE["running"]:
            return {"ok": False, "error": "已经在跑了"}
    todo = _todo(cfg, full, int(limit or 0))
    if not todo:
        return {"ok": True, "started": False,
                "message": "没有需要打标的图（全部已有标签）" if not full else "图库里没有图片"}
    with _LOCK:
        _STATE.update({
            "running": True, "total": len(todo), "done": 0, "ok": 0, "fail": 0,
            "current": "", "error": "", "started_at": time.time(),
            "finished_at": 0.0, "cancel": False,
        })
    _THREAD = threading.Thread(target=_worker, args=(cfg, todo), daemon=True,
                               name="image-tagger")
    _THREAD.start()
    return {"ok": True, "started": True, "total": len(todo),
            "provider": cfg["provider"], "dir": str(gallery_dir(cfg))}


def cancel() -> dict:
    """请求停止。**不会打断正在进行的那一张**（要等它返回或超时）。"""
    with _LOCK:
        if not _STATE["running"]:
            return {"ok": True, "running": False, "message": "没在跑"}
        _STATE["cancel"] = True
    return {"ok": True, "running": True, "message": "已请求停止，当前这张跑完就停"}


def _worker(cfg: dict, todo: list[Path]) -> None:
    meta = _load_meta(cfg)
    ok = fail = 0
    try:
        tagger = _tagger()
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
                if cfg.get("rename") and new_name and new_name != path.name:
                    target = path.parent / new_name
                    if target.exists():
                        # 撞名不动：宁可这次不改名，也不覆盖另一张图
                        raise RuntimeError(f"改名目标已存在：{new_name}")
                    path.rename(target)
                    meta.pop(path.name, None)
                    meta[new_name] = rec
                else:
                    meta[path.name] = rec
                _save_meta(cfg, meta)   # 每张落盘：中断最多丢当前这张
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
    global _THREAD, _TAGGER
    with _LOCK:
        _STATE.update({
            "running": False, "total": 0, "done": 0, "ok": 0, "fail": 0,
            "current": "", "error": "", "started_at": 0.0, "finished_at": 0.0,
            "cancel": False,
        })
    _THREAD = None
    _TAGGER = None


def describe_one(name: str) -> dict:
    """只跑模型、**不落盘**：设置页「试一张」用。

    存在的意义：配好 Ollama / API 之后要先知道「这条路通不通、模型吐什么」，
    否则第一次跑批量就是几十张失败。所以这里刻意不写 metadata。
    """
    cfg = load_config()
    why = _provider_ready(cfg)
    if why:
        return {"ok": False, "error": why}
    d = gallery_dir(cfg).resolve()
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
        tagger = _tagger()
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
