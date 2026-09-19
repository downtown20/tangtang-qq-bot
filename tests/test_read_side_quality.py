"""批 5 验收：读侧排序与健康治理（2026-08-16）

confidence 进 recall 打分（0.55 种子与 1.0 事实不再同权）；合成行不强化；
新健康检查（真值卫生/feedback 管道）可运行。
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


class TestRecallScoring:
    def test_higher_confidence_ranks_first(self, memory):
        """同 importance 下，高置信度排前——confidence 不再只是入库门槛"""
        memory.store.insert_memory(
            "8201", "like", "特摄仙人一句话", importance=5,
            confidence=0.55, origin="manual",
        )
        memory.store.insert_memory(
            "8201", "like", "喜欢喝咖啡的偏好", importance=5,
            confidence=0.95, origin="manual",
        )
        rows = memory.recall("8201", limit=5)
        assert rows[0].value == "喜欢喝咖啡的偏好"

    def test_corrected_origin_boosted(self, memory):
        memory.store.insert_memory("8202", "fact", "在杭州上班工作", importance=6, confidence=0.9)
        memory.store.insert_memory("8202", "fact_correction", "成展对时政不感兴趣",
                                   importance=6, confidence=1.0, origin="corrected")
        rows = memory.recall("8202", limit=5)
        assert rows[0].origin == "corrected"


class TestReinforceExemption:
    def test_summarized_rows_not_reinforced(self, memory):
        """合成行不强化——「检索→强化→再合成」的滚雪球通道关闭"""
        mid = memory.store.insert_memory("8203", "profile_synthesis", "画像内容",
                                         importance=4, origin="summarized")
        entry = type("M", (), {"id": mid, "origin": "summarized"})()
        memory.reinforce("8203", [entry])
        row = memory.store.query_memories("8203")[0]
        assert row["importance"] == 4

    def test_extracted_rows_still_reinforced(self, memory):
        mid = memory.store.insert_memory("8204", "fact", "真实事实内容", importance=4)
        entry = type("M", (), {"id": mid, "origin": "extracted"})()
        memory.reinforce("8204", [entry])
        row = memory.store.query_memories("8204")[0]
        assert row["importance"] == 5


class TestHealthChecks:
    def _handler(self, memory):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = memory
        return h

    def test_memory_truth_check_runs(self, memory):
        from agent.health_check import _check_memory_truth
        h = self._handler(memory)
        result = asyncio.run(_check_memory_truth(h))
        assert result["status"] in ("ok", "warn")

    def test_memory_truth_reports_active_untrusted_rows(self, memory):
        """历史线索虽不进入 trusted_only 召回，仍必须在健康检查中显式可见。"""
        from agent.health_check import _check_memory_truth

        memory.store.insert_memory("8205", "fact", "未经证据验证的旧事实")
        result = asyncio.run(_check_memory_truth(self._handler(memory)))

        assert result["status"] == "warn"
        assert "1 条 active 记忆缺少可验证信任等级" in result["message"]
        assert "已排除自动召回" in result["message"]

    def test_memory_truth_detects_meta_prefix_notes(self, memory):
        """元话术污染可被健康检查发现——事故在旧检查里隐身，现在不再隐身"""
        from agent.health_check import _check_memory_truth
        memory.store.get_or_create_person("8205", "污染")
        memory.store.update_person("8205", notes="这是个人群像的增量更新版，内容……")
        h = self._handler(memory)
        result = asyncio.run(_check_memory_truth(h))
        assert result["status"] == "warn"
        assert "元话术" in result["message"]

    def test_memory_truth_detects_unanchored_cluster_facts(self, memory):
        """事实簇无证据存量必须可观测，避免后台再污染被静默掩盖。"""
        from agent.health_check import _check_memory_truth

        cluster_id = memory.store.upsert_fact_cluster(
            "8206", "经历", "游戏", summary="历史摘要",
        )
        memory.store.add_cluster_fact(
            cluster_id, "8206", "无证据历史事实", evidence_ids="",
        )
        result = asyncio.run(_check_memory_truth(self._handler(memory)))

        assert result["status"] == "warn"
        assert "事实簇原子事实无证据" in result["message"]
        assert "事实簇摘要含无证据存量" in result["message"]

    def test_memory_truth_detects_cross_subject_and_self_binding_corruption(self, memory):
        """关系表被外部写坏时，健康检查必须暴露主体/作用域/主语错误。"""
        from agent.health_check import _check_memory_truth

        external_chat = memory.store.insert_chat(
            "8208", "错误来源", group_id="group-b", is_bot=True,
        )
        external_memory = memory.store.insert_memory(
            "8207", "fact", "外部错误绑定", origin="extracted",
        )
        self_chat = memory.store.insert_chat(
            "8210", "并非糖糖回复", group_id="group-b", is_bot=False,
        )
        self_memory = memory.store.insert_memory(
            "9000", "promise", "自我错误绑定", origin="self",
            target_qq="8209", source_group_id="group-a",
        )
        with memory.store._connect() as conn:
            conn.execute(
                "UPDATE memories SET trust_level='verified', evidence_ids=?, "
                "source_group_id=? WHERE id=?",
                (str(external_chat), "group-a", external_memory),
            )
            conn.execute(
                "INSERT INTO memory_evidence(memory_id,chat_id,relation,created_at) "
                "VALUES (?,?,?,?)",
                (external_memory, external_chat, "supports", "2026-08-30 00:00:00"),
            )
            conn.execute(
                "UPDATE memories SET trust_level='verified', evidence_ids=? WHERE id=?",
                (str(self_chat), self_memory),
            )
            conn.execute(
                "INSERT INTO memory_evidence(memory_id,chat_id,relation,created_at) "
                "VALUES (?,?,?,?)",
                (self_memory, self_chat, "supports", "2026-08-30 00:00:00"),
            )
            conn.commit()

        result = asyncio.run(_check_memory_truth(self._handler(memory)))

        assert result["status"] == "warn"
        assert "外部记忆绑定了糖糖回复" in result["message"]
        assert "外部记忆主体与证据不一致" in result["message"]
        assert "外部记忆作用域与证据不一致" in result["message"]
        assert "自我记忆目标或回复绑定异常" in result["message"]

    def test_zero_memory_health_ignores_ambient_group_speakers(self, memory):
        """群里刷过很多环境消息，不等于与糖糖建立了需要记忆的关系。"""
        from agent.health_check import _check_zero_memory_users

        for i in range(6):
            qq_id = f"ambient-{i}"
            memory.store.get_or_create_person(qq_id, "群友")
            for _ in range(51):
                memory.store.insert_chat(qq_id, "群环境消息", group_id="g1")

        result = asyncio.run(_check_zero_memory_users(self._handler(memory)))

        assert result["status"] == "ok"
        assert "0 人" in result["message"]

    def test_zero_memory_health_warns_for_direct_relationships(self, memory):
        """频繁收到糖糖回复却仍无记忆，才是覆盖缺口。"""
        from agent.health_check import _check_zero_memory_users

        for i in range(6):
            qq_id = f"direct-{i}"
            memory.store.get_or_create_person(qq_id, "熟人")
            for _ in range(51):
                memory.store.insert_chat(qq_id, "群互动消息", group_id="g1")
            for _ in range(5):
                memory.store.insert_chat(
                    qq_id, "糖糖回复", group_id="g1", is_bot=True,
                )

        result = asyncio.run(_check_zero_memory_users(self._handler(memory)))

        assert result["status"] == "warn"
        assert "6 个" in result["message"]

    def test_feedback_pipeline_check_runs(self, memory):
        from agent.health_check import _check_feedback_pipeline
        h = self._handler(memory)
        result = asyncio.run(_check_feedback_pipeline(h))
        assert result["status"] in ("ok", "warn")

    def test_report_stats_consumes_feedback(self, memory):
        """有数据时才记录消费（Codex M3）——孤儿管道有了消费者"""
        from agent.handler import MessageHandler
        h = self._handler(memory)
        h.napcat = MagicMock()
        for i in range(6):
            memory.store.record_feedback(
                bot_reply=f"回复{i}", user_reaction="好耶", user_qq="8001",
                group_id="", sentiment="positive", confidence=0.9, reply_ms=100,
                direction_verified=True,
            )
        out = h._report_stats_with_feedback()
        assert isinstance(out, dict)
        assert "feedback_vibe" in out
        assert memory.store.kv_get("feedback:last_reported")

    def test_report_ignores_unverified_feedback(self, memory):
        from agent.handler import MessageHandler

        h = self._handler(memory)
        h.napcat = MagicMock()
        for i in range(6):
            memory.store.record_feedback(
                bot_reply=f"未绑定回复{i}", user_reaction="下一条路人消息",
                user_qq="8001", group_id="group-1", sentiment="positive",
                direction_verified=False,
            )

        out = h._report_stats_with_feedback()

        assert "feedback_vibe" not in out
        assert not memory.store.kv_get("feedback:last_reported")

    def test_feedback_health_reports_verified_coverage(self, memory):
        from agent.health_check import _check_feedback_pipeline

        for i in range(10):
            memory.store.record_feedback(
                bot_reply=f"未绑定回复{i}", user_reaction="路人消息",
                user_qq="8001", group_id="group-1",
                direction_verified=i < 2,
            )
        memory.store.kv_set(
            "feedback:last_reported", datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

        result = asyncio.run(_check_feedback_pipeline(self._handler(memory)))

        assert result["status"] == "warn"
        assert "可信覆盖率 20%" in result["message"]

    def test_feedback_health_uses_current_quality_not_legacy_quarantine(self, memory):
        from agent.health_check import _check_feedback_pipeline

        for i in range(20):
            memory.store.record_feedback(
                bot_reply=f"历史未绑定回复{i}", user_reaction="历史路人消息",
                user_qq="8001", group_id="group-1", direction_verified=False,
            )
        legacy_at = (datetime.now() - timedelta(days=3)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with memory.store._connect() as conn:
            conn.execute("UPDATE feedback SET timestamp=?", (legacy_at,))
            conn.commit()
        for i in range(12):
            memory.store.record_feedback(
                bot_reply=f"当前绑定回复{i}", user_reaction="当前目标用户回复",
                user_qq="8001", group_id="group-1", direction_verified=True,
            )
        memory.store.kv_set(
            "feedback:last_reported", datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

        result = asyncio.run(_check_feedback_pipeline(self._handler(memory)))

        assert result["status"] == "ok"
        assert "近1日 12 条，可信 12 条，可信覆盖率 100%" in result["message"]

    def test_no_data_no_consumption_mark(self, memory):
        """空表不记消费（Codex M3）——健康检查不能靠空转假绿"""
        from agent.handler import MessageHandler
        h = self._handler(memory)
        h.napcat = MagicMock()
        out = h._report_stats_with_feedback()
        assert isinstance(out, dict)
        assert "feedback_vibe" not in out
        assert not memory.store.kv_get("feedback:last_reported")
