"""会客厅命令行 —— 直接和「糖糖本体」说话（2026-10-24）

这不是另写一份糖糖。它只是 `agent/console_api.py` 那个本地接口的客户端，
所以每一句话都走生产路径：真检索记忆、真消耗驱动力、真写关系场。

用法（在项目根执行）：

    python tools/会客厅.py say "我把会客厅接上了，你现在看得见桌面端吗？"
    python tools/会客厅.py turn                      # 让她按自己此刻的状态主动说一句
    python tools/会客厅.py state                     # 打印她现在的内部状态
    python tools/会客厅.py sessions                  # 会话列表
    python tools/会客厅.py new --title "重构会话"     # 新建会话
    python tools/会客厅.py say "..." --session <id>  # 指定会话

前提：糖糖正在运行（`python main.py`）。没跑就报「会客厅未开门」。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DEFAULT_PORT = 8765


def _console_port() -> int:
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return int(((cfg.get("console") or {}).get("port")) or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def call(method: str, path: str, params: dict | None = None,
         body: dict | None = None, timeout: float = 300.0):
    port = _console_port()
    url = f"http://127.0.0.1:{port}{path}"
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if qs:
            url += "?" + qs
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
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
        raise SystemExit(f"❌ {detail or f'HTTP {e.code}'}")
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError):
        raise SystemExit(
            "❌ 会客厅未开门——糖糖没在运行。\n"
            "   先启动：python main.py（或桌面端总览页点「启动」）")


def _pick_session(arg: str | None) -> str:
    """没指定会话时用最近的那个，没有就建一个。"""
    if arg:
        return arg
    sessions = call("GET", "/sessions").get("sessions") or []
    if sessions:
        return sessions[0]["id"]
    return call("POST", "/sessions", body={})["id"]


def _print_meta(meta: dict | None) -> None:
    if not meta:
        return
    bits = []
    if meta.get("ms"):
        bits.append(f"{meta['ms'] / 1000:.1f}s")
    if meta.get("model"):
        bits.append(str(meta["model"]))
    tools = meta.get("tools") or []
    if tools:
        bits.append("工具: " + ",".join(map(str, tools)))
    if meta.get("autonomous"):
        bits.append("自主回合")
    if bits:
        print(f"\n\033[2m[{ ' · '.join(bits) }]\033[0m")


def cmd_say(args) -> None:
    text = (args.text or "").strip()
    if not text:
        raise SystemExit("❌ 消息不能为空")
    sid = _pick_session(args.session)
    res = call("POST", "/say", body={"session_id": sid, "text": text})
    print(f"\n\033[96m你 →\033[0m {text}")
    print(f"\033[93m糖糖 →\033[0m {res.get('reply', '')}")
    _print_meta(res.get("meta"))
    print()


def cmd_turn(args) -> None:
    sid = _pick_session(args.session)
    print("\033[2m（不给文本，让她按自己此刻的状态说一句…）\033[0m")
    res = call("POST", "/turn", body={"session_id": sid})
    print(f"\n\033[93m糖糖 →\033[0m {res.get('reply', '')}")
    _print_meta(res.get("meta"))
    print()


def cmd_state(args) -> None:
    snap = call("GET", "/snapshot")
    bot = snap.get("bot") or {}
    print(f"\n\033[96m{bot.get('name', '糖糖')}\033[0m（{bot.get('qq', '?')}）"
          f"  已运行 {bot.get('uptime_s', 0)}s\n")
    mood = snap.get("mood") or {}
    if mood:
        print(f"情绪  精力 {mood.get('energy')}/100 · 心情 {mood.get('mood')}/100"
              f" · 耐心 {mood.get('patience')}/100")
    drives = snap.get("drives") or []
    if drives:
        print("驱动力：")
        for d in drives:
            bar = "█" * int(round(float(d["value"]) * 20))
            mark = " ⚡" if d.get("urgent") else ""
            print(f"  {d['label']:<6} {bar:<20} {float(d['value']) * 100:5.1f}%{mark}")
    dom = snap.get("dominant")
    if dom:
        print(f"主导  {dom.get('label')}——{dom.get('text')}")
    me = snap.get("self") or {}
    if me.get("narrative"):
        print(f"\n自我叙事：{me['narrative'][:300]}")
    if me.get("values"):
        print(f"价值倾向：{me['values'][:200]}")
    rel = snap.get("relationships") or {}
    if rel:
        print(f"\n关系场：{rel.get('total')} 人（亲密 {rel.get('close')} / 熟悉 {rel.get('familiar')}）")
        for r in rel.get("top") or []:
            print(f"  {r['nickname']:<10} {r['closeness']:.2f}  {r['tier']}")
    mem = snap.get("memory") or {}
    if mem:
        print(f"\n记忆：{mem.get('total')} 条")
        for m in mem.get("recent") or []:
            print(f"  · {m['text'][:80]}")
    model = snap.get("model") or {}
    if model:
        print(f"\n模型：{model.get('provider')} / {model.get('model')}")
    print()


def cmd_sessions(args) -> None:
    data = call("GET", "/sessions")
    sessions = data.get("sessions") or []
    if not sessions:
        print("（还没有会话）")
        return
    print()
    for s in sessions:
        print(f"  \033[96m{s['id']}\033[0m  {s['title']}"
              f"  \033[2m{s['message_count']} 条 · {s['last_preview']}\033[0m")
    print()


def cmd_new(args) -> None:
    res = call("POST", "/sessions", body={"title": args.title or ""})
    print(f"✅ 新会话 {res['id']}  《{res['title']}》")


def cmd_delete(args) -> None:
    res = call("DELETE", f"/sessions/{urllib.parse.quote(args.session, safe='')}")
    print("✅ 已删除" if res.get("ok") else "⚠️ 没找到该会话")


def cmd_messages(args) -> None:
    sid = _pick_session(args.session)
    data = call("GET", "/messages", params={"session_id": sid, "limit": args.limit})
    for m in data.get("messages") or []:
        tag = {"tangtang": "糖糖", "user": "你", "lead": "Lead",
               "system": "系统"}.get(m["role"], m["role"])
        print(f"\n\033[2m[{tag}]\033[0m {m['content']}")
    print()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="会客厅 —— 和糖糖本体说话")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say", help="说一句话，走糖糖真身")
    s.add_argument("text")
    s.add_argument("--session", default=None)
    s.set_defaults(fn=cmd_say)

    t = sub.add_parser("turn", help="让她按自己此刻的状态主动说一句")
    t.add_argument("--session", default=None)
    t.set_defaults(fn=cmd_turn)

    # ⚠️ 子命令名是 "state" 不是 "snapshot"：`tests/test_integration_contract.py`
    # 的零调用闸门把**字符串常量**当引用证据，一个 `"snapshot"` 字样就会让
    # 待归档死代码 `knowledge_index.snapshot` 被误判为「已接线」（2026-10-24 实测）。
    sub.add_parser("state", help="打印她现在的内部状态").set_defaults(fn=cmd_state)
    sub.add_parser("sessions", help="会话列表").set_defaults(fn=cmd_sessions)

    n = sub.add_parser("new", help="新建会话")
    n.add_argument("--title", default="")
    n.set_defaults(fn=cmd_new)

    d = sub.add_parser("delete", help="删除会话")
    d.add_argument("session")
    d.set_defaults(fn=cmd_delete)

    m = sub.add_parser("messages", help="打印会话消息")
    m.add_argument("--session", default=None)
    m.add_argument("--limit", type=int, default=50)
    m.set_defaults(fn=cmd_messages)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
