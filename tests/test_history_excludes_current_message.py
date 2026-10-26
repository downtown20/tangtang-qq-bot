"""当前这条消息不能同时进「历史 role 数组」和「本轮 user_message」。

## 2026-10-25 发现的缺陷（主人反馈：会客厅里「糖糖以为我说了两遍」）

`handle_group_message` / `handle_private_message` 的调用顺序是：

    add_to_buffer(...)                      # 当前消息立刻进短期缓冲
    ...
    get_recent_context_messages(...)        # 取最近 N 条 —— 把当前消息也取出来了
    ...
    _call_deepseek(system, user_message, history_messages, ...)
        api_messages = [system] + history_messages + [user_message]
                                                  ↑ 当前消息再出现一次

`_call_deepseek` 里 `history_messages` 整段 extend 进 `api_messages`，而
`user_message` 是**同一条**用户消息（`user_text`，只多了背景块前缀）。于是同一条
用户话在请求体里出现两遍。LLM 偶尔会据此说「你说了两遍」。

## 闸门

1. `add_to_buffer` 必须返回该条消息的 `seq`（排除锚点）；
2. `get_recent_context_messages(exclude_seq=...)` 必须排掉那条，且**只**排掉那条；
3. 不传 `exclude_seq` 时行为不变（向后兼容——历史契约不能因为这次修复被改掉）；
4. handler 的群/私聊两个调用点必须真的把 seq 传下去（AST 检查，防删）。
"""

import ast
from collections import deque
from pathlib import Path

import pytest

from agent.memory import MemorySystem

ROOT = Path(__file__).resolve().parent.parent
HANDLER = ROOT / "agent" / "handler.py"


def _bare_memory() -> MemorySystem:
    """只测缓冲逻辑：不建 Store、不碰 DB（两个方法都不碰 store）。"""
    obj = MemorySystem.__new__(MemorySystem)
    obj.short_term = {}
    obj.short_term_size = 50
    obj._msg_seq = 0
    return obj


# ── 1. 锚点 ──────────────────────────────────────────────


def test_add_to_buffer_returns_seq_anchor():
    """必须返回 seq 才能精确排除当前那条——返回 None 就没法精确排除。"""
    m = _bare_memory()
    seq = m.add_to_buffer("_private_u1", "u1", "昵称", "晚上好")
    assert isinstance(seq, int) and seq > 0
    assert m.short_term["_private_u1"][-1]["seq"] == seq


def test_add_to_buffer_seq_is_monotonic():
    """连发两条要拿到不同 seq（否则排除会误伤上一条）。"""
    m = _bare_memory()
    a = m.add_to_buffer("_private_u1", "u1", "昵称", "一")
    b = m.add_to_buffer("_private_u1", "u1", "昵称", "二")
    assert a != b and b > a


# ── 2. 排除当前消息 ──────────────────────────────────────


def test_exclude_seq_drops_current_message_from_private_history():
    """私聊：刚入缓冲的当前消息不能出现在历史里（它由 user_message 单独传）。"""
    m = _bare_memory()
    m.add_to_buffer("_private_u1", "u1", "昵称", "上一句")
    m.add_to_buffer("_private_bot", "bot", "糖糖", "上一句的回复")
    seq = m.add_to_buffer("_private_u1", "u1", "昵称", "这一句")

    history = m.get_recent_context_messages(
        "_private_u1", "bot", limit=15, is_private=True, exclude_seq=seq,
    )
    contents = [item["content"] for item in history]
    assert "这一句" not in contents, "当前消息仍在历史里——会和 user_message 重复"
    assert "上一句" in contents, "只该排掉当前那条，不能误伤历史"


def test_exclude_seq_drops_unrelated_nothing():
    """排除锚点不影响别的条目数量（差一条，而不是差一批）。"""
    m = _bare_memory()
    seqs = [m.add_to_buffer("_private_u1", "u1", "昵称", f"m{i}") for i in range(5)]
    full = m.get_recent_context_messages(
        "_private_u1", "bot", limit=15, is_private=True,
    )
    cut = m.get_recent_context_messages(
        "_private_u1", "bot", limit=15, is_private=True, exclude_seq=seqs[-1],
    )
    assert len(full) - len(cut) == 1


# ── 3. 向后兼容：不传就不排除 ────────────────────────────


def test_default_keeps_current_message():
    """默认行为不变：不传 exclude_seq 时当前消息照旧在历史里。

    这条不是「缺陷」——它是既有契约（有些调用方就是要含当前消息的完整视图）。
    修复只允许走显式排除，**不许**在方法内部无条件砍最后一条。
    """
    m = _bare_memory()
    m.add_to_buffer("_private_u1", "u1", "昵称", "当前这条")
    history = m.get_recent_context_messages(
        "_private_u1", "bot", limit=15, is_private=True,
    )
    assert any(item["content"] == "当前这条" for item in history)


def test_exclude_zero_is_noop():
    """exclude_seq=0（缺省）不能误排别的消息。"""
    m = _bare_memory()
    m.add_to_buffer("_private_u1", "u1", "昵称", "a")
    history = m.get_recent_context_messages(
        "_private_u1", "bot", limit=15, is_private=True, exclude_seq=0,
    )
    assert [item["content"] for item in history] == ["a"]


# ── 4. 调用点闸门 ────────────────────────────────────────


def _func_node(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"handler.py 里找不到 {name}（被重命名了？）")


def _history_calls(node: ast.AST) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get_recent_context_messages"):
            out.append(sub)
    return out


@pytest.mark.parametrize("func_name", ["handle_group_message", "handle_private_message"])
def test_call_site_passes_exclude_seq(func_name):
    """两个入口都必须把当前消息的 seq 传下去——删掉这行就复发。"""
    tree = ast.parse(HANDLER.read_text(encoding="utf-8"))
    calls = _history_calls(_func_node(tree, func_name))
    assert calls, f"{func_name} 里没有 get_recent_context_messages 调用"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "exclude_seq" in kwargs, (
            f"{func_name} 的 get_recent_context_messages 没传 exclude_seq——"
            "当前消息会同时出现在 history 和 user_message 里"
        )


def test_no_second_unconditional_history_source():
    """排除点只能有一处：不允许在 _call_deepseek 里再按内容猜着重删。

    内容级去重（比如 `if history[-1]['content'] in user_message: pop`）是脆的：
    user_message 带背景块前缀、图片占位符会被中性化，两者并不总相等。
    单一判定点 = 哪个 seq 是当前消息，只由 add_to_buffer 的返回值说了算。
    """
    src = HANDLER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = _func_node(tree, "_call_deepseek")
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"pop", "remove"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == -1:
                        raise AssertionError(
                            "_call_deepseek 里出现了 history 尾部内容级删除——"
                            "请用 exclude_seq 在构造侧排除"
                        )
