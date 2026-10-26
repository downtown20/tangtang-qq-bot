"""工具接入记忆访问策略的闸门（2026-10-16，T2.4 补的窟窿）。

由来：加 `get_offline_summary` 时漏登记进 `MEMORY_ACCESS_TOOLS`，于是 `memory_access` 为 None，
`args` 不会被绑定 `group_id`——工具**静默**拿不到本群数据（返回「没有记录」而不是报错）。
这次靠人肉发现。三条闸门把这个类别钉死：

1. 执行器里凡读 `args["group_id"]`（=依赖策略绑定）的工具，必须在 `MEMORY_ACCESS_TOOLS` 里；
2. 策略表里的每个名字都必须是**真实注册**的工具（防表腐烂、防想象出来的能力）；
3. 策略表里的每个工具都必须有执行器分支（防「登记了但没接线」）。

例外必须写在这里并说明**为什么可以不进策略表**——例外是给人看的，不是给脚本看的。"""
from __future__ import annotations

import ast
from pathlib import Path

from agent.memory_access import MEMORY_ACCESS_TOOLS

_SRC = (Path(__file__).resolve().parents[1] / "agent" / "handler.py").read_text(encoding="utf-8")
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

# 出站工具：group_id 是「她要去哪个群说话」的**目标参数**，不是查询作用域。
# 权限走 `_allowed_groups` 白名单（`handler.py` group_say 分支内的显式检查），与记忆访问策略无关。
_EXEMPT_GROUP_ID_TOOLS = {
    "group_say": "出站目标参数 + _allowed_groups 白名单，不读别人的数据",
    "group_say_later": "同 group_say（延迟版本）",
}


def _reads_group_id(body) -> bool:
    """该分支是否读 args["group_id"]（策略绑定后的作用域）。"""
    for sub in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "get" and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "args" and sub.args):
            first = sub.args[0]
            if isinstance(first, ast.Constant) and first.value == "group_id":
                return True
        if (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name)
                and sub.value.id == "args"):
            key = getattr(sub.slice, "value", None)
            if key == "group_id":
                return True
    return False


def _scan(source: str) -> dict:
    """扫描一份源码，返回 {工具 schema 名} / {执行器分支名: 是否读 group_id}。"""
    tree = ast.parse(source)
    schemas: set[str] = set()
    branches: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCS):
            continue
        if node.name == "_build_memory_tools":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for key, value in zip(sub.keys, sub.values):
                        if (isinstance(key, ast.Constant) and key.value == "name"
                                and isinstance(value, ast.Constant)
                                and isinstance(value.value, str)):
                            schemas.add(value.value)
        elif node.name == "_execute_tool":
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                test = sub.test
                if not (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                        and test.left.id == "name" and test.comparators):
                    continue
                cand = test.comparators[0]
                if not (isinstance(cand, ast.Constant) and isinstance(cand.value, str)):
                    continue
                flag = _reads_group_id(sub.body) or (
                    _reads_group_id(sub.orelse) if sub.orelse else False)
                branches[cand.value] = branches.get(cand.value, False) or flag
    return {"schemas": schemas, "branches": branches,
            "group_id_tools": {k for k, v in branches.items() if v}}


_REAL = _scan(_SRC)


def test_scanner_has_positive_control():
    """教训 #30：检查器必须有阳性对照——先证明它抓得住一个**真样本**。"""
    planted = (
        "async def _execute_tool(self, name, args):\n"
        "    if name == \"planted_tool\":\n"
        "        gid = args.get(\"group_id\", \"\")\n"
        "        return gid\n"
        "\n"
        "def _build_memory_tools(self):\n"
        "    tools = []\n"
        "    tools.append({\"function\": {\"name\": \"planted_tool\"}})\n"
        "    return tools\n"
    )
    found = _scan(planted)
    assert "planted_tool" in found["group_id_tools"]
    assert "planted_tool" in found["schemas"]
    # 阴性对照：不读 group_id 的分支不得被误判
    quiet = planted.replace('args.get("group_id", "")', 'args.get("query", "")')
    assert "planted_tool" not in _scan(quiet)["group_id_tools"]


def test_group_id_reading_tools_must_be_in_the_policy_table():
    """漏登记的后果是**静默**拿不到数据（group_id 不会被绑定），所以必须机器拦。"""
    unregistered = _REAL["group_id_tools"] - set(MEMORY_ACCESS_TOOLS) - set(_EXEMPT_GROUP_ID_TOOLS)
    assert not unregistered, (
        "这些工具读 group_id 却不在 MEMORY_ACCESS_TOOLS 里（漏登记会静默拿不到本群数据）："
        f"{sorted(unregistered)}")


def test_policy_entries_must_be_real_registered_tools():
    """反向：策略表里不许出现**不存在**的工具（表腐烂 = 想象出来的能力）。"""
    stale = set(MEMORY_ACCESS_TOOLS) - _REAL["schemas"]
    assert not stale, f"策略表里有没注册的工具：{sorted(stale)}"


def test_policy_entries_must_have_an_executor_branch():
    """第三条：登记了但没接线的工具会「看起来有权限，实际拿不到」。"""
    unwired = set(MEMORY_ACCESS_TOOLS) - set(_REAL["branches"])
    assert not unwired, f"策略表里这些工具没有执行器分支：{sorted(unwired)}"


def test_exemptions_are_still_needed():
    """例外清单也要防腐烂：一旦某个工具不再读 group_id，例外就该删。"""
    orphans = set(_EXEMPT_GROUP_ID_TOOLS) - _REAL["group_id_tools"]
    assert not orphans, f"这些例外已经用不上了，删掉：{sorted(orphans)}"
