# -*- coding: utf-8 -*-
"""会客厅验「悬着的事」——端到端验证「写侧 + 读侧」真的在生产路径上。

## 为什么需要它

2026-10-25 第 12 轮发现：`open_loops` 表 11145 个提取任务、**0 行**，根因是
open_loop 的落库写在一个生产零调用的死方法里。单元测试当时是绿的——因为它
直接调 `complete_extraction_job`，证明不了**生产会不会调用它**。

第 13 轮把读侧（注入/收尾/过期）接上后，同样只能靠端到端证明：
**注入块真的进了最终 prompt**、**她真的调了收尾工具**、**收尾后不再浮上来**。

## 两种用法

    python tools/会客厅验_未闭合事件.py            # ① 说 6 句（私聊 6 条触发提取）
    python tools/会客厅验_未闭合事件.py --check    # ② 只查现状（不产生对话）

**第一步（建悬事）**：说 6 句带将来时的话 → 等 45 秒 → 查 `open_loops` 是否有新行，
`source_memory_id` 指回 `key='event'` 的记忆。

**第二步（验注入与收尾）**：再发一句「事情办完了」→ 用 `--check` 看
`/traces` 最近一回合的 `prompt_digest.user_bg_labels` 是否含「悬着的事」、
`tool_steps` 里有没有 `resolve_open_loop`；再查 DB 的 `status` / `resolution`。

## ⚠️ 它会写生产数据（有意为之）

会客厅是项目指定的验证通道：`/say` 会写真实 `chat_log`、可能触发记忆提取与
`open_loops` 落库。**只在本机 bot 运行时用**，说话内容要像真人。
"""

import argparse
import os
import sqlite3
import time

import httpx

BASE = "http://127.0.0.1:8765"
SESSION = "e2e-openloop"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 6 句：前 5 句铺垫 + 第 2/3 句构成一件明确的未结事 + 第 6 句第二件。
MESSAGES = [
    "我今天有点累，事情好多",
    "我明天要去医院复查一下",
    "还没预约，得早上八点起来抢号",
    "你说我要不要现在就预约",
    "算了，等会儿再说吧",
    "对了，下周还要交一份报告，我完全没开始写",
]
DONE_MESSAGE = "我今天去医院复查了，结果还行，医生说没事了"


def _db():
    return sqlite3.connect(
        "file:%s?mode=ro" % os.path.join(ROOT, "memory.db").replace("\\", "/"),
        uri=True)


def check():
    with httpx.Client(timeout=60) as client:
        traces = (client.get(f"{BASE}/traces", params={"limit": 3}).json()
                  .get("traces") or [])
    print("== 最近 3 个回合的 prompt_digest ==")
    for t in traces:
        digest = t.get("prompt_digest") or {}
        tools = [s.get("name") for s in (t.get("tool_steps") or [])]
        print("  %s  bg=%s" % (str(t.get("scope_id")), digest.get("user_bg_labels")))
        if tools:
            print("    tools =", tools)
    print()
    print("== open_loops ==")
    rows = _db().execute(
        "SELECT id, qq_id, topic, kind, expected_at, status, asked_count, resolution "
        "FROM open_loops ORDER BY id").fetchall()
    for row in rows:
        print("  ", row)
    return rows, traces


def talk(messages, session=SESSION):
    replies = []
    with httpx.Client(timeout=180) as client:
        for text in messages:
            resp = client.post(f"{BASE}/say",
                               json={"session_id": session, "text": text})
            resp.raise_for_status()
            reply = str(resp.json().get("reply") or "")
            replies.append(reply)
            print("→", text)
            print("←", reply[:160].replace("\n", " "))
            print()
    print("=" * 60)
    print("完成 %d 轮（%s）" % (len(replies), session))
    if messages is MESSAGES:
        print("等 45 秒让提取 worker 跑完（私有 6 条触发 → 建任务 → 落库）…")
        time.sleep(45)
    print("done")
    return replies


def main():
    ap = argparse.ArgumentParser(description="会客厅端到端验证（只读检查 + 可选说话）")
    ap.add_argument("--check", action="store_true", help="只查现状，不说话")
    ap.add_argument("--done", action="store_true",
                    help="发一句「事情办完了」，验证收尾链路")
    # 2026-10-25 第 14 轮：同一件事复验要换 session，否则历史上下文混进来
    ap.add_argument("--session", default=SESSION,
                    help="会客厅会话 id（默认 %s）" % SESSION)
    args = ap.parse_args()

    if args.done:
        talk([DONE_MESSAGE], session=args.session)
        time.sleep(2)
        check()
        return 0
    if args.check:
        check()
        return 0
    talk(MESSAGES, session=args.session)
    check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
