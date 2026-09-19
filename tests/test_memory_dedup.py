"""
测试记忆去重修复（2026-08-14）：
- Phase 2a 精确匹配：跨 key 全库查重，不受 top-100/importance 排序限制
- 同 value 不同 key（like/said 撞车）不再双写
- 合成 KEY_FACTS 与提取记忆撞车时保留高 importance
"""

import pytest
from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


class TestExactDedup:
    def test_find_memory_by_value(self, store):
        store.insert_memory("123", "like", "对AI没有兴趣", importance=4)
        found = store.find_memory_by_value("123", "对AI没有兴趣")
        assert found is not None
        assert found["key"] == "like"
        assert store.find_memory_by_value("123", "不存在的记忆") is None

    def test_cross_key_same_value_no_duplicate(self, memory):
        """回填(key=like)与提取(key=said)撞车——只保留一条，importance 取高值"""
        memory._deduped_remember("123", "like", "对AI没有兴趣", importance=4)
        memory._deduped_remember("123", "said", "对AI没有兴趣", importance=2)
        rows = memory.store.query_memories("123")
        assert len(rows) == 1
        assert rows[0]["importance"] == 4  # max(4, 2)

    def test_same_key_same_value_no_duplicate(self, memory):
        """同 key 同 value 重复——Phase 1 进程内缓存拦截，只保留一条"""
        memory._deduped_remember("123", "like", "喜欢喝咖啡", importance=3)
        memory._deduped_remember("123", "like", "喜欢喝咖啡", importance=5)
        rows = memory.store.query_memories("123")
        assert len(rows) == 1

    def test_new_value_inserts(self, memory):
        memory._deduped_remember("123", "like", "喜欢喝茶", importance=3)
        memory._deduped_remember("123", "like", "喜欢喝咖啡", importance=3)
        assert len(memory.store.query_memories("123")) == 2


class TestSynthesisDedup:
    @pytest.mark.asyncio
    async def test_double_synthesis_no_dup_facts(self, memory):
        """两次合成产出相同 KEY_FACTS → active 只有一份（批 3 替换语义：
        2026-08-14 的去重预期已升级为「完整新集合替换」，不再靠精确去重）"""
        for i in range(5):
            memory.store.insert_memory("456", "fact", f"测试记忆内容第{i}条",
                                       importance=3, confidence=0.9, origin="manual")

        async def fake_llm(system, user):
            return ("测试用户的画像正文，一百字左右的自然连贯描述。" * 3 +
                    "\nKEY_FACTS:\n"
                    "- 在雷霆服务器当过管理员，高考那年一边备考一边管机房\n"
                    "- 喜欢利他牺牲式的角色关系\n")

        await memory.synthesize_profile("456", fake_llm)
        await memory.synthesize_profile("456", fake_llm, bypass_cooldown=True)

        rows = memory.store.query_memories("456")
        facts = [r for r in rows if r["key"] == "fact_synthesis"]
        profiles = [r for r in rows if r["key"] == "profile_synthesis"]
        assert len(facts) == 2  # 完整新集合（两条 KEY_FACTS），无堆积
        assert len(profiles) == 1  # singleton
        # 审计历史：旧世代保留为 superseded
        all_rows = memory.store.query_memories("456", include_retracted=True)
        assert len([r for r in all_rows if r["key"] == "fact_synthesis"]) == 4
