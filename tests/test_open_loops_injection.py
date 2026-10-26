# -*- coding: utf-8 -*-
"""闸门：未闭合事件的**读侧**（L3 注入 / L4 收尾工具 / L5 过期清理）。

方案：`docs/开发规划/未闭合事件_技术方案_20261024.md` §2.3–§2.5。
写侧（L2）由 `tests/test_open_loop_live_path.py` 钉住；这里钉「写进去的东西有没有
被看见、被收尾、被清理」。

每一组测试都对应一种**真实伤害**：

- 注入块缺 id → 她调不了收尾工具，同一件事永远浮上来（骚扰机器）；
- 注入块不带「问过几次」 → 她不知道自己已经问过，重复追问；
- 渲染时顺手回写 `asked_count` → 「被看见」被记成「问过了」（第 11 轮写放大同族）；
- 工具不校验归属 → 群里/私聊里可以改写别人的悬事；
- 群里也注入 → 把甲的悬事说给全群（错配影响面全场）；
- 过期不清理 → 三年前的事永远占着 3 个注入位。
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.handler as handler_module
import agent.protocols as protocols
from agent.handler import MessageHandler

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════════
# 1. 渲染：只给事实 + 带 id + 带「问过几次」
# ══════════════════════════════════════════════════════════════════
class TestRender:
    def test_empty_rows_render_nothing(self):
        assert protocols.render_open_loops([], "2026-10-25") == ""

    def test_line_carries_id_topic_and_when(self):
        text = protocols.render_open_loops(
            [{"id": 12, "topic": "明天要去医院复查",
              "expected_at": "2026-10-26", "asked_count": 0}],
            "2026-10-25")
        assert "#12" in text, "没有 #id 她就调不了 resolve_open_loop"
        assert "明天要去医院复查" in text
        assert "2026-10-26" in text
        assert "还有 1 天" in text
        assert "还没问过" in text

    def test_overdue_is_reported_as_days_past(self):
        text = protocols.render_open_loops(
            [{"id": 3, "topic": "该交的作业", "expected_at": "2026-10-20",
              "asked_count": 1}], "2026-10-25")
        assert "已过 5 天" in text
        assert "已问过 1 次" in text

    def test_due_today(self):
        text = protocols.render_open_loops(
            [{"id": 4, "topic": "面试", "expected_at": "2026-10-25",
              "asked_count": 0}], "2026-10-25")
        assert "就是今天" in text

    def test_no_time_is_stated_as_such(self):
        text = protocols.render_open_loops(
            [{"id": 5, "topic": "想跟老王和好", "expected_at": "",
              "asked_count": 0}], "2026-10-25")
        assert "没说时间" in text

    def test_broken_date_does_not_crash(self):
        text = protocols.render_open_loops(
            [{"id": 6, "topic": "某事", "expected_at": "不是日期",
              "asked_count": 0}], "2026-10-25")
        assert "#6" in text and "某事" in text

    def test_blank_topic_is_skipped(self):
        assert protocols.render_open_loops(
            [{"id": 7, "topic": "  ", "expected_at": ""}], "2026-10-25") == ""

    def test_render_does_not_mutate_rows(self):
        """渲染是纯函数：不许顺手记「问过了」（第 11 轮写放大的同族错误）。"""
        row = {"id": 8, "topic": "某事", "expected_at": "", "asked_count": 0}
        protocols.render_open_loops([row], "2026-10-25")
        assert row["asked_count"] == 0


class TestPriorityRegistration:
    def test_label_matches_constant(self):
        assert protocols.OPEN_LOOP_LABEL == "悬着的事"

    def test_priority_registered(self):
        assert protocols.BACKGROUND_PRIORITY["悬着的事"] == 5

    def test_not_registered_as_recoverable(self):
        """没有「取回悬事」的工具，就不许登记取回路径（教训 #39）。"""
        assert "悬着的事" not in protocols._BG_RECOVERY_TOOLS


# ══════════════════════════════════════════════════════════════════
# 2. 注入上下文：开关 / 上限 / fail-open
# ══════════════════════════════════════════════════════════════════
class _FakeStore:
    def __init__(self, rows=None, boom=False):
        self.rows = rows if rows is not None else []
        self.boom = boom
        self.calls = []

    def list_open_loops(self, qq_id, **kwargs):
        self.calls.append((qq_id, kwargs))
        if self.boom:
            raise RuntimeError("db down")
        return self.rows


def _fake_self(store, *, enabled=True):
    async def _run_store_io(operation, func, *args, **kwargs):
        return func(*args, **kwargs)

    return SimpleNamespace(
        config={"memory": {"open_loops_injection": enabled}},
        memory=SimpleNamespace(store=store),
        _run_store_io=_run_store_io,
    )


class TestOpenLoopsContext:
    @pytest.mark.asyncio
    async def test_disabled_by_config(self):
        store = _FakeStore(rows=[{"id": 1, "topic": "某事", "expected_at": ""}])
        text = await MessageHandler._get_open_loops_context(
            _fake_self(store, enabled=False), "1001")
        assert text == ""
        assert store.calls == [], "开关关着时不该查库"

    @pytest.mark.asyncio
    async def test_enabled_returns_rendered_block(self):
        store = _FakeStore(rows=[{"id": 1, "topic": "明天面试",
                                  "expected_at": "2026-10-26",
                                  "asked_count": 0}])
        text = await MessageHandler._get_open_loops_context(
            _fake_self(store), "1001")
        assert "#1" in text and "明天面试" in text
        assert store.calls[0][0] == "1001"

    @pytest.mark.asyncio
    async def test_limit_is_three(self):
        """注入位只有 3 个——不是渲染时截断，是查询时就限。"""
        store = _FakeStore(rows=[])
        await MessageHandler._get_open_loops_context(_fake_self(store), "1001")
        assert store.calls[0][1].get("limit") == 3

    @pytest.mark.asyncio
    async def test_store_failure_is_fail_open(self):
        """查库失败绝不影响回复（观察数据不是事实契约）。"""
        store = _FakeStore(boom=True)
        text = await MessageHandler._get_open_loops_context(
            _fake_self(store), "1001")
        assert text == ""

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty(self):
        text = await MessageHandler._get_open_loops_context(
            _fake_self(_FakeStore(rows=[])), "1001")
        assert text == ""


# ══════════════════════════════════════════════════════════════════
# 3. 接线位置：私聊注入、群聊不注入
# ══════════════════════════════════════════════════════════════════
def _bg_append_labels(func_name: str) -> set[str]:
    tree = ast.parse((PROJECT_ROOT / "agent" / "handler.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    node = funcs[func_name]
    labels = set()
    for call in ast.walk(node):
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "append" and call.args):
            continue
        first = call.args[0]
        if isinstance(first, ast.Tuple) and first.elts:
            head = first.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                labels.add(head.value)
    return labels


def test_private_path_injects_open_loops():
    assert "悬着的事" in _bg_append_labels("handle_private_message")


def test_group_path_does_not_inject_open_loops():
    """第一版只开私聊：群里把甲的悬事说给全群，错配影响面是全场。"""
    assert "悬着的事" not in _bg_append_labels("handle_group_message")


# ══════════════════════════════════════════════════════════════════
# 4. 工具 schema：私聊有、群聊没有
# ══════════════════════════════════════════════════════════════════
def _tool_names(handler_like, *, group_id=""):
    tools = MessageHandler._build_memory_tools(
        handler_like, "1001", group_id=group_id, has_image=False)
    return {t["function"]["name"] for t in tools}


class _ToolSelf:
    def __getattr__(self, item):
        # 工具构建只读少量属性；缺省给空值，避免为了测一个 schema 造整个 handler。
        return "" if item.endswith("role") else None


def test_tool_available_in_private():
    assert "resolve_open_loop" in _tool_names(_ToolSelf())


def test_tool_absent_in_group():
    assert "resolve_open_loop" not in _tool_names(_ToolSelf(), group_id="123456")


# ══════════════════════════════════════════════════════════════════
# 5. 收尾工具的行为（真跑一次 store）
# ══════════════════════════════════════════════════════════════════
class _ExecSelf:
    def __init__(self, store):
        self.memory = SimpleNamespace(store=store)
        self.metrics = SimpleNamespace(incr=lambda *a, **k: None)

    async def _run_store_io(self, operation, func, *args, **kwargs):
        return func(*args, **kwargs)


async def _execute(store, name, args, *, current_user="7001"):
    return await MessageHandler._execute_tool(
        _ExecSelf(store), name, args, scope_id="_private_" + current_user,
        current_user=current_user)


class TestResolveTool:
    @pytest.mark.asyncio
    async def test_asked_increments_count(self, store):
        loop_id = store.add_open_loop("7001", "明天要去面试")
        result = await _execute(store, "resolve_open_loop",
                                {"loop_id": loop_id, "outcome": "asked"})
        assert "问过" in result
        rows = store.list_open_loops("7001")
        assert len(rows) == 1 and rows[0]["asked_count"] == 1

    @pytest.mark.asyncio
    async def test_second_asked_hides_from_listing(self, store):
        loop_id = store.add_open_loop("7001", "明天要去面试")
        await _execute(store, "resolve_open_loop",
                       {"loop_id": loop_id, "outcome": "asked"})
        await _execute(store, "resolve_open_loop",
                       {"loop_id": loop_id, "outcome": "asked"})
        assert store.list_open_loops("7001") == []

    @pytest.mark.asyncio
    async def test_cross_person_refused(self, store):
        """甲的悬事不许被乙收尾——越权改写别人的事。</summary>"""
        loop_id = store.add_open_loop("7002", "乙的私事")
        result = await _execute(store, "resolve_open_loop",
                                {"loop_id": loop_id, "outcome": "resolved"},
                                current_user="7001")
        assert "不归你" in result
        assert store.list_open_loops("7002")[0]["asked_count"] == 0

    @pytest.mark.asyncio
    async def test_terminal_is_irreversible(self, store):
        loop_id = store.add_open_loop("7001", "某件已结的事")
        first = await _execute(store, "resolve_open_loop",
                               {"loop_id": loop_id, "outcome": "resolved",
                                "note": "知道结果了"})
        assert "有着落" in first
        second = await _execute(store, "resolve_open_loop",
                                {"loop_id": loop_id, "outcome": "asked"})
        assert "已经收过尾" in second or "不归你" in second
        assert store.list_open_loops("7001") == []

    @pytest.mark.asyncio
    async def test_bad_outcome_is_rejected_without_side_effect(self, store):
        loop_id = store.add_open_loop("7001", "某件事")
        result = await _execute(store, "resolve_open_loop",
                                {"loop_id": loop_id, "outcome": "随便"})
        assert "asked / resolved / dropped" in result
        assert store.list_open_loops("7001")[0]["asked_count"] == 0


# ══════════════════════════════════════════════════════════════════
# 6. L5：过期清理必须真的被调度
# ══════════════════════════════════════════════════════════════════
def test_expire_is_scheduled_in_autonomy():
    """`expire_open_loops` 有实现但没人调 = 悬事永不过期（半条命，教训 #12/#14）。

    注意这里数的是**属性引用**（`self.memory.store.expire_open_loops` 作为参数
    传给 `_run_store_io`），不是调用——第一次我把判据写成 `Call`，结果真的接线了
    也报 0（「工具报 0 是关于工具的事实」，教训 #36 的同族）。
    """
    tree = ast.parse((PROJECT_ROOT / "agent" / "handler_autonomy.py")
                     .read_text(encoding="utf-8"))
    refs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "expire_open_loops"]
    assert len(refs) >= 2, (
        f"过期清理只在 {len(refs)} 处被引用；启动清理与每日汇总都要有")
