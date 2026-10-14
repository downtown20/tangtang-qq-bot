"""P0B/P0C：长期记忆真值、证据、时间与保留语义。"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

from agent.memory import MemorySystem


TRUSTED_LEVELS = {"verified", "manual", "corrected"}


def test_store_startup_demotes_unanchored_verified_summary(tmp_path):
    """历史无证据摘要保留审计，但重启后不得继续进入可信召回。"""
    from agent.store import Store

    db_path = tmp_path / "memory.db"
    store = Store(str(db_path))
    memory_id = store.insert_memory(
        "20001", "profile_synthesis", "历史无证据摘要", origin="summarized",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE memories SET trust_level='verified' WHERE id=?",
            (memory_id,),
        )
        conn.commit()

    reopened = Store(str(db_path))
    row = reopened.get_memory_by_id(memory_id)
    assert row["status"] == "active"
    assert row["trust_level"] == "legacy_unverified"


def test_truth_schema_and_indexes_exist(store):
    with store._connect() as conn:
        memory_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(memories)").fetchall()
        }

    assert {
        "trust_level", "retention", "event_time", "ingested_at",
        "valid_from", "valid_to", "superseded_by", "idempotency_key",
    } <= memory_columns
    assert {
        "memory_evidence", "memory_reverification_events",
        "memory_reverification_dispositions", "schema_migrations",
    } <= tables
    assert "idx_memories_subject_truth" in indexes


def test_cluster_fact_evidence_migration_retracts_untrusted_rows(tmp_path):
    """历史簇证据必须绑定同一用户的非 bot 原消息；摘要不可追溯时清空。"""
    from agent.store import Store

    db_path = tmp_path / "cluster-evidence.db"
    store = Store(str(db_path))
    store.get_or_create_person("20002", "用户")
    user_id = store.insert_chat("20002", "我喜欢咖啡")
    bot_id = store.insert_chat("20002", "你是咖啡爱好者", is_bot=True)
    other_id = store.insert_chat("20003", "我喜欢茶")
    cluster_id = store.upsert_fact_cluster(
        "20002", "偏好", "饮品", summary="历史摘要",
    )
    invalid_id = store.add_cluster_fact(
        cluster_id, "20002", "喜欢咖啡", evidence_ids=f"{user_id},{bot_id}",
    )
    no_anchor_id = store.add_cluster_fact(
        cluster_id, "20002", "喜欢茶", evidence_ids=str(other_id),
    )
    valid_cluster_id = store.upsert_fact_cluster(
        "20002", "偏好", "咖啡", summary="保留摘要",
    )
    valid_id = store.add_cluster_fact(
        valid_cluster_id, "20002", "喜欢咖啡", evidence_ids=str(user_id),
    )

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE version='20260830_cluster_fact_evidence_v1'"
        )
        conn.commit()

    reopened = Store(str(db_path))
    with reopened._connect() as conn:
        states = dict(conn.execute(
            "SELECT id,status FROM cluster_facts WHERE id IN (?,?)",
            (invalid_id, no_anchor_id),
        ).fetchall())
        summary = conn.execute(
            "SELECT summary FROM fact_clusters WHERE id=?", (cluster_id,)
        ).fetchone()[0]
        valid_state = conn.execute(
            "SELECT status FROM cluster_facts WHERE id=?", (valid_id,)
        ).fetchone()[0]
        valid_summary = conn.execute(
            "SELECT summary FROM fact_clusters WHERE id=?", (valid_cluster_id,)
        ).fetchone()[0]
    assert states == {invalid_id: "retracted", no_anchor_id: "retracted"}
    assert summary == ""
    assert valid_state == "active"
    assert valid_summary == "保留摘要"


def test_unanchored_cluster_fact_migration_is_reversible_and_idempotent(store):
    """历史无证据行只撤回/清摘要，不删除锚定事实；重复执行无副作用。"""
    from tools.migrate_unanchored_cluster_facts import audit, apply_migration

    user_chat = store.insert_chat("20002", "我喜欢咖啡", is_bot=False)
    cluster_id = store.upsert_fact_cluster(
        "20002", "偏好", "饮品", summary="可能被历史行污染的摘要",
    )
    unanchored_id = store.add_cluster_fact(
        cluster_id, "20002", "历史无证据事实", evidence_ids="",
    )
    anchored_id = store.add_cluster_fact(
        cluster_id, "20002", "有证据事实", evidence_ids=str(user_chat),
    )

    with sqlite3.connect(store.db_path) as conn:
        before = audit(conn)
        assert before["active_unanchored_facts"] == 1
        result = apply_migration(conn)
        after = audit(conn)

    assert result == {"retracted_facts": 1, "cleared_summaries": 1, "clusters": 1}
    assert after["active_unanchored_facts"] == 0
    with store._connect() as conn:
        states = dict(conn.execute(
            "SELECT id,status FROM cluster_facts WHERE id IN (?,?)",
            (unanchored_id, anchored_id),
        ).fetchall())
        summary, fact_count = conn.execute(
            "SELECT summary,fact_count FROM fact_clusters WHERE id=?",
            (cluster_id,),
        ).fetchone()
    assert states == {unanchored_id: "retracted", anchored_id: "active"}
    assert summary == ""
    assert fact_count == 1

    with sqlite3.connect(store.db_path) as conn:
        assert apply_migration(conn) == {
            "retracted_facts": 0, "cleared_summaries": 0, "clusters": 0,
        }


def test_unanchored_migration_backup_keeps_pre_apply_state(store, tmp_path):
    """apply 前的 backup 必须能恢复到迁移前，而不是只验证文件存在。"""
    from tools.migrate_unanchored_cluster_facts import _backup, apply_migration

    cluster_id = store.upsert_fact_cluster(
        "20002", "偏好", "饮品", summary="待清理摘要",
    )
    fact_id = store.add_cluster_fact(
        cluster_id, "20002", "无证据事实", evidence_ids="",
    )
    backup_path = tmp_path / "before-apply.db"

    with sqlite3.connect(store.db_path) as conn:
        created = _backup(conn, store.db_path, str(backup_path))
        result = apply_migration(conn)

    assert created == backup_path.resolve()
    assert result["retracted_facts"] == 1
    with sqlite3.connect(backup_path) as conn:
        status, summary = conn.execute(
            "SELECT cf.status,fc.summary FROM cluster_facts cf "
            "JOIN fact_clusters fc ON fc.id=cf.cluster_id WHERE cf.id=?",
            (fact_id,),
        ).fetchone()
    assert status == "active"
    assert summary == "待清理摘要"


def test_unanchored_migration_cli_refuses_online_apply(store, monkeypatch, capsys):
    """在线实例存在时，CLI 必须先拒绝而不是生成 backup 后改库。"""
    import tools.migrate_unanchored_cluster_facts as migration

    cluster_id = store.upsert_fact_cluster(
        "20002", "偏好", "饮品", summary="在线保护",
    )
    fact_id = store.add_cluster_fact(
        cluster_id, "20002", "无证据事实", evidence_ids="",
    )
    monkeypatch.setattr(migration, "_bot_port_open", lambda: True)

    result = migration.main(["--db", str(store.db_path), "--apply"])

    assert result == 2
    assert "拒绝在线迁移" in capsys.readouterr().err
    with store._connect() as conn:
        assert conn.execute(
            "SELECT status FROM cluster_facts WHERE id=?", (fact_id,)
        ).fetchone()[0] == "active"


def test_valid_chat_evidence_without_quote_is_legacy_unverified(store):
    """P0-D2 收口（审查 Critical 3）：有 evidence_ids 但无 evidence_quote 的
    extracted 记忆不得 verified——来源存在性 ≠ claim 被原文支持。
    证据关联与 event_time 保留（供人工复核），trust 降级。"""
    chat_id = store.insert_chat(
        "20002", "我最喜欢草莓蛋糕", group_id="group-a", is_bot=False,
        timestamp="2026-08-20 12:34:56",
    )

    memory_id = store.insert_memory(
        "20002", "like", "喜欢草莓蛋糕",
        origin="extracted", evidence_ids=str(chat_id), source_group_id="group-a",
    )

    row = store.get_memory_by_id(memory_id)
    evidence = store.get_memory_evidence(memory_id)
    assert row["trust_level"] == "legacy_unverified"  # 无 quote 不提升
    assert row["event_time"] == "2026-08-20 12:34:56"
    assert row["ingested_at"]
    assert evidence == [{"chat_id": chat_id, "relation": "supports"}]


def test_valid_chat_evidence_with_quote_promotes_to_verified(store):
    """P0-D2：evidence_quote 被原文支持 + claim_type=stated → verified"""
    chat_id = store.insert_chat(
        "20002", "我最喜欢草莓蛋糕", group_id="group-a", is_bot=False,
        timestamp="2026-08-20 12:34:56",
    )

    memory_id = store.insert_memory(
        "20002", "like", "喜欢草莓蛋糕",
        origin="extracted", evidence_ids=str(chat_id), source_group_id="group-a",
        evidence_quote="我最喜欢草莓蛋糕", claim_type="stated",
    )

    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "verified"


def test_wrong_user_or_scope_evidence_cannot_promote_memory(store):
    wrong_user = store.insert_chat(
        "30003", "第三人的消息", group_id="group-a", is_bot=False,
        timestamp="2026-08-20 13:00:00",
    )
    wrong_group = store.insert_chat(
        "20002", "另一个群的消息", group_id="group-b", is_bot=False,
        timestamp="2026-08-20 13:01:00",
    )

    memory_id = store.insert_memory(
        "20002", "fact", "伪造证据",
        origin="extracted",
        evidence_ids=f"{wrong_user},{wrong_group}",
        source_group_id="group-a",
        trust_level="verified",
    )

    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "legacy_unverified"
    assert row["event_time"] == ""
    assert store.get_memory_evidence(memory_id) == []


def test_self_memory_evidence_is_bound_to_target_and_bot_reply(store):
    chat_id = store.insert_chat(
        "20002", "我答应明天提醒你", group_id="group-a", is_bot=True,
        timestamp="2026-08-20 14:00:00",
    )
    memory_id = store.insert_memory(
        "90000", "promise", "答应提醒对方",
        origin="self", target_qq="20002", source_group_id="group-a",
        evidence_ids=str(chat_id),
    )

    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "verified"
    assert row["event_time"] == "2026-08-20 14:00:00"


def test_manual_and_corrected_are_explicit_trust_paths(store):
    manual_id = store.insert_memory("20002", "fact", "用户要求记住", origin="manual")
    corrected_id = store.insert_memory(
        "20002", "fact_correction", "用户明确纠正", origin="corrected"
    )

    assert store.get_memory_by_id(manual_id)["trust_level"] == "manual"
    assert store.get_memory_by_id(corrected_id)["trust_level"] == "corrected"


def test_trusted_query_and_default_recall_exclude_legacy(store):
    store.insert_memory("20002", "fact", "无来源旧记忆", origin="extracted")
    store.insert_memory("20002", "fact", "人工确认记忆", origin="manual")

    trusted = store.query_memories("20002", trusted_only=True)
    recalled = MemorySystem(store=store).recall("20002", limit=10)

    assert {row["trust_level"] for row in trusted} <= TRUSTED_LEVELS
    assert [row["value"] for row in trusted] == ["人工确认记忆"]
    assert [entry.value for entry in recalled] == ["人工确认记忆"]


def test_idempotency_key_returns_existing_memory(store):
    first = store.insert_memory(
        "20002", "fact", "同一任务事实", origin="manual",
        idempotency_key="job-1:fact-1",
    )
    second = store.insert_memory(
        "20002", "fact", "同一任务事实", origin="manual",
        idempotency_key="job-1:fact-1",
    )

    assert second == first
    assert len(store.query_memories("20002")) == 1


def test_permanent_memory_is_not_deleted_by_stale_cleanup(store):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M")
    permanent_id = store.insert_memory(
        "20002", "fact", "永久事实", importance=1, timestamp=old,
        origin="manual", retention="permanent",
    )
    transient_id = store.insert_memory(
        "20002", "said", "可过期闲聊", importance=1, timestamp=old,
        origin="manual", retention="transient",
    )

    store.delete_stale_memories(days=90)

    assert store.get_memory_by_id(permanent_id) is not None
    assert store.get_memory_by_id(transient_id) is None


def test_consolidation_never_physically_deletes_permanent_or_cross_scope_rows(store):
    memory = MemorySystem(store=store)
    ids = []
    for index in range(5):
        ids.append(store.insert_memory(
            "20002", "fact", f"长期治疗记录共同片段A{index}",
            importance=1, timestamp="2020-01-01 00:00",
            cognitive="episodic", origin="manual", retention="permanent",
            source_group_id="group-a",
        ))
        ids.append(store.insert_memory(
            "20002", "fact", f"长期治疗记录共同片段B{index}",
            importance=1, timestamp="2020-01-01 00:00",
            cognitive="episodic", origin="manual", retention="permanent",
            source_group_id="group-b",
        ))

    async def merge_everything(_system, _user):
        return "\n".join(f"#{i}: 合并后的治疗记录" for i in range(1, 6))

    asyncio.run(memory.consolidate_memories("20002", merge_everything))

    rows = store.query_memories("20002", include_retracted=True, limit=None)
    by_id = {row["id"]: row for row in rows}
    assert set(ids) <= set(by_id)
    assert all(by_id[memory_id]["retention"] == "permanent" for memory_id in ids)
    assert all(by_id[memory_id]["status"] == "active" for memory_id in ids)


def test_evidence_update_requires_controlled_review_to_promote_legacy(store):
    first_chat = store.insert_chat(
        "20002", "第一次确认", group_id="group-a", is_bot=False,
        timestamp="2026-08-20 15:00:00",
    )
    second_chat = store.insert_chat(
        "20002", "再次确认", group_id="group-a", is_bot=False,
        timestamp="2026-08-21 16:00:00",
    )
    memory_id = store.insert_memory(
        "20002", "fact", "被重复确认的事实", origin="extracted"
    )

    # 普通后台更新不能把旧行自动洗白。
    assert store.update_memory_evidence(
        memory_id, str(first_chat), "group-a", evidence_quote="第一次确认"
    ) is False
    assert store.update_memory_evidence(
        memory_id, str(first_chat), "group-a", evidence_quote="第一次确认",
        allow_legacy_promotion=True, reviewer="tester", review_reason="原文复核",
    ) is True
    assert store.update_memory_evidence(
        memory_id, str(second_chat), "group-a", evidence_quote="再次确认"
    ) is True

    row = store.get_memory_by_id(memory_id)
    assert row["trust_level"] == "verified"
    assert row["evidence_ids"] == f"{first_chat},{second_chat}"
    assert row["event_time"] == "2026-08-21 16:00:00"
    assert store.get_memory_evidence(memory_id) == [
        {"chat_id": first_chat, "relation": "supports"},
        {"chat_id": second_chat, "relation": "supports"},
    ]
    with store._connect() as conn:
        events = conn.execute(
            "SELECT previous_trust_level,new_trust_level,reviewer,reason "
            "FROM memory_reverification_events WHERE memory_id=?", (memory_id,),
        ).fetchall()
    assert events == [("legacy_unverified", "verified", "tester", "原文复核")]

    assert store.rollback_memory_reverification(
        memory_id, reviewer="tester", review_reason="复核结论撤回",
    ) is True
    assert store.get_memory_by_id(memory_id)["trust_level"] == "legacy_unverified"
    with store._connect() as conn:
        rollback = conn.execute(
            "SELECT previous_trust_level,new_trust_level,reviewer,reason "
            "FROM memory_reverification_events WHERE memory_id=? ORDER BY id DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
    assert rollback == ("verified", "legacy_unverified", "tester", "复核结论撤回")


def test_reverification_reject_is_persisted_and_removed_from_audit(store):
    from tools.memory_truth_audit import audit

    chat_id = store.insert_chat(
        "20002", "感觉上高中好累", group_id="group-a", is_bot=False,
    )
    memory_id = store.insert_memory(
        "20002", "fact", "是高中的学生", origin="extracted",
        evidence_ids=str(chat_id), source_group_id="group-a",
    )

    assert store.record_memory_reverification_disposition(
        memory_id, "rejected", reviewer="tester",
        reason="原文只是在评价高中生活，不能证明说话者是高中生",
        reviewed_at="2026-09-02 10:00:00",
    ) is True

    result = audit(store.db_path)
    assert memory_id not in {item["id"] for item in result["candidate_sample"]}
    assert result["legacy_verifiable_candidates"] == 0
    assert result["rejected"] == 1
    assert result["rejected_sample"] == [{
        "memory_id": memory_id,
        "evidence_ids": [chat_id],
        "reviewer": "tester",
        "reason": "原文只是在评价高中生活，不能证明说话者是高中生",
        "reviewed_at": "2026-09-02 10:00:00",
    }]


def test_reverification_defer_is_listed_separately_and_removed_from_queue(store):
    from tools.memory_truth_audit import audit

    chat_id = store.insert_chat(
        "20002", "还有四个小时就走了", group_id="group-a", is_bot=False,
    )
    memory_id = store.insert_memory(
        "20002", "event", "即将返校", origin="extracted",
        evidence_ids=str(chat_id), source_group_id="group-a",
    )

    assert store.record_memory_reverification_disposition(
        memory_id, "deferred", reviewer="tester", reason="等待上下文",
        reviewed_at="2026-09-02 10:01:00",
    ) is True

    result = audit(store.db_path)
    assert memory_id not in {item["id"] for item in result["candidate_sample"]}
    assert result["deferred"] == 1
    assert result["deferred_sample"][0]["reason"] == "等待上下文"


def test_reverification_disposition_requires_complete_review_fields(store):
    chat_id = store.insert_chat("20002", "证据", group_id="group-a", is_bot=False)
    memory_id = store.insert_memory(
        "20002", "fact", "待复核", origin="extracted",
        evidence_ids=str(chat_id), source_group_id="group-a",
    )

    assert store.record_memory_reverification_disposition(
        memory_id, "rejected", reviewer="", reason="理由",
        reviewed_at="2026-09-02 10:00:00",
    ) is False
    assert store.record_memory_reverification_disposition(
        memory_id, "rejected", reviewer="tester", reason="",
        reviewed_at="2026-09-02 10:00:00",
    ) is False
    assert store.record_memory_reverification_disposition(
        memory_id, "rejected", reviewer="tester", reason="理由", reviewed_at="",
    ) is False
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_reverification_dispositions"
        ).fetchone()[0] == 0


def test_rejected_memory_can_be_explicitly_promoted_by_later_review(store):
    chat_id = store.insert_chat(
        "20002", "我是高中生", group_id="group-a", is_bot=False,
    )
    memory_id = store.insert_memory(
        "20002", "fact", "是高中的学生", origin="extracted",
        evidence_ids=str(chat_id), source_group_id="group-a",
    )
    assert store.record_memory_reverification_disposition(
        memory_id, "rejected", reviewer="first-reviewer", reason="首次拒绝",
        reviewed_at="2026-09-02 10:00:00",
    ) is True

    assert store.update_memory_evidence(
        memory_id, str(chat_id), "group-a", evidence_quote="我是高中生",
        allow_legacy_promotion=True, reviewer="second-reviewer",
        review_reason="新一轮人工复核推翻此前拒绝",
    ) is True
    assert store.get_memory_by_id(memory_id)["trust_level"] == "verified"


def test_invalid_evidence_update_does_not_overwrite_verified_lineage(store):
    valid_chat = store.insert_chat("20002", "本人证据", group_id="group-a")
    invalid_chat = store.insert_chat("30003", "他人证据", group_id="group-a")
    memory_id = store.insert_memory(
        "20002", "fact", "已有可信事实", origin="extracted",
        evidence_ids=str(valid_chat), source_group_id="group-a",
    )

    assert store.update_memory_evidence(
        memory_id, str(invalid_chat), "group-a"
    ) is False

    row = store.get_memory_by_id(memory_id)
    assert row["evidence_ids"] == str(valid_chat)
    assert store.get_memory_evidence(memory_id) == [
        {"chat_id": valid_chat, "relation": "supports"}
    ]


def test_in_process_dedup_still_unions_new_evidence(store):
    memory = MemorySystem(store=store)
    first_chat = store.insert_chat("20002", "证据一", group_id="group-a")
    second_chat = store.insert_chat("20002", "证据二", group_id="group-a")

    memory._deduped_remember(
        "20002", "fact", "同一个事实", origin="extracted",
        evidence_ids=str(first_chat), source_group_id="group-a",
        evidence_quote="证据一", claim_type="stated",
    )
    memory._deduped_remember(
        "20002", "fact", "同一个事实", origin="extracted",
        evidence_ids=str(second_chat), source_group_id="group-a",
        evidence_quote="证据二", claim_type="stated",
    )

    rows = store.query_memories("20002", trusted_only=True)
    assert len(rows) == 1
    assert rows[0]["evidence_ids"] == f"{first_chat},{second_chat}"


def test_fact_tool_projection_uses_only_trusted_memories(store):
    memory = MemorySystem(store=store)
    store.insert_memory("20002", "fact", "无来源幻觉事实", origin="extracted")
    store.insert_memory("20002", "fact", "人工确认事实", origin="manual")

    result = memory.search_fact_clusters("20002", "事实", embed_engine=None)

    assert "人工确认事实" in result
    assert "无来源幻觉事实" not in result
    assert "来源" in result


def test_global_keyword_projection_excludes_unverified_legacy_rows(store):
    store.insert_memory("5101", "fact", "和小羽是大学同学", origin="extracted")
    store.insert_memory("5102", "fact", "和小羽是高中同学", origin="manual")

    rows = store.search_memories_by_keyword("小羽")

    assert [row["value"] for row in rows] == ["和小羽是高中同学"]


def test_relation_search_does_not_treat_negated_relation_as_positive(store):
    """关系检索按完整词并识别否定，避免生成错误的人际关联。"""
    memory = MemorySystem(store=store)
    store.insert_memory(
        "5103", "fact", "我不是朋友，也不想一起出去", origin="manual",
    )
    store.insert_memory(
        "5103", "fact", "他是我的朋友", origin="manual",
    )

    rows = memory.search_relation_triples("任意", subject_qq="5103")

    assert len(rows) == 1
    assert "他是我的朋友" in rows[0]
    assert "置信度" in rows[0] and "来源" in rows[0]
    assert "不是朋友" not in rows[0]


def test_legacy_profile_is_quarantined_but_manual_profile_is_visible(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("20002", "测试用户")
    store.update_person("20002", notes="人工确认画像")
    assert memory.active_notes("20002") == "人工确认画像"

    with store._connect() as conn:
        conn.execute(
            "UPDATE people SET notes='历史无来源画像', "
            "notes_trust_level='legacy_unverified' WHERE qq_id='20002'"
        )
        conn.commit()

    assert memory.active_notes("20002") == ""
    assert store.search_people("测试用户")[0]["notes"] == ""


def test_synthesized_profile_records_trusted_source_lineage(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("20002", "测试用户")
    source_ids = [
        store.insert_memory(
            "20002", "fact", f"人工确认事实第{i}条内容", importance=5,
            confidence=0.9, origin="manual",
        )
        for i in range(5)
    ]

    async def fake_llm(system, user):
        return (
            "测试用户喜欢画画，也认真维护自己的项目，是个耐心而可靠的人。"
            "\nKEY_FACTS:\n- 测试用户喜欢画画并认真维护项目"
        )

    result = asyncio.run(memory.synthesize_profile("20002", fake_llm))

    person = store.get_or_create_person("20002")
    profile_rows = [
        row for row in store.query_memories("20002", trusted_only=True)
        if row["key"] == "profile_synthesis"
    ]
    assert result
    assert person["notes_trust_level"] == "verified"
    assert set(person["notes_source_ids"].split(",")) == {str(item) for item in source_ids}
    assert memory.active_notes("20002") == person["notes"]
    assert len(profile_rows) == 1


def test_profile_synthesis_refuses_to_mix_conversation_scopes(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("20002", "测试用户")
    for index in range(3):
        store.insert_memory(
            "20002", "fact", f"A群事实第{index}条内容", importance=5,
            confidence=0.9, origin="manual", source_group_id="group-a",
        )
        store.insert_memory(
            "20002", "fact", f"B群事实第{index}条内容", importance=5,
            confidence=0.9, origin="manual", source_group_id="group-b",
        )

    called = False

    async def forbidden_llm(system, user):
        nonlocal called
        called = True
        return "不应生成跨群画像"

    result = asyncio.run(memory.synthesize_profile("20002", forbidden_llm))

    assert result is None
    assert called is False
    assert not [
        row for row in store.query_memories("20002", trusted_only=True)
        if row["key"] in {"profile_synthesis", "fact_synthesis"}
    ]


def test_profile_synthesis_does_not_increment_across_scopes(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("20002", "测试用户")
    for index in range(5):
        store.insert_memory(
            "20002", "fact", f"A群独有事实第{index}条内容", importance=5,
            confidence=0.9, origin="manual", source_group_id="group-a",
        )

    async def first_llm(_system, _user):
        return (
            "A群画像只描述A群中有证据的经历和习惯，不包含其他会话的信息。"
            "\nKEY_FACTS:\n- A群中有证据的独有经历和习惯"
        )

    first = asyncio.run(memory.synthesize_profile(
        "20002", first_llm,
        memories=memory.recall("20002", limit=20, source_group_id="group-a"),
    ))
    assert first

    for index in range(5):
        store.insert_memory(
            "20002", "fact", f"B群独有事实第{index}条内容", importance=5,
            confidence=0.9, origin="manual", source_group_id="group-b",
        )

    called = False

    async def forbidden_second_llm(_system, _user):
        nonlocal called
        called = True
        return "不应跨作用域增量画像，这段内容不应被写入数据库。"

    second = asyncio.run(memory.synthesize_profile(
        "20002", forbidden_second_llm,
        memories=memory.recall("20002", limit=20, source_group_id="group-b"),
        bypass_cooldown=True,
    ))

    assert second is None
    assert called is False


def test_scoped_correction_invalidates_profile_from_same_scope(store):
    memory = MemorySystem(store=store)

    async def profile_llm(_system, _user):
        return (
            "测试用户一直住在旧地址，并在这里形成了稳定的生活习惯和日常安排。"
            "\nKEY_FACTS:\n- 测试用户目前一直住在旧地址"
        )

    for qq_id, scope in (("21001", "group-a"), ("21002", "")):
        store.get_or_create_person(qq_id, "测试用户")
        for index in range(5):
            store.insert_memory(
                qq_id, "fact", f"住在旧地址的证据片段{index}", importance=5,
                confidence=0.9, origin="manual", source_group_id=scope,
            )
        profile = asyncio.run(memory.synthesize_profile(
            qq_id, profile_llm,
            memories=memory.recall(qq_id, limit=20, source_group_id=scope),
        ))
        assert profile and "旧地址" in memory.active_notes(qq_id)

        result = memory.correct_memory(
            qq_id, "旧地址", "已经搬到新地址", source_group_id=scope,
        )

        assert result["notes_dirty"] is True
        assert store.get_or_create_person(qq_id)["notes_dirty"] == 1
        assert memory.active_notes(qq_id) == ""
