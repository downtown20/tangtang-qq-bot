"""批 3 验收：合成生命周期（2026-08-16）

事故机制：合成触发计数把合成输出算输入（自激）、合成行只增不换（15/63 行堆积）、
元话术前缀直入 notes。批 3：singleton 替换 + 事务化集合替换 + 成功冷却 + 输入排除。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


def _seed_facts(memory, qq, n=6):
    # confidence=0.9：批 4 起 <0.75 的语义记忆不进画像合成输入（真值分层）
    for i in range(n):
        memory.store.insert_memory(qq, "fact", f"测试事实内容第{i}条信息",
                                   importance=3, confidence=0.9, origin="manual")


def _fake_llm(profile_text):
    async def llm(system, user):
        return profile_text
    return llm


class TestSingletonReplace:
    def test_profile_without_evidence_is_not_trusted(self, memory):
        """摘要不能仅凭可信来源行升级；必须存在原始 memory_evidence。"""
        source_ids = [
            memory.store.insert_memory(
                "8100", "fact", f"历史自动事实第{i}条内容", importance=5,
                confidence=0.9, origin="extracted",
            )
            for i in range(6)
        ]
        # 模拟旧数据曾错误标成 verified，但没有 memory_evidence。
        with memory.store._connect() as conn:
            conn.execute(
                "UPDATE memories SET trust_level='verified' "
                "WHERE id IN (%s)" % ",".join("?" * len(source_ids)),
                tuple(source_ids),
            )
            conn.commit()
        profile = "没有原始证据的测试画像，不能进入可信记忆召回。"
        asyncio.run(memory.synthesize_profile("8100", _fake_llm(profile)))

        rows = memory.store.query_memories("8100", include_retracted=True)
        summary = next(row for row in rows if row["key"] == "profile_synthesis")
        person = memory.store.get_or_create_person("8100")
        assert summary["trust_level"] == "legacy_unverified"
        assert summary["evidence_ids"] == ""
        assert person["notes_trust_level"] == "legacy_unverified"

    def test_double_synthesis_singleton_profile(self, memory):
        """两次合成（近义改写）→ profile_synthesis 只剩 1 条 active"""
        _seed_facts(memory, "8101")
        p1 = "成展是大二学生，住在郴州出租屋，喜欢画画。" + "和糖糖一起开发项目。"
        p2 = "成展同学，大二在读，郴州租房，画画爱好者。" + "糖糖的开发者兼维护者。"
        asyncio.run(memory.synthesize_profile("8101", _fake_llm(
            p1 + "\nKEY_FACTS:\n- 成展是大二学生\n- 喜欢画画")))
        asyncio.run(memory.synthesize_profile("8101", _fake_llm(
            p2 + "\nKEY_FACTS:\n- 大二在读，画画爱好者"), bypass_cooldown=True))

        active = memory.store.query_memories("8101")
        profiles = [m for m in active if m["key"] == "profile_synthesis"]
        facts = [m for m in active if m["key"] == "fact_synthesis"]
        assert len(profiles) == 1
        assert profiles[0]["value"] == p2  # 最新快照
        assert [f["value"] for f in facts] == ["大二在读，画画爱好者"]  # 完整新集合

        # 审计历史：旧行 superseded 保留
        all_rows = memory.store.query_memories("8101", include_retracted=True)
        assert len([m for m in all_rows if m["key"] == "profile_synthesis"]) == 2

    def test_replace_clears_old_embeddings(self, memory):
        """旧行 embedding 在替换事务内删除——无孤儿向量"""
        _seed_facts(memory, "8102")
        asyncio.run(memory.synthesize_profile("8102", _fake_llm(
            "画像正文内容足够长，超过二十个字符的测试画像。" + "\nKEY_FACTS:\n- 测试事实一条")))
        rows = memory.store.query_memories("8102", include_retracted=True)
        mid = [m for m in rows if m["key"] == "profile_synthesis"][0]["id"]
        assert memory.store.get_embedding(mid) is None  # 新行暂无 embedding（未传引擎）
        # 预置 embedding 后再次合成 → 旧 embedding 被删
        import numpy as np
        memory.store.set_embedding(mid, np.array([0.1, 0.2, 0.3], dtype=np.float32))
        asyncio.run(memory.synthesize_profile("8102", _fake_llm(
            "第二版画像正文，内容超过二十个字符长度，确实足够长了。"), bypass_cooldown=True))
        assert memory.store.get_embedding(mid) is None


class TestCooldown:
    def test_success_sets_cooldown(self, memory):
        _seed_facts(memory, "8103")
        asyncio.run(memory.synthesize_profile("8103", _fake_llm(
            "画像正文内容足够长，超过二十个字符的测试画像。")))
        assert memory.store.kv_get("profile_syn_at:8103")

    def test_cooldown_blocks_daily_trigger(self, memory):
        _seed_facts(memory, "8104")
        asyncio.run(memory.synthesize_profile("8104", _fake_llm(
            "画像正文内容足够长，超过二十个字符的测试画像。")))
        llm = AsyncMock(return_value="不会走到")
        out = asyncio.run(memory.synthesize_profile("8104", llm))
        assert out is None
        llm.assert_not_called()

    def test_bypass_cooldown_allows_correction(self, memory):
        _seed_facts(memory, "8105")
        asyncio.run(memory.synthesize_profile("8105", _fake_llm(
            "画像正文内容足够长，超过二十个字符的测试画像。")))
        llm = AsyncMock(return_value="纠正后的画像正文，足够长超过二十个字符。")
        out = asyncio.run(memory.synthesize_profile("8105", llm, bypass_cooldown=True))
        assert out is not None
        llm.assert_called_once()

    def test_failure_does_not_set_cooldown(self, memory):
        _seed_facts(memory, "8106")
        asyncio.run(memory.synthesize_profile("8106", AsyncMock(return_value="")))
        assert not memory.store.kv_get("profile_syn_at:8106")


class TestInputExcludesSynthesis:
    def test_old_synthesis_not_fed_back(self, memory):
        """合成输入不含旧 profile_synthesis/fact_synthesis（第二自我复制通道关闭）"""
        _seed_facts(memory, "8107")
        memory.store.insert_memory("8107", "profile_synthesis",
                                   "旧画像自称特摄仙人喜欢特摄", importance=4)
        memory.store.insert_memory("8107", "fact_synthesis", "喜欢特摄", importance=4)
        seen = {}

        async def llm(system, user):
            seen["user"] = user
            return "新画像正文，长度超过二十个字符，不含旧内容。"

        asyncio.run(memory.synthesize_profile("8107", llm))
        assert "特摄" not in seen["user"]

    def test_prefix_stripped(self, memory):
        """元话术前缀剥离——「这是个人群像的增量更新版」不进入 notes"""
        _seed_facts(memory, "8108")
        dirty_prefix = ("这是个人群像的增量更新版，保持了原有的调性。\n"
                        "成展是大二学生，住在郴州，喜欢画画，平时常和糖糖一起开发项目。")
        asyncio.run(memory.synthesize_profile("8108", _fake_llm(
            dirty_prefix + "\nKEY_FACTS:\n- 成展是大二学生")))
        person = memory.store.get_or_create_person("8108")
        assert "增量更新" not in person["notes"]
        assert person["notes"].startswith("成展是")


class TestCountExcludesSynthesis:
    def test_count_active_facts_only(self, memory):
        memory.store.insert_memory("8109", "fact", "事实A内容", importance=3)
        memory.store.insert_memory("8109", "fact_synthesis", "合成事实", importance=4)
        memory.store.insert_memory("8109", "profile_synthesis", "画像内容", importance=4)
        # 撤销一条后不再计数
        mid = memory.store.insert_memory("8109", "fact", "事实B内容", importance=3)
        assert memory.store.count_active_fact_memories("8109") == 2
        memory.store.set_memory_status(mid, "retracted")
        assert memory.store.count_active_fact_memories("8109") == 1


class TestCodexC1DirtyNotFedBack:
    def test_dirty_notes_excluded_from_synthesis_prompt(self, memory):
        """C1 回归：dirty 旧画像不进入合成 prompt——纠正后的重合成不自我污染"""
        _seed_facts(memory, "8110")
        memory.store.get_or_create_person("8110", "测试")
        memory.store.update_person("8110", notes="旧画像：自称特摄仙人喜欢特摄", notes_dirty=1)
        seen = {}
        async def llm(system, user):
            seen["user"] = user
            return "新画像正文，长度超过二十个字符，内容正确。"
        asyncio.run(memory.synthesize_profile("8110", llm, bypass_cooldown=True))
        assert "特摄仙人" not in seen["user"]
        assert "旧画像" not in seen["user"]
