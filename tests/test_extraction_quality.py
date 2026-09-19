"""批 4 验收：提取与事实簇质量（2026-08-16）

事故根源：单句「特摄仙人」零语境入库（0.55 恰好压线）→ 洗白成 0.7 画像。
批 4：缺失 confidence 拒绝 / 显式 0.7 保留低可信 / 语境批次 / 证据 id / 独立游标。
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pytest

from agent.memory import MemorySystem

# 源码闸门按 __file__ 定位项目文件，不硬编码本机路径（clone 到别处/换机都能跑）
BASE = Path(__file__).resolve().parent.parent


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


def _batch(messages, bot_first=False):
    out = []
    for i, m in enumerate(messages):
        out.append({"id": i + 1, "message": m, "timestamp": "2026-08-16 10:00",
                    "is_bot_reply": bot_first and i == 0})
    return out


class TestConfidenceTiers:
    async def _extract(self, memory, llm_raw):
        async def fake_llm(system, user):
            return llm_raw
        return await memory.extract_semantic_memories(
            [{"id": 1, "message": "我喜欢喝咖啡加糖", "timestamp": "2026-08-16 10:00",
              "is_bot_reply": False}],
            "测试", "8801", fake_llm)

    def test_missing_confidence_rejected(self, memory):
        items, stats = asyncio.run(self._extract(memory, json.dumps(
            [{"type": "preference", "cognitive": "semantic",
              "value": "喜欢喝咖啡加糖", "importance": 3}])))
        assert items == []

    def test_explicit_low_confidence_rejected(self, memory):
        items, _ = asyncio.run(self._extract(memory, json.dumps(
            [{"type": "preference", "cognitive": "semantic",
              "value": "喜欢喝咖啡加糖", "importance": 3, "confidence": 0.4}])))
        assert items == []

    def test_explicit_07_kept_as_low_tier(self, memory):
        """显式 0.7 保留（合法弱证据）——旧逻辑会降权到 0.55 恰好压线"""
        items, _ = asyncio.run(self._extract(memory, json.dumps(
            [{"type": "preference", "cognitive": "semantic",
              "value": "喜欢喝咖啡加糖", "importance": 3, "confidence": 0.7}])))
        assert len(items) == 1 and items[0]["confidence"] == 0.7

    def test_high_confidence_kept(self, memory):
        items, _ = asyncio.run(self._extract(memory, json.dumps(
            [{"type": "preference", "cognitive": "semantic",
              "value": "喜欢喝咖啡加糖", "importance": 3, "confidence": 0.95}])))
        assert len(items) == 1 and items[0]["confidence"] == 0.95


class TestExtractionPromptRules:
    def test_forbidden_rules_present(self):
        """玩笑/测试语句/引用转述/否定方向/单句标签规则在主提取与事实簇 prompt 都在场（防删闸门）"""
        src = (BASE / "agent" / "memory.py").read_text(encoding="utf-8")
        for kw in ["玩笑/玩梗", "测试语句", "引用转述", "否定句注意方向", "无上下文单句标签"]:
            assert kw in src, kw
        # 主提取出现一次，事实簇 prompt 出现一次（共用段落）
        assert src.count("测试语句") >= 2


class TestBotReplyContext:
    def test_include_bot_replies_private_only(self, store):
        store.get_or_create_person("8802", "语境")
        store.insert_chat("8802", "你好呀", is_bot=False)
        store.insert_chat("8802", "喵~在呢", is_bot=True)
        # 群聊中的 bot 回复不计入（group_id 非空场景由 insert_chat group 参数控制，
        # 这里验证私聊 bot 回复可被带出且带标记）
        msgs = store.get_unprocessed_messages("8802", 0, limit=10, include_bot_replies=True)
        assert any(m["is_bot_reply"] for m in msgs)
        msgs_no = store.get_unprocessed_messages("8802", 0, limit=10)
        assert not any(m["is_bot_reply"] for m in msgs_no)

    def test_chat_text_labels_bot_speaker(self, memory):
        """语境批次里糖糖的回复标「糖糖:」——不再全部标成用户昵称"""
        seen = {}
        async def fake_llm(system, user):
            seen["u"] = user
            return "[]"
        asyncio.run(memory.extract_semantic_memories(
            _batch(["特摄仙人", "这句是我开玩笑的"], bot_first=True),
            "用户甲", "8803", fake_llm))
        assert "糖糖: " in seen["u"]


class TestFactClusterEvidence:
    def test_fact_cluster_response_accepts_fenced_json_and_envelope(self):
        assert MemorySystem._parse_fact_cluster_response(
            "```json\n[]\n```"
        ) == []
        item = {"topic": "爱好", "category": "preference", "fact": "喜欢看特摄"}
        assert MemorySystem._parse_fact_cluster_response(
            json.dumps({"facts": [item]}, ensure_ascii=False)
        ) == [item]

    def test_fact_cluster_response_rejects_non_document_text(self):
        assert MemorySystem._parse_fact_cluster_response("没有需要记录的信息") is None
        assert MemorySystem._parse_fact_cluster_response("[]\n补充说明") is None

    def test_evidence_ids_persisted(self, memory):
        async def fake_llm(system, user):
            return json.dumps([{
                "topic": "爱好", "category": "preference",
                "fact": "喜欢看特摄剧", "confidence": 0.95, "importance": 5,
                "evidence_ids": [2, 3],
            }])
        result = asyncio.run(memory.extract_fact_clusters(
            qq_id="8804",
            messages=_batch(["我说我喜欢特摄", "是真的喜欢", "经常看假面骑士"]),
            nickname="测试", llm_call=fake_llm, embed_engine=None))
        assert result["ok"] is True
        assert result["new_facts"] == 1
        facts = memory.store.get_cluster_facts(
            memory.store.get_fact_clusters("8804")[0]["id"])
        assert facts[0]["evidence_ids"] == "2,3"

    def test_fact_cluster_embedding_does_not_block_event_loop(self, memory):
        """事实簇提取中的 BGE 编码必须移出异步任务的事件循环。"""
        class SlowEmbed:
            ready = True

            def encode(self, _text):
                time.sleep(0.05)
                return np.array([1.0, 0.0], dtype=np.float32)

            @staticmethod
            def similarity(_left, _right):
                return 0.0

        async def fake_llm(_system, _user):
            return json.dumps([{
                "topic": "爱好", "category": "preference",
                "fact": "喜欢看特摄剧", "confidence": 0.95,
                "importance": 5, "evidence_ids": [1],
            }])

        async def scenario():
            ticks = 0

            async def heartbeat():
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.001)

            task = asyncio.create_task(heartbeat())
            try:
                result = await memory.extract_fact_clusters(
                    qq_id="8810", messages=_batch(["我喜欢看特摄"]),
                    nickname="测试", llm_call=fake_llm,
                    embed_engine=SlowEmbed(),
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return ticks, result

        ticks, result = asyncio.run(scenario())
        assert ticks >= 10
        assert result["ok"] is True
        assert result["new_facts"] == 1

    def test_duplicate_insert_not_counted(self, store):
        cid = store.upsert_fact_cluster("8805", "爱好", "特摄")
        f1 = store.add_cluster_fact(cid, "8805", "喜欢特摄", confidence=1.0)
        f2 = store.add_cluster_fact(cid, "8805", "喜欢特摄", confidence=1.0)
        assert f1 > 0 and f2 == 0  # 重复返回 0——调用方不虚增计数

    def test_merge_preserves_fact_evidence(self, memory):
        """事实簇合并后不能因删除来源簇而丢失原始证据 ID。"""
        first = memory.store.upsert_fact_cluster("8806", "爱好", "特摄")
        second = memory.store.upsert_fact_cluster("8806", "兴趣", "影视")
        memory.store.add_cluster_fact(
            first, "8806", "喜欢看特摄", evidence_ids="101",
        )
        memory.store.add_cluster_fact(
            second, "8806", "喜欢看动画", evidence_ids="202",
        )

        class Embed:
            ready = True

            def encode(self, _text):
                return np.array([1.0, 0.0], dtype=np.float32)

            @staticmethod
            def similarity(_left, _right):
                return 0.95

        async def llm(system, _user):
            if "判断这两组事实是否应该合并" in system:
                return "YES, 兴趣"
            return "喜欢看特摄和动画。"

        merged = asyncio.run(memory.merge_similar_clusters(
            "8806", llm, Embed(),
        ))

        assert merged == 1
        facts = memory.store.get_cluster_facts(first)
        by_fact = {fact["fact"]: fact for fact in facts}
        assert by_fact["喜欢看动画"]["evidence_ids"] == "202"

    def test_cluster_summary_excludes_unanchored_active_facts(self, memory):
        """历史 active 但无证据的事实不能重新进入 LLM 摘要输入。"""
        cluster_id = memory.store.upsert_fact_cluster(
            "8807", "偏好", "饮品", summary="已有摘要",
        )
        memory.store.add_cluster_fact(
            cluster_id, "8807", "喜欢咖啡", evidence_ids="101",
        )
        memory.store.add_cluster_fact(
            cluster_id, "8807", "历史无证据幻觉", evidence_ids="",
        )
        seen = {}

        async def llm(_system, user):
            seen["user"] = user
            return "喜欢咖啡。"

        asyncio.run(memory._regenerate_cluster_summary(cluster_id, llm, None))

        assert "喜欢咖啡" in seen["user"]
        assert "历史无证据幻觉" not in seen["user"]

    def test_fact_extraction_context_excludes_mixed_cluster_summary(self, memory):
        """含历史无证据原子事实的簇，其旧摘要也不能作为提取上下文。"""
        cluster_id = memory.store.upsert_fact_cluster(
            "8808", "经历", "游戏", summary="污染的历史摘要",
        )
        memory.store.add_cluster_fact(
            cluster_id, "8808", "有证据的事实", evidence_ids="201",
        )
        memory.store.add_cluster_fact(
            cluster_id, "8808", "无证据的历史事实", evidence_ids="",
        )
        seen = {}

        async def llm(_system, user):
            seen["user"] = user
            return "[]"

        result = asyncio.run(memory.extract_fact_clusters(
            qq_id="8808",
            messages=_batch(["今天聊游戏"]),
            nickname="测试",
            llm_call=llm,
            embed_engine=None,
        ))

        assert result["ok"] is True
        assert "污染的历史摘要" not in seen["user"]


class TestCodexC3FailureVsEmpty:
    def test_llm_failure_returns_ok_false(self, memory):
        """C3 回归：LLM 失败 ok=False（调用方不推进游标）；合法空 [] ok=True"""
        async def broken_llm(system, user):
            return "上游网关错误页（无 JSON）"
        r1 = asyncio.run(memory.extract_fact_clusters(
            qq_id="8901", messages=_batch(["我说我喜欢特摄"]), nickname="测试",
            llm_call=broken_llm, embed_engine=None))
        assert r1["ok"] is False
        async def empty_llm(system, user):
            return "[]"
        r2 = asyncio.run(memory.extract_fact_clusters(
            qq_id="8901", messages=_batch(["我说我喜欢特摄"]), nickname="测试",
            llm_call=empty_llm, embed_engine=None))
        assert r2["ok"] is True


class TestSemanticExtractionOutcomes:
    def test_empty_array_is_protocol_success(self, memory):
        async def empty_llm(_system, _user):
            return "[]"

        items, stats = asyncio.run(memory.extract_semantic_memories(
            _batch(["今天没有需要长期记住的新信息"]),
            "测试", "8902", empty_llm,
        ))

        assert items == []
        assert stats["outcome"] == "success_empty"

    def test_multiple_json_documents_are_invalid(self, memory):
        async def duplicated_llm(_system, _user):
            return "[]\n[]"

        items, stats = asyncio.run(memory.extract_semantic_memories(
            _batch(["测试重复 JSON"]), "测试", "8903", duplicated_llm,
        ))

        assert items == []
        assert stats["outcome"] == "invalid_json"
        assert stats["raw_length"] == len("[]\n[]")

    def test_all_rejected_is_distinct_from_empty(self, memory):
        async def rejected_llm(_system, _user):
            return json.dumps([{
                "type": "preference", "value": "喜欢茶",
                "importance": 3, "confidence": 0.4,
            }])

        items, stats = asyncio.run(memory.extract_semantic_memories(
            _batch(["也许我喜欢茶吧"]), "测试", "8904", rejected_llm,
        ))

        assert items == []
        assert stats["outcome"] == "rejected_all"
        assert stats["rejected"] == 1


def test_extraction_metrics_separate_protocol_success_and_yield(store):
    from agent.metrics import MemoryMetrics

    metrics = MemoryMetrics(store)
    metrics.record_extract_result("success_empty")
    metrics.record_extract_result("success_with_items", count=2)
    metrics.record_extract_result("invalid_json")

    assert metrics.get_current("extract_outcomes_total") == 3
    assert metrics.get_current("extract_protocol_success") == 2
    assert metrics.get_current("extract_with_items") == 1
    assert metrics.get_current("extract_success_empty") == 1
    assert metrics.get_current("extract_invalid_json") == 1
    assert metrics.get_current("extract_memories_total") == 2


def test_extraction_health_reports_protocol_and_yield_rates():
    from agent.health_check import _check_extract_success

    values = {
        "extract_outcomes_total": 12,
        "extract_protocol_success": 11,
        "extract_with_items": 2,
        "extract_invalid_json": 1,
        "extract_transport_error": 0,
    }
    metrics = type("Metrics", (), {
        "get_current": lambda self, name: values.get(name, 0),
    })()
    result = asyncio.run(_check_extract_success(type("Handler", (), {
        "metrics": metrics,
    })()))

    assert result["status"] == "ok"
    assert "协议成功率 92%" in result["message"]
    assert "产出率 17%" in result["message"]
