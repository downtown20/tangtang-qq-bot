# -*- coding: utf-8 -*-
"""桌面端后端桥 —— 本地 HTTP API，供 desktop/ 前端（Electron + React）调用。

## 为什么要有这一层

桌面端（Electron 渲染进程）**不直接读写文件**。原因不是洁癖：

  · 密钥字段的归一/迁移/体检逻辑在 ``agent/secret_refs.py``（单一事实源）。
    在 JS 里重写一遍必然漂移——那正是 2026-09-23 第三次 401 事故的形状。
  · config.yaml 的保存要「备份 + 合并未接入字段 + normalize」，这套语义
    控制台已经踩过一轮坑（见 save_config_file 的注释）。
  · bot 进程启停、日志流、SQLite 统计全在 Python 侧，跨语言桥比复写好。

所以：渲染进程 → HTTP(127.0.0.1:随机端口) → 本文件 → 复用 agent/ 的模块。

## 安全边界

只监听 127.0.0.1；每次启动生成随机 token，Electron 主进程从 stdout 读取后
通过 preload 注入渲染进程。所有请求（除 /api/health）必须带 token。
没有它就等于把「读写 config.yaml + 起停 bot」暴露给本机任意网页。

## 启动协议（Electron 主进程解析）

    python tools/desktop_bridge.py --port 0
    stdout 首行：BRIDGE_READY {"port": 51234, "token": "..."}
"""
from __future__ import annotations

import argparse
import inspect
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 可编辑资产层的错误要在 HTTP 层带上正确状态码（403/404/413），
# 而它继承自 ValueError——必须排在通用 except 之前。
from tools.asset_edit import AssetError as _AssetError  # noqa: E402

CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
DB_PATH = ROOT / "memory.db"

TOKEN = secrets.token_urlsafe(24)

# 进程内状态
_state_lock = threading.Lock()
_bot_proc: subprocess.Popen | None = None
_log_buffer: deque[str] = deque(maxlen=4000)
_log_seq = 0
_log_cond = threading.Condition()


# ═══════════════════════════════════════════════════════════════
# 配置读写（复用 agent/secret_refs，绝不在这里重写密钥语义）
# ═══════════════════════════════════════════════════════════════
#: 本次进程是否从 config.example.yaml 自举出了 config.yaml（前端据此提示「还没配置」）
_example_bootstrapped = False


def _bootstrap_config() -> bool:
    """没有 config.yaml 时，从 config.example.yaml 复制一份（与安装器同口径）。

    为什么桥自己要做（2026-10-26）：用户「下载解压 → 双击 启动桌面端.bat」并不
    经过安装器；桥一启动就 ``open(config.yaml)``，缺文件直接崩、前端白屏，而
    屏幕上没有任何线索说缺的是这个文件（教训 #30：检查写了却静默失败）。
    这段逻辑原先由 Qt 控制台的 ``load_config()`` 兜底，它已移入 ``运维/`` 且不再
    是主入口，所以兜底搬到这里。
    """
    global _example_bootstrapped
    if CONFIG_PATH.exists():
        return False
    example = ROOT / "config.example.yaml"
    if not example.is_file():
        return False
    try:
        shutil.copy2(example, CONFIG_PATH)
    except OSError:
        return False
    _example_bootstrapped = True
    return True


def _load_raw_config() -> dict:
    """读配置；缺文件时先尝试从模板自举（返回 {} 而不是抛异常——桥必须能起来）。"""
    _bootstrap_config()
    if not CONFIG_PATH.exists():
        return {}
    import yaml
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_dotenv() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _resolve_env_vars(obj, env: dict[str, str]):
    """与 main._resolve_env_vars 同构：整串匹配 ${VAR}，解析不到原样返回。"""
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v, env) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", obj)
        if m:
            return env.get(m.group(1), obj)
        return obj
    return obj


def _mask_secret(value) -> str:
    """给前端看的值：引用原样（${VAR} 本身不敏感），明文只露形状。"""
    s = "" if value is None else str(value)
    if not s:
        return ""
    if s.startswith("${"):
        return s
    if len(s) <= 8:
        return "•" * len(s)
    return f"{s[:4]}…{s[-3:]}（{len(s)} 字符）"


def _mask_config(cfg: dict) -> dict:
    from agent.secret_refs import secret_paths

    out = json.loads(json.dumps(cfg))
    for path, _env_name in secret_paths(cfg):
        node = out
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and path[-1] in node:
            node[path[-1]] = _mask_secret(node[path[-1]])
    return out


def _is_masked(value) -> bool:
    """前端只拿得到掩码（sk-a…xyz（35 字符））。它绝不能被当成真 Key 写回磁盘。"""
    s = "" if value is None else str(value)
    return "…" in s or s.endswith("字符）")


def _restore_masked_secrets(incoming: dict, existing: dict) -> list[str]:
    """把前端回传的掩码还原成磁盘上的原值，返回被还原的路径。"""
    from agent.secret_refs import secret_paths

    restored: list[str] = []

    def dig(data, path):
        node = data
        for k in path:
            if not isinstance(node, dict) or k not in node:
                return None
            node = node[k]
        return node

    def put(data, path, value):
        node = data
        for k in path[:-1]:
            if not isinstance(node, dict) or not isinstance(node.get(k), dict):
                return False
            node = node[k]
        if not isinstance(node, dict) or path[-1] not in node:
            return False
        node[path[-1]] = value
        return True

    for path, _env in secret_paths(existing):
        cur = dig(incoming, path)
        if _is_masked(cur):
            if put(incoming, path, dig(existing, path)):
                restored.append(".".join(path))

    # 多模型档位里的 api_key 也要还原（按 id 对齐）
    inc_models = dig(incoming, ("llm", "models"))
    ex_models = dig(existing, ("llm", "models"))
    if isinstance(inc_models, list) and isinstance(ex_models, list):
        by_id = {m.get("id"): m for m in ex_models if isinstance(m, dict)}
        for m in inc_models:
            if isinstance(m, dict) and _is_masked(m.get("api_key")):
                old = by_id.get(m.get("id"))
                if isinstance(old, dict):
                    m["api_key"] = old.get("api_key", "")
                    restored.append(f"llm.models[{m.get('id')}].api_key")
    return restored


# ═══════════════════════════════════════════════════════════════
# 业务端点
# ═══════════════════════════════════════════════════════════════
def api_health() -> dict:
    return {"ok": True, "root": str(ROOT), "python": sys.version.split()[0]}


def _coerce_llm_numbers(cfg: dict) -> list[str]:
    """把 llm 段里的数值字段收口成真正的数字（原地改，返回改了哪些路径）。

    **2026-10-24 实测事故**：桌面端把 max_tokens 以字符串写回配置
    （`llm.models[0].max_tokens: '64000'`），糖糖读出来原样喂给网关 →
    500 `json: cannot unmarshal string into Go struct field
    GeneralOpenAIRequest.max_tokens of type uint`，整轮回复变成
    「糖糖脑袋卡住了」。

    写侧归一（这里）+ 读侧兜底（`agent/handler.py::_normalize_llm_numeric`）
    两层都要有：只修一边，下一次保存又把脏数据写回去。
    """
    fixed: list[str] = []
    llm = cfg.get("llm")
    if not isinstance(llm, dict):
        return fixed
    targets: list[tuple[str, dict]] = [("llm", llm)]
    models = llm.get("models")
    if isinstance(models, list):
        for i, p in enumerate(models):
            if isinstance(p, dict):
                targets.append((f"llm.models[{i}]", p))
    for prefix, node in targets:
        for key, cast in (("max_tokens", int), ("temperature", float)):
            if key not in node:
                continue
            raw = node[key]
            if isinstance(raw, bool) or isinstance(raw, (int, float)):
                continue
            try:
                node[key] = cast(float(str(raw).strip()))
                fixed.append(f"{prefix}.{key}")
            except (TypeError, ValueError):
                pass
    return fixed


def api_config() -> dict:
    raw = _load_raw_config()
    env = _load_dotenv()
    resolved = _resolve_env_vars(raw, env)
    return {
        "raw": _mask_config(raw),
        "resolved": _mask_config(resolved),
        # configured=false 时前端必须给出「去跑安装器/填模板」的提示，而不是
        # 显示一个空表单让用户以为设置页坏了（2026-10-26 桌面端主入口化）。
        "configured": bool(raw),
        "bootstrapped": _example_bootstrapped,
        "example_available": (ROOT / "config.example.yaml").is_file(),
    }


# ═══════════════════════════════════════════════════════════════
# 连接密钥（.env 白名单）+ SnowLuma + 连接自检
#
# 2026-10-26：桌面端成为主入口，原先只有 Qt 控制台提供的「密钥管理 /
# 启动 SnowLuma / 连接自检」三件事必须能在桌面端完成，否则新用户装完
# 连不上 QQ，整个项目对他就是不能用的。
# ═══════════════════════════════════════════════════════════════
SNOWLUMA_WEBUI_URL = "http://127.0.0.1:5099"
SNOWLUMA_RELEASE_URL = "https://github.com/SnowLuma/SnowLuma/releases"
#: 允许写入 .env 的键由 agent/secret_refs.PROVIDER_KEY_ENV 决定（单一事实源）
_EXTRA_ENV_NAMES = {
    "SNOWLUMA_TOKEN": "SnowLuma 授权 Token",
    # 2026-10-27：独立的「连接密钥」页取消了，密钥改成跟着各自主人走。
    # 这两个变量名都是**代码真实读取**的，不是这里发明的：
    #   QWEN_KEY          ← agent/secret_refs.py:58（llm.vision.api_key 的映射）
    #   IMAGE_TAG_API_KEY ← agent/image_tagger.py:233 / :260 / :308
    # 白名单本身是安全边界，只放行「界面上真的有地方填」的键，不要放宽成任意键。
    "QWEN_KEY": "识图 API 密钥",
    "IMAGE_TAG_API_KEY": "图库打标 API 密钥",
}


def _secret_env_names() -> dict[str, str]:
    """{环境变量名: 人话标签}——只含「新用户最先要填的两个值」。

    LLM Key 的变量名跟着当前 provider 走（PROVIDER_KEY_ENV），绝不在这里
    另写一份映射：那份表同时被控制台写 .env、运行期回填、体检核对共用。
    """
    from agent.secret_refs import PROVIDER_KEY_ENV

    # 2026-10-27：放行**所有** provider 的变量名，不只是当前那一个。
    # 档位化之后一个档位可能用别的服务商（主档 deepseek、备用档 openai），
    # 只放行当前 provider 会让用户在备用档位里填不了 key。
    # 仍然是白名单：只有 PROVIDER_KEY_ENV 表里有的键 + 三个固定项。
    out = {env: f"LLM API Key（{prov}）" for prov, env in PROVIDER_KEY_ENV.items()}
    out.update(_EXTRA_ENV_NAMES)
    return out


def _mask_env_value(value: str) -> str:
    """给前端看的掩码：绝不回显明文。"""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "••••••"
    return f"••••{v[-4:]}"


def api_secrets() -> dict:
    env = _load_dotenv()
    names = _secret_env_names()
    return {
        "provider_env": next(iter(names)),
        "entries": [
            {"name": name, "label": label,
             "configured": bool(env.get(name)),
             "masked": _mask_env_value(env.get(name, ""))}
            for name, label in names.items()
        ],
        "env_path": str(ENV_PATH),
    }


def api_secrets_save(body: dict) -> dict:
    """把**白名单内**的一个键写进 .env（最小编辑：保留注释与其它行）。

    安全边界（不是可选项）：
      · 只允许 _secret_env_names() 里的键，其它一律拒绝；
      · 值里禁换行（否则可以注入一个新键）；
      · 不整份重写，只替换/追加目标行；写前备份、写后回读校验。
    """
    name = str(body.get("name") or "").strip()
    value = body.get("value")
    if not isinstance(value, str):
        raise ValueError("value 必须是字符串")
    # 只挡 \n/\r 是不够的：读侧 `_load_dotenv` 用 `splitlines()`，而
    # \v \f \x1c \x1d \x1e \x85 \u2028 \u2029 同样是行边界——
    # 这些字符一样能把「一个新的 KEY=... 行」注入进 .env（2026-10-27 审查发现）。
    # 判据直接问 `splitlines()` 自己，不手抄字符表（手抄必漏，这正是教训 #37 的形态）。
    if len(value.splitlines()) > 1:
        raise ValueError("value 不能包含换行或行分隔符（\\n \\r \\v \\f \\u2028 等）")
    if value != value.strip("\r\n"):
        raise ValueError("value 首尾不能带换行")
    allowed = _secret_env_names()
    if name not in allowed:
        raise ValueError(
            f"不允许写的键: {name!r}（只允许 {', '.join(sorted(allowed))}）")
    value = value.strip()
    if value.startswith("${"):
        raise ValueError("这里要填真实值，不要填 ${VAR} 引用")

    # ⚠ 必须走 read_bytes + 自己 decode：`read_text()` 默认 newline=None 是
    # universal newlines，会把 \r\n 读成 \n —— 于是下面那句 nl 判定**永远**是
    # "\n"，写回时 newline="" 直写就把整个文件的行尾从 CRLF 换成 LF。
    # 一次「最小编辑」变成「整文件重写」（2026-10-27 实测：3 个 CRLF → 3 个 LF）。
    text = (ENV_PATH.read_bytes().decode("utf-8")
            if ENV_PATH.exists() else "")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl) if text else []
    while lines and lines[-1] == "":
        lines.pop()

    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == name:
            if value:
                out.append(f"{name}={value}")
            # value 为空 = 删掉这一行，表示「未配置」
            replaced = True
        else:
            out.append(line)
    if not replaced and value:
        out.append(f"{name}={value}")

    # 注入自检的基线：写入前 .env 里已经有哪些键。
    before_keys = set(_load_dotenv())
    backup = None
    if ENV_PATH.exists():
        backup = ENV_PATH.with_name(
            ".env.bak_" + time.strftime("%Y%m%d_%H%M%S"))
        shutil.copy2(ENV_PATH, backup)
    ENV_PATH.write_text(nl.join(out) + (nl if out else ""),
                        encoding="utf-8", newline="")

    env = _load_dotenv()
    # 注入自检：写入后不许冒出预期外的键。
    # 比「字符黑名单」强——黑名单只能挡已知的行边界，键集合差能挡未知的。
    injected = sorted(set(env) - before_keys - {name})
    if injected:
        if backup is not None:
            shutil.copy2(backup, ENV_PATH)      # 写坏了就回滚，不留坏文件
        raise RuntimeError(
            "写入 .env 后出现了预期外的键，已回滚：" + ", ".join(injected))
    if value and env.get(name) != value:
        if backup is not None:
            shutil.copy2(backup, ENV_PATH)
        raise RuntimeError("写入 .env 后回读不一致——已回滚，请检查文件编码/权限")
    return {
        "ok": True,
        "name": name,
        "configured": bool(env.get(name)),
        "masked": _mask_env_value(env.get(name, "")),
        "backup": backup.name if backup else "",
    }


def _find_snowluma_dir() -> Path | None:
    """SnowLuma 安装目录（取最新版本；兼容直接解压到 SnowLuma/ 根）。"""
    base = ROOT / "SnowLuma"
    if not base.is_dir():
        return None
    cands = sorted(p for p in base.iterdir()
                   if p.is_dir() and (p / "launcher.bat").is_file())
    if cands:
        return cands[-1]
    return base if (base / "launcher.bat").is_file() else None


def _port_open(port: int, timeout: float = 0.4) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def api_snowluma_status() -> dict:
    d = _find_snowluma_dir()
    return {
        "installed": d is not None,
        "dir": str(d) if d else "",
        "running": _port_open(5099),
        "webui_url": SNOWLUMA_WEBUI_URL,
        "release_url": SNOWLUMA_RELEASE_URL,
    }


def api_snowluma_start() -> dict:
    """启动 SnowLuma（与 Qt 的 _start_snowluma 同口径）。

    ⚠ 不要给 stdout/stderr 加 DEVNULL：SnowLuma 首次启动把网页面板初始密码
    **只打印在它自己的新窗口里一次**（process.stdout.write，绕过了日志），
    2026-09-20 主人实机踩过——窗口开了、密码丢了，只能删 webui.json 重来。
    """
    d = _find_snowluma_dir()
    if d is None:
        raise ValueError(
            f"没找到 SnowLuma（期望 {ROOT / 'SnowLuma'}/SnowLuma-vX.Y.Z-win-x64/"
            f"launcher.bat）——请到官方发布页下载 Windows x64 版解压进去")
    if _port_open(5099):
        return {"ok": True, "already": True, "webui_url": SNOWLUMA_WEBUI_URL}
    launcher = d / "launcher.bat"
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    proc = subprocess.Popen([str(launcher)], cwd=str(d), creationflags=flags)
    return {
        "ok": True,
        "pid": proc.pid,
        "dir": str(d),
        "webui_url": SNOWLUMA_WEBUI_URL,
        "note": "首次启动会在新窗口打印网页面板初始密码（只显示一次，记下来再关窗口）。",
    }


def _stop_snowluma() -> bool:
    """杀掉正在监听 5099 的进程（SnowLuma 的 node 子进程）。真杀了返回 True。

    `/T` 连子树一起杀：`launcher.bat` 起的是 cmd → node，只杀 cmd 会留个孤儿 node
    继续占着 5099，后面 `api_snowluma_start` 就会以为「已经在跑」。
    """
    pid = _pid_on_port(5099)
    if not pid:
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, timeout=25)
    return True


def api_snowluma_restart() -> dict:
    """重启 SnowLuma：杀 → 等端口真的释放 → 再拉起。

    为什么要等：`taskkill` 返回不代表端口已经放开，立刻 Popen launcher.bat 会出现
    「新进程绑不上 5099、旧进程还没死」的竞态，表现为「点了重启，但还是原来那个」。
    """
    killed = _stop_snowluma()
    if killed:
        for _ in range(25):          # 最多等 7.5 秒
            if not _port_open(5099):
                break
            time.sleep(0.3)
    info = api_snowluma_start()
    info["restarted"] = killed
    return info


def _pid_on_port(port: int):
    """找正在监听某端口的进程 PID（Windows，用系统 netstat，不引新依赖）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        if "LISTENING" not in line.upper():
            continue
        if (":" + str(port)) not in line:
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            return int(parts[-1])
    return None


def _snowluma_restart_watcher():
    """消费 `.trigger_restart_snowluma`（2026-10-27）。

    `onebot/ws_client.py:1306` 在「SnowLuma 那侧不正常、需要它重启」时写这个文件。
    原来**只有**旧 Qt 控制台读它；Qt 移进 `运维/` 后这个信号就没人消费了，
    文件会一直躺在项目根目录。

    谁启动 SnowLuma，谁就该消费它 —— 桌面端现在就是那个启动者。
    桌面端没开时信号会留在磁盘上，下次开桥的第一轮循环会先补做一次。
    """
    signal_file = ROOT / ".trigger_restart_snowluma"
    while True:
        try:
            if signal_file.is_file():
                signal_file.unlink(missing_ok=True)
                _stop_snowluma()
                time.sleep(2)          # 让端口真的释放，避免绑不上
                info = api_snowluma_start()
                print("[bridge] 已消费 .trigger_restart_snowluma：SnowLuma 重启"
                      + ("（原本就在跑，端口仍占着）" if info.get("already") else ""),
                      flush=True)
        except Exception as e:  # noqa: BLE001 —— watcher 不许因为一次失败就死掉
            print("[bridge] 消费 .trigger_restart_snowluma 失败：" + repr(e), flush=True)
        time.sleep(3)


def _load_conn_check_module():
    import importlib.util as ilu
    path = ROOT / "tools" / "检查连接.py"
    if not path.is_file():
        raise ValueError("找不到 tools/检查连接.py（这一版发布包可能不完整）")
    spec = ilu.spec_from_file_location("_conn_check_bridge", path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def api_connect_check() -> dict:
    """跑连接自检，返回逐项结论（只读，不改任何配置）。"""
    mod = _load_conn_check_module()
    results = mod.check_all(ROOT)
    return {"results": [
        {"key": r.key, "title": r.title,
         "status": getattr(r.status, "value", str(r.status)),
         "detail": r.detail, "next_step": r.next_step}
        for r in results
    ]}


def api_scenario_packs() -> dict:
    """特殊模式包：配置 + **每个包当前的实际情况**（篇数/字数/占用/告警）。

    只读、不启动 bot：解析走 `agent/scenario_packs`（与 handler 同一份实现），
    统计走 `agent/intimate_reference.load`（与注入同一份实现）。
    """
    from agent import intimate_reference as _iref
    from agent import scenario_packs as _sp

    cfg = _load_raw_config()
    packs = _sp.normalize_packs(cfg)
    out = []
    for pack in packs:
        try:
            ref = _iref.load(None, pack)
        except Exception as e:                    # 一个包读不了不该让整页打不开
            out.append({**pack, "error": f"{type(e).__name__}: {e}"})
            continue
        budget = int(pack.get("budget") or 0)
        out.append({
            **pack,
            "files": len(ref.files),
            "chars": ref.chars,
            "slots": ref.slots,
            "warnings": list(ref.warnings),
            "usage_pct": round(ref.chars * 100 / budget) if budget else 0,
            "dir_exists": (ROOT / pack["dir"]).is_dir() if pack.get("dir") else False,
            # 这个包的场景在 scenarios/*.yaml 里还存不存在——配置指向已删除的场景
            # 现在**完全是静默的**（她永远不会进这个模式），所以要在这里报出来。
            "scenario_known": _scenario_exists(pack.get("scenario") or ""),
        })
    return {"packs": out, "available_scenarios": _available_scenarios()}


def _available_scenarios() -> list[dict]:
    """磁盘上现有的场景（name + display）。下拉用——让用户选，而不是背名字。"""
    import yaml
    out = []
    for f in sorted((ROOT / "scenarios").glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "display": str(data.get("display") or name),
            "type": str(data.get("type") or ""),
        })
    return out


def _scenario_exists(name: str) -> bool:
    """场景 name 在 scenarios/*.yaml 里存不存在（现读磁盘，不用进程内快照）。"""
    if not name:
        return False
    import yaml
    for f in sorted((ROOT / "scenarios").glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(data, dict) and str(data.get("name") or "").strip() == name:
            return True
    return False


def api_scenario_packs_save(body: dict) -> dict:
    """保存特殊模式包（只改 `scenario_reference` 这一块）。

    **不整份重写 config.yaml**：那份文件的注释是它的一半价值，而这里只会
    改 `scenario_reference` 的 `packs` 列表。做法是把它交给 `api_config_save`
    （已有备份 + 合并 + normalize 链路），而不是自己 open('w') ——
    自己写会绕过 `merge_missing_fields` 的判断，把别的字段悄悄补回来/删掉。
    """
    from agent import scenario_packs as _sp

    packs_in = (body or {}).get("packs")
    if not isinstance(packs_in, list):
        raise ValueError("packs 必须是列表")

    cleaned = []
    for item in packs_in:
        if not isinstance(item, dict):
            continue
        scenario = str(item.get("scenario") or "").strip()
        if not scenario:
            continue
        cleaned.append({
            "id": str(item.get("id") or "main").strip() or "main",
            "scenario": scenario,
            "dir": str(item.get("dir") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "budget": int(item.get("budget") or 0),
            "require_marker": bool(item.get("require_marker", True)),
            "ttl_seconds": max(0, int(item.get("ttl_seconds") or 0)),
            # 白名单必须跟得上字段：落在白名单外的键会在每次保存时**静默消失**。
            # 2026-10-26 实证：`allow_groups`（群聊是否允许注入这个包）就是被这里
            # 丢掉的，配置从 `true` 变「没有这个键」，而 handler 的守卫把缺失当 False，
            # 于是「群聊注入」被静默关掉。新增 pack 字段时**必须同步加到这里**。
            "allow_groups": bool(item.get("allow_groups", False)),
        })
    if not cleaned:
        raise ValueError("至少要有一个包（每个包要有 scenario）")

    # **文本级替换，不走 api_config_save**：那个函数用 yaml.safe_dump 整份重写，
    # 会把 config.yaml 的注释全部丢掉——那份文件的注释是理解成本的一半。
    # 这里只重写 `scenario_reference` 这个块（并保留它上方的说明注释）。
    import yaml as _yaml
    text = CONFIG_PATH.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)

    start = None
    for i, line in enumerate(lines):
        if line.startswith("scenario_reference:"):
            start = i
            break
    if start is None:
        raise ValueError("config.yaml 里没有顶层 scenario_reference，无法只改这一块")

    # 向上吃掉紧邻的说明注释（它们是这个键的文档，不能跟着被删）
    lead = start
    while lead > 0 and lines[lead - 1].lstrip().startswith("#"):
        lead -= 1

    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not lines[j][:1].isspace():
            end = j
            break

    header = lines[lead:start]                      # 原注释
    dumped = _yaml.safe_dump(
        {"scenario_reference": {"packs": cleaned}},
        allow_unicode=True, sort_keys=False).rstrip("\n").split("\n")
    new_block = header + dumped + [""]

    # 备份（自己备份，因为绕过了 api_config_save 的备份链路）
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = CONFIG_PATH.with_name(f"config.yaml.bak_{stamp}")
    backup.write_bytes(CONFIG_PATH.read_bytes())

    out = lines[:lead] + new_block + lines[end:]
    CONFIG_PATH.write_text(nl.join(out), encoding="utf-8", newline="")
    print(f"[bridge] 特殊模式包已保存（文本级替换），备份 {backup.name}", flush=True)

    # 存完回读：让界面看到解析后的实际形状（默认值补了什么）
    return {
        "ok": True,
        "backup": backup.name,
        "packs": _sp.normalize_packs(_load_raw_config()),
    }


def api_config_save(body: dict, *, merge_missing_fields: bool = True) -> dict:
    """保存配置：合并磁盘上未接入 UI 的字段 → 备份 → normalize → 写盘。

    `merge_missing_fields=False` 给「调用方已基于全量配置改写」的场景用
    （群管理的保存）：那种情况下补字段是**有害**的——它会把调用方故意删掉的
    `scenario` / `scenario_targets` 从旧配置里补回来，让「清除场景」静默失效。
    """
    from agent.secret_refs import normalize_secret_fields

    incoming = body.get("config") or {}
    if not isinstance(incoming, dict):
        raise ValueError("config 必须是对象")

    existing = _load_raw_config()
    restored = _restore_masked_secrets(incoming, existing)

    def merge_missing(dst: dict, src: dict):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(dst[k], dict) and isinstance(v, dict):
                merge_missing(dst[k], v)

    if merge_missing_fields:
        merge_missing(incoming, existing)
    changed = normalize_secret_fields(incoming)
    # 数值字段类型收口——否则前端表单的字符串会被原样写回（见函数注释）
    changed += _coerce_llm_numbers(incoming)
    backup = CONFIG_PATH.with_suffix(
        ".yaml.bak_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(CONFIG_PATH, backup)

    import yaml
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(incoming, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    return {"ok": True, "normalized": changed, "backup": backup.name,
            "restored_secrets": restored}


def _scenario_core(value: str) -> str:
    """去掉场景显示名里的 emoji 前缀：`❤️ 亲密模式` → `亲密模式`。

    `scenarios/*.yaml` 的 display 带 emoji 前缀，而 config.yaml 里历史上存的是
    不带前缀的名字（`亲密模式`）。只做字符串比较的话，保存一次就会把老值
    「升级」成带 emoji 的形式——用户什么都没改，配置却漂移了（2026-10-24
    实测：一次无改动保存把 `2524950083: 亲密模式` 改成了 `❤️ 亲密模式`）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(" ")
    return parts[-1].strip() if len(parts) > 1 else text


def _same_scenario_label(old, new) -> bool:
    """两个场景值是否指向**同一个场景**（忽略 display 的 emoji 前缀）。"""
    before = _scenario_core(old)
    after = _scenario_core(new)
    return bool(before) and before == after


def api_group_manage_save(body: dict) -> dict:
    """保存群管理：群场景 / 群内定向场景 / 全局场景目标 / 黑名单。

    只改 `config.yaml` 里这四处，其余字段（`groups.<gid>.owner/admins` 等）原样
    保留：

        groups.<群号>.scenario            群默认场景（存 name，如 'psychology'）
        groups.<群号>.scenario_targets     群内某个人单独指定（存 name）
        scenario_targets                  全局（私聊用，**存 display**，如 '亲密模式'）
        blacklist.groups / private_users   黑名单

    为什么「群内定向」存 name 而「全局」存 display：这是历史包袱，
    `handler.py` 两处读法不同（群内按 name 精确匹配，全局按 display 模糊匹配），
    这里**沿用现状**而不是统一——统一要动运行时解析，风险大于收益。

    两条硬纪律：
      · **等价值不写回**。前端拿到的是归一化后的 display（可能带 emoji），
        原样写回会造成配置漂移。这里用 `_same_scenario_label` 判断「仍是同一个
        场景」就保留磁盘上的原值。
      · **非法字段返回 ignored**。类型不对的字段被跳过而不是抛错（前端传坏了
        也不该 500），但必须让调用方看见「这条没生效」，否则就是静默失效。

    ⚠️ **不会热生效**：`handler.__init__` 启动时只读一次这两块配置
    （`self._group_blacklist` / `config["scenario_targets"]`），所以保存后必须
    重启糖糖。返回 `needs_restart=True`，前端据此提示——不能让界面假装改完就灵。
    """
    merged = _load_raw_config()
    ignored: list[str] = []
    for key in ("groups", "blacklist", "global_targets"):
        if body.get(key) is not None and not isinstance(body.get(key), dict):
            ignored.append(key)

    groups_in = body.get("groups")
    if isinstance(groups_in, dict):
        cur_groups = merged.get("groups")
        cur_groups = dict(cur_groups) if isinstance(cur_groups, dict) else {}
        for gid, patch in groups_in.items():
            sid = str(gid or "").strip()
            if not sid or not isinstance(patch, dict):
                continue
            node = cur_groups.get(sid)
            node = dict(node) if isinstance(node, dict) else {}
            scenario = str(patch.get("scenario") or "").strip()
            if scenario:
                old_sc = str(node.get("scenario") or "")
                node["scenario"] = (old_sc
                                    if _same_scenario_label(old_sc, scenario)
                                    else scenario)
            else:
                node.pop("scenario", None)
            targets_in = patch.get("targets")
            if isinstance(targets_in, dict):
                old_targets = node.get("scenario_targets")
                old_targets = old_targets if isinstance(old_targets, dict) else {}
                clean: dict[str, str] = {}
                for qq, sc in targets_in.items():
                    q = str(qq or "").strip()
                    s = str(sc or "").strip()
                    if not (q and s):
                        continue
                    old_t = str(old_targets.get(q) or "")
                    clean[q] = (old_t if _same_scenario_label(old_t, s) else s)
                if clean:
                    node["scenario_targets"] = clean
                else:
                    node.pop("scenario_targets", None)
            if node:
                cur_groups[sid] = node
            else:
                cur_groups.pop(sid, None)
        merged["groups"] = cur_groups

    blacklist_in = body.get("blacklist")
    if isinstance(blacklist_in, dict):
        cur_bl = merged.get("blacklist")
        cur_bl = dict(cur_bl) if isinstance(cur_bl, dict) else {}
        for key in ("groups", "private_users"):
            if isinstance(blacklist_in.get(key), list):
                cur_bl[key] = [str(x).strip() for x in blacklist_in[key]
                               if str(x).strip()]
        merged["blacklist"] = cur_bl

    global_in = body.get("global_targets")
    if isinstance(global_in, dict):
        old_global = merged.get("scenario_targets")
        old_global = old_global if isinstance(old_global, dict) else {}
        clean_g: dict[str, str] = {}
        for qq, sc in global_in.items():
            q = str(qq or "").strip()
            s = str(sc or "").strip()
            if not (q and s):
                continue
            old_g = str(old_global.get(q) or "")
            clean_g[q] = old_g if _same_scenario_label(old_g, s) else s
        merged["scenario_targets"] = clean_g

    saved = api_config_save({"config": merged}, merge_missing_fields=False)
    return {
        "ok": True,
        "needs_restart": True,
        "backup": saved.get("backup") or "",
        "normalized": saved.get("normalized") or [],
        "ignored": ignored,
    }

def api_models() -> dict:
    """模型清单。当前读扁平 llm.*；llm.models 结构由 /api/models/* 管理。"""
    raw = _load_raw_config()
    llm = raw.get("llm") or {}
    profiles = llm.get("models")
    if isinstance(profiles, list) and profiles:
        active = llm.get("active_model") or (profiles[0].get("id") if profiles else "")
        return {"profiles": profiles, "active": active, "legacy": False}
    # 兼容旧扁平结构：合成一个「默认」档
    return {
        "profiles": [{
            "id": "default",
            "label": llm.get("model") or "默认模型",
            "provider": llm.get("provider") or "deepseek",
            "base_url": llm.get("base_url") or "",
            "api_key": _mask_secret(llm.get("api_key")),
            "model": llm.get("model") or "",
            "max_tokens": llm.get("max_tokens"),
            "temperature": llm.get("temperature"),
            "thinking": llm.get("thinking"),
            "thinking_effort": llm.get("thinking_effort"),
        }],
        "active": "default",
        "legacy": True,
    }


def _normalize_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1") and not base.endswith("/v1beta"):
        base += "/v1"
    return base


def api_models_fetch(body: dict) -> dict:
    """输入 base_url + API Key → 拉取该端点的模型列表（OpenAI 兼容 /models）。"""
    import httpx

    base = _normalize_base(body.get("base_url", ""))
    key = (body.get("api_key") or "").strip()
    if not base:
        raise ValueError("缺少 base_url")
    # 引用形式（${VAR}）先解析，否则会把它当字面量发出去 → 401
    if key.startswith("${") and key.endswith("}"):
        key = _load_dotenv().get(key[2:-1], "")
    if not key:
        raise ValueError("缺少 API Key（或在 .env 里找不到对应的变量）")

    url = f"{base}/models"
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}"},
                      timeout=20.0)
    except Exception as e:
        raise ValueError(f"连接失败: {type(e).__name__}: {e}")
    if r.status_code == 401:
        raise ValueError("401 鉴权失败：这个 Key 与该接口地址不匹配")
    if r.status_code >= 400:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        raise ValueError(f"返回不是 JSON: {r.text[:200]}")
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("返回里没有 data 列表（不是 OpenAI 兼容格式）")
    ids = [str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")]
    return {"models": ids, "count": len(ids), "endpoint": url}


def api_models_activate(body: dict) -> dict:
    """只切换当前模型档位（可选同时改推理档位），不动其它配置。

    为什么单独一个端点：模型切换器在输入框里一点就该生效，而「保存设置」
    会重写整份 config.yaml。这里只改 llm 下的少数几个键，并且**用磁盘原文**
    （_load_raw_config 不做 mask）来同步扁平字段——profile 里存的可能是
    ``${CUSTOM_LLM_KEY}`` 引用，用掩码值回写会直接把密钥写坏。
    """
    raw = _load_raw_config()
    llm = dict(raw.get("llm") or {})
    profiles = llm.get("models")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("当前是旧版单模型配置，请先到「设置 → AI 模型」添加模型档位")
    target = str(body.get("id") or "").strip()
    if not target:
        raise ValueError("缺少模型档位 id")

    has_effort = "thinking_effort" in body
    effort = str(body.get("thinking_effort") or "").strip() if has_effort else None
    has_thinking = "thinking" in body
    thinking = str(body.get("thinking") or "").strip() if has_thinking else None

    sel = None
    new_profiles = []
    for p in profiles:
        if isinstance(p, dict) and str(p.get("id")) == target:
            p = dict(p)
            if has_effort:
                p["thinking_effort"] = effort
            if has_thinking:
                p["thinking"] = thinking
            sel = p
        new_profiles.append(p)
    if sel is None:
        raise ValueError(f"找不到模型档位: {target}")

    llm["models"] = new_profiles
    llm["active_model"] = target
    for key in ("provider", "base_url", "model", "api_key"):
        if sel.get(key):
            llm[key] = sel[key]
    for key in ("max_tokens", "temperature"):
        if sel.get(key) not in (None, ""):
            llm[key] = sel[key]
    if has_effort:
        llm["thinking_effort"] = effort
    if has_thinking:
        llm["thinking"] = thinking
    elif not has_effort and sel.get("thinking"):
        llm["thinking"] = sel["thinking"]

    backup = CONFIG_PATH.with_suffix(
        ".yaml.bak_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(CONFIG_PATH, backup)
    raw["llm"] = llm
    # 数值字段类型收口——档位里的 max_tokens/temperature 可能是字符串，
    # 直接写盘会让糖糖下一轮 LLM 调用 500（见 _coerce_llm_numbers 注释）
    coerced = _coerce_llm_numbers(raw)
    import yaml
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)
    return {
        "ok": True,
        "active": target,
        "model": sel.get("model"),
        "provider": sel.get("provider"),
        "thinking_effort": llm.get("thinking_effort") or "",
        "backup": backup.name,
        "coerced": coerced,
    }


def api_chat(body: dict) -> dict:
    """本机测试对话：用「当前生效的模型」直接问一句，不经过 QQ/agent。

    用途：验证模型配置是否通（换了 Key/端点后立刻能确认），
    以及快速感受这个模型的口气。不注入人格与记忆——那不是这个端点的职责。
    """
    import httpx

    message = (body.get("message") or "").strip()
    if not message:
        raise ValueError("消息不能为空")

    cfg = _resolve_env_vars(_load_raw_config(), _load_dotenv())
    llm = cfg.get("llm") or {}
    profiles = llm.get("models")
    prof = None
    if isinstance(profiles, list) and profiles:
        active = llm.get("active_model")
        prof = next((p for p in profiles
                     if isinstance(p, dict) and p.get("id") == active), None)
        if prof is None:
            prof = next((p for p in profiles if isinstance(p, dict)), None)
    prof = prof or {}

    provider = prof.get("provider") or llm.get("provider") or "custom"
    base = _normalize_base(prof.get("base_url") or llm.get("base_url") or "")
    key = prof.get("api_key") or llm.get("api_key") or ""
    model = prof.get("model") or llm.get("model") or ""

    if not base:
        raise ValueError("当前模型没有配置接口地址")
    if not key:
        raise ValueError("当前模型没有可用的 API Key")
    if not model:
        raise ValueError("当前模型没有配置模型名")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "max_tokens": int(prof.get("max_tokens") or 512),
        "temperature": float(prof.get("temperature") or 0.8),
        "stream": False,
    }
    try:
        r = httpx.post(f"{base}/chat/completions", json=payload,
                       headers={"Authorization": f"Bearer {key}"}, timeout=90.0)
    except Exception as e:
        raise ValueError(f"连接失败: {type(e).__name__}: {e}")
    if r.status_code == 401:
        raise ValueError("401：这个 Key 与该接口地址不匹配")
    if r.status_code >= 400:
        raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    reply = msg.get("content") or msg.get("reasoning_content") or ""
    return {"reply": str(reply).strip(), "model": model, "provider": provider}


def api_groups() -> dict:
    """群列表：群名/人数（group_info）+ 最近消息时间（chat_log）+ 场景配置（config.groups）。"""
    raw = _load_raw_config()
    cfg_groups = raw.get("groups") or {}
    info: dict[str, tuple] = {}
    last: dict[str, str] = {}

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            cur = conn.cursor()
            try:
                cur.execute("SELECT group_id, group_name, member_count FROM group_info")
                info = {str(r[0]): (r[1], r[2]) for r in cur.fetchall()}
            except sqlite3.Error:
                info = {}
            try:
                cur.execute("SELECT group_id, MAX(timestamp) FROM chat_log GROUP BY group_id")
                last = {str(r[0]): r[1] for r in cur.fetchall()}
            except sqlite3.Error:
                last = {}
            conn.close()
        except sqlite3.Error:
            pass

    ids = set(info) | set(str(k) for k in cfg_groups) | set(last)
    out = []
    for gid in ids:
        # 历史库里存在 group_id 为空串的脏记录（会渲染成「群 」并污染排序），跳过
        if not gid or not str(gid).strip():
            continue
        name, count = info.get(gid, (None, None))
        gcfg = cfg_groups.get(gid) or {}
        out.append({
            "id": gid,
            "name": name or f"群 {gid[-4:]}",
            "member_count": count,
            "last_at": last.get(gid),
            "scenario": (gcfg.get("scenario") or "") if isinstance(gcfg, dict) else "",
        })
    out.sort(key=lambda g: (g["last_at"] or ""), reverse=True)
    return {"groups": out, "count": len(out)}


def api_stats() -> dict:
    out = {"people": None, "memories": None, "chats": None, "stickers": None}
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3)
            cur = conn.cursor()
            for k, table in (("people", "people"), ("memories", "memories"),
                             ("chats", "chat_log")):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    out[k] = cur.fetchone()[0]
                except sqlite3.Error:
                    out[k] = None
            conn.close()
        except sqlite3.Error:
            pass
    meta = ROOT / "stickers" / "metadata.json"
    if meta.exists():
        try:
            out["stickers"] = len(json.loads(meta.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


# ═══════════════════════════════════════════════════════════════
# 图库（贴图 stickers/ ＋ CG stickers_cg/）
#
# 两套库的元数据同名但字段不同：
#   stickers/metadata.json    {文件名: {emotions, desc, source}}
#   stickers_cg/metadata.json {文件名: {emotions, scene_tags, act, emotion_desc}}
# 这里读出统一视图（emotions / scene_tags / act / desc），写回时只碰
# emotions —— scene_tags 是识图流水线的产物，UI 不该改写它。
#
# 缩略图：原图最大 7.9MB、956 张共 233MB，直接塞给 <img> 会把渲染进程拖死。
# 所以按需生成 320px JPEG 缓存（~11ms/张）到系统临时目录，不污染仓库。
# ═══════════════════════════════════════════════════════════════
GALLERY_SOURCES = {
    "sticker": ROOT / "stickers",
    "murasame": ROOT / "stickers_murasame",
    "michele": ROOT / "stickers_michele",
    "cg": ROOT / "stickers_cg",
}
# 展示名。murasame / michele 是 agent 侧已存在的两套「角色贴图」
# （handler.py 的 StickerManager 分别挂在 ./stickers_murasame、./stickers_michele），
# 只是图库页之前没接进来。
GALLERY_LABELS = {
    "sticker": "贴图",
    "murasame": "丛雨",
    "michele": "米雪儿",
    "cg": "CG",
}
# 「贴图」这个一级分段是三个贴图库的合集；CG 单独一组。
GALLERY_GROUPS = {
    "sticker_all": ("sticker", "murasame", "michele"),
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif"}
THUMB_ROOT = Path(tempfile.gettempdir()) / "tangtang-thumbs"
_META_CACHE: dict[str, tuple[float, dict]] = {}
_meta_lock = threading.Lock()


def _gallery_dir(source: str) -> Path:
    """解析成**单个**库目录（写操作只允许单库，避免一次请求跨库误伤）。"""
    d = GALLERY_SOURCES.get(source)
    if d is None:
        raise ValueError(f"未知图库来源: {source}（可选 {', '.join(GALLERY_SOURCES)}）")
    return d


def _gallery_keys(source: str) -> list[str]:
    """列表读取用：source 可以是单库，也可以是 GALLERY_GROUPS 里的合集。"""
    if source in GALLERY_GROUPS:
        return [k for k in GALLERY_GROUPS[source] if k in GALLERY_SOURCES]
    _gallery_dir(source)  # 校验合法性
    return [source]


def _safe_name(name: str) -> str:
    """只接受纯文件名——挡住 ../、绝对路径、子目录、非图片后缀。

    渲染进程传来的 name 本质是不可信输入（即便它由我们自己的列表接口产出），
    一旦放行 '..' 就能读/删任意文件。
    """
    name = (name or "").strip()
    if not name or name in (".", ".."):
        raise ValueError("非法文件名")
    if "/" in name or "\\" in name or ":" in name:
        raise ValueError("非法文件名：不允许路径分隔符")
    if Path(name).name != name:
        raise ValueError("非法文件名：不允许子路径")
    if Path(name).suffix.lower() not in IMAGE_EXTS:
        raise ValueError("不是图片文件")
    return name


def _load_meta(source: str) -> dict:
    """读 metadata.json（按 mtime 缓存，避免每页都解析 400KB）。"""
    path = _gallery_dir(source) / "metadata.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _meta_lock:
        cached = _META_CACHE.get(source)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    with _meta_lock:
        _META_CACHE[source] = (mtime, data)
    return data


def _gallery_items(source: str, meta: dict) -> list[dict]:
    """列出某个库的全部图片（统一字段），不做筛选。"""
    d = _gallery_dir(source)
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        info = meta.get(p.name) or {}
        if not isinstance(info, dict):
            info = {}
        try:
            st = p.stat()
        except OSError:
            continue
        emotions = [str(x) for x in (info.get("emotions") or []) if x]
        scene = [str(x) for x in (info.get("scene_tags") or []) if x]
        out.append({
            "name": p.name,
            "source": source,
            "ext": p.suffix.lower(),
            "size": st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "emotions": emotions,
            "scene_tags": scene,
            "act": str(info.get("act") or ""),
            "desc": str(info.get("emotion_desc") or info.get("desc") or ""),
        })
    return out


def _tag_counter(items: list[dict], field: str) -> list[dict]:
    counter: dict[str, int] = {}
    for it in items:
        for tag in it.get(field) or []:
            counter[tag] = counter.get(tag, 0) + 1
    return [
        {"tag": t, "count": n}
        for t, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


_SOURCE_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_count_lock = threading.Lock()


def _source_counts() -> list[dict]:
    """各库图片数（60s 缓存）—— 供前端渲染分段控件上的数量。"""
    now = time.time()
    out = []
    for key, d in GALLERY_SOURCES.items():
        with _count_lock:
            cached = _SOURCE_COUNT_CACHE.get(key)
        if cached and now - cached[0] < 60:
            n = cached[1]
        else:
            n = 0
            if d.exists():
                n = sum(
                    1 for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                )
            with _count_lock:
                _SOURCE_COUNT_CACHE[key] = (now, n)
        out.append({"key": key, "label": GALLERY_LABELS.get(key, key), "count": n})
    return out


def api_gallery(params: dict) -> dict:
    """图库列表：筛选 + 排序 + 分页，并附带标签聚合（供筛选器渲染）。"""
    source = (params.get("source") or "sticker").strip()
    q = (params.get("q") or "").strip().lower()
    tag = (params.get("tag") or "").strip()
    tag_field = (params.get("tag_field") or "emotions").strip()
    state = (params.get("state") or "").strip()      # tagged | untagged | ''
    sort = (params.get("sort") or "mtime_desc").strip()

    def clamp_int(raw, default, lo, hi):
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    page = clamp_int(params.get("page"), 1, 1, 10 ** 6)
    page_size = clamp_int(params.get("page_size"), 60, 12, 200)

    # source 可以是单库（sticker/murasame/michele/cg），也可以是 "sticker_all"
    # —— 后者把三套贴图合并展示（每项仍带自己的 source，写操作按 source 分组回调用）。
    keys = _gallery_keys(source)
    items: list[dict] = []
    for k in keys:
        items.extend(_gallery_items(k, _load_meta(k)))
    total_all = len(items)
    emotions = _tag_counter(items, "emotions")
    scene_tags = _tag_counter(items, "scene_tags")

    if tag:
        items = [it for it in items if tag in (it.get(tag_field) or [])]
    if state == "tagged":
        items = [it for it in items if it["emotions"] or it["scene_tags"]]
    elif state == "untagged":
        items = [it for it in items if not it["emotions"] and not it["scene_tags"]]
    if q:
        def hit(it: dict) -> bool:
            hay = " ".join([
                it["name"], it["desc"], it["act"],
                " ".join(it["emotions"]), " ".join(it["scene_tags"]),
            ]).lower()
            return q in hay
        items = [it for it in items if hit(it)]

    if sort == "mtime_asc":
        items.sort(key=lambda it: it["mtime"])
    elif sort == "size_desc":
        items.sort(key=lambda it: -it["size"])
    elif sort == "size_asc":
        items.sort(key=lambda it: it["size"])
    elif sort == "name":
        items.sort(key=lambda it: it["name"].lower())
    elif sort == "tagged_first":
        items.sort(key=lambda it: (not (it["emotions"] or it["scene_tags"]), it["mtime"]))
    else:
        items.sort(key=lambda it: it["mtime"], reverse=True)

    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "total_all": total_all,
        "source": source,
        "keys": keys,
        "sources": _source_counts(),
        "emotions": emotions,
        "scene_tags": scene_tags,
    }


def _thumb_cache_path(source: str, name: str, w: int) -> Path:
    d = THUMB_ROOT / source / str(w)
    d.mkdir(parents=True, exist_ok=True)
    return d / (name + ".jpg")


def build_thumb(source: str, name: str, w: int = 320) -> tuple[bytes, str]:
    """返回 (图片字节, content-type)。首次生成后落盘缓存，原图变了才重建。"""
    src = _gallery_dir(source) / _safe_name(name)
    if not src.exists():
        raise ValueError("图片不存在")
    w = max(64, min(1024, int(w or 320)))
    cache = _thumb_cache_path(source, name, w)
    try:
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            return cache.read_bytes(), "image/jpeg"
    except OSError:
        pass
    try:
        from PIL import Image
        im = Image.open(src)
        im = im.convert("RGB")          # GIF/WebP 取首帧，透明底压白
        im.thumbnail((w, w))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        data = buf.getvalue()
    except Exception:
        # 没有 PIL 或图片损坏：退回原图（慢，但至少能看）
        return src.read_bytes(), "application/octet-stream"
    try:
        cache.write_bytes(data)
    except OSError:
        pass
    return data, "image/jpeg"


def _save_meta(source: str, data: dict) -> str:
    """备份 → 原子写 metadata.json（写坏了整库标签全丢，必须先备份）。"""
    path = _gallery_dir(source) / "metadata.json"
    backup = path.with_name(
        path.stem + ".bak_" + time.strftime("%Y%m%d_%H%M%S") + path.suffix)
    if path.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    with _meta_lock:
        _META_CACHE.pop(source, None)
    return backup.name


def api_gallery_tags(body: dict) -> dict:
    """批量加/减情绪标签（只改 emotions，不动识图产物 scene_tags）。"""
    source = str(body.get("source") or "")
    names = body.get("names") or []
    add = [str(t).strip() for t in (body.get("add") or []) if str(t).strip()]
    remove = [str(t).strip() for t in (body.get("remove") or []) if str(t).strip()]
    if not isinstance(names, list) or not names:
        raise ValueError("没有选中图片")
    if not add and not remove:
        raise ValueError("没有要修改的标签")

    d = _gallery_dir(source)
    meta = dict(_load_meta(source))
    changed = 0
    for raw in names:
        name = _safe_name(str(raw))
        if not (d / name).exists():
            continue
        info = meta.get(name)
        if not isinstance(info, dict):
            info = {}
        emotions = [str(x) for x in (info.get("emotions") or []) if x]
        before = list(emotions)
        for t in add:
            if t not in emotions:
                emotions.append(t)
        for t in remove:
            if t in emotions:
                emotions.remove(t)
        if emotions != before:
            info = {**info, "emotions": emotions}
            meta[name] = info
            changed += 1
    backup = None
    if changed:
        backup = _save_meta(source, meta)
    return {"ok": True, "changed": changed, "backup": backup}


def _image_tagger():
    """延迟导入打标模块：它要读 config.yaml、可能要加载打标脚本，图库页平时用不到。"""
    from agent import image_tagger
    return image_tagger


def api_gallery_tag_status(params: dict) -> dict:
    """打标现状：配置（**隐去 key**）+ 图库扫描 + 这一轮进度。

    「进度」和「还剩多少张」分两个字段给：合成一个的话，跑完那一刻两个数字会互相打架。
    """
    return _image_tagger().status()


def api_gallery_tag_run(body: dict) -> dict:
    """启动一轮打标（后台线程，立刻返回；进度靠 status 轮询）。"""
    b = body or {}
    try:
        limit = int(b.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return _image_tagger().run(full=bool(b.get("full")), limit=limit)


def api_gallery_tag_cancel(body: dict) -> dict:
    return _image_tagger().cancel()


def api_gallery_tag_probe(body: dict) -> dict:
    """试打一张：**不写 metadata**，只回答「这条路通不通、模型吐什么」。"""
    b = body or {}
    return _image_tagger().describe_one(str(b.get("name") or ""))


def api_gallery_trash(body: dict) -> dict:
    """批量移入回收站（不是真删）：stickers/_trash/<时间戳>/ 下可手动恢复。"""
    source = str(body.get("source") or "")
    names = body.get("names") or []
    if not isinstance(names, list) or not names:
        raise ValueError("没有选中图片")
    d = _gallery_dir(source)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash = d / "_trash" / stamp
    trash.mkdir(parents=True, exist_ok=True)

    meta = dict(_load_meta(source))
    moved, missing = [], []
    for raw in names:
        name = _safe_name(str(raw))
        src = d / name
        if not src.exists():
            missing.append(name)
            continue
        shutil.move(str(src), str(trash / name))
        meta.pop(name, None)
        moved.append(name)
    backup = _save_meta(source, meta) if moved else None
    return {
        "ok": True, "moved": moved, "missing": missing,
        "trash_dir": str(trash.relative_to(ROOT)), "backup": backup,
    }


def api_gallery_reveal(body: dict) -> dict:
    """在系统文件管理器里定位这张图（右键菜单用）。"""
    source = str(body.get("source") or "")
    name = _safe_name(str(body.get("name") or ""))
    target = _gallery_dir(source) / name
    if not target.exists():
        raise ValueError("文件不存在")
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{target}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except Exception as e:
        raise ValueError(f"无法打开文件管理器: {type(e).__name__}: {e}")
    return {"ok": True, "path": str(target)}


def _find_bot_pid() -> int | None:
    """找到糖糖本体进程的 PID，不管它是谁启动的。

    为什么需要：用户经常从控制台 / 命令行 / 开机脚本启动糖糖，这时 bridge
    的 `_bot_proc` 是 None。旧实现只看 `_bot_proc`，于是概览页显示
    「糖糖进程 未启动」，而同一时刻会客厅页面显示「糖糖在线」
    （2026-10-24 实测两个页面自相矛盾）。

    判定顺序：bridge 自己 spawn 的 → 会客厅接口活着 → 谁在监听 bot 的
    WebSocket 端口（3001）。
    """
    if _bot_proc is not None and _bot_proc.poll() is None:
        return _bot_proc.pid
    try:
        if _console_call("GET", "/health", timeout=2.0).get("ok"):
            pid = _port_owner(8765)
            if pid:
                return pid
    except Exception:
        pass
    return _port_owner(3001)


def _port_owner(port: int) -> int | None:
    """谁在 LISTEN 这个端口。拿不到返回 None（不抛异常）。"""
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            if (conn.laddr and getattr(conn.laddr, "port", None) == port
                    and conn.status == psutil.CONN_LISTEN and conn.pid):
                return int(conn.pid)
    except Exception:
        pass
    return None


def _bot_running() -> bool:
    return _find_bot_pid() is not None


def api_status() -> dict:
    raw = _load_raw_config()
    llm = raw.get("llm") or {}
    pid = _find_bot_pid()
    return {
        "bot": {
            "running": pid is not None,
            "pid": pid,
            # 会客厅是否可用——桌面端「会客厅」页据此决定显示引导态还是页面
            "console": _console_available(),
        },
        "llm": {
            "provider": llm.get("provider"),
            "model": llm.get("model"),
            "models_count": len(llm.get("models") or []) if isinstance(llm.get("models"), list) else 0,
        },
        "log_seq": _log_seq,
    }


def _console_available() -> bool:
    try:
        return bool(_console_call("GET", "/health", timeout=2.0).get("ok"))
    except Exception:
        return False


def api_bot_start() -> dict:
    global _bot_proc
    with _state_lock:
        existing = _find_bot_pid()
        if existing is not None:
            # 可能是 bridge 自己启的，也可能是外部启的——两种情况都算「已在跑」
            return {"ok": True, "already": True, "pid": existing}
        launcher = ROOT / "main.py"
        if not launcher.exists():
            raise ValueError("找不到 main.py")
        proc = subprocess.Popen(
            [sys.executable, str(launcher)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        _bot_proc = proc

        def pump():
            global _log_seq
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                with _log_cond:
                    _log_buffer.append(line)
                    _log_seq += 1
                    _log_cond.notify_all()

        threading.Thread(target=pump, daemon=True).start()
        return {"ok": True, "pid": proc.pid}


def api_bot_stop() -> dict:
    global _bot_proc
    with _state_lock:
        pid = _find_bot_pid()
        if pid is None:
            return {"ok": True, "already": True}
        stopped_external = _bot_proc is None or _bot_proc.poll() is not None
        if not stopped_external:
            proc = _bot_proc
            assert proc is not None
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            _bot_proc = None
            return {"ok": True, "pid": pid, "external": False}
    # 外部启动的糖糖：terminate 拿不到句柄，用 psutil 结束（优雅 → 强杀）
    try:
        import psutil
        target = psutil.Process(pid)
        target.terminate()
        try:
            target.wait(timeout=8)
        except psutil.TimeoutExpired:
            target.kill()
            target.wait(timeout=5)
        return {"ok": True, "pid": pid, "external": True}
    except Exception as e:
        raise RuntimeError(f"糖糖是外部启动的（PID {pid}），停止失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 会客厅转发（2026-10-24）
#
# 会客厅的实体在 **bot 进程内**（agent/console_api.py）——那里才有唯一的糖糖。
# bridge 只做一层薄转发：桌面端 → bridge → 127.0.0.1:<console.port>。
#
# 为什么不在 bridge 里内嵌一份糖糖：会分裂状态文件与短期记忆窗口（两个糖糖）。
# 代价是糖糖没启动时这里只能报「未开门」，前端据此显示引导态。
# ═══════════════════════════════════════════════════════════════
CONSOLE_HOST = "127.0.0.1"
CONSOLE_DEFAULT_PORT = 8765


def _console_port() -> int:
    try:
        cfg = _load_raw_config()
        return int(((cfg.get("console") or {}).get("port")) or CONSOLE_DEFAULT_PORT)
    except Exception:
        return CONSOLE_DEFAULT_PORT


def _console_call(method: str, path: str, params: dict | None = None,
                  body: dict | None = None, timeout: float = 30.0):
    """把请求转给 bot 进程里的会客厅接口。连不上统一转成一句人话。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    port = _console_port()
    url = f"http://{CONSOLE_HOST}:{port}{path}"
    if params:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
        if qs:
            url += "?" + qs
    data = None
    if method in ("POST", "PUT", "PATCH"):
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise ValueError(detail or f"会客厅返回 HTTP {e.code}")
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
        raise RuntimeError("会客厅未开门——请先在总览页启动糖糖")


def _console_bytes(path: str, params: dict | None = None) -> tuple[bytes, str]:
    """从会客厅取二进制（图片/语音）。不走 JSON —— 基类那 33% 体积不值得。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    port = _console_port()
    url = f"http://{CONSOLE_HOST}:{port}{path}"
    if params:
        qs = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")})
        if qs:
            url += "?" + qs
    try:
        with urllib.request.urlopen(url, timeout=30.0) as r:
            return r.read(), r.headers.get("Content-Type") or "application/octet-stream"
    except urllib.error.HTTPError as e:
        # 410 = QQ 的临时链接过期且本地没有缓存。这是常态，前端要显示
        # 「媒体已过期」而不是报错弹窗。
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise ValueError(detail or f"会客厅返回 HTTP {e.code}")
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
        raise RuntimeError("会客厅未开门——请先在总览页启动糖糖")


def api_console_status() -> dict:
    """会客厅是否可用。**这个端点永远返回 200**，前端靠 available 判断。"""
    port = _console_port()
    try:
        health = _console_call("GET", "/health", timeout=3.0)
        return {"available": True, "port": port, "bot_online": True, **health}
    except Exception as e:
        return {"available": False, "port": port, "bot_online": False,
                "reason": str(e)}


def api_console_sessions() -> dict:
    return _console_call("GET", "/sessions")


def api_console_new_session(body: dict) -> dict:
    return _console_call("POST", "/sessions", body=body)


def api_console_messages(params: dict) -> dict:
    return _console_call("GET", "/messages", params=params)


def api_console_snapshot() -> dict:
    return _console_call("GET", "/snapshot", timeout=15.0)


def api_console_traces(params: dict) -> dict:
    """某个会话的回合痕迹列表（思维链 + 工具步骤）。"""
    return _console_call("GET", "/traces", params=params, timeout=15.0)


def api_console_trace(params: dict) -> dict:
    """单条回合痕迹：run_id 或 event_key 二选一。"""
    return _console_call("GET", "/trace", params=params, timeout=15.0)


def api_console_replay(params: dict) -> dict:
    """一条回复的完整回合回放：思维链 → 工具链 → 决定 → 对方反应。

    比 /trace 多带了 decision_runs 和 feedback 两段，前端点开消息一次拿全。
    """
    return _console_call("GET", "/replay",
                         params={"event_key": params.get("event_key") or ""},
                         timeout=20.0)


def api_console_groups() -> dict:
    """群管理页数据：群列表 + 场景清单 + 现有绑定。

    读走 bot（要 `chat_log` 的消息量与最后活跃），写走 bridge 的
    `/api/group-manage/save`（只碰 config.yaml）。
    """
    return _console_call("GET", "/groups", timeout=25.0)


def api_console_memory_graph(params: dict) -> dict:
    """记忆图谱：center 空 → 全局（人—类别）；center=QQ → 那个人的认知切片。"""
    return _console_call(
        "GET", "/memory-graph",
        params={"center": params.get("center") or "",
                "limit": params.get("limit") or 40},
        timeout=30.0)


def api_console_memory_profile(params: dict) -> dict:
    """记忆剖面：七维索引 + 六层架构 + 全局存储能力。

    比 memory-graph 重（多查证据样本和全局统计），给足超时。
    """
    return _console_call(
        "GET", "/memory-profile",
        params={"qq": params.get("qq") or ""},
        timeout=45.0)


def api_console_memory_history(params: dict) -> dict:
    """记忆演化史：同一主题的版本链（A 被 B 取代 → B 被 C 取代）+ 断链清单。"""
    return _console_call(
        "GET", "/memory-history",
        params={"qq": params.get("qq") or ""},
        timeout=40.0)


def api_console_memory_revisions(params: dict) -> dict:
    """记忆变更流水：每次改记忆的「何时/谁/从什么/改成什么/为什么」。

    与 memory-history 分工：history 是从 superseded_by 推出来的版本链（含历史，
    只认「整行被替代」）；revisions 是写操作的事实记录（能表达内容改写与删除，
    但只有 2026-10-24 之后）。两者在 UI 上分开显示，不合并。
    """
    return _console_call(
        "GET", "/memory-revisions",
        params={"qq": params.get("qq") or "",
                "limit": params.get("limit") or 200},
        timeout=40.0)


def api_console_sources() -> dict:
    """会话源列表：本地历史 + 私聊镜像 + 群聊镜像。"""
    return _console_call("GET", "/sources", timeout=20.0)


def api_console_feed(params: dict) -> dict:
    """某会话的消息流（`scope` + `after_id` + `limit`）。"""
    return _console_call("GET", "/feed", params=params, timeout=20.0)


def api_console_search(params: dict) -> dict:
    """搜 QQ 镜像的**消息正文**（会客厅侧栏搜索，不是搜会话标题）。"""
    return _console_call(
        "GET", "/search",
        params={"q": params.get("q") or "", "limit": params.get("limit") or 40},
        timeout=20.0,
    )


def api_console_send(body: dict) -> dict:
    """统一发送：本地注入 / 真发 QQ。走真身时可能几十秒。"""
    return _console_call("POST", "/send", body=body, timeout=280.0)


def api_console_say(body: dict) -> dict:
    return _console_call("POST", "/say", body=body, timeout=280.0)


def api_console_turn(body: dict) -> dict:
    return _console_call("POST", "/turn", body=body, timeout=280.0)


def api_console_delete(session_id: str) -> dict:
    import urllib.parse
    sid = urllib.parse.quote(str(session_id), safe="")
    return _console_call("DELETE", f"/sessions/{sid}", timeout=15.0)


# ═══════════════════════════════════════════════════════════════
# 可编辑资产（2026-10-26 第 49 轮）
#
# 主人要「参数完全暴露可以改，自由度拉满」——人格卡 / 协议条文 / 场景规则 /
# 知识库都能在桌面上改。
#
# **规则不在这里**：白名单、备份、校验全在 `tools/asset_edit.py`（那里能单测，
# 而且 `tests/test_asset_edit.py` 的 26 条闸门盯着它）。本层只做三件事：
# 拆参数、调过去、把 `AssetError.status` 原样回给前端。
# 判据只有一处——本层**不许**再判断一次「这个路径能不能改」（教训 #38）。
# ═══════════════════════════════════════════════════════════════

def _asset_edit():
    """惰性导入：bridge 的很多端点跟资产无关，没必要启动就拉起 yaml 依赖。"""
    from tools import asset_edit
    return asset_edit


def api_knowledge_status() -> dict:
    """知识库两条通道的现状（检索 / 直投）+ 索引缺口。

    `tools/knowledge_status.py` 是唯一口径：它复用
    `agent.knowledge.discover_document_files` 的边界，不在这里重算
    「哪些目录算敏感」——两处各算一次必然对不上（教训 #38）。
    """
    from tools.knowledge_status import status

    return status()


def api_assets() -> dict:
    """可编辑资产清单（按 人格 / 规则 / 知识 分组）。

    额外带一份 `prompt_overrides`：22 条协议文本里哪几条**真的被覆盖了**。
    不带上它的话，「协议条文」那 22 个文件在界面上长得一模一样，
    主人分不出「这条是我改过的」还是「这条还是出厂默认」。
    """
    out = _asset_edit().list_assets()
    # 只导 prompt_overrides（它只依赖 pathlib），不导 protocols——
    # 那会把整个协议栈拉进 bridge，启动慢且没必要。
    from agent.prompt_overrides import list_overrides
    out["prompt_overrides"] = list_overrides()
    return out


def api_asset_read(params: dict) -> dict:
    p = (params or {}).get("path") or ""
    return _asset_edit().read_asset(p)


def api_asset_write(body: dict) -> dict:
    b = body or {}
    return _asset_edit().write_asset(str(b.get("path") or ""),
                                    str(b.get("content") if b.get("content") is not None else ""))


def api_asset_restore(body: dict) -> dict:
    b = body or {}
    return _asset_edit().restore_asset(str(b.get("path") or ""),
                                       str(b.get("backup") or ""))


def api_asset_reset(body: dict) -> dict:
    b = body or {}
    return _asset_edit().reset_asset(str(b.get("path") or ""))


def api_asset_create(body: dict) -> dict:
    """新建篇目（第 49 轮 B5）。"""
    b = body or {}
    return _asset_edit().create_asset(
        str(b.get("path") or ""),
        str(b.get("content") if b.get("content") is not None else ""))


def api_asset_rename_dir(body: dict) -> dict:
    """给槽位目录改名（数字前缀保留，只改 `-` 后面那段）。"""
    b = body or {}
    return _asset_edit().rename_dir(
        str(b.get("path") or ""), str(b.get("name") or ""))


def api_asset_rename(body: dict) -> dict:
    """给一个资产文件改名（只改文件名，不改路径）。

    场景卡的 `name` / `display` 都在文件内容里，所以改名不影响挂载与触发——
    这一点在返回值 `note` 里如实告诉界面，免得用户以为改名把场景弄坏了。
    """
    b = body or {}
    return _asset_edit().rename_asset(
        str(b.get("path") or ""), str(b.get("name") or ""))


def api_asset_delete(body: dict) -> dict:
    """删除篇目（先备份，可 restore 回来）。"""
    b = body or {}
    return _asset_edit().delete_asset(str(b.get("path") or ""))


def api_asset_mkdir(body: dict) -> dict:
    """新建目录（槽位 / 分类）。名字规则在 `asset_edit._DIR_RULES`。"""
    b = body or {}
    return _asset_edit().create_dir(str(b.get("path") or ""))


def api_asset_rmdir(body: dict) -> dict:
    """删除空目录（非空一律拒绝）。"""
    b = body or {}
    return _asset_edit().delete_dir(str(b.get("path") or ""))


def api_prompt_groups(params: dict) -> dict:
    """协议分组表（顺序 + 标题 + 每组条数）。

    组标题的唯一真相是 `prompts/_groups.yaml`（2026-10-26 从文件头收敛过来：
    以前同组每条都写一份 `group_title`，改哪份生效取决于遍历顺序）。
    这里只读；改标题走 POST `/api/prompts/group-rename`。
    """
    return _asset_edit().prompt_groups()


def api_prompt_group_rename(body: dict) -> dict:
    """改一个组的显示名（桌面端双击组标题）。只动 `_groups.yaml` 里那一块。"""
    b = body or {}
    return _asset_edit().save_prompt_group_title(
        str(b.get("key") or ""), str(b.get("title") or ""))



# ═══════════════════════════════════════════════════════════════
# 高级调参（2026-10-26 第 49 轮 B4）
#
# 主人：「常量也开放，做成『高级调参』页」——每个值要看到
# **当前值 / 默认值 / 作用 / 文件:行**，改完重启生效。
#
# 台账、范围校验、跨项约束全在 `agent/tuning.py`（那里能单测，
# `tests/test_tuning_contract.py` 盯着它）。本层只做三件事：读台账、
# 校验、把改动写进 config.yaml 的 `tuning` 节。
# **判据只有一处**——本层不许再判断「这个值合不合法」（教训 #38）。
#
# 写盘复用 `api_config_save`：备份、secret 归一、数值收口都在那条路上，
# 不另起一条写盘路径（否则两处备份命名/权限迟早分家）。
# ═══════════════════════════════════════════════════════════════

def _tuning():
    """惰性导入：bridge 其它端点不该为了调参页承受 config 解析开销。"""
    from agent import tuning
    return tuning


def api_tuning() -> dict:
    """调参台账：9 组 32 项，每项带 当前值/默认值/范围/作用/文件:行。"""
    return _tuning().ui_payload()


def api_tuning_save(body: dict) -> dict:
    b = body or {}
    values = b.get("values")
    if values is None:
        values = b                      # 允许直接把 {key: value} 当 body
    if not isinstance(values, dict):
        raise ValueError("values 必须是对象")

    tuning = _tuning()
    clean, errors = tuning.validate_updates(values)
    if errors:
        # **不写盘**：用户手输的错误必须当场告诉他，不能悄悄回落默认值
        return {"ok": False, "errors": errors}
    if not clean:
        return {"ok": False, "errors": ["没有变化可保存"]}

    cfg = _load_raw_config()
    section = cfg.get("tuning")
    if not isinstance(section, dict):
        section = {}
    section.update(clean)
    cfg["tuning"] = section
    res = api_config_save({"config": cfg}, merge_missing_fields=False)
    tuning.reload()                     # 让本进程的台账立刻反映新值
    return {
        "ok": True,
        "saved": clean,
        "backup": res.get("backup"),
        "tuning": tuning.ui_payload(),
        "note": "已写入 config.yaml；糖糖下次重启生效（不需要重装依赖）",
    }


# ═══════════════════════════════════════════════════════════════
# HTTP 层
# ═══════════════════════════════════════════════════════════════
ROUTES_GET = {
    "/api/health": api_health,
    "/api/config": api_config,
    "/api/models": api_models,
    "/api/groups": api_groups,
    "/api/gallery": api_gallery,          # 需要 query 参数（自带一个形参）
    "/api/stats": api_stats,
    "/api/status": api_status,
    "/api/logs": None,  # SSE，单独处理
    # 会客厅
    "/api/console/status": api_console_status,
    "/api/console/sessions": api_console_sessions,
    "/api/console/messages": api_console_messages,
    "/api/console/snapshot": api_console_snapshot,
    "/api/console/traces": api_console_traces,
    "/api/console/trace": api_console_trace,
    "/api/console/replay": api_console_replay,
    "/api/console/groups": api_console_groups,
    "/api/console/memory-graph": api_console_memory_graph,
    "/api/console/memory-profile": api_console_memory_profile,
    "/api/console/memory-history": api_console_memory_history,
    "/api/console/memory-revisions": api_console_memory_revisions,
    "/api/console/sources": api_console_sources,
    "/api/console/feed": api_console_feed,
    "/api/console/search": api_console_search,
    # 可编辑资产（第 49 轮）
    "/api/assets": api_assets,
    "/api/knowledge/status": api_knowledge_status,
    "/api/assets/read": api_asset_read,
    # 高级调参（第 49 轮 B4）
    "/api/tuning": api_tuning,
    "/api/scenario/packs": api_scenario_packs,
    "/api/gallery/tag/status": api_gallery_tag_status,
    "/api/prompts/groups": api_prompt_groups,
    # 连接密钥 / SnowLuma / 连接自检（2026-10-26 桌面端主入口化）
    "/api/secrets": api_secrets,
    "/api/snowluma/status": api_snowluma_status,
    "/api/connect-check": api_connect_check,
}
ROUTES_POST = {
    "/api/config": lambda b: api_config_save(b),
    "/api/models/fetch": api_models_fetch,
    "/api/models/activate": api_models_activate,
    "/api/gallery/tags": api_gallery_tags,
    "/api/gallery/trash": api_gallery_trash,
    "/api/gallery/reveal": api_gallery_reveal,
    "/api/chat": api_chat,
    "/api/bot/start": lambda b: api_bot_start(),
    "/api/bot/stop": lambda b: api_bot_stop(),
    "/api/group-manage/save": api_group_manage_save,
    # 会客厅
    "/api/console/sessions": api_console_new_session,
    "/api/console/say": api_console_say,
    "/api/console/turn": api_console_turn,
    "/api/console/send": api_console_send,
    # 可编辑资产（第 49 轮）
    "/api/assets/write": api_asset_write,
    "/api/assets/restore": api_asset_restore,
    "/api/assets/reset": api_asset_reset,
    # 结构管理（第 49 轮 B5）
    "/api/assets/create": api_asset_create,
    "/api/assets/delete": api_asset_delete,
    "/api/assets/rename": api_asset_rename,
    "/api/assets/rename-dir": api_asset_rename_dir,
    "/api/scenario/packs/save": api_scenario_packs_save,
    "/api/assets/mkdir": api_asset_mkdir,
    "/api/assets/rmdir": api_asset_rmdir,
    "/api/prompts/group-rename": api_prompt_group_rename,
    "/api/gallery/tag/run": api_gallery_tag_run,
    "/api/gallery/tag/cancel": api_gallery_tag_cancel,
    "/api/gallery/tag/probe": api_gallery_tag_probe,
    # 高级调参（第 49 轮 B4）
    "/api/tuning/save": api_tuning_save,
    # 连接密钥 / SnowLuma（2026-10-26）
    "/api/secrets/save": api_secrets_save,
    "/api/snowluma/start": lambda b: api_snowluma_start(),
    # 运行中的时候给的是「重启」：只调 start 会走 already 分支，等于什么都没做
    "/api/snowluma/restart": lambda b: api_snowluma_restart(),
}


def _call_with_params(fn, params: dict):
    """无参端点照旧调用；声明了形参的端点（如 api_gallery）拿到 query dict。

    用 inspect 而不是给所有端点加参数——大部分端点不需要 query，
    统一加形参只会让「这个端点看什么参数」变得看不出来。
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn()
    return fn(params) if len(sig.parameters) >= 1 else fn()


class Handler(BaseHTTPRequestHandler):
    server_version = "TangTangBridge/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code: int, payload: dict | None = None, raw: bytes | None = None,
              ctype: str = "application/json; charset=utf-8"):
        body = raw if raw is not None else json.dumps(
            payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        if not supplied:
            from urllib.parse import parse_qs
            supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, TOKEN)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self._send(200, api_health())
        if not self._authorized():
            return self._send(401, {"error": "invalid token"})
        if path == "/api/logs":
            return self._serve_logs()
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if path in ("/api/gallery/thumb", "/api/gallery/file"):
            return self._serve_image(path, params)
        if path == "/api/console/media":
            return self._serve_console_media(params)
        fn = ROUTES_GET.get(path)
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(200, _call_with_params(fn, params))
        except _AssetError as e:
            return self._send(e.status, {"error": str(e), "code": e.code})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _serve_image(self, path: str, params: dict):
        """图片字节流（缩略图/原图）。不走 JSON —— <img> 直接指过来。"""
        source = (params.get("source") or "").strip()
        name = (params.get("name") or "").strip()
        try:
            if path == "/api/gallery/thumb":
                try:
                    w = int(params.get("w") or 320)
                except (TypeError, ValueError):
                    w = 320
                data, ctype = build_thumb(source, name, w)
            else:
                src = _gallery_dir(source) / _safe_name(name)
                if not src.exists():
                    raise ValueError("图片不存在")
                data = src.read_bytes()
                ctype = {
                    ".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                }.get(src.suffix.lower(), "image/jpeg")
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_console_media(self, params: dict):
        """会客厅的图片/语音：转发 bot 进程的 `/media`，二进制直通。

        410 是**常态**而不是错误——QQ 的临时链接几小时就过期，而本地缓存
        只覆盖「收到消息时就已经预取」的那些。前端据此显示「媒体已过期」。
        """
        try:
            data, ctype = _console_bytes("/media", {
                "scope": params.get("scope") or "",
                "id": params.get("id") or "0",
                "n": params.get("n") or "0",
            })
        except ValueError as e:
            return self._send(410, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._authorized():
            return self._send(401, {"error": "invalid token"})
        fn = ROUTES_POST.get(path)
        if fn is None:
            return self._send(404, {"error": "not found"})
        try:
            body = self._read_body()
        except Exception as e:
            return self._send(400, {"error": f"body 解析失败: {e}"})
        try:
            return self._send(200, fn(body))
        except _AssetError as e:
            return self._send(e.status, {"error": str(e), "code": e.code})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_DELETE(self):
        """只有会客厅的会话删除走 DELETE（2026-10-24）。"""
        if not self._authorized():
            return self._send(401, {"error": "invalid token"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        # /api/console/sessions/<id>
        if len(parts) == 4 and parts[:3] == ["api", "console", "sessions"]:
            from urllib.parse import unquote
            try:
                return self._send(200, api_console_delete(unquote(parts[3])))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    def _serve_logs(self):
        """SSE：先回放缓冲，再实时推送新行。"""
        from urllib.parse import parse_qs
        after = int((parse_qs(urlparse(self.path).query).get("after") or ["0"])[0] or 0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        sent = 0
        try:
            with _log_cond:
                snapshot = list(_log_buffer)
            for line in snapshot[-500:]:
                sent += 1
                self.wfile.write(f"data: {json.dumps(line, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            last_seq = _log_seq
            while True:
                with _log_cond:
                    if _log_seq <= last_seq:
                        _log_cond.wait(timeout=15)
                    new_lines = list(_log_buffer)[-(_log_seq - last_seq):] if _log_seq > last_seq else []
                    last_seq = _log_seq
                if not new_lines:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                for line in new_lines:
                    self.wfile.write(f"data: {json.dumps(line, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # 后台消费 .trigger_restart_snowluma（原来只有旧 Qt 控制台读，现在没人读了）
    threading.Thread(target=_snowluma_restart_watcher, daemon=True,
                     name="snowluma-restart-watcher").start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    port = server.server_address[1]
    print(f"BRIDGE_READY {json.dumps({'port': port, 'token': TOKEN})}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
