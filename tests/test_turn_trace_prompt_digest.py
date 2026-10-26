"""2026-10-25 第三轮自查：补上「她这一轮看到了什么」的留档

## 为什么这是地基

`turn_traces` 原来只记两样东西：`reasoning_text`（她怎么想的）和
`tool_steps`（她查了什么）。**没有「她输入了什么」**。

后果在前两轮里各撞了一次：

- 第 1 轮：她在 `turn_traces#67` 的 reasoning 里写「这个日期可能是我编的？
  我没有查时间」——我只能靠本地复刻 `build_system_prompt()` + 读
  `context_builder` 源码推断出「时间块被截断吃掉了」。**如果有 prompt 留档，
  这一条一眼就能看出来。**
- 第 2 轮：要证明「44% 的说明书被砍」，同样只能本地复刻渲染。

所以补 `prompt_digest`：存**结构**不存全文（system prompt 的段标题、user
message 里的 `<背景·X>` 标签、历史条数与字符数）。每轮几百字符，
而 `memory.db` 已经 900MB，全存会把库撑爆。

顺带修了同一个写入点的 `prompt_chars` 字段——它从建表起就是 0
（`_persist_turn_trace` 根本没传这个参数），等于刻度和刻度盘都没接线。
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest


# ═══════════════════════════════════════════════════════════════════
# 摘要生成
# ═══════════════════════════════════════════════════════════════════

class TestSummarizePrompt:
    def _sys(self):
        return (
            "## 你是谁\n18岁猫娘少女。\n\n"
            "## 性格\n刀子嘴豆腐心。\n\n"
            "## 关系\n亲密度 0/100 — 路人。\n\n"
            "## 此刻\n现在是2026年10月25日 星期日 深夜1点09分"
        )

    def test_extracts_section_titles(self):
        """段标题：第 2 轮那种「说明书被砍」一眼能看出来的判据"""
        from agent.handler import _summarize_prompt
        d = _summarize_prompt(self._sys(), [], "在吗")
        assert d["system_sections"] == ["你是谁", "性格", "关系", "此刻"]

    def test_missing_section_is_visible(self):
        """反向：被截断掉的段不会出现在摘要里——这正是它的用途"""
        from agent.handler import _summarize_prompt
        truncated = "## 你是谁\n18岁猫娘少女。"
        d = _summarize_prompt(truncated, [], "在吗")
        assert "关系" not in d["system_sections"]
        assert "此刻" not in d["system_sections"]

    def test_extracts_background_labels(self):
        """背景块标签：这一轮到底注入了哪些材料"""
        from agent.handler import _summarize_prompt
        from agent import protocols
        user_msg = (
            protocols.bg("记忆", "她喜欢喝美式") + "\n\n"
            + protocols.bg("图片", "一只猫") + "\n\n我们聊聊吧"
        )
        d = _summarize_prompt("## 你是谁\nx", [], user_msg)
        assert d["user_bg_labels"] == ["图片", "记忆"]

    def test_counts_history(self):
        """历史条数与字符数——第 1 轮「同一条消息被送两次」靠它能直接看见"""
        from agent.handler import _summarize_prompt
        hist = [
            {"role": "assistant", "content": "在的"},
            {"role": "user", "content": "你好啊"},
            {"role": "system", "content": ""},
        ]
        d = _summarize_prompt("## 你是谁\nx", hist, "在吗")
        assert d["history_count"] == 3
        assert d["history_chars"] == len("在的") + len("你好啊")

    def test_total_chars_matches_parts(self):
        from agent.handler import _summarize_prompt
        d = _summarize_prompt("## 你是谁\nabc", [{"content": "xx"}], "在吗")
        assert d["total_chars"] == d["system_chars"] + d["user_chars"] + d["history_chars"]

    def test_empty_inputs_do_not_crash(self):
        from agent.handler import _summarize_prompt
        d = _summarize_prompt("", None, "")
        assert d["system_sections"] == [] and d["total_chars"] == 0

    def test_non_dict_history_entries_ignored(self):
        from agent.handler import _summarize_prompt
        d = _summarize_prompt("## 你是谁\nx", ["不是字典", None, {"content": "ok"}], "在吗")
        assert d["history_count"] == 1

    def test_counts_memory_marks_by_line(self):
        """注入标记的存活计数：✏️ / 💭~ 各算各的，契约行不算。

        2026-10-25 第 8 轮：`user_bg_labels` 只能证明「记忆块注入了」，证明不了
        「带标记的那几行活到了最终 prompt」——而背景预算会裁块。
        """
        from agent.handler import _summarize_prompt
        from agent import protocols
        user_msg = (
            protocols.bg("记忆",
                         "📋 成展(ID:0083)\n"
                         f"  {protocols.CORRECTED_MARK} 成展是大学大二学生\n"
                         "  💭 成展在杭州上班\n"
                         f"  {protocols.UNCERTAIN_MARK} 某件模糊的事\n"
                         f"  {protocols.CORRECTED_MEMORY_CONTRACT}")
            + "\n\n你现在是高中还是大学"
        )
        d = _summarize_prompt("## 你是谁\nx", [], user_msg)
        assert d["memory_marks"] == {"corrected": 1, "uncertain": 1}, (
            "契约行本身含 ✏️，不许被算成一条记忆")

    def test_memory_marks_zero_when_no_marks(self):
        """阴性对照：没有标记时必须都是 0——否则「全都算 1」也会绿（教训 #37）。"""
        from agent.handler import _summarize_prompt
        from agent import protocols
        user_msg = protocols.bg("记忆", "📋 小明\n  💭 小明养了一只猫") + "\n\n在吗"
        d = _summarize_prompt("## 你是谁\nx", [], user_msg)
        assert d["memory_marks"] == {"corrected": 0, "uncertain": 0}

    def test_memory_marks_reflect_budget_truncation(self):
        """预算把标记行裁掉 → 计数掉到 0。

        这是这个字段存在的**唯一理由**：上游 `memory_block()` 里标记在，不代表
        最终 prompt 里还在（第 6 轮实测记忆块 77% 回合被整块淘汰）。
        """
        from agent.handler import _summarize_prompt
        from agent import protocols
        full = protocols.bg("记忆",
                            f"  {protocols.CORRECTED_MARK} 成展是大学大二学生\n"
                            "  💭 成展在杭州上班")
        truncated = protocols.bg("记忆", "  💭 成展在杭州上班")
        assert _summarize_prompt("", [], full)["memory_marks"]["corrected"] == 1
        assert _summarize_prompt("", [], truncated)["memory_marks"]["corrected"] == 0


# ═══════════════════════════════════════════════════════════════════
# 存储与迁移
# ═══════════════════════════════════════════════════════════════════

class TestStoreRoundTrip:
    def test_record_and_read_back_digest(self, store):
        """写进去能原样读出来（含 prompt_chars）"""
        digest = {"system_chars": 3005, "system_sections": ["你是谁", "此刻"],
                  "user_chars": 40, "user_bg_labels": ["记忆"],
                  "history_count": 12, "history_chars": 900, "total_chars": 3945}
        store.record_turn_trace(
            run_id="run-test-1", event_key="v2:private:1:2:3:4",
            scope_id="private:1", model="m",
            reasoning_text="想一想", tool_steps=[{"name": "get_time"}],
            prompt_chars=3945, duration_ms=1234, prompt_digest=digest,
        )
        row = store.get_turn_trace("run-test-1")
        assert row is not None
        assert row["prompt_chars"] == 3945
        assert row["prompt_digest"] == digest

    def test_old_rows_without_digest_read_as_empty_dict(self, store):
        """老行没有摘要 → 返回空 dict，不抛异常（观察数据必须容错）"""
        store.record_turn_trace(run_id="run-test-2", event_key="v2:x",
                                scope_id="s", model="m", reasoning_text="r")
        row = store.get_turn_trace("run-test-2")
        assert row["prompt_digest"] == {}

    def test_query_returns_digest_too(self, store):
        store.record_turn_trace(run_id="run-test-3", prompt_digest={"total_chars": 7})
        rows = store.query_turn_traces(limit=5)
        assert rows and rows[0]["prompt_digest"] == {"total_chars": 7}

    def test_oversized_digest_is_truncated(self, store):
        """摘要本身也要有上限——否则等于变相存全文"""
        huge = {"system_sections": ["很长"] * 5000, "blob": "x" * 20000}
        store.record_turn_trace(run_id="run-test-4", prompt_digest=huge)
        row = store.get_turn_trace("run-test-4")
        raw = json.dumps(row["prompt_digest"], ensure_ascii=False)
        assert len(raw) <= store.TRACE_DIGEST_LIMIT


class TestSchemaMigration:
    def test_old_table_gets_column_on_init(self, tmp_path):
        """老库（没有 prompt_digest 列）在 Store 初始化时被 ALTER 补上"""
        from agent.store import Store
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE turn_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                event_key TEXT NOT NULL DEFAULT '',
                scope_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                reasoning_text TEXT NOT NULL DEFAULT '',
                tool_steps_json TEXT NOT NULL DEFAULT '[]',
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        s = Store(str(db))          # 触发迁移
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turn_traces)")}
        conn.close()
        assert "prompt_digest" in cols
        # 迁移后老库也能正常写入
        s.record_turn_trace(run_id="run-old-db", prompt_digest={"total_chars": 5})
        assert s.get_turn_trace("run-old-db")["prompt_digest"] == {"total_chars": 5}


# ═══════════════════════════════════════════════════════════════════
# 落库链路
# ═══════════════════════════════════════════════════════════════════

class _FakeRun:
    run_id = "run-fake"
    event_key = "v2:private:1:2:3:4"
    scope_id = "private:1"
    model = "deepseek-v4.1-flash"
    started_at = "2026-10-25 01:00:00"
    finished_at = "2026-10-25 01:00:02"


class _CapturingStore:
    def __init__(self, accept_digest=True):
        self.accept_digest = accept_digest
        self.calls = []

    def record_turn_trace(self, **kwargs):
        if not self.accept_digest and "prompt_digest" in kwargs:
            raise TypeError("unexpected keyword argument 'prompt_digest'")
        self.calls.append(kwargs)
        return {"id": 1}


class TestPersistTurnTrace:
    def test_passes_digest_and_prompt_chars(self):
        from agent.handler import _persist_turn_trace
        st = _CapturingStore()
        digest = {"total_chars": 4321, "system_sections": ["此刻"]}
        _persist_turn_trace(None, _FakeRun(), st,
                            {"reasoning": ["想了"], "tools": [], "prompt": digest})
        assert st.calls, "没写痕迹"
        call = st.calls[0]
        assert call["prompt_digest"] == digest
        assert call["prompt_chars"] == 4321, "prompt_chars 必须来自摘要——它此前恒为 0"

    def test_degrades_when_writer_lacks_digest_param(self):
        """老 store/测试替身没有 prompt_digest 参数时：丢字段，别丢整条痕迹"""
        from agent.handler import _persist_turn_trace
        st = _CapturingStore(accept_digest=False)
        _persist_turn_trace(None, _FakeRun(), st,
                            {"reasoning": ["想了"], "tools": [], "prompt": {"total_chars": 9}})
        assert st.calls, "降级路径把整条痕迹丢了"
        assert "prompt_digest" not in st.calls[0]

    def test_trace_without_prompt_still_writes(self):
        """没开摘要的回合（子调用/旧路径）照常写，不因缺字段失败"""
        from agent.handler import _persist_turn_trace
        st = _CapturingStore()
        _persist_turn_trace(None, _FakeRun(), st, {"reasoning": [], "tools": []})
        assert st.calls and "prompt_digest" not in st.calls[0]

    def test_none_trace_writes_nothing(self):
        from agent.handler import _persist_turn_trace
        st = _CapturingStore()
        _persist_turn_trace(None, _FakeRun(), st, None)
        assert st.calls == []


class TestTraceContainerHasPromptSlot:
    def test_begin_turn_trace_has_prompt_key(self):
        """容器必须有 prompt 槽——否则 _call_deepseek 里写不进去（静默失效）"""
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        trace = h._begin_turn_trace()
        assert "prompt" in trace

    def test_call_deepseek_writes_prompt_digest(self):
        """源码闸门：api_messages 拼完后必须把摘要写进 trace

        `_call_deepseek` 是 async + 要网络，没法直接跑；用 AST 断言它在
        `api_messages.append(... user_message)` 之后调用了 `_summarize_prompt`
        并且赋值给 `trace["prompt"]`。
        """
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent / "agent" / "handler.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call_deepseek":
                fn = node
                break
        assert fn is not None, "找不到 _call_deepseek"
        called = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_summarize_prompt":
                called = True
        assert called, (
            "_call_deepseek 没有再生成 prompt 摘要——turn_traces 会退回「只记她"
            "怎么想、不记她看到了什么」，前两轮那种只能本地复刻 prompt 的取证困境会回来"
        )
