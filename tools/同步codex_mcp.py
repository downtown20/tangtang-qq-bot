# -*- coding: utf-8 -*-
"""
同步 Claude Code 的 MCP 服务器到 Codex CLI 配置（2026-08-25）

背景：MCP 协议本身跨 harness 互通——Codex 原生支持 MCP（实测 codex-cli 0.147），
同一个服务器进程两边共用。唯一痛点是配置写两份：Claude Code 在 ~/.claude.json
（JSON mcpServers），Codex 在 ~/.codex/config.toml（TOML [mcp_servers.<name>]）。
本脚本把 .claude.json 里登记的 MCP 服务器镜像成 config.toml 的对应段。

纪律（不要改）：
- 只追加不解析：config.toml 里还有 Codex 自己维护的表（features/plugins/
  model_providers 等），全文件解析重写会破坏它们——纯文本追加，TOML 允许
  表头分散出现，Codex 正常加载。
- 幂等：已存在的 [mcp_servers.<name>] 段跳过，重复运行安全。
- 密钥不出日志：header/env 值直接从 .claude.json 读入写出，绝不 print。
- token 轮换后（anysearch/github 的 Authorization）重跑本脚本刷新。

用法：python tools/同步codex_mcp.py（改完重启 Codex 会话，codex mcp list 可确认）
"""
import json
import sys
from pathlib import Path

CLAUDE_JSON = Path.home() / ".claude.json"
CODEX_TOML = Path.home() / ".codex" / "config.toml"


def _toml_str(s: str) -> str:
    """最小 TOML 双引号字符串转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _inline_table(pairs: dict) -> str:
    return "{ " + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in pairs.items()) + " }"


def is_usable(cfg) -> bool:
    """这份 MCP 配置能不能真的用起来（2026-10-14 台账 T12）。

    Codex 的 mcp_servers 条目必须二选一：要么有 `url`（HTTP/SSE 型），
    要么有非空 `command`（stdio 型）。两者都没有的条目写进 config.toml 就是**死配置**——
    Codex 启动时认出这个 server 却起不来，而同步脚本已经报「已写入」。
    """
    if not isinstance(cfg, dict):
        return False
    if (cfg.get("url") or "").strip():
        return True
    return bool((cfg.get("command") or "").strip())


def build_block(name: str, cfg: dict) -> str:
    """生成一个 [mcp_servers.X] 块。**调用前必须先用 is_usable() 过滤**——
    退化配置（既无 url 又无 command）会产出 `command = ""` 死块，
    这里显式抛 ValueError 而不是静默产出（教训：死配置比报错更贵）。"""
    if not is_usable(cfg):
        raise ValueError(
            f"mcp server {name!r} 既没有 url 也没有 command——"
            f"写进 config.toml 会是死配置，拒绝生成")
    lines = []
    if (cfg.get("url") or "").strip():
        lines.append(f"url = {_toml_str(cfg['url'])}")
        headers = cfg.get("headers") or {}
        if headers:
            lines.append(f"http_headers = {_inline_table(headers)}")
    else:
        lines.append(f"command = {_toml_str(cfg['command'])}")
        lines.append("args = [" + ", ".join(_toml_str(a) for a in (cfg.get("args") or [])) + "]")
    env = cfg.get("env") or {}
    if env:
        lines.append(f"env = {_inline_table(env)}")
    return f"[mcp_servers.{name}]\n" + "\n".join(lines)


def main() -> bool:
    """同步 MCP 配置。**返回是否全部成功**（2026-10-14 台账 T12）。

    三处修复：
    1. 退化配置（既无 url 又无 command）**不进 added**——过去只按 exists 分流，
       于是 `{'args': ['a']}` 这种会被写成 `command = ""` 死块；
    2. 这类条目计入 `skipped` 并**在输出里点名**，不再制造「已写入 N 个」的虚假成功；
    3. 返回 bool，供 `__main__` 变成退出码（与 同步skills.py 同一课）。
    """
    if not CLAUDE_JSON.exists():
        print(f"未找到 {CLAUDE_JSON}")
        return False
    servers = json.loads(CLAUDE_JSON.read_text(encoding="utf-8")).get("mcpServers") or {}
    if not servers:
        print("~/.claude.json 里没有 mcpServers")
        return True   # 没东西要同步不算失败
    existing = CODEX_TOML.read_text(encoding="utf-8") if CODEX_TOML.exists() else ""
    added, skipped, unusable = [], [], []
    for name in servers:
        if f"[mcp_servers.{name}]" in existing:
            skipped.append(name)
            continue
        if not is_usable(servers[name]):
            unusable.append(name)
            continue
        added.append(name)
    if unusable:
        print(f"⚠️ 跳过 {len(unusable)} 个不可用配置（既无 url 又无 command，写了也是死块）: "
              f"{', '.join(unusable)}")
    if not added:
        tail = f"（跳过已存在: {', '.join(skipped)}）" if skipped else ""
        print(f"没有需要写入的服务器{tail}")
        return True
    blocks = "\n\n".join(build_block(n, servers[n]) for n in added)
    with CODEX_TOML.open("a", encoding="utf-8") as f:
        f.write("\n" + blocks + "\n")
    print(f"已写入 {len(added)} 个服务器: {', '.join(added)}"
          f"（跳过已存在: {', '.join(skipped) or '无'}）")
    print("重启 Codex 会话后生效，`codex mcp list` 可确认")
    return True


if __name__ == "__main__":
    _rc = 0
    try:
        # 2026-10-14 台账 T12：与 同步skills.py 同课——结果要变成退出码。
        if not main():
            _rc = 1
    finally:
        # 双击 .py 时窗口跑完即关（看起来像闪退）——stdin 是终端才暂停等回车；
        # 命令行/管道/自动化调用不受影响（isatty 在 cmd 管道下可能误判 True，
        # EOFError 守卫兜底：无输入源立即退出，不报错不阻塞）
        if sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except EOFError:
                pass
        sys.exit(_rc)
