"""`tools/配置SnowLuma.py` 的沙箱回归。

为什么要有这个文件：这个脚本会把 token 写进**第三方程序**（SnowLuma）的配置里，
写错了不会报错——只会让两边暗号对不上，症状是「连上了、发不出去、报 401」，
而糖糖控制台的界面全绿。属于「静默失败」重灾区。

2026-09-20 修的两个缺陷，各有一条闸门：
  1. 它读 `config.yaml` 时**不做 `${}` 展开**，会把字面量 `${SNOWLUMA_TOKEN}`
     当成 token 写进 SnowLuma；
  2. 它有一个写死的兜底值，是维护者本机的真实 token（随包发出去过）。

全部在 tmp_path 里跑，绝不碰本机上真实的 SnowLuma。
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "tools" / "配置SnowLuma.py"

# 夹具里的 token **分两段拼**：脱敏扫描器把 `XXX_TOKEN=<一串像 token 的值>` 当作
# 真泄漏的形状来抓——它抓得对，所以夹具不该长成那个样子（第一版就是这么被
# `tools/准备发布.py` 拦下的）。扫描器自己藏 `_TOK_ASSIGN` 用的也是同一招。
_FAKE_TOKEN = "real-token" + "-1234"
_ENV_WITH_TOKEN = "SNOWLUMA_TOKEN=" + _FAKE_TOKEN + "\n"


# SnowLuma 的出厂配置（`makeDefaultOneBotConfig()` 逐字对照过）：
#   http-default 在 127.0.0.1:3000，ws-default 在 127.0.0.1:3001，wsClients 空。
# 那个 ws-default 占的 3001 正是糖糖要绑的端口——本文件第一条测试就是为它写的。
FACTORY_NETWORKS = {
    "httpServers": [{"name": "http-default", "host": "127.0.0.1", "port": 3000,
                     "path": "/", "accessToken": "factory-http-token-aaaaaaaa"}],
    "httpClients": [],
    "wsServers": [{"name": "ws-default", "host": "127.0.0.1", "port": 3001,
                   "path": "/", "accessToken": "factory-ws-token-bbbbbbbb"}],
    "wsClients": [],
}


def _sandbox(tmp_path: Path, env_line: str, networks: dict | None = None) -> Path:
    """搭一个最小的假项目：config.yaml + .env + 一个已登录 QQ 的 SnowLuma 配置。"""
    (tmp_path / "tools").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "tools" / "配置SnowLuma.py")
    (tmp_path / "config.yaml").write_text(
        "bot:\n  qq_id: '10001'\nnapcat:\n  access_token: ${SNOWLUMA_TOKEN}\n",
        encoding="utf-8")
    (tmp_path / ".env").write_text(env_line, encoding="utf-8")

    cfg_dir = tmp_path / "SnowLuma" / "SnowLuma-v1.14.9-win-x64" / "config"
    cfg_dir.mkdir(parents=True)
    onebot = cfg_dir / "onebot_10001.json"
    onebot.write_text(
        json.dumps({"networks": networks if networks is not None else FACTORY_NETWORKS},
                   ensure_ascii=False),
        encoding="utf-8")
    return onebot


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    # 清掉环境里可能存在的 SNOWLUMA_TOKEN——否则本机环境会盖过沙箱的 .env，
    # 测试就变成了「测本机」而不是「测脚本」。
    import os
    env = {k: v for k, v in os.environ.items() if k != "SNOWLUMA_TOKEN"}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "配置SnowLuma.py")],
        cwd=str(tmp_path), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=90)


def test_writes_resolved_token_never_the_placeholder(tmp_path: Path):
    """跑通了就要写**解析后的真值**。

    这是 2026-09-20 修的主缺陷：原实现直接把 `${SNOWLUMA_TOKEN}` 写进 SnowLuma，
    而糖糖发出去的是环境变量里的真值 → 两边必然不一致。
    """
    onebot = _sandbox(tmp_path, _ENV_WITH_TOKEN)
    _run(tmp_path)

    cfg = json.loads(onebot.read_text(encoding="utf-8"))
    nets = cfg["networks"]
    tokens = ([s["accessToken"] for s in nets["httpServers"]]
              + [c["accessToken"] for c in nets["wsClients"]])
    assert tokens, f"连接一条也没写进去：{nets}"
    assert all(t == _FAKE_TOKEN for t in tokens), (
        f"写进去的不是解析后的真值：{tokens}")
    assert not any("${" in t for t in tokens), (
        f"占位符被当成 token 写进了第三方配置：{tokens}")


def test_refuses_and_writes_nothing_when_token_unset(tmp_path: Path):
    """没设 token 时必须**什么都不写**，并告诉用户去哪填。

    宁可不动，也不能替他造一个假暗号——那会让 SnowLuma 和糖糖各说各话。
    """
    onebot = _sandbox(tmp_path, "# 空 .env\n")
    before = onebot.read_text(encoding="utf-8")
    proc = _run(tmp_path)

    assert onebot.read_text(encoding="utf-8") == before, (
        "没设 token 却还是动了 SnowLuma 的配置")
    assert "密钥管理" in proc.stdout, (
        f"拒绝时没告诉用户下一步去哪填：\n{proc.stdout}\n{proc.stderr}")


def test_removes_factory_ws_server_that_steals_port_3001(tmp_path: Path):
    """出厂自带的 `ws-default` 必须被清掉——它占着糖糖要绑的 3001。

    这是 2026-09-20 补的洞：脚本原先只写 `httpServers` 和 `wsClients`，
    **完全不碰 `wsServers`**。于是脚本打印「✅ 配置完成」，而糖糖一启动就抛
    「反向 WS 端口 127.0.0.1:3001 已被占用」——那个节点还在 3001 上监听。
    """
    onebot = _sandbox(tmp_path, _ENV_WITH_TOKEN)
    proc = _run(tmp_path)

    cfg = json.loads(onebot.read_text(encoding="utf-8"))
    still_there = [s.get("name") for s in cfg["networks"]["wsServers"]
                   if int(s.get("port") or 0) == 3001]
    assert not still_there, (
        f"出厂节点还占着 3001：{still_there}——糖糖会起不来。\n{proc.stdout}")
    assert cfg["networks"]["wsClients"], "反向 WS 客户端没写进去"


def test_refuses_when_a_foreign_node_holds_port_3001(tmp_path: Path):
    """不是出厂默认、却占着 3001 的节点：不许自作主张删，要停下来说清楚。

    换名字/换地址的节点可能是用户自己有意配的，删它就越界了。
    """
    nets = json.loads(json.dumps(FACTORY_NETWORKS))
    nets["wsServers"] = [{"name": "我自己建的转发", "host": "127.0.0.1", "port": 3001}]
    onebot = _sandbox(tmp_path, _ENV_WITH_TOKEN, nets)
    before = onebot.read_text(encoding="utf-8")
    proc = _run(tmp_path)

    assert onebot.read_text(encoding="utf-8") == before, (
        "把用户自己配的节点删了——越界了")
    assert "3001" in proc.stdout and "WS 服务端" in proc.stdout, (
        f"拒绝时没说清下一步：\n{proc.stdout}\n{proc.stderr}")


def test_never_ships_a_hardcoded_fallback_token():
    """源码里不许再出现写死的 token 兜底值。

    原先那行是 `cfg.get("napcat", {}).get("access_token", "<维护者本机的真 token>")`
    ——它随着发布包发到了每个用户手里。
    """
    import re
    src = SCRIPT.read_text(encoding="utf-8")
    # 只看代码行，注释里提到旧值不算（本文件的注释里就引用了它）
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    # 默认值必须"看起来像 token"（ASCII 字母数字，≥8 位）才算真兜底——
    # 中文提示文案（如 "（没有这一项）"）是给人看的，不是暗号。
    bad = re.findall(
        r'\.get\(\s*["\']access_token["\']\s*,\s*["\']([A-Za-z0-9_~.\-]{8,})["\']', code)
    assert not bad, f"又出现了写死的 token 兜底值：{bad}"


def test_does_not_claim_deletion_when_it_aborts(tmp_path: Path):
    """中止时不许说「已删掉」——那次写盘根本没发生。

    2026-09-20 审查发现的顺序问题：原先先打印「已删掉出厂自带的 ws-default」，
    再检查有没有别的节点也占着 3001，有就 `sys.exit(1)` —— 而退出**跳过了写盘**。
    用户被告知扔了一样实际还在的东西，下次跑还是同一结果。

    触发很现实：新手按旧文档试过「新建 WS 服务端」，那个标签页的默认端口
    恰好就是 3001。
    """
    nets = json.loads(json.dumps(FACTORY_NETWORKS))
    nets["wsServers"].append({"name": "我自己建的转发", "host": "127.0.0.1", "port": 3001})
    onebot = _sandbox(tmp_path, _ENV_WITH_TOKEN, nets)
    before = onebot.read_text(encoding="utf-8")
    proc = _run(tmp_path)

    assert onebot.read_text(encoding="utf-8") == before, "中止了却还是写了盘"
    assert "已删掉" not in proc.stdout, (
        f"中止了却告诉用户「已删掉」——他会以为已经处理好了：\n{proc.stdout}")
