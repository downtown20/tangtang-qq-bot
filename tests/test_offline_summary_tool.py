"""T2.4：离线摘要从「关键词门控自动注入」改成「她自己调工具取回」（2026-10-16）。

三条契约：
1. 系统侧的关键词门控必须消失（11 条短语 + 疑问词检测不得复活）；
2. 工具 get_offline_summary 必须注册进记忆工具目录，且有执行器；
3. 执行器按当前会话作用域取材料（群聊取本群摘要；私聊无群作用域），没摘要时给事实性空提示。"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from agent.handler import MessageHandler

_ROOT = Path(__file__).resolve().parents[1]
_HANDLER_SRC = (_ROOT / "agent" / "handler.py").read_text(encoding="utf-8")

# 退役的关键词门控短语（原文来自 _check_catch_up_query，已删除）
_RETIRED_PHRASES = [
    "不在的时候聊了什么", "糖糖不在的时候", "你不在的时候",
    "掉线的时候", "刚才不在", "不在线的时候",
    "你刚才去哪了", "你没看到", "你错过了",
    "你不在时", "不在的这段时间",
]


def _run(coro):
    return asyncio.run(coro)


def _fake_handler(store, summary, with_catch_up=True):
    """替身：只要 _execute_tool 前置所需的字段 + 一个假 catch_up（同 test_history_query 的写法）。"""
    fields = {
        "memory": type("M", (), {"store": store})(),
        "owner_qq": "owner-1", "bot_qq": "10000",
        "_allowed_groups": {"g1"},
    }
    if with_catch_up:
        fields["catch_up"] = type("C", (), {
            "get_summary": lambda self, gid: summary if gid == "g1" else None,
        })()
    handler = type("H", (), fields)()
    handler._run_store_io = MessageHandler._run_store_io.__get__(handler)
    return handler


def _call(handler, scope_id="g1"):
    return _run(MessageHandler._execute_tool(
        handler, "get_offline_summary", {}, scope_id, "1001", {"respond": True}))


def test_keyword_gate_is_gone():
    """系统不许再替她判断「这句话是在问补读」——判断归她（单脑原则）。"""
    assert "_check_catch_up_query" not in _HANDLER_SRC
    for phrase in _RETIRED_PHRASES:
        assert phrase not in _HANDLER_SRC, f"关键词门控短语复活：{phrase}"
    assert '("补读"' not in _HANDLER_SRC, "系统又开始自己预判注入补读块了"


def test_tool_is_registered_in_memory_tools():
    """工具目录是能力的唯一入口——注册漏了，她就永远调不到。"""
    tree = ast.parse(_HANDLER_SRC)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_memory_tools":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for k, v in zip(sub.keys, sub.values):
                        if (isinstance(k, ast.Constant) and k.value == "name"
                                and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                            names.add(v.value)
    assert "get_offline_summary" in names, f"工具没注册进记忆工具目录：{sorted(names)}"


def test_executor_returns_the_summary_as_material(store):
    h = _fake_handler(store, "大家在聊火锅")
    result = _call(h)
    assert "大家在聊火锅" in result
    assert "我不在的时候" in result, "材料抬头丢了——她需要知道这段是什么"


def test_empty_summary_gives_a_factual_hint(store):
    h = _fake_handler(store, None)
    result = _call(h)
    assert "没有离线摘要" in result


def test_without_catch_up_manager_gives_a_factual_hint(store):
    h = _fake_handler(store, None, with_catch_up=False)
    result = _call(h)
    assert "没有离线补读记录" in result


def test_private_scope_has_no_group_summary(store):
    """私聊作用域没有群 id：不许把某个群的离线摘要塞进私聊（跨场景串材料）。
    非主人私聊由记忆访问策略直接拒（fail-closed），主人私聊也拿不到群摘要。"""
    h = _fake_handler(store, "群里的秘密摘要")
    result = _call(h, scope_id="_private_1001")
    assert "群里的秘密摘要" not in result
    assert result.strip(), "拒绝也要给一句可读的说明，不能返回空串"


def test_owner_private_scope_gets_no_group_summary_either(store):
    h = _fake_handler(store, "群里的秘密摘要")
    h.owner_qq = "1001"  # 主人私聊：策略放行，但没有群 id，仍然拿不到群摘要
    result = _call(h, scope_id="_private_1001")
    assert "群里的秘密摘要" not in result
    assert "没有离线补读记录" in result
