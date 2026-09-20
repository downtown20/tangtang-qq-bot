"""把糖糖要用的两条 OneBot 连接写进 SnowLuma 的配置（反向 WebSocket + HTTP）。

用法：`python tools/配置SnowLuma.py`

前提（缺一不可）：
  1. SnowLuma 已经**登录过机器人 QQ**——配置文件名是 `onebot_<QQ号>.json`，
     没登录过就没有这个文件，本脚本无从下手。
  2. 控制台「密钥管理」里已经填了 SnowLuma Token。**不填的话本脚本会拒绝运行**：
     `config.yaml` 里存的是占位符 `${SNOWLUMA_TOKEN}`，直接写进去等于给 SnowLuma
     设了一个和糖糖不一致的暗号，症状是「连上了、发不出去、报 401」。

不想跑脚本也可以——同样的东西在 SnowLuma 网页面板（`http://127.0.0.1:5099`）
「节点配置」页里手动加一遍即可，控制台「连接自检」会把每一步该填什么都列出来。
写完要**重启 SnowLuma** 才生效。
"""
import json
import os
import re
import sys
from pathlib import Path

# 强制 UTF-8 输出：管道/重定向时 Python 退回 GBK，print 非 GBK 字符会抛
# UnicodeEncodeError，而且**崩在业务逻辑之前**（2026-09-20 独立复核实测撞到）。
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = Path(__file__).parent.parent  # 项目根目录

# 查找 SnowLuma 安装目录（优先 v1.12+）
SNOWLUMA_DIRS = list(BASE.glob("SnowLuma/SnowLuma-v*.win-x64*"))
if not SNOWLUMA_DIRS:
    SNOWLUMA_DIRS = list(BASE.glob("SnowLuma/SnowLuma-*"))

if not SNOWLUMA_DIRS:
    print("[×] 未找到 SnowLuma 安装目录（SnowLuma/SnowLuma-v*/）")
    print("   请先解压 SnowLuma 到项目目录")
    input("按回车退出...")
    sys.exit(1)

SNOWLUMA_DIR = sorted(SNOWLUMA_DIRS)[-1]  # 取最新版本
CONFIG_DIR = SNOWLUMA_DIR / "config"

# 读取 config.yaml 获取机器人 QQ 号和 token
import yaml

cfg_path = BASE / "config.yaml"
if not cfg_path.exists():
    print("[×] 未找到 config.yaml")
    sys.exit(1)

# `.env` 里的值优先——控制台「密钥管理」保存时就写在那儿
try:
    from dotenv import load_dotenv
    if (BASE / ".env").exists():
        load_dotenv(BASE / ".env")
except ImportError:
    pass

_ENV_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')


def resolve_env(value: str) -> str:
    """把 `${VAR}` 解析成环境变量值。规则与 main.py 的 _resolve_env_vars 一致。

    [!] 2026-09-20 修：原实现直接 `cfg.get(...)`，读到的是**未解析的占位符**。
    `config.yaml` 里写的就是 `access_token: ${SNOWLUMA_TOKEN}`，于是本脚本把这串
    字面量当成 token 写进了 SnowLuma，而糖糖实际发出去的是环境变量里的真值——
    两边必然对不上，表现为「连上了、发不出去、报 401」，且界面全绿看不出问题。
    """
    m = _ENV_RE.match(value or "")
    if not m:
        return value or ""
    return os.environ.get(m.group(1), value)


# [!] 原先这里的兜底值是维护者本机的真实 token（硬编码在随包发布的文件里）。
#   已删除：token 必须来自用户自己的配置，没有就让用户去填，不能替他决定。
cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
bot_qq = str(cfg.get("bot", {}).get("qq_id", ""))
token = resolve_env(cfg.get("napcat", {}).get("access_token", ""))

if not token or _ENV_RE.match(token):
    print("[×] 还没设置 SnowLuma Token —— 不能帮你写一个假的进去")
    print(f"   config.yaml 里是：{cfg.get('napcat', {}).get('access_token', '（没有这一项）')}")
    print("   请先：双击 启动控制台.bat →「密钥管理」→ 填 / 生成「SnowLuma Token」→ 保存")
    print("   然后回来重跑本脚本。")
    print("   （这一串要和 SnowLuma 网页面板里那一栏「授权 Token」完全一样。）")
    input("按回车退出...")
    sys.exit(1)

if not bot_qq or bot_qq == "你的机器人QQ号":
    print("[×] config.yaml 中的 bot.qq_id 未填写，请先设置机器人 QQ 号")
    input("按回车退出...")
    sys.exit(1)

# SnowLuma 配置文件：onebot_<QQ号>.json
onebot_config_path = CONFIG_DIR / f"onebot_{bot_qq}.json"

# 如果还没登录过（配置文件不存在），列出已有配置供参考
if not onebot_config_path.exists():
    existing = list(CONFIG_DIR.glob("onebot_*.json"))
    if existing:
        print(f"[!] 未找到 {onebot_config_path.name}")
        print(f"  当前已登录的 QQ: {[f.stem.replace('onebot_', '') for f in existing]}")
        print(f"  config.yaml 中设置的 bot.qq_id = {bot_qq}")
        print("  请确认 config.yaml 中的 QQ 号与 SnowLuma 登录的账号一致")
        if len(existing) == 1:
            print(f"  → 使用已有配置: {existing[0].name}")
            onebot_config_path = existing[0]
        else:
            print("[×] 请先在 SnowLuma 中登录机器人 QQ，然后重新运行此脚本")
            input("按回车退出...")
            sys.exit(1)
    else:
        print("[×] 请先用 QQ 客户端登录机器人 QQ 并保持开着，再运行此脚本")
        input("按回车退出...")
        sys.exit(1)

print(f"[i] SnowLuma 目录: {SNOWLUMA_DIR.name}")
print(f"[i] OneBot 配置: {onebot_config_path.name}")

# 读取现有配置
with open(onebot_config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

net = config.setdefault("networks", {})

# ---- HTTP Server (端口 3000) ----
http_servers = net.setdefault("httpServers", [])
has_http = any(s.get("port") == 3000 for s in http_servers)
if not has_http:
    http_servers.append({
        "name": "http-default",
        "accessToken": token,
        "messageFormat": "array",
        "reportSelfMessage": False,
        # host 用 127.0.0.1 而不是 0.0.0.0：糖糖永远从本机连过来
        # （main.py 里 ws_host 是写死的 127.0.0.1），绑到所有网卡只会把
        # OneBot 接口白送给局域网。SnowLuma 自己的出厂默认也是 127.0.0.1。
        "host": "127.0.0.1",
        "port": 3000,
        "path": "/"
    })
    print("[√] 已添加 HTTP 服务 (端口 3000)")
else:
    # 更新已有 HTTP 配置的 token
    for s in http_servers:
        if s.get("port") == 3000:
            s["accessToken"] = token
    print("[√] HTTP 服务已存在，已同步 Token")

# ---- 反向 WebSocket 客户端 (连接 ws://127.0.0.1:3001) ----
ws_clients = net.setdefault("wsClients", [])
has_ws = any(c.get("url") == "ws://127.0.0.1:3001" for c in ws_clients)
if not has_ws:
    ws_clients.append({
        "name": "wsclient-1",
        "accessToken": token,
        "messageFormat": "array",
        "reportSelfMessage": False,
        "url": "ws://127.0.0.1:3001",
        "role": "Universal",
        "reconnectIntervalMs": 5000
    })
    print("[√] 已添加反向 WebSocket (ws://127.0.0.1:3001)")
else:
    # 更新已有 WS 配置的 token
    for c in ws_clients:
        if c.get("url") == "ws://127.0.0.1:3001":
            c["accessToken"] = token
    print("[√] 反向 WebSocket 已存在，已同步 Token")

# ---- 反向 WebSocket 服务端：清掉占着 3001 的出厂节点 ----
#
# [!] 2026-09-20 补。这一段原先没有，于是本脚本"成功"了、糖糖却照样起不来：
#   SnowLuma 的出厂配置（`config-*.js` 里的 `makeDefaultOneBotConfig()`）自带一个
#   叫 `ws-default` 的节点，在 `127.0.0.1:3001` 上**开一个 WebSocket 服务端**——
#   而 3001 正是**糖糖**要绑的端口（拓扑是糖糖开服务端、SnowLuma 连过来）。
#   两边抢同一个端口，糖糖启动时直接抛「反向 WS 端口 127.0.0.1:3001 已被占用」，
#   而那句报错把它归因成"旧糖糖进程"，用户永远找不到真凶。
#   这个节点在糖糖的拓扑里没有用处，删掉不影响任何功能。
#
# 保守起见：**只删认出是出厂默认的那一个**（名字 `ws-default` + 本机地址 +
# 正好占着 3001）。换了名字或地址的就不动，只提示用户自己去面板处理。
def _port_of(node) -> int:
    try:
        return int(node.get("port") or 0)
    except (TypeError, ValueError):
        return 0


TANGSANG_WS_PORT = 3001   # 与 main.py 里写死的 ws_port 一致
ws_servers = net.setdefault("wsServers", [])
_factory = [s for s in ws_servers
            if s.get("name") == "ws-default"
            and str(s.get("host") or "") in ("127.0.0.1", "0.0.0.0", "localhost")
            and _port_of(s) == TANGSANG_WS_PORT]
_others = [s for s in ws_servers
           if _port_of(s) == TANGSANG_WS_PORT and s not in _factory]

# [!] 顺序要紧（2026-09-20 审查）：先判 `_others` 再动手。
#   原先先打印「已删掉出厂自带的 ws-default」、再检查 `_others`、发现冲突就
#   `sys.exit(1)` 退出 —— 而退出**跳过了下面的写盘**。于是用户被告知扔了一样
#   实际还在的东西，下次跑还是同一结果。触发很现实：新手按旧文档试过
#   「新建 WS 服务端」，而那个标签页的默认端口恰好就是 3001。
if _others:
    print(f"[×] 有节点占着 {TANGSANG_WS_PORT}：{[s.get('name') for s in _others]}")
    print("    它不是出厂默认的那个，我不替你删。请到网页面板 → 节点配置 → 选中账号 →")
    print(f"    「WS 服务端」→ 把它删掉或改成别的端口（糖糖要用 {TANGSANG_WS_PORT}）→ 保存。")
    print("    （改完再跑一次本脚本。）")
    input("按回车退出...")
    sys.exit(1)

if _factory:
    net["wsServers"] = [s for s in ws_servers if s not in _factory]
    print(f"[!] 已删掉出厂自带的 {[s.get('name') for s in _factory]} —— "
          f"它占着 {TANGSANG_WS_PORT}，而那个端口归糖糖用")
    print("    （这才是「端口已被占用」的真凶；它不是旧糖糖进程）")

with open(onebot_config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("\n[√] SnowLuma OneBot 配置完成！")
print("   HTTP API:  http://127.0.0.1:3000")
print("   反向 WS:   ws://127.0.0.1:3001（由 SnowLuma 主动连糖糖）")
print(f"   Token:     {token[:8]}…（两边一致才有用）")
print()
print("[!] 一定要**重启 SnowLuma** 这份配置才生效——" +
      "在它的窗口里关掉，再从控制台点「启动 SnowLuma」；")
print("    或者在网页面板「节点配置」里点一下右上角「保存」，会热重载。")
print("    起来之后回控制台点「连接自检」，全绿了再点「启动小糖糖」。")
input("按回车退出...")
