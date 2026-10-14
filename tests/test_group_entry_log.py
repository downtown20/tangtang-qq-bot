# -*- coding: utf-8 -*-
"""T40 回归：群消息入口必须留痕（2026-10-14）。

现场：生产实测当天 160 条群消息，日志里**只有 SnowLuma 的接收行、之后零痕迹**
—— `handle_group_message` 没有入口日志，于是**分不清
「看到了并选择沉默」与「根本没进来」**。排查时为此耗了约 8 个探针。

两条不变量：
  1. 入口处必须有日志；
  2. 它必须在**第一个 return 之前** —— 否则被早退分支（自己发的/黑名单/去重重投）
     过滤掉的消息仍然不可见，「过滤掉」和「没到达」在日志里还是长得一样。
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler


def _src():
    return inspect.getsource(MessageHandler.handle_group_message)


def test_entry_log_exists():
    """入口必须有那条 `📥 [群:...]` 留痕。"""
    assert "📥 [群:" in _src(), (
        "handle_group_message 没有入口日志 —— 群消息到了没到又只能靠猜")


def test_entry_log_precedes_first_return():
    """**关键**：留痕必须在第一个 return 之前，否则早退分支仍然不可见。"""
    src = _src()
    log_at = src.find("📥 [群:")
    ret_at = src.find("return")
    assert log_at != -1, "没有入口日志"
    assert ret_at != -1, "函数里没有 return？源码变了，先核对"
    assert log_at < ret_at, (
        "入口日志排在第一个 return 之后 —— 被早退过滤的消息依旧不可见")


def test_entry_log_carries_group_and_user():
    """留痕要能回答「哪个群的谁」——否则排查时还得再猜一轮。"""
    src = _src()
    line = next(l for l in src.splitlines() if "📥 [群:" in l)
    assert "group_id" in line, line
    assert "user_id" in line, line
    assert "text[:" in line, "必须截断正文，不能把整条消息打进日志"
