"""媒体缓存 —— 在 QQ 临时链接过期前，把图片/语音落到本地（2026-10-24）

## 为什么必须有这一层

`chat_log.segments` 里存的是**当时的** CQ 码，图片/语音带的是 QQ 的
`multimedia.nt.qq.com.cn/download?...&rkey=...` 临时链接。实测（2026-10-26）：

```
GET <url>  →  HTTP 400
{"retcode":-5503007,"retmsg":"download url has expired","retflag":0}
```

也就是**几小时后就取不回来了**。桌面端要「图片和语音也同步显示」，唯一的
办法是在收到消息的那一刻先下一份到本地——等用户打开会客厅时，链接早就死了。

## 边界

- 只缓存**媒体字节**，不碰 chat_log、不解析业务语义；
- 全程 try/except 兜死并在后台线程跑——媒体缓存失败绝不影响消息处理
  （这是旁路，不是主链路）；
- 语音统一转 mp3：QQ 发的是 `.amr`（或 `.silk`），Chromium 一个都播不了。
  ffmpeg 用 `agent/paths.py` 的项目内自带版本（`ensure_ffmpeg_on_path`）。
- 缓存文件按 `<chat_log 行 id>_<序号>.<ext>` 命名，不需要额外的索引表。
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from .paths import PROJECT_ROOT, ensure_ffmpeg_on_path, state_path

logger = logging.getLogger("糖糖.媒体缓存")

#: CQ 码：`[CQ:image,file=xxx.jpg,url=https://...]` / `[CQ:record,...]`
_CQ_RE = re.compile(r"\[CQ:(image|record),([^\]]*)\]")
#: CQ 参数表。值里不会有裸逗号（QQ 会把逗号转义成 &#44;）
_FIELD_RE = re.compile(r"(\w+)=([^,\]]*)")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
AUDIO_EXT = {".amr", ".silk", ".wav", ".mp3", ".ogg", ".m4a", ".flac"}

#: 单文件上限——QQ 表情/截图远小于这个数，超了多半是异常响应
MAX_BYTES = 20 * 1024 * 1024

#: 下载超时（秒）。含首字节，QQ 的 CDN 偶尔慢，但没必要等太久
TIMEOUT = 12.0

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-cache")
_INFLIGHT: set[str] = set()
_LOCK = threading.Lock()


def cache_dir() -> Path:
    """缓存目录：`状态/media_cache/`（走 paths.state_path，不许自己拼路径）。"""
    d = state_path("media_cache")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── CQ 解析 ───────────────────────────────────────────────────────────


def _fields(raw: str) -> dict[str, str]:
    return {k: v for k, v in _FIELD_RE.findall(raw)}


def _strip_query(value: str) -> str:
    return unquote(str(value or "").strip().strip('"'))


def _local_path(value: str) -> Path | None:
    """把 `file:///D:/...` 或 `D:/...` 转成本机路径；不在项目内的一律拒绝。

    只允许项目目录内的文件——媒体端点是对外的 HTTP 口，不能让它变成
    「任意文件读取」（`?file=file:///C:/Windows/...`）。
    """
    text = _strip_query(value)
    if not text:
        return None
    if text.startswith("file:///"):
        text = text[len("file:///"):]
    elif text.startswith("file://"):
        text = text[len("file://"):]
    try:
        p = Path(text).expanduser().resolve()
    except (OSError, ValueError):
        return None
    try:
        p.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return p if p.is_file() else None


def parse_media(message: str = "", segments: str = "") -> list[dict]:
    """把一条 chat_log 行的媒体附件抽出来，**顺序稳定**（前端用序号当下标）。

    三个来源都要认（实测都真实存在）：
      1. 用户发的图/语音 → 只在 `segments` 的 `{"type":"cq","content":"..."}` 里，
         `message` 已经被归一成「（发了张图片）」「[语音]」；
      2. 糖糖发的贴图 → 内嵌在 `message` 正文里，`file=file:///D:/.../stickers/x.jpg`；
      3. 糖糖发的图（generate_image）→ 同上，本地路径。

    返回 `[{type, name, url, file, local}]`：
      - `local=True` 表示 `file` 是本机项目内路径（可直接读盘）
      - `local=False` 表示 `url` 是 QQ 临时链接（可能已过期）
    """
    items: list[dict] = []
    seen: set[str] = set()

    def _add(kind: str, raw: str) -> None:
        f = _fields(raw)
        file_raw = _strip_query(f.get("file", ""))
        url = html.unescape(str(f.get("url", "") or "").strip())
        key = (file_raw or url).lower()
        if not key or key in seen:
            return
        seen.add(key)
        local_p = _local_path(file_raw) if file_raw else None
        if local_p is not None:
            items.append({
                "type": "image" if kind == "image" else "audio",
                "name": local_p.name,
                "file": str(local_p),
                "url": "",
                "local": True,
            })
            return
        if not url:
            return
        name = Path(file_raw).name if file_raw else f"{kind}"
        items.append({
            "type": "image" if kind == "image" else "audio",
            "name": name or f"{kind}",
            "file": "",
            "url": url,
            "local": False,
        })

    # 1) segments 里的 CQ 段（用户发来的媒体只在这里）
    if segments:
        try:
            import json

            for seg in json.loads(segments) or []:
                if not isinstance(seg, dict):
                    continue
                content = str(seg.get("content") or "")
                for m in _CQ_RE.finditer(content):
                    _add(m.group(1), m.group(2))
        except (ValueError, TypeError):
            pass

    # 2) 正文里的 CQ 码（糖糖自己发的贴图/图片）
    for m in _CQ_RE.finditer(str(message or "")):
        _add(m.group(1), m.group(2))

    return items


# ── 缓存文件 ──────────────────────────────────────────────────────────


def _safe_ext(name: str, kind: str) -> str:
    ext = Path(str(name or "")).suffix.lower()
    allowed = IMAGE_EXT if kind == "image" else AUDIO_EXT
    if ext in allowed:
        return ext
    return ".jpg" if kind == "image" else ".amr"


def find_cached(chat_id: int, index: int) -> Path | None:
    """找已经落地的缓存文件；没有返回 None。

    语音优先给 mp3（浏览器能播），没有 mp3 才回原文件。
    """
    try:
        d = cache_dir()
        stem = f"{int(chat_id)}_{int(index)}"
        mp3 = d / f"{stem}.mp3"
        if mp3.is_file() and mp3.stat().st_size > 0:
            return mp3
        for p in sorted(d.glob(f"{stem}.*")):
            if p.is_file() and p.stat().st_size > 0:
                return p
    except OSError:
        pass
    return None


def _ffmpeg() -> str | None:
    try:
        return ensure_ffmpeg_on_path()
    except Exception:
        return None


def _transcode_to_mp3(src: Path, dest: Path) -> bool:
    exe = _ffmpeg()
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "24000", "-b:a", "48k", str(dest)],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        logger.debug("媒体缓存：转码失败 %s", src, exc_info=True)
        return False


def _download(url: str, dest: Path) -> bool:
    try:
        import httpx
    except ImportError:  # pragma: no cover - 依赖缺失时静默降级
        return False
    try:
        with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code != 200:
                return False
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                return False
            written = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > MAX_BYTES:
                        fh.close()
                        tmp.unlink(missing_ok=True)
                        return False
                    fh.write(chunk)
        if written <= 0:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(dest)
        return True
    except Exception:
        logger.debug("媒体缓存：下载失败 %s", url[:80], exc_info=True)
        return False


def _fetch_one(chat_id: int, index: int, item: dict) -> Path | None:
    """落地一条媒体，返回本地文件路径；失败返回 None。"""
    d = cache_dir()
    kind = item["type"]
    if item.get("local"):
        return Path(item["file"]) if item.get("file") else None
    url = item.get("url") or ""
    if not url:
        return None
    ext = _safe_ext(item.get("name", ""), kind)
    dest = d / f"{int(chat_id)}_{int(index)}{ext}"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if not _download(url, dest):
        return None
    if kind == "audio" and ext != ".mp3":
        mp3 = d / f"{int(chat_id)}_{int(index)}.mp3"
        if _transcode_to_mp3(dest, mp3):
            return mp3
    return dest


def _prefetch_job(chat_id: int, message: str, segments: str) -> None:
    try:
        items = parse_media(message, segments)
        for i, item in enumerate(items):
            if item.get("local"):
                continue  # 本地文件不需要缓存
            _fetch_one(chat_id, i, item)
    except Exception:
        logger.debug("媒体缓存：预取异常 chat_id=%s", chat_id, exc_info=True)
    finally:
        with _LOCK:
            _INFLIGHT.discard(str(chat_id))


def prefetch(chat_id: int, message: str = "", segments: str = "") -> None:
    """收到消息时调一次：把里面的远程媒体下载到本地（后台线程，不阻塞）。

    调用点是 `agent/inbound_event.py` 落库之后——那里是**所有**入站消息的
    唯一入口，所以私聊/群聊/批处理都能覆盖到。
    """
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return
    if cid <= 0:
        return
    if not str(message or "") and not str(segments or ""):
        return
    # 没有远程媒体就不要开线程（绝大多数消息都是纯文字，这里是热路径）
    if "[CQ:" not in str(message or "") + str(segments or ""):
        return
    with _LOCK:
        key = str(cid)
        if key in _INFLIGHT:
            return
        _INFLIGHT.add(key)
    try:
        _POOL.submit(_prefetch_job, cid, message, segments)
    except RuntimeError:  # 解释器关闭中
        with _LOCK:
            _INFLIGHT.discard(str(cid))


def resolve(chat_id: int, message: str, segments: str, index: int = 0) -> Path | None:
    """给媒体端点用：取第 index 个附件的本地文件。

    先看缓存；缓存没有就**当场试一次**（链接还活着就能救回来）。
    """
    items = parse_media(message, segments)
    if not (0 <= index < len(items)):
        return None
    cached = find_cached(chat_id, index)
    if cached is not None:
        return cached
    return _fetch_one(chat_id, index, items[index])


def summarize(message: str = "", segments: str = "") -> list[dict]:
    """给 feed 用的**轻量**描述（不含 bytes），前端据此拼 `<img>` / `<audio>`。

    `path` 只带 scope/id/n，真正的字节由 `/media` 端点现取——这样一轮 feed
    不会因为十几张图变成几 MB 的 JSON。
    """
    out: list[dict] = []
    for i, item in enumerate(parse_media(message, segments)):
        out.append({
            "type": item["type"],
            "name": item["name"],
            "duration": 0,
            "local": bool(item.get("local")),
            "index": i,
        })
    return out
