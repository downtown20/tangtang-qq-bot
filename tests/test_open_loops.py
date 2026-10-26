# -*- coding: utf-8 -*-
"""未闭合事件（open_loops）L1：表结构 + 状态机闸门（2026-10-24 设计一）。

方案：`docs/开发规划/未闭合事件_技术方案_20261024.md`

要解决的判据（主人原话）：她能不能说出「你上次说的那件事，后来怎样了」？
现在不能——记忆只能被检索，不会主动来找你。open_loops 是「悬着的事」这个对象。

本文件只测 L1（存储层状态机），测的每一条都对应一种**真实伤害**：
- 跨人串（把甲的悬事说给乙）→ 越权拒绝
- 收了尾的事被重新提（重复打扰）→ 终态不可逆
- 问过上限还继续问（变成骚扰机器）→ asked_count 闸门
- 历史垃圾挤掉近期悬事（注入位只有 3 个）→ 过期清理
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.store import Store


class TestTableShape:
    def test_open_loops_table_exists_with_state_machine(self, store):
        """表必须存在，且 status 有 CHECK 约束——写进去非法状态要当场报错，
        不能靠调用方自觉（教训 #19：状态分流不用内容字符串）。"""
        cols = {row[1] for row in store._connect().execute(
            "PRAGMA table_info(open_loops)").fetchall()}
        assert {"qq_id", "topic", "kind", "expected_at", "status",
                "asked_count", "created_at", "updated_at"} <= cols

        with store._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO open_loops (qq_id,topic,status,created_at,updated_at) "
                    "VALUES ('1','x','bogus','now','now')")
                conn.commit()
                raised = False
            except Exception:
                raised = True
        assert raised, "status 必须由 CHECK 约束兜底"


class TestAddAndDedup:
    def test_add_returns_id_and_is_listable(self, store):
        loop_id = store.add_open_loop("7001", "下周三有个面试")
        assert loop_id > 0
        rows = store.list_open_loops("7001")
        assert len(rows) == 1
        assert rows[0]["topic"] == "下周三有个面试"
        assert rows[0]["status"] == "open"
        assert rows[0]["asked_count"] == 0

    def test_same_topic_mentioned_again_updates_not_duplicates(self, store):
        """同一件事被再次提到：只更新预期时间，不新建行。
        否则「面试」被提 5 次会建 5 行，注入 3 个位置全被同一件事占满。"""
        first = store.add_open_loop("7002", "下周三有个面试", expected_at="2026-10-28")
        second = store.add_open_loop("7002", "下周三有个面试", expected_at="2026-10-29")
        assert first == second
        rows = store.list_open_loops("7002")
        assert len(rows) == 1
        assert rows[0]["expected_at"] == "2026-10-29"

    def test_topic_normalisation_ignores_punctuation_and_spaces(self, store):
        a = store.add_open_loop("7003", "准备体检")
        b = store.add_open_loop("7003", "准备体检。")
        c = store.add_open_loop("7003", " 准备 体检 ")
        assert a == b == c, "结构归一化失效：多打一个空格就建两行"

    def test_different_people_are_isolated(self, store):
        store.add_open_loop("7004", "准备体检")
        store.add_open_loop("7005", "准备体检")
        assert len(store.list_open_loops("7004")) == 1
        assert len(store.list_open_loops("7005")) == 1

    def test_resolved_topic_can_be_reopened_as_new_loop(self, store):
        """已经收尾的事再被提起，应该是**新的一件事**（上次面试→这次面试）。"""
        old = store.add_open_loop("7006", "准备面试")
        assert store.resolve_open_loop(old, qq_id="7006", outcome="resolved")
        new = store.add_open_loop("7006", "准备面试")
        assert new != old
        assert len(store.list_open_loops("7006")) == 1  # 只看到新的那条

    def test_empty_input_is_rejected_not_silently_stored(self, store):
        assert store.add_open_loop("", "x") == 0
        assert store.add_open_loop("7007", "") == 0
        assert store.add_open_loop("7007", "   ") == 0


class TestResolveStateMachine:
    def test_asked_increments_and_stays_open(self, store):
        loop_id = store.add_open_loop("7101", "要去看牙医")
        assert store.resolve_open_loop(loop_id, qq_id="7101", outcome="asked")
        rows = store.list_open_loops("7101")
        assert rows[0]["status"] == "open"
        assert rows[0]["asked_count"] == 1

    def test_cross_person_is_rejected(self, store):
        """甲的悬事被乙收尾 = 把别人的事当成自己的处理。必须拒绝且不静默成功。"""
        loop_id = store.add_open_loop("7102", "要去看牙医")
        assert store.resolve_open_loop(loop_id, qq_id="9999", outcome="resolved") is False
        rows = store.list_open_loops("7102")
        assert rows[0]["status"] == "open", "越权调用不许改变状态"

    def test_terminal_state_is_irreversible(self, store):
        """收尾后不许再被 asked/resolved 改回去——终态可逆会让注入层重复打扰。"""
        loop_id = store.add_open_loop("7103", "要去看牙医")
        assert store.resolve_open_loop(loop_id, qq_id="7103", outcome="resolved")
        assert store.resolve_open_loop(loop_id, qq_id="7103", outcome="asked") is False
        assert store.resolve_open_loop(loop_id, qq_id="7103", outcome="dropped") is False
        with store._connect() as conn:
            row = conn.execute(
                "SELECT status, asked_count FROM open_loops WHERE id=?",
                (loop_id,)).fetchone()
        assert row[0] == "resolved" and row[1] == 0

    def test_unknown_outcome_is_rejected(self, store):
        loop_id = store.add_open_loop("7104", "要去看牙医")
        for bad in ("", "done", "open", "RESOLVED "):
            if bad.strip().lower() == "resolved":
                continue
            assert store.resolve_open_loop(
                loop_id, qq_id="7104", outcome=bad) is False, bad

    def test_resolved_loop_disappears_from_injection_list(self, store):
        loop_id = store.add_open_loop("7105", "要去看牙医")
        store.resolve_open_loop(loop_id, qq_id="7105", outcome="resolved",
                                note="看完了，只是洗牙")
        assert store.list_open_loops("7105") == []


class TestInjectionQuery:
    def test_asked_out_loops_stop_surfacing(self, store):
        """问过 2 次仍没结果 → 不再浮上来（上限 2，防骚扰）。"""
        loop_id = store.add_open_loop("7201", "要去看牙医")
        assert len(store.list_open_loops("7201")) == 1
        store.resolve_open_loop(loop_id, qq_id="7201", outcome="asked")
        assert len(store.list_open_loops("7201")) == 1, "问 1 次还算正常"
        store.resolve_open_loop(loop_id, qq_id="7201", outcome="asked")
        assert store.list_open_loops("7201") == [], "问过 2 次必须停"

    def test_due_items_sort_first(self, store):
        """到期/超期的排在前面——注入位只有 3 个，最该问的先上。"""
        store.add_open_loop("7202", "很久以后想学吉他", expected_at="2030-01-01")
        store.add_open_loop("7202", "上周该交的作业", expected_at="2026-10-20")
        rows = store.list_open_loops("7202", today="2026-10-24")
        assert rows[0]["topic"] == "上周该交的作业"

    def test_limit_is_respected(self, store):
        for i in range(6):
            store.add_open_loop("7203", f"第{i}件事")
        assert len(store.list_open_loops("7203", limit=3)) == 3

    def test_unknown_person_returns_empty(self, store):
        assert store.list_open_loops("") == []
        assert store.list_open_loops("404404") == []


class TestExpire:
    def test_asked_out_and_long_overdue_expires(self, store):
        loop_id = store.add_open_loop("7301", "三个月前说要搬家的",
                                      expected_at="2026-08-01")
        store.resolve_open_loop(loop_id, qq_id="7301", outcome="asked")
        store.resolve_open_loop(loop_id, qq_id="7301", outcome="asked")
        changed = store.expire_open_loops(today="2026-10-24", days_overdue=30)
        assert changed == 1
        with store._connect() as conn:
            assert conn.execute(
                "SELECT status FROM open_loops WHERE id=?", (loop_id,)
            ).fetchone()[0] == "expired"

    def test_never_asked_loops_are_never_expired(self, store):
        """没问过的不许替她放弃——「没来得及问」不等于「不重要」。"""
        store.add_open_loop("7302", "三年没提过的事", expected_at="2023-01-01")
        assert store.expire_open_loops(today="2026-10-24", days_overdue=30) == 0

    def test_not_yet_overdue_is_kept(self, store):
        loop_id = store.add_open_loop("7303", "刚到期的事", expected_at="2026-10-20")
        store.resolve_open_loop(loop_id, qq_id="7303", outcome="asked")
        store.resolve_open_loop(loop_id, qq_id="7303", outcome="asked")
        assert store.expire_open_loops(today="2026-10-24", days_overdue=30) == 0
