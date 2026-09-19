"""P2：全量、可信、可拒绝的长期记忆召回。"""

import asyncio
from datetime import datetime, timedelta

import numpy as np

from agent.memory import MemorySystem


class _EmbedStub:
    ready = True

    def encode(self, _text):
        return np.asarray([1.0, 0.0], dtype=np.float32)

    @staticmethod
    def similarity(left, right):
        return float(np.dot(
            np.asarray(left, dtype=np.float32),
            np.asarray(right, dtype=np.float32),
        ))


def _verified_memory(store, qq_id, value, importance=5, event_time="",
                     vector=None):
    timestamp = event_time or "2026-08-20 10:00:00"
    chat_id = store.insert_chat(
        qq_id, value, group_id="group-a", is_bot=False, timestamp=timestamp,
    )
    memory_id = store.insert_memory(
        qq_id, "fact", value, importance=importance,
        timestamp=timestamp[:16], event_time=event_time,
        origin="extracted", source_group_id="group-a",
        evidence_ids=str(chat_id), evidence_quote=value, claim_type="stated",
    )
    if vector is not None:
        store.set_embedding(memory_id, np.asarray(vector, dtype=np.float32))
    return memory_id


def test_query_recall_finds_relevant_memory_beyond_legacy_top_200(store):
    target_id = _verified_memory(
        store, "30003", "小时候养过一只叫星星的鹦鹉",
        importance=1, vector=[1.0, 0.0],
    )
    for index in range(205):
        _verified_memory(
            store, "30003", f"无关的高优先事实{index}",
            importance=10, vector=[0.0, 1.0],
        )

    recalled = MemorySystem(store=store).recall(
        "30003", limit=5, query_text="以前养过什么宠物",
        embed_engine=_EmbedStub(),
    )

    assert [item.id for item in recalled] == [target_id]


def test_query_recall_returns_empty_when_all_semantic_scores_are_zero(store):
    for index in range(8):
        _verified_memory(
            store, "30003", f"只与烘焙有关的记录{index}",
            importance=9, vector=[0.0, 1.0],
        )

    recalled = MemorySystem(store=store).recall(
        "30003", limit=6, query_text="量子纠缠",
        embed_engine=_EmbedStub(),
    )

    assert recalled == []


def test_semantic_threshold_rejects_below_point_four(store):
    rejected_id = _verified_memory(
        store, "30003", "低于阈值候选", importance=10,
        vector=[0.39, 0.920814],
    )
    accepted_id = _verified_memory(
        store, "30003", "达到阈值候选", importance=1,
        vector=[0.40, 0.916515],
    )

    recalled = MemorySystem(store=store).recall(
        "30003", limit=5, query_text="完全不同的检索问题",
        embed_engine=_EmbedStub(),
    )

    ids = [item.id for item in recalled]
    assert accepted_id in ids
    assert rejected_id not in ids


def test_strong_semantic_match_is_not_displaced_by_threshold_candidates(store):
    target_id = _verified_memory(
        store, "30003", "真正相关但重要性较低的事实",
        importance=1, vector=[1.0, 0.0],
    )
    for index in range(30):
        _verified_memory(
            store, "30003", f"临界候选{index}", importance=10,
            vector=[0.40, 0.916515],
        )

    recalled = MemorySystem(store=store).recall(
        "30003", limit=25, query_text="无词法重叠的查询",
        embed_engine=_EmbedStub(),
    )

    assert target_id in [item.id for item in recalled]


def test_query_recall_uses_tokenized_lexical_fallback_without_embeddings(store):
    target_id = _verified_memory(
        store, "30003", "最喜欢龙眼蜂蜜做的甜点", importance=4,
    )
    for index in range(10):
        _verified_memory(store, "30003", f"普通日常记录第{index}条", importance=9)

    recalled = MemorySystem(store=store).recall(
        "30003", limit=5, query_text="龙眼蜂蜜是什么味道",
    )

    assert [item.id for item in recalled] == [target_id]


def test_lexical_fallback_rejects_single_generic_word_overlap(store):
    _verified_memory(store, "30003", "以前在学校喜欢数学", importance=9)

    recalled = MemorySystem(store=store).recall(
        "30003", limit=5, query_text="以前养过什么宠物",
    )

    assert recalled == []


def test_batch_embeddings_remain_numpy_buffers(store):
    memory_id = _verified_memory(
        store, "30003", "向量内存测试", vector=[1.0, 0.0],
    )

    embeddings = store.batch_get_embeddings([memory_id, memory_id])

    assert isinstance(embeddings[memory_id], np.ndarray)
    assert embeddings[memory_id].dtype == np.float32


def test_recall_decay_uses_event_time_instead_of_ingestion_timestamp(store):
    now = datetime.now()
    fresh = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=240)).strftime("%Y-%m-%d %H:%M:%S")
    fresh_id = _verified_memory(store, "30003", "最近发生的事情", event_time=fresh)
    old_id = _verified_memory(store, "30003", "很久以前发生的事情", event_time=old)

    recalled = MemorySystem(store=store).recall("30003", limit=2)

    assert [item.id for item in recalled] == [fresh_id, old_id]


def test_episode_aggregation_uses_trusted_event_time(store):
    memory = MemorySystem(store=store)
    for index in range(3):
        store.insert_memory(
            "30003", "event", f"同一次出游片段{index}",
            timestamp=f"2025-0{index + 1}-01 10:00",
            event_time=f"2026-08-20 10:0{index}:00",
            cognitive="episodic", origin="manual",
        )

    created = asyncio.run(memory._aggregate_episodes("30003"))
    episodes = store.query_episodes("30003")

    assert created == 1
    assert episodes[0]["time_start"] == "2026-08-20 10:00:00"
    assert episodes[0]["time_end"] == "2026-08-20 10:02:00"


def test_episode_aggregation_and_queries_are_isolated_by_scope(store):
    memory = MemorySystem(store=store)
    ids_by_scope = {"group-a": [], "group-b": []}
    for index in range(3):
        for offset, scope in enumerate(("group-a", "group-b")):
            ids_by_scope[scope].append(store.insert_memory(
                "30004", "event", f"{scope}独有事件片段{index}",
                timestamp=f"2026-08-20 10:0{index * 2 + offset}",
                event_time=f"2026-08-20 10:0{index * 2 + offset}:00",
                cognitive="episodic", origin="manual",
                source_group_id=scope,
            ))

    created = asyncio.run(memory._aggregate_episodes("30004"))
    group_a = store.query_episodes("30004", limit=10, source_group_id="group-a")
    group_b = store.query_episodes("30004", limit=10, source_group_id="group-b")

    assert created == 2
    assert len(group_a) == len(group_b) == 1
    assert set(map(int, group_a[0]["paragraph_ids"].split(","))) == set(ids_by_scope["group-a"])
    assert set(map(int, group_b[0]["paragraph_ids"].split(","))) == set(ids_by_scope["group-b"])
    assert "group-b" not in group_a[0]["summary"]
    assert "group-a" not in group_b[0]["summary"]


def test_legacy_unscoped_episode_is_not_treated_as_private(store):
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO episodes (qq_id,title,summary,time_start,time_end) "
            "VALUES ('30005','旧事件','可能混合多个会话','2026-01-01','2026-01-01')"
        )
        conn.commit()

    assert store.query_episodes("30005", source_group_id="") == []
