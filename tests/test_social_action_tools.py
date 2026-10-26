"""T2.6（2026-10-16）社交小动作（点赞/戳一戳）的决策权归她。

为什么有这份闸门：
  旧实现用 random.random() 替她做社交判断——`_natural_like` 被@80% / 亲密度≥60 40% /
  熟人 5% 决定给谁点赞，`_try_proactive_poke` 50% 决定戳谁。骰子做的是「社交动作要不要
  发生、对谁发生」，属她的决策域（2026-08-16 范式转换同族：内容判断归 LLM；教训 #9 同族）。
  现在两个动作由她在回合内调用 like_speaker / poke_speaker 决定；系统只保留
  ① 目标绑定（= 本回合说话人，schema 里没有 target 参数，她不能指定任意第三人）
  ② 平台风控（冷却 / 日上限 / 亲密度下限——客观档，不是她的心情）
  ③ 状态回执（不再无条件报「已完成」，ADR-002 同族）。
"""

import ast
import asyncio
from pathlib import Path

from agent.handler import MessageHandler

HANDLER_SRC = (Path(__file__).resolve().parents[1] / "agent" / "handler.py").read_text(
    encoding="utf-8"
)


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════
# A. 源码闸门：骰子不许复活（带阳性对照——教训 #30）
# ═══════════════════════════════════════════════════════

_DICE_ATTRS = {"random", "randint", "choice", "shuffle", "sample", "uniform"}


def _dice_sites(src: str) -> list[str]:
    """扫出「替她掷骰子做社交判断」的痕迹。合成样本同样会被扫出来。

    只看 **AST 节点**，不看文本——注释与 docstring 里提到 random 是说明，不是骰子
    （第一版写成文本匹配，结果被自己的 docstring 顶红：这就是阳性对照的价值）。
    """
    found: list[str] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_natural_like":
                found.append("def _natural_like")
            if node.name not in ("_try_proactive_poke", "_try_like"):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    if any(a.name == "random" for a in sub.names):
                        found.append(f"{node.name} 里 import random")
                elif isinstance(sub, ast.ImportFrom) and sub.module == "random":
                    found.append(f"{node.name} 里 from random import")
                elif (isinstance(sub, ast.Attribute)
                      and sub.attr in _DICE_ATTRS):
                    found.append(f"{node.name} 里用了 .{sub.attr}()")
                elif isinstance(sub, ast.Name) and sub.id in ("random", "_random"):
                    found.append(f"{node.name} 里引用了 {sub.id}")
    if 'name=f"natural_like:' in src:
        found.append("系统侧自动点赞调度点")
    if 'name=f"proactive_poke:' in src:
        found.append("系统侧自动戳的调度点")
    return found


def test_no_dice_decides_social_actions():
    assert _dice_sites(HANDLER_SRC) == []


def test_dice_scanner_has_positive_control():
    """教训 #30：种一个真样本进去，看它报不报——只测「跑起来没报错」等于没测。"""
    planted = (
        "import random\n"
        "class X:\n"
        "    async def _try_proactive_poke(self):\n"
        "        if random.random() > 0.5:\n"
        "            return\n"
        "    async def _natural_like(self):\n"
        "        pass\n"
        "x = 1\n"
        'self._safe_task(f(y), name=f"proactive_poke:{u}")\n'
    )
    hits = _dice_sites(planted)
    assert "def _natural_like" in hits
    assert any("_try_proactive_poke" in h for h in hits)
    assert any("调度点" in h for h in hits)


# ═══════════════════════════════════════════════════════
# B. 工具注册：无 target 参数 + 有执行器分支
# ═══════════════════════════════════════════════════════

def _appended_tool_dicts(src: str, func_name: str) -> list[str]:
    """取某个方法体里 `tools.append({...})` 的源码片段。"""
    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "append"
                        and sub.args):
                    seg = ast.get_source_segment(src, sub.args[0]) or ""
                    out.append(seg)
    return out


def test_social_tools_are_registered_without_target_arg():
    segs = [s for s in _appended_tool_dicts(HANDLER_SRC, "_build_memory_tools")
            if "like_speaker" in s or "poke_speaker" in s]
    assert len(segs) == 2, "like_speaker / poke_speaker 必须都注册"
    for seg in segs:
        # 目标只能是系统绑定的本回合说话人——schema 里**不能**有 target 参数
        assert '"target"' not in seg
        assert '"properties": {}' in seg
        assert '"required": []' in seg


def test_social_tools_have_executor_branches():
    tree = ast.parse(HANDLER_SRC)
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_tool"):
            body = ast.get_source_segment(HANDLER_SRC, node) or ""
            assert 'name in {"like_speaker", "poke_speaker"}' in body
            assert "like_speaker" in body and "poke_speaker" in body
            return
    raise AssertionError("没找到 _execute_tool")


def test_social_tools_are_behavior_class():
    """归 behavior，否则会多一轮 LLM 收尾（见 _HANDLER_METHOD_TYPES 注释）。"""
    tree = ast.parse(HANDLER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_HANDLER_METHOD_TYPES":
                    d = {k.value: v.value for k, v in zip(node.value.keys, node.value.values)}
                    assert d.get("like_speaker") == "behavior"
                    assert d.get("poke_speaker") == "behavior"
                    return
    raise AssertionError("没找到 _HANDLER_METHOD_TYPES")

# ═══════════════════════════════════════════════════════
# C. 执行器：目标绑定 / 私聊不戳 / fail-closed / 事实回执
# ═══════════════════════════════════════════════════════

class _NapcatStub:
    def __init__(self, status="ok"):
        self.calls = []
        self._status = status

    async def _call_api(self, api, params):
        self.calls.append((api, params))
        return {"status": self._status}


def _stub_handler(like_status="ok", poke_status="ok"):
    """薄替身：只提供这两个工具真正会碰到的字段。"""
    h = object.__new__(MessageHandler)
    h.napcat = _NapcatStub()
    h.calls = {"like": [], "poke": []}

    async def _like(user_id, message_id, reason=""):
        h.calls["like"].append((user_id, message_id, reason))
        return like_status

    async def _poke(user_id, group_id, intimacy=None, nickname=""):
        h.calls["poke"].append((user_id, group_id))
        return poke_status

    h._try_like = _like
    h._try_proactive_poke = _poke
    return h


def _exec(h, name, scope_id, current_user):
    return _run(MessageHandler._execute_tool(
        h, name, {}, scope_id, current_user, {"respond": True}))


def test_like_speaker_binds_current_speaker():
    h = _stub_handler()
    out = _exec(h, "like_speaker", "g1", "1001")
    assert h.calls["like"] == [("1001", 0, "她说要点赞")]
    assert "点了个赞" in out


def test_social_tools_fail_closed_without_a_speaker():
    """没有具体发言人（自治/空上下文）时不许凭空挑一个人来点赞。"""
    h = _stub_handler()
    out = _exec(h, "like_speaker", "g1", "")
    assert "没有具体的人" in out
    assert h.calls["like"] == []


def test_poke_speaker_is_refused_in_private():
    h = _stub_handler()
    out = _exec(h, "poke_speaker", "_private_1001", "1001")
    assert "私聊里戳不了" in out
    assert h.calls["poke"] == []


def test_poke_speaker_passes_objective_risk_fact_back():
    h = _stub_handler(poke_status="too_cold")
    out = _exec(h, "poke_speaker", "12345", "1001")
    assert h.calls["poke"] == [("1001", "12345")]
    assert "还不够熟" in out and "戳了" not in out.replace("戳不了", "")


def test_like_speaker_reports_blocked_instead_of_success():
    h = _stub_handler(like_status="skipped")
    out = _exec(h, "like_speaker", "g1", "1001")
    assert "没赞出去" in out
    assert "点了个赞" not in out


# ═══════════════════════════════════════════════════════
# D. 风控仍是客观档：冷却 / 日上限 / 亲密度下限 / 私聊无此动作
# ═══════════════════════════════════════════════════════

def _poke_handler():
    h = object.__new__(MessageHandler)
    h.napcat = _NapcatStub()
    h._poke_tracker = {}
    h._save_state_kv = lambda k, v: True
    return h


def _poke(h, group_id="12345", intimacy=None, nickname=""):
    return _run(MessageHandler._try_proactive_poke(
        h, "1001", group_id, intimacy, nickname))


def test_poke_is_deterministic_now_that_the_dice_is_gone():
    """旧实现 `if random.random() > 0.50: return`——删掉后 20 次必须全发出去
    （骰子还在时「20 次全成功」的概率是 2^-20 ≈ 1e-6，这个断言会红）。"""
    for _ in range(20):
        h = _poke_handler()
        assert _poke(h, intimacy=90, nickname="小明") == "ok"
        assert h.napcat.calls[0][0] == "group_poke"
        assert h.napcat.calls[0][1]["group_id"] == 12345


def test_poke_min_intimacy_is_still_a_gate():
    h = _poke_handler()
    assert _poke(h, intimacy=10, nickname="小明") == "too_cold"
    assert h.napcat.calls == []


def test_poke_cooldown_is_still_a_gate():
    import time as _t
    h = _poke_handler()
    h._poke_tracker = {"1001": {"count": 1, "last_time": _t.time(),
                                "date": _t.strftime("%Y-%m-%d")}}
    assert _poke(h, intimacy=90, nickname="小明") == "cooldown"
    assert h.napcat.calls == []


def test_poke_private_has_no_channel():
    h = _poke_handler()
    assert _poke(h, group_id="", intimacy=90, nickname="小明") == "no_channel"
    assert h.napcat.calls == []


def test_like_returns_status_instead_of_none():
    h = object.__new__(MessageHandler)
    h.napcat = _NapcatStub()
    h._like_tracker = {}
    h._like_last_any_time = 0.0
    h._save_state_kv = lambda k, v: True
    assert _run(MessageHandler._try_like(h, "1001", 0, "测试")) == "ok"
    # 刚赞完同人冷却立刻生效：第二次必须**如实**报 skipped（旧实现静默 return None，
    # 调用方只能假装成功）
    assert _run(MessageHandler._try_like(h, "1001", 0, "测试")) == "skipped"

