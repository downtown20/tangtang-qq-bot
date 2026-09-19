"""
自动配置 SnowLuma 的 OneBot WebSocket 和 HTTP
用法：登录 SnowLuma 后运行 python 配置SnowLuma.py
     或首次启动 SnowLuma 并扫码后运行
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent  # 项目根目录

# 查找 SnowLuma 安装目录（优先 v1.12+）
SNOWLUMA_DIRS = list(BASE.glob("SnowLuma/SnowLuma-v*.win-x64*"))
if not SNOWLUMA_DIRS:
    SNOWLUMA_DIRS = list(BASE.glob("SnowLuma/SnowLuma-*"))

if not SNOWLUMA_DIRS:
    print("❌ 未找到 SnowLuma 安装目录（SnowLuma/SnowLuma-v*/）")
    print("   请先解压 SnowLuma 到项目目录")
    input("按回车退出...")
    sys.exit(1)

SNOWLUMA_DIR = sorted(SNOWLUMA_DIRS)[-1]  # 取最新版本
CONFIG_DIR = SNOWLUMA_DIR / "config"

# 读取 config.yaml 获取机器人 QQ 号和 token
import yaml

cfg_path = BASE / "config.yaml"
if not cfg_path.exists():
    print("❌ 未找到 config.yaml")
    sys.exit(1)

cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
bot_qq = str(cfg.get("bot", {}).get("qq_id", ""))
token = cfg.get("napcat", {}).get("access_token", "8bzJAHPHug~kvooW")

if not bot_qq or bot_qq == "你的机器人QQ号":
    print("❌ config.yaml 中的 bot.qq_id 未填写，请先设置机器人 QQ 号")
    input("按回车退出...")
    sys.exit(1)

# SnowLuma 配置文件：onebot_<QQ号>.json
onebot_config_path = CONFIG_DIR / f"onebot_{bot_qq}.json"

# 如果还没登录过（配置文件不存在），列出已有配置供参考
if not onebot_config_path.exists():
    existing = list(CONFIG_DIR.glob("onebot_*.json"))
    if existing:
        print(f"⚠ 未找到 {onebot_config_path.name}")
        print(f"  当前已登录的 QQ: {[f.stem.replace('onebot_', '') for f in existing]}")
        print(f"  config.yaml 中设置的 bot.qq_id = {bot_qq}")
        print(f"  请确认 config.yaml 中的 QQ 号与 SnowLuma 登录的账号一致")
        if len(existing) == 1:
            print(f"  → 使用已有配置: {existing[0].name}")
            onebot_config_path = existing[0]
        else:
            print("❌ 请先在 SnowLuma 中登录机器人 QQ，然后重新运行此脚本")
            input("按回车退出...")
            sys.exit(1)
    else:
        print("❌ 请先在 SnowLuma 中登录机器人 QQ（扫码），然后重新运行此脚本")
        input("按回车退出...")
        sys.exit(1)

print(f"📄 SnowLuma 目录: {SNOWLUMA_DIR.name}")
print(f"📄 OneBot 配置: {onebot_config_path.name}")

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
        "host": "0.0.0.0",
        "port": 3000,
        "path": "/"
    })
    print("✅ 已添加 HTTP 服务 (端口 3000)")
else:
    # 更新已有 HTTP 配置的 token
    for s in http_servers:
        if s.get("port") == 3000:
            s["accessToken"] = token
    print("✅ HTTP 服务已存在，已同步 Token")

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
    print("✅ 已添加反向 WebSocket (ws://127.0.0.1:3001)")
else:
    # 更新已有 WS 配置的 token
    for c in ws_clients:
        if c.get("url") == "ws://127.0.0.1:3001":
            c["accessToken"] = token
    print("✅ 反向 WebSocket 已存在，已同步 Token")

with open(onebot_config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print(f"\n✅ SnowLuma OneBot 配置完成！")
print(f"   HTTP API:  http://127.0.0.1:3000")
print(f"   反向 WS:   ws://127.0.0.1:3001")
print(f"   Token:     {token[:8]}...")
print(f"\n⚠ 请重启 SnowLuma 使配置生效")
input("按回车退出...")
