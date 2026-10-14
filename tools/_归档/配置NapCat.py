"""
自动配置 NapCat 的 WebSocket 和 HTTP
用法：登录 NapCat 后运行 python 配置NapCat.py
"""
import json, glob, os
from pathlib import Path

BASE = Path(__file__).parent
CONFIG_DIR = BASE / "NapCat.Shell.Windows.OneKey/NapCat.44498.Shell/versions"
PATTERN = "*/resources/app/napcat/config/onebot11_*.json"

files = list(Path(CONFIG_DIR).glob(PATTERN))
if not files:
    print("❌ 未找到 NapCat 配置文件，请先启动 NapCat 并扫码登录")
    input("按回车退出...")
    exit(1)

# 找到最新的配置
config_path = max(files, key=lambda f: f.stat().st_mtime)
print(f"📄 找到配置: {config_path.name}")

with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

net = config.setdefault("network", {})

# 确保 HTTP 服务端口 3000
http_servers = net.setdefault("httpServers", [])
has_http = any(s.get("port") == 3000 for s in http_servers)
if not has_http:
    http_servers.append({
        "enable": True, "name": "糖糖HTTP",
        "host": "127.0.0.1", "port": 3000,
        "enableCors": True, "enableWebsocket": False,
        "messagePostFormat": "array",
        "token": "8bzJAHPHug~kvooW", "debug": False
    })
    print("✅ 已添加 HTTP 服务 (端口3000)")

# 确保反向 WebSocket 连接 ws://127.0.0.1:3001
ws_clients = net.setdefault("websocketClients", [])
has_ws = any(c.get("url") == "ws://127.0.0.1:3001" for c in ws_clients)
if not has_ws:
    ws_clients.append({
        "enable": True, "name": "糖糖反向WS",
        "url": "ws://127.0.0.1:3001",
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": "~6bQbCW5TT6Y1AXM",
        "debug": False, "heartInterval": 30000,
        "reconnectInterval": 30000, "verifyCertificate": True
    })
    print("✅ 已添加反向 WebSocket (ws://127.0.0.1:3001)")

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("✅ NapCat 配置完成！重启 NapCat 后生效")
input("按回车退出...")
