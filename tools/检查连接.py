#!/usr/bin/env python3
"""小糖糖 · 链路自检：SnowLuma 网络配置 <-> 糖糖 [·]

## 为什么会有这个工具

控制台把卡片从「启动中」翻成「运行中」，唯一条件是子进程打出 ``SnowLuma 已连接``
这一行（糖糖控制台_qt.py ``_read_sugar_output``）。可这一行的前提——SnowLuma 网页面板
里的 OneBot 网络配置（反向 WS 客户端 / HTTP API / 授权 Token）——以前一个字文档都没有、
也没有任何检查。于是新手的症状是「点了启动糖糖，卡片永远停在启动中」，看不到任何解释。

本工具把静默失败变成指名道姓的下一步：逐项检查接线，每项要么 [√]，
要么给一句**带着具体数值**、能照着做的下一步（例如 ws://127.0.0.1:3001）。

## 只读

不写任何文件、不起任何进程、不改任何配置。只做三件事：
读配置文件、原地解析 .env（用 ``dotenv_values``——**不**调 ``load_dotenv``，
不污染 ``os.environ``）、以及对端口做 TCP 连通探测（connect 后立刻关闭，不发协议）。

调用方：控制台按钮、``tools/体检.py``、命令行。

    python tools/检查连接.py [项目根]

    from tools.检查连接 import check_all          # 中文模块名可以正常 import
    for r in check_all(Path("项目根目录")):
        print(r.status, r.detail, r.next_step)

## 已核实的事实（改判据前先读）

- SnowLuma 安装目录 ``<根>/SnowLuma/SnowLuma-v<版本>-win-x64/``，可能有多份。
  目录里**没有 SnowLuma.exe**，进程是 ``node.exe``，入口是 ``launcher.bat``。
  取版本号最大的；但带 ``launcher.bat`` 的优先——仓库里真躺着一个只剩 ``logs/`` 的
  ``SnowLuma-v1.11.4-win-x64-lite``，残缺目录不该顶掉能用的那份。
- 网页面板端口在 ``<安装目录>/config/runtime.json`` 的 ``webuiPort``（实机 5099），
  登录用户名恒为 admin。
- 每个已登录 QQ 一份 ``config/onebot_<QQ号>.json``，结构见 ``networks`` 四个数组。
- **SnowLuma 里新建 wsClient 的默认 url 是 ``ws://127.0.0.1:8080/ws``**，不是 3001——
  新手「新建 -> 保存」就会连错端口，症状和「根本没配」一模一样。本工具单独报这一项。
- 反之新建 **wsServer** 的默认是 127.0.0.1:3001——选错标签页会先把糖糖要的端口占住。
- SnowLuma 侧 token 留空 = 全部放行（``isAuthorized`` 里 ``if (!token) return true``）。
- 糖糖的反向 WS **不校验入站 token**，token 只影响**出站 HTTP**。所以两边不一致的
  症状是「连上了但发不出去（401）」，**不是**「连不上」。
- config.yaml 里 token 常写成占位符 ``${SNOWLUMA_TOKEN}``，解析规则照抄
  ``main.py:_resolve_env_vars``。[!] 环境变量缺失时它会**保留字面量**而不是空串，
  于是「看起来有值，其实是占位符」——本工具把它单独报出来（第 7 项）。
"""

import json
import os
import re
import socket
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlparse

import yaml

# 强制 UTF-8 输出：控制台重定向时 Python 退回 GBK，print 非 GBK 字符会抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent.parent

# SnowLuma 官方发布页（与 糖糖控制台_qt.py 的 SNOWLUMA_RELEASE_URL 同一个地址；
# 这里不 import 那个模块——它顶层要加载 PySide6，太重）
RELEASE_URL = "https://github.com/SnowLuma/SnowLuma/releases"

# 糖糖侧的默认接线（与 main.py:143-147 构造 NapCatClient 的实参一致）
DEFAULT_WS_HOST = "127.0.0.1"
DEFAULT_WS_PORT = 3001
DEFAULT_HTTP_URL = "http://127.0.0.1:3000"
DEFAULT_WEBUI_PORT = 5099

# SnowLuma 新建 wsClient 时的默认 url——必须单独报出来的一项
SNOWLUMA_DEFAULT_WS_CLIENT_URL = "ws://127.0.0.1:8080/ws"

# 环境变量占位符——与 main.py:_resolve_env_vars 的正则逐字一致
ENV_PLACEHOLDER_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')

KEY_INSTALLED = "snowluma_installed"
KEY_RUNNING = "snowluma_running"
KEY_ONEBOT = "onebot_config"
KEY_WS_CLIENT = "snowluma_ws_client"
KEY_WS_SERVER = "snowluma_ws_server"
KEY_HTTP_SERVER = "snowluma_http_server"
KEY_TOKEN = "token_consistency"
KEY_SUGAR_LISTEN = "sugar_ws_listening"
KEY_SUMMARY = "summary"


# ═══════════════════════════════════════════════════════
# 结果模型
# ═══════════════════════════════════════════════════════

class Status(str, Enum):
    """检查结论。用枚举而不是裸字符串——上游改文案不该影响判据。"""

    OK = "ok"       # 这一环通
    WARN = "warn"   # 能用，但有隐患/要确认
    FAIL = "fail"   # 断了，链路卡在这里
    SKIP = "skip"   # 上游没就绪，本项没得查（等于「还没走到」）


# 控制台字形：Win10 传统 conhost 画不出 emoji（见 tests/test_console_glyphs.py）
GLYPH = {
    Status.OK: "[√]",
    Status.WARN: "[!]",
    Status.FAIL: "[×]",
    Status.SKIP: "[ ]",
}


@dataclass(frozen=True)
class CheckResult:
    """一项检查的结论。

    ``next_step`` 是本工具存在的全部意义：非 OK 时必须是一句能照着做的、
    带具体数值的指令（错在哪、去哪改、填什么）。
    """

    key: str
    title: str
    status: Status
    detail: str = ""
    next_step: str = ""


# ═══════════════════════════════════════════════════════
# Token：解析（照抄 main.py）与比对
# ═══════════════════════════════════════════════════════

@dataclass(frozen=True)
class TokenValue:
    """一个 token 值的解析结果。

    ``var_name`` 非空 = 配置里写的是 ``${NAME}`` 占位符；
    此时 ``resolved=False`` 表示环境变量没取到——**运行期 token 就是那个字面量**
    （``os.environ.get(name, obj)`` 的返回值是原串，不是空串）。
    """

    raw: str = ""
    value: str = ""
    var_name: str = ""
    resolved: bool = True

    @property
    def is_unresolved_placeholder(self) -> bool:
        return bool(self.var_name) and not self.resolved

    @property
    def text(self) -> str:
        return (self.value or "").strip()


def mask_token(token: str) -> str:
    """打码：前 4 后 2，中间星号。太短的一律全星号，避免「打码等于没打」。"""
    tok = (token or "").strip()
    if not tok:
        return ""
    if len(tok) < 8:
        return "*" * 4
    return tok[:4] + "****" + tok[-2:]


def resolve_token(raw, env: Mapping[str, str]) -> TokenValue:
    """把配置里的 token 原串解析成实际值（单值版 ``_resolve_env_vars``）。

    规则逐字照抄 ``main.py:70-84``：只有整串形如 ``${NAME}`` 才替换，
    取 ``env.get(NAME, 原串)``——取不到就**保留字面量**。
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    m = ENV_PLACEHOLDER_RE.match(text)
    if not m:
        return TokenValue(raw=text, value=text)
    name = m.group(1)
    if name in env:
        return TokenValue(raw=text, value=env.get(name) or "", var_name=name, resolved=True)
    return TokenValue(raw=text, value=text, var_name=name, resolved=False)


@dataclass(frozen=True)
class TokenVerdict:
    status: Status
    detail: str
    next_step: str = ""


def compare_tokens(http_token: str, ws_token: str, sugar: TokenValue) -> TokenVerdict:
    """三边 token 判据（纯函数）。

    三边 = SnowLuma 的 ``httpServers[].accessToken``、``wsClients[].accessToken``、
    以及糖糖解析后的 token。判据有意不对称：

    - 糖糖 vs HTTP API 决定**发不发得出去**（不一致 = 401）
    - 糖糖 vs WS 客户端不决定连通性（糖糖不校验入站 token）
    - SnowLuma 侧留空 = 全部放行，所以「糖糖有值、SnowLuma 留空」其实能通
    """
    http_tok = (http_token or "").strip()
    ws_tok = (ws_token or "").strip()
    sugar_tok = sugar.text

    if sugar.is_unresolved_placeholder:
        return TokenVerdict(
            Status.FAIL,
            detail=(f"糖糖 config.yaml 写的是 {sugar.raw}，但环境变量 {sugar.var_name} 没取到值，"
                    f"运行期这个 token 就是字面量「{sugar.raw}」（SnowLuma 侧现在是 "
                    f"{mask_token(http_tok) or '空'}）"),
            next_step=(f"打开控制台 → 设置 →「密钥管理」，给 {sugar.var_name} 填上和 SnowLuma "
                       f"「授权 Token」一样的一串（当前 SnowLuma 是 {mask_token(http_tok) or '空'}），"
                       f"保存后重启糖糖"),
        )

    if not http_tok and not ws_tok and not sugar_tok:
        return TokenVerdict(
            Status.OK,
            detail="三边都留空——SnowLuma 侧留空 = 全部放行，糖糖也不校验入站 token，链路能通",
        )

    if http_tok and http_tok == ws_tok == sugar_tok:
        return TokenVerdict(Status.OK, detail=f"三边一致（{mask_token(http_tok)}）")

    if not http_tok:
        return TokenVerdict(
            Status.WARN,
            detail=(f"SnowLuma 的「授权 Token」留空 = 全部放行，所以能通；但本机任何程序都能调它的接口"
                    f"（糖糖侧是 {mask_token(sugar_tok) or '空'}）"),
            next_step=(f"想收紧就两边填同一串：SnowLuma 网页面板 → 节点配置 →「HTTP API」与「WS 客户端」"
                       f"的「授权 Token」都填 {mask_token(sugar_tok) or '（糖糖在密钥管理里那串）'}"),
        )

    if sugar_tok and sugar_tok == http_tok:
        return TokenVerdict(
            Status.WARN,
            detail=(f"糖糖和 HTTP API 对得上（发得出去）；WS 客户端那串不同"
                    f"（{mask_token(ws_tok) or '空'}）——糖糖不校验入站 token，所以不影响能用"),
            next_step=f"想三边统一就把 WS 客户端的「授权 Token」也改成 {mask_token(sugar_tok)}",
        )

    return TokenVerdict(
        Status.WARN,
        detail=(f"三边不一致：糖糖 {mask_token(sugar_tok) or '空'} / "
                f"HTTP API {mask_token(http_tok) or '空'} / "
                f"WS 客户端 {mask_token(ws_tok) or '空'}。"
                f"症状是「连上了但发不出去（401）」，不是连不上"),
        next_step=(f"把 SnowLuma「授权 Token」（网页面板 → 节点配置 →「HTTP API」与「WS 客户端」）"
                   f"改成糖糖那串 {mask_token(sugar_tok) or '（在控制台「密钥管理」里看完整值）'}，"
                   f"保存后重启 SnowLuma 与糖糖"),
    )


# ═══════════════════════════════════════════════════════
# 读配置（全部只读；坏文件一律降级为默认值，绝不抛）
# ═══════════════════════════════════════════════════════

def read_env_file(path: Path) -> dict:
    """原地解析 .env —— 不写 os.environ（load_dotenv 会）。

    读不到 dotenv 时退回极简 KEY=VALUE 解析（不处理多行与转义，够用即可）。
    """
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        return {k: (v or "") for k, v in dotenv_values(str(p)).items() if k}
    except Exception:
        pass
    out: dict = {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            out[name] = value.strip().strip('"').strip("'")
    return out


def build_env(root: Path, environ: Optional[Mapping[str, str]] = None) -> dict:
    """os.environ 优先、.env 兜底。

    与运行期等价：main.py 先 ``load_dotenv()``（默认不覆盖已存在的环境变量），
    再 ``os.environ.get(name, 原串)``。
    """
    base = dict(os.environ if environ is None else environ)
    merged = read_env_file(Path(root) / ".env")
    merged.update(base)
    return merged


def read_json_object(path: Path) -> dict:
    """读 JSON 对象；不存在/坏 JSON/顶层不是对象 -> 返回 {}。"""
    p = Path(path)
    try:
        if not p.is_file():
            return {}
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def network_lists(doc: Mapping) -> dict:
    """从 onebot_<QQ>.json 里取出四个网络数组（读 SnowLuma 配置的唯一入口）。"""
    net = doc.get("networks") if isinstance(doc, Mapping) else None
    net = net if isinstance(net, Mapping) else {}
    out = {}
    for name in ("httpServers", "httpClients", "wsServers", "wsClients"):
        items = net.get(name)
        out[name] = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    return out


def normalize_host(host: str) -> str:
    """回环地址归一到 127.0.0.1——配 localhost 不该被判成「不一致」。"""
    h = (host or "").strip().strip("[]").lower()
    if h in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1", "::"):
        return "127.0.0.1"
    return h


def parse_endpoint(url: str) -> Optional[tuple]:
    """``ws://127.0.0.1:3001/ws`` -> ``("127.0.0.1", 3001)``；解析不出主机/端口 -> None。"""
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "ws://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if parsed.scheme in ("wss", "https") else 80
    return normalize_host(parsed.hostname), int(port)


def probe_host(host: str) -> str:
    """探测用的目标地址：0.0.0.0/:: 不是可连地址，换回环。"""
    return normalize_host(host)


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """TCP 连通探测——只 connect，不发协议、不留下连接。"""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if not port:
        return False
    try:
        with socket.create_connection((probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def find_snowluma_dir(root: Path) -> Optional[Path]:
    """``<根>/SnowLuma/SnowLuma-v<版本>-win-x64/``，多份取版本最大。

    优先带 ``launcher.bat`` 的目录：仓库里真有一个只剩 ``logs/`` 的
    ``SnowLuma-v1.11.4-win-x64-lite``，残缺目录不该顶掉能用的那份。
    """
    base = Path(root) / "SnowLuma"
    try:
        dirs = [p for p in base.glob("SnowLuma-v*") if p.is_dir()]
    except OSError:
        return None
    if not dirs:
        return None
    usable = [p for p in dirs if (p / "launcher.bat").is_file()] or dirs
    return max(usable, key=lambda p: (version_key(p.name), p.name))


def version_key(dir_name: str) -> tuple:
    """目录名 -> 可排序版本号元组；解析不出给 (0,)。"""
    m = re.match(r"SnowLuma-v([0-9]+(?:\.[0-9]+)*)", str(dir_name))
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def read_webui_port(sl_dir: Path) -> int:
    """``config/runtime.json`` 的 webuiPort（读不到就按实机默认 5099）。"""
    doc = read_json_object(Path(sl_dir) / "config" / "runtime.json")
    try:
        port = int(doc.get("webuiPort"))
    except (TypeError, ValueError):
        return DEFAULT_WEBUI_PORT
    return port or DEFAULT_WEBUI_PORT


@dataclass(frozen=True)
class SugarSide:
    """糖糖侧接线的解析结果（``read_sugar_side`` 的唯一产物）。"""

    config_path: Path
    config_error: str = ""
    bot_qq: str = ""
    ws_host: str = DEFAULT_WS_HOST
    ws_port: int = DEFAULT_WS_PORT
    http_url: str = DEFAULT_HTTP_URL
    http_host: str = "127.0.0.1"
    http_port: int = 3000
    token: TokenValue = TokenValue()

    @property
    def ws_url(self) -> str:
        return f"ws://{self.ws_host}:{self.ws_port}"


def read_sugar_side(root: Path, env: Mapping[str, str]) -> SugarSide:
    """读 config.yaml 并解析出接线（纯读；坏文件不抛，写进 config_error）。"""
    path = Path(root) / "config.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise ValueError("顶层不是字典")
    except Exception as e:  # noqa: BLE001 - 换个看门狗来报，不在解析处炸
        return SugarSide(config_path=path, config_error=f"{type(e).__name__}: {e}")

    napcat = doc.get("napcat") if isinstance(doc.get("napcat"), dict) else {}
    bot = doc.get("bot") if isinstance(doc.get("bot"), dict) else {}

    ws_host = str(napcat.get("ws_host") or DEFAULT_WS_HOST)
    try:
        ws_port = int(napcat.get("ws_port") or DEFAULT_WS_PORT)
    except (TypeError, ValueError):
        ws_port = DEFAULT_WS_PORT

    http_url = str(napcat.get("http_url") or DEFAULT_HTTP_URL)
    http_host, http_port = "127.0.0.1", 3000
    parsed = parse_endpoint(http_url if "://" in http_url else "http://" + http_url)
    if parsed:
        http_host, http_port = parsed

    qq = bot.get("qq_id")
    return SugarSide(
        config_path=path,
        bot_qq=str(qq).strip() if qq is not None else "",
        ws_host=ws_host,
        ws_port=ws_port,
        http_url=http_url,
        http_host=http_host,
        http_port=http_port,
        token=resolve_token(napcat.get("access_token"), env),
    )


# ═══════════════════════════════════════════════════════
# 逐项检查
# ═══════════════════════════════════════════════════════

def _first(items: list, pred: Callable[[dict], bool]) -> Optional[dict]:
    for item in items:
        if pred(item):
            return item
    return None


def _server_endpoint(item: Mapping) -> Optional[tuple]:
    """httpServers / wsServers 的 host:port（端口解析不出 -> None）。"""
    try:
        port = int(item.get("port"))
    except (TypeError, ValueError):
        return None
    return normalize_host(item.get("host")), port


def check_all(
    root: Path,
    *,
    port_probe: Optional[Callable[[str, int], bool]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> list:
    """跑完全部检查，返回按顺序的 CheckResult 列表。

    最后一项的 ``key`` 恒为 ``KEY_SUMMARY``（汇总结论，含「卡在第几步 + 一句话下一步」），
    其余为逐项检查。``port_probe`` / ``environ`` 供测试注入——不注入就会探本机真端口。

    控制台按钮直接 ``check_all(BASE)`` 后遍历打印；只读，随时可重复调用。
    """
    root = Path(root)
    probe = port_probe if port_probe is not None else port_open
    env = build_env(root, environ)
    sugar = read_sugar_side(root, env)

    results: list = []
    blocked: Optional[tuple] = None  # (步号, 标题)：上游没就绪时后续项 SKIP 的依据

    def add(key, title, status, detail="", next_step=""):
        results.append(CheckResult(key=key, title=title, status=status,
                                   detail=detail, next_step=next_step))

    def skip(key, title, why):
        if blocked:
            step, blocker = blocked
            add(key, title, Status.SKIP, why, f"先解决第 {step} 步（{blocker}）")
        else:
            add(key, title, Status.SKIP, why)

    # ── 1. SnowLuma 装了吗 ──────────────────────────────
    sl_dir = find_snowluma_dir(root)
    install_hint = (
        f"到 {RELEASE_URL} 下载 Windows x64 版，解压到 {root / 'SnowLuma'}"
        f"（解压后应得到 SnowLuma-vX.Y.Z-win-x64 文件夹），再点控制台的「启动 SnowLuma」"
    )
    if sl_dir is None:
        add(KEY_INSTALLED, "SnowLuma 装了吗", Status.FAIL,
            f"{root / 'SnowLuma'} 下没有任何 SnowLuma-v* 安装目录",
            install_hint)
        blocked = (1, "SnowLuma 装了吗")
    elif not (sl_dir / "launcher.bat").is_file():
        add(KEY_INSTALLED, "SnowLuma 装了吗", Status.WARN,
            f"找到 {sl_dir.name}，但里面没有 launcher.bat（解压可能不完整）",
            f"重新解压 Windows x64 完整包到 {root / 'SnowLuma'}，"
            f"确保 launcher.bat 与 node.exe 都在 {sl_dir}")
    elif not (sl_dir / "node.exe").is_file():
        # [!] 2026-09-20 审查补：发布页的资产列表里 `-win-x64-lite.zip` 按字典序
        #   **排在完整包前面**，而 lite 版**不包含 Node.js**。新手点错那个，
        #   launcher.bat 第一行 `node .\check-node-version.cjs` 就会报
        #   「'node' 不是内部或外部命令」，然后 pause 停住——而这里如果只判
        #   launcher.bat 在不在，会给他一个 [√]，下一步再让他"再点一次启动"，
        #   死循环。
        add(KEY_INSTALLED, "SnowLuma 装了吗", Status.WARN,
            f"找到 {sl_dir.name}，但缺 node.exe —— 这是 **-lite**（精简）包，"
            f"它不带 Node.js，launcher.bat 起不来",
            f"回到 {RELEASE_URL} 重新下载**不带 lite** 的那个 "
            f"SnowLuma-vX.Y.Z-win-x64.zip（完整包自带 node.exe），"
            f"解压覆盖到 {root / 'SnowLuma'}")
    else:
        add(KEY_INSTALLED, "SnowLuma 装了吗", Status.OK, f"{sl_dir.name}（{sl_dir}）")

    # ── 2. SnowLuma 在跑吗 ──────────────────────────────
    webui_port = read_webui_port(sl_dir) if sl_dir else DEFAULT_WEBUI_PORT
    webui_url = f"http://127.0.0.1:{webui_port}"
    launcher = (sl_dir / "launcher.bat") if sl_dir else None
    webui_up = bool(probe("127.0.0.1", webui_port))
    http_up = bool(probe(probe_host(sugar.http_host), sugar.http_port))
    if sl_dir is None:
        # 还没装就谈不上"在不在跑"。原先这里会 FAIL 并给出
        # 「（或双击 <项目根>/SnowLuma）」——那是个**目录**，双击只会打开资源管理器，
        # 而括号里的错指引会把正确的"点控制台按钮"稀释掉（审查实测的 I3）。
        skip(KEY_RUNNING, "SnowLuma 在跑吗", "SnowLuma 还没装，先看上一项")
    elif sl_dir is not None and (webui_up or http_up):
        which = " / ".join(
            x for x in (f"网页面板 {webui_port}" if webui_up else "",
                        f"HTTP {sugar.http_port}" if http_up else "") if x)
        add(KEY_RUNNING, "SnowLuma 在跑吗", Status.OK, f"端口响应中：{which}")
    else:
        add(KEY_RUNNING, "SnowLuma 在跑吗", Status.FAIL,
            f"{webui_port}（网页面板）与 {sugar.http_port}（HTTP API）都没开",
            f"先确认 QQ 客户端开着、且登录的是机器人账号（SnowLuma 是注入进 QQ 进程工作的，"
            f"它自己不登录），再点控制台顶部的「启动 SnowLuma」"
            f"（或自己双击 {launcher}）；起来后重新运行本检查")

    # ── 3. 找到这个 QQ 的 OneBot 配置了吗 ───────────────
    config_dir = (sl_dir / "config") if sl_dir else None
    onebot_doc: dict = {}
    if blocked:
        skip(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", "SnowLuma 还没装，没得读配置")
    elif sugar.config_error:
        add(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", Status.FAIL,
            f"读不到 {sugar.config_path}（{sugar.config_error}）",
            f"确认 {sugar.config_path} 存在且是合法 YAML（可从 config.example.yaml 复制一份），"
            f"里头的 bot.qq_id 填机器人 QQ 号")
        blocked = (3, "找到这个 QQ 的 OneBot 配置了吗")
    elif not sugar.bot_qq:
        add(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", Status.FAIL,
            f"{sugar.config_path} 的 bot.qq_id 是空的",
            "在 config.yaml 的 bot.qq_id 填入机器人 QQ 号（就是 SnowLuma 里登录的那个号，"
            "注意要带引号写成字符串），和 SnowLuma 保持同一个号")
        blocked = (3, "找到这个 QQ 的 OneBot 配置了吗")
    else:
        onebot = (config_dir or Path()) / f"onebot_{sugar.bot_qq}.json"
        try:
            others = sorted(p.stem.replace("onebot_", "")
                            for p in (config_dir or Path()).glob("onebot_*.json"))
        except OSError:
            others = []
        if onebot.is_file():
            onebot_doc = read_json_object(onebot)
            add(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", Status.OK,
                f"config.yaml 的 bot.qq_id = {sugar.bot_qq}，对应 {onebot.name}")
        elif not others:
            add(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", Status.FAIL,
                f"SnowLuma 里还没有登录任何 QQ（{config_dir} 下没有 onebot_*.json）；"
                f"糖糖这边要的是 {sugar.bot_qq}",
                f"用 QQ 客户端登录 {sugar.bot_qq} 并保持开着（SnowLuma 靠注入 QQ 进程工作，"
                f"它自己不登录）→ 点控制台顶部的「启动 SnowLuma」→ 到网页面板「进程注入」"
                f"里加载（登录成功后会自动生成 onebot_{sugar.bot_qq}.json）→ 重新运行本检查")
            blocked = (3, "找到这个 QQ 的 OneBot 配置了吗")
        else:
            logged = "、".join(others)
            add(KEY_ONEBOT, "找到这个 QQ 的 OneBot 配置了吗", Status.FAIL,
                f"两边不是同一个号：config.yaml 里 bot.qq_id = {sugar.bot_qq}，"
                f"SnowLuma 里已登录的是 {logged}",
                f"改成同一个号——要么把 config.yaml 的 bot.qq_id 改成 {others[0]}，"
                f"要么在 SnowLuma 里登录 {sugar.bot_qq}"
                f"（当前已登录：{logged}；改完两边都重启）")
            blocked = (3, "找到这个 QQ 的 OneBot 配置了吗")

    # SnowLuma 侧的四个网络数组——下面三项都从这里读（读不出来就是空列表）
    lists = network_lists(onebot_doc)

    # ── 4. 有指向糖糖的反向 WS 客户端吗 ─────────────────
    if blocked:
        skip(KEY_WS_CLIENT, "有指向糖糖的反向 WS 客户端吗", "上游没就绪，配置读不出来")
    else:
        ws_clients = lists["wsClients"]
        wanted = (normalize_host(sugar.ws_host), int(sugar.ws_port))
        hit = _first(ws_clients, lambda c: parse_endpoint(c.get("url")) == wanted)
        panel = f"打开 SnowLuma 网页面板 {webui_url}（用户名 admin）→ 节点配置 → 选机器人 {sugar.bot_qq}"
        if hit:
            extra = [str(c.get("url")) for c in ws_clients if c is not hit]
            add(KEY_WS_CLIENT, "有指向糖糖的反向 WS 客户端吗", Status.OK,
                f"wsClients 里有 {sugar.ws_url}"
                + (f"（另有 {len(extra)} 条指向别处：{'、'.join(extra)}）" if extra else ""))
        elif not ws_clients:
            add(KEY_WS_CLIENT, "有指向糖糖的反向 WS 客户端吗", Status.FAIL,
                "配置里一个 WS 客户端都没有（wsClients 为空）——SnowLuma 不会主动连糖糖",
                f"{panel} →「WS 客户端」→ 新建 → URL 填 {sugar.ws_url} → 保存 → 重启 SnowLuma")
        else:
            urls = "、".join(str(c.get("url") or "（空）") for c in ws_clients)
            default_hit = _first(ws_clients,
                                 lambda c: parse_endpoint(c.get("url"))
                                 == ("127.0.0.1", 8080))
            if default_hit is not None:
                why = (f"WS 客户端的 URL 是 {urls}——这是新建时自带的默认值 8080，没有改；"
                       f"糖糖在听的是 {sugar.ws_url}")
            else:
                why = f"WS 客户端的 URL 是 {urls}，不是糖糖在听的 {sugar.ws_url}"
            add(KEY_WS_CLIENT, "有指向糖糖的反向 WS 客户端吗", Status.FAIL, why,
                f"把那条 WS 客户端的 URL（新建时默认就是 {SNOWLUMA_DEFAULT_WS_CLIENT_URL}，"
                f"必须手动改）改成 {sugar.ws_url}：{panel} →「WS 客户端」→ 编辑 → 保存 → 重启 SnowLuma")

    # ── 5. 有没有误建的「WS 服务端」占了糖糖的端口 ──────
    if blocked:
        skip(KEY_WS_SERVER, "有没有误建的「WS 服务端」占了糖糖的端口", "上游没就绪，配置读不出来")
    else:
        ws_servers = lists["wsServers"]
        wanted = (normalize_host(sugar.ws_host), int(sugar.ws_port))
        conflict = _first(ws_servers, lambda s: _server_endpoint(s) == wanted)
        others_txt = "、".join(
            "{}:{}".format(*(_server_endpoint(s) or ("?", "?"))) for s in ws_servers)
        if conflict is not None:
            add(KEY_WS_SERVER, "有没有误建的「WS 服务端」占了糖糖的端口", Status.FAIL,
                f"SnowLuma 里建了一个「WS 服务端」占着 {sugar.ws_host}:{sugar.ws_port}"
                f"——那是让 SnowLuma 自己去监听这个端口，会把糖糖要用的端口抢走",
                f"打开 SnowLuma 网页面板 {webui_url} → 节点配置 →「WS 服务端」标签页 → "
                f"删掉占 {sugar.ws_port} 的那条（糖糖要的是「WS 客户端」，不是「服务端」）→ 重启 SnowLuma")
        elif ws_servers:
            add(KEY_WS_SERVER, "有没有误建的「WS 服务端」占了糖糖的端口", Status.OK,
                f"有 {len(ws_servers)} 个 WS 服务端（{others_txt}），但没占糖糖的 "
                f"{sugar.ws_port}，不影响")
        else:
            add(KEY_WS_SERVER, "有没有误建的「WS 服务端」占了糖糖的端口", Status.OK,
                "没有多余的「WS 服务端」")

    # ── 6. 有 HTTP 服务器在糖糖要的端口吗 ───────────────
    if blocked:
        skip(KEY_HTTP_SERVER, "有 HTTP 服务器在糖糖要的端口吗", "上游没就绪，配置读不出来")
    else:
        http_servers = lists["httpServers"]
        hit = _first(http_servers, lambda s: _server_endpoint(s)
                     == (normalize_host(sugar.http_host), int(sugar.http_port)))
        panel = f"打开 SnowLuma 网页面板 {webui_url} → 节点配置 → 选机器人 {sugar.bot_qq}"
        if hit:
            add(KEY_HTTP_SERVER, "有 HTTP 服务器在糖糖要的端口吗", Status.OK,
                f"httpServers 里有 {sugar.http_host}:{sugar.http_port}（糖糖靠它发消息）")
        elif not http_servers:
            add(KEY_HTTP_SERVER, "有 HTTP 服务器在糖糖要的端口吗", Status.FAIL,
                "配置里没有 HTTP 服务器——糖糖连上了也发不出消息（它靠 HTTP 调发送接口）",
                f"{panel} →「HTTP API」→ 新建 → 端口填 {sugar.http_port} → 保存 → 重启 SnowLuma")
        else:
            found = "、".join(
                "{}:{}".format(*(_server_endpoint(s) or ("?", "?"))) for s in http_servers)
            add(KEY_HTTP_SERVER, "有 HTTP 服务器在糖糖要的端口吗", Status.FAIL,
                f"HTTP 服务器的端口是 {found}，糖糖在找的是 {sugar.http_port}"
                f"（config.yaml 的 napcat.http_url = {sugar.http_url}）",
                f"把「HTTP API」的端口改成 {sugar.http_port}（{panel} →「HTTP API」→ 编辑 → 保存），"
                f"或把 config.yaml 的 napcat.http_url 改成 {found} 那个端口——两边一致就行；"
                f"改完重启 SnowLuma 与糖糖")

    # ── 7. 授权 Token 三边一致吗 ────────────────────────
    if blocked:
        skip(KEY_TOKEN, "授权 Token 三边一致吗", "上游没就绪，配置读不出来")
    else:
        http_hit = _first(lists["httpServers"], lambda s: _server_endpoint(s)
                          == (normalize_host(sugar.http_host), int(sugar.http_port)))
        ws_hit = _first(lists["wsClients"], lambda c: parse_endpoint(c.get("url"))
                        == (normalize_host(sugar.ws_host), int(sugar.ws_port)))
        http_tok = str((http_hit or {}).get("accessToken") or "")
        ws_tok = str((ws_hit or {}).get("accessToken") or "")
        verdict = compare_tokens(http_tok, ws_tok, sugar.token)
        add(KEY_TOKEN, "授权 Token 三边一致吗", verdict.status, verdict.detail, verdict.next_step)

    # ── 8. 糖糖的反向 WS 服务端在监听吗 ─────────────────
    if probe(probe_host(sugar.ws_host), sugar.ws_port):
        add(KEY_SUGAR_LISTEN, "糖糖的反向 WS 服务端在监听吗", Status.OK,
            f"{sugar.ws_host}:{sugar.ws_port} 有进程在监听——糖糖在跑")
    else:
        add(KEY_SUGAR_LISTEN, "糖糖的反向 WS 服务端在监听吗", Status.FAIL,
            f"{sugar.ws_host}:{sugar.ws_port} 没人监听——糖糖没在跑（或刚崩了）",
            "回控制台点「启动小糖糖」；若卡片一直停在「启动中」"
            "（那表示 SnowLuma 还没连上来），把控制台日志最后 20 行贴出来对照本清单")

    results.append(summarize(results))
    return results


def summarize(results: list) -> CheckResult:
    """汇总结论：卡在第几步 + 一句话下一步。

    入参是**不含 summary 本身**的检查列表（``check_all`` 里就是这么调的）；
    步号即打印出来的序号（从 1 起）。
    """
    problems = [r for r in results if r.status is not Status.OK]
    fails = [r for r in results if r.status is Status.FAIL]
    warns = [r for r in results if r.status is Status.WARN]
    if not problems:
        return CheckResult(KEY_SUMMARY, "汇总结论", Status.OK,
                           "全部通过：SnowLuma 与糖糖的接线是对上的，"
                           "该做的只剩启动小糖糖 / 保持两边运行")

    first = problems[0]
    step = results.index(first) + 1
    if fails:
        status = Status.FAIL
    elif warns:
        status = Status.WARN
    else:
        status = Status.WARN  # 全是 SKIP：有东西没验到，不能算过
    tail = f"（{len(fails)} 项 [×]，{len(warns)} 项 [!]）" if (fails or warns) else ""
    # 把所有待处理步骤列全。
    # 只报"卡在第 N 步"会漏掉后面的问题：新装用户的第一处 [×] 永远是「SnowLuma 没在跑」，
    # 可他真正要动手改的是第 4/5/7 步（没建 WS 客户端、端口被占、token 不一致）——
    # 只告诉他"第 2 步"会让他在启动完 SnowLuma 之后又得再跑一次才发现剩下的。
    todo = "、".join(f"第 {results.index(r) + 1} 步" for r in problems[:6])
    more = "…" if len(problems) > 6 else ""
    return CheckResult(
        KEY_SUMMARY, "汇总结论", status,
        f"卡在第 {step} 步：{first.title}{tail}。待处理：{todo}{more}",
        first.next_step or first.detail,
    )


# ═══════════════════════════════════════════════════════
# 命令行
# ═══════════════════════════════════════════════════════

def main(argv: Optional[list] = None) -> int:
    """打印给小白看的中文清单。返回 0=没有 [×]，1=有 [×]。"""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else BASE

    from datetime import datetime
    print(f"[·] 小糖糖 · SnowLuma 连接检查  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"    项目根: {root}")
    print("    （只读检查：不写文件、不起进程、不改配置）")

    results = check_all(root)
    summary = results[-1] if results and results[-1].key == KEY_SUMMARY else None
    checks = results[:-1] if summary else results

    for i, r in enumerate(checks, 1):
        print(f"\n{GLYPH[r.status]} {i}. {r.title}")
        if r.detail:
            print(f"      {r.detail}")
        if r.next_step and r.status is not Status.OK:
            print(f"      -> {r.next_step}")

    if summary:
        print("\n" + "━" * 58)
        print(f"{GLYPH[summary.status]} {summary.detail}")
        if summary.next_step:
            print(f"    -> {summary.next_step}")
        if summary.status is Status.OK:
            print("    （本工具只查接线；糖糖有没有真的回消息，看控制台日志。）")
    return 1 if any(r.status is Status.FAIL for r in checks) else 0


if __name__ == "__main__":
    code = main()
    # 双击运行时保持窗口不闪退；被控制台/体检脚本调用（stdin 非终端）不阻塞
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n（按任意键退出…）")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
