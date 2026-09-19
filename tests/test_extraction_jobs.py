"""P1：持久化提取任务、原子确认与崩溃恢复。"""

import asyncio
import json

import pytest
import numpy as np

from agent.memory import MemorySystem
from agent.handler import MessageHandler


def _messages(store, qq_id="20002", group_id="group-a", count=3):
    rows = []
    for index in range(count):
        chat_id = store.insert_chat(
            qq_id,
            f"第{index}条明确陈述",
            group_id=group_id,
            is_bot=False,
            timestamp=f"2026-08-2{index + 1} 10:00:00",
        )
        rows.append({
            "id": chat_id,
            "message": f"第{index}条明确陈述",
            "timestamp": f"2026-08-2{index + 1} 10:00:00",
            "is_bot_reply": 0,
            "group_id": group_id,
        })
    return rows


def _result_item(messages, value="喜欢草莓蛋糕"):
    return {
        "type": "preference",
        "cognitive": "semantic",
        "value": value,
        "importance": 6,
        "confidence": 0.95,
        "claim_type": "stated",
        "evidence_quote": messages[0]["message"],
        "evidence_ids": [messages[0]["id"]],
        "source_group_id": messages[0]["group_id"],
    }


def test_legacy_cursors_migrate_once_and_never_move_backwards(store):
    store.migrate_extraction_cursors(
        forward={"20002": 12}, backfill={"20002": 90}
    )
    store.migrate_extraction_cursors(
        forward={"20002": 3}, backfill={"20002": 120}
    )

    assert store.get_extraction_cursor("20002", "forward") == 12
    assert store.get_extraction_cursor("20002", "backfill") == 90


def test_ready_job_completes_memory_evidence_and_cursor_atomically(store):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    assert leased and leased["lease_token"]
    assert store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [_result_item(messages)],
        {"protocol_ok": True, "outcome": "success_with_items"},
    ) is True

    completed = store.complete_extraction_job(job["id"], origin="extracted")

    rows = store.query_memories("20002", trusted_only=True)
    assert completed["status"] == "done"
    assert completed["cursor_chat_id"] == messages[-1]["id"]
    assert len(rows) == 1 and rows[0]["value"] == "喜欢草莓蛋糕"
    assert store.get_memory_evidence(rows[0]["id"]) == [
        {"chat_id": messages[0]["id"], "relation": "supports"}
    ]
    assert store.get_extraction_cursor("20002", "forward") == messages[-1]["id"]


def test_ready_job_crash_rolls_back_and_replays_without_duplicate(store, monkeypatch):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [_result_item(messages)],
        {"protocol_ok": True},
    )

    original = store._apply_extraction_item

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash before commit")

    monkeypatch.setattr(store, "_apply_extraction_item", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.complete_extraction_job(job["id"], origin="extracted")

    assert store.get_extraction_job(job["id"])["status"] == "ready"
    assert store.query_memories("20002") == []
    assert store.get_extraction_cursor("20002", "forward") == 0

    monkeypatch.setattr(store, "_apply_extraction_item", original)
    first = store.complete_extraction_job(job["id"], origin="extracted")
    second = store.complete_extraction_job(job["id"], origin="extracted")
    assert first["status"] == second["status"] == "done"
    assert len(store.query_memories("20002")) == 1


def test_identical_fact_in_two_groups_keeps_separate_evidence(store):
    first = _messages(store, group_id="group-a", count=1)
    second = _messages(store, group_id="group-b", count=1)
    for messages in (first, second):
        job = store.create_extraction_job(
            "20002", messages, direction="forward",
            protocol_version=f"scope-{messages[0]['group_id']}",
        )
        leased = store.lease_extraction_job(job["id"])
        store.mark_extraction_job_ready(
            job["id"], leased["lease_token"],
            [_result_item(messages, value="在两个群都说过的事实")],
            {"protocol_ok": True},
        )
        store.complete_extraction_job(job["id"], origin="extracted")

    group_a = store.query_memories(
        "20002", trusted_only=True, source_group_id="group-a",
    )
    group_b = store.query_memories(
        "20002", trusted_only=True, source_group_id="group-b",
    )
    assert [row["value"] for row in group_a] == ["在两个群都说过的事实"]
    assert [row["value"] for row in group_b] == ["在两个群都说过的事实"]
    assert group_a[0]["evidence_ids"] == str(first[0]["id"])
    assert group_b[0]["evidence_ids"] == str(second[0]["id"])


def test_backfill_cursor_moves_toward_older_messages(store):
    messages = _messages(store, count=3)
    job = store.create_extraction_job("20002", messages, direction="backfill")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [],
        {"protocol_ok": True, "outcome": "success_empty"},
    )

    completed = store.complete_extraction_job(job["id"], origin="backfill")

    assert completed["cursor_chat_id"] == messages[0]["id"]
    assert store.get_extraction_cursor("20002", "backfill") == messages[0]["id"]


def test_job_key_is_stable_for_same_range_and_protocol(store):
    messages = _messages(store)
    first = store.create_extraction_job("20002", messages, direction="forward")
    second = store.create_extraction_job("20002", messages, direction="forward")

    assert second["id"] == first["id"]
    assert second["job_key"] == first["job_key"]
    assert first["created_now"] is True
    assert second["created_now"] is False


def test_extractor_keeps_evidence_in_one_conversation_scope(store):
    memory = MemorySystem(store=store)
    first = store.insert_chat("20002", "我喜欢草莓", group_id="group-a")
    second = store.insert_chat("20002", "我也喜欢蛋糕", group_id="group-b")
    messages = [
        {"id": first, "message": "我喜欢草莓", "timestamp": "2026-08-20 10:00:00",
         "is_bot_reply": 0, "group_id": "group-a"},
        {"id": second, "message": "我也喜欢蛋糕", "timestamp": "2026-08-20 10:01:00",
         "is_bot_reply": 0, "group_id": "group-b"},
    ]

    async def fake_llm(system, user):
        return json.dumps([{
            "type": "preference", "cognitive": "semantic",
            "value": "喜欢草莓和蛋糕", "importance": 6, "confidence": 0.95,
            "evidence_ids": [first, second],
        }], ensure_ascii=False)

    items, stats = asyncio.run(memory.extract_semantic_memories(
        messages, "测试用户", "20002", fake_llm,
    ))

    assert stats["protocol_ok"] is True
    assert items[0]["source_group_id"] == "group-a"
    assert items[0]["evidence_ids"] == [first]


class _MetricsStub:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _bare_handler(store):
    handler = MessageHandler.__new__(MessageHandler)
    handler.memory = MemorySystem(store=store)
    handler.metrics = _MetricsStub()
    return handler


def test_extraction_lifecycle_records_terminal_stages_and_queue_age(store, caplog):
    """提取任务必须能把 created→lease→ready→done 串成可观测生命周期。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)

    async def fake_llm(_system, _user):
        return json.dumps([_result_item(messages)], ensure_ascii=False)

    with caplog.at_level("INFO", logger="糖糖.Handler"):
        result = asyncio.run(handler._process_extraction_batch(
            user_id="20002", messages=messages, nickname="测试用户",
            existing_summary="", direction="forward", origin="extracted",
            llm_call=fake_llm,
        ))

    assert result["completed"] is True
    assert handler.metrics.get_current("extract_jobs_created") == 1
    assert handler.metrics.get_current("extract_jobs_lease_acquired") == 1
    assert handler.metrics.get_current("extract_jobs_ready") == 1
    assert handler.metrics.get_current("extract_jobs_completed") == 1
    lifecycle = [
        record.message for record in caplog.records
        if "🧠 提取生命周期" in record.message
    ]
    assert any("stage=created" in message for message in lifecycle)
    assert any(
        "stage=completed" in message
        and "job_id=1" in message
        and "direction=forward" in message
        and "queue_age_s=" in message
        for message in lifecycle
    )


def test_ready_commit_is_not_reclassified_when_followup_read_fails(
        store, monkeypatch):
    """ready 提交成功后，后续只读故障不得污染失败状态或计数。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)

    async def fake_llm(_system, _user):
        return json.dumps([_result_item(messages)], ensure_ascii=False)

    original_get_job = store.get_extraction_job
    read_count = 0

    def fail_post_ready_read(job_id):
        nonlocal read_count
        read_count += 1
        if read_count >= 2:
            raise RuntimeError("injected post-ready read failure")
        return original_get_job(job_id)

    monkeypatch.setattr(store, "get_extraction_job", fail_post_ready_read)
    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=fake_llm,
    ))

    assert result["completed"] is True
    assert handler.metrics.get_current("extract_jobs_ready") == 1
    assert handler.metrics.get_current("extract_jobs_failed") == 0
    assert handler.metrics.get_current("extract_job_llm_failed") == 0
    assert store.get_extraction_queue_health()["done"] == 1


def test_completion_failure_is_recorded_without_marking_done(store, monkeypatch):
    """ready→done 提交异常必须留下可重试的失败证据。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)

    async def fake_llm(_system, _user):
        return json.dumps([_result_item(messages)], ensure_ascii=False)

    def fail_completion(_job_id, origin="extracted"):
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(store, "complete_extraction_job", fail_completion)
    with pytest.raises(RuntimeError, match="injected completion failure"):
        asyncio.run(handler._process_extraction_batch(
            user_id="20002", messages=messages, nickname="测试用户",
            existing_summary="", direction="forward", origin="extracted",
            llm_call=fake_llm,
        ))

    assert handler.metrics.get_current("extract_jobs_failed") == 1
    assert handler.metrics.get_current("extract_jobs_dead") == 0


def test_direct_processing_quarantines_missing_frozen_messages(store, monkeypatch):
    """直接处理路径也必须拒绝空冻结窗口，不得推进游标。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)
    monkeypatch.setattr(
        store, "get_extraction_job_messages_with_integrity",
        lambda _job_id: ([], []),
    )

    async def unexpected_llm(_system, _user):
        raise AssertionError("missing frozen messages must not call LLM")

    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=unexpected_llm,
    ))

    assert result == {"completed": False, "count": 0, "cursor_chat_id": 0}
    job = store.list_resumable_extraction_jobs(limit=1)
    assert job == []
    assert store.get_extraction_queue_health()["dead"] == 1
    assert handler.metrics.get_current("extract_jobs_missing_messages") == 1
    assert handler.metrics.get_current("extract_jobs_dead") == 1


def test_partial_frozen_message_loss_is_quarantined(store):
    """冻结窗口只丢一条也不得把剩余消息当完整证据推进游标。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    with store._connect() as conn:
        conn.execute("DELETE FROM chat_log WHERE id=?", (messages[1]["id"],))
        conn.commit()

    async def unexpected_llm(_system, _user):
        raise AssertionError("partial frozen messages must not call LLM")

    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=unexpected_llm,
    ))

    assert result["completed"] is False
    assert store.get_extraction_job(job["id"])["status"] == "dead"
    assert handler.metrics.get_current("extract_jobs_missing_messages") == 1


def test_extraction_quality_counts_each_llm_sub_batch(store):
    """多子批部分失败时，attempts 与 outcomes 必须保持同粒度。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store, count=25)
    calls = 0

    async def fake_llm(_system, _user):
        nonlocal calls
        calls += 1
        return "[]" if calls == 1 else "not-json"

    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=fake_llm,
    ))

    assert result["completed"] is False
    assert calls == 2
    assert handler.metrics.get_current("extract_attempts") == 2
    assert handler.metrics.get_current("extract_outcomes_total") == 2
    assert handler.metrics.get_current("extract_success_empty") == 1
    assert handler.metrics.get_current("extract_invalid_json") == 1


def test_failure_state_persistence_error_is_not_double_counted(store, monkeypatch):
    """释放失败租约的 DB 异常不能把同一 LLM 失败计两次。"""
    from agent.metrics import MemoryMetrics

    handler = _bare_handler(store)
    handler.metrics = MemoryMetrics(store)
    messages = _messages(store)
    original_fail = store.fail_extraction_job

    async def fake_llm(_system, _user):
        return "not-json"

    def fail_persist(*_args, **_kwargs):
        raise RuntimeError("injected state persistence failure")

    monkeypatch.setattr(store, "fail_extraction_job", fail_persist)
    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=fake_llm,
    ))

    assert result["completed"] is False
    assert handler.metrics.get_current("extract_job_llm_failed") == 1
    assert handler.metrics.get_current("extract_jobs_failed") == 1
    assert store.get_extraction_queue_health()["leased"] == 1
    # Keep the fixture's original method exercised for cleanup/debugging.
    assert original_fail is not None


def test_corrupt_ready_payload_is_quarantined_then_recoverable(store):
    handler = _bare_handler(store)
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [_result_item(messages)],
        {"protocol_ok": True, "outcome": "success_with_items"},
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET result_json=? WHERE id=?",
            ('{"items":[', job["id"]),
        )
        conn.commit()

    with pytest.raises(ValueError, match="corrupt ready payload"):
        store.complete_extraction_job(job["id"], origin="extracted")

    quarantined = store.get_extraction_job(job["id"])
    assert quarantined["status"] == "dead"
    assert quarantined["error"].startswith("corrupt_ready_payload:")
    assert store.get_extraction_cursor("20002", "forward") == 0
    assert store.query_memories("20002") == []

    assert store.requeue_dead_extraction_jobs(
        limit=1, retry_after_seconds=0,
    ) == 1

    async def recovered_llm(_system, _user):
        return json.dumps([_result_item(messages)], ensure_ascii=False)

    recovered = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=messages, nickname="测试用户",
        existing_summary="", direction="forward", origin="extracted",
        llm_call=recovered_llm,
    ))

    assert recovered["completed"] is True
    assert store.get_extraction_job(job["id"])["status"] == "done"
    assert len(store.query_memories("20002", trusted_only=True)) == 1


def test_ready_payload_with_non_list_items_is_quarantined_without_cursor_advance(store):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [], {"protocol_ok": True},
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET result_json=? WHERE id=?",
            ('{"items":0}', job["id"]),
        )
        conn.commit()

    with pytest.raises(ValueError, match="corrupt ready payload"):
        store.complete_extraction_job(job["id"])

    assert store.get_extraction_job(job["id"])["status"] == "dead"
    assert store.get_extraction_cursor("20002", "forward") == 0


def test_ready_payload_with_non_numeric_confidence_is_quarantined(store):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [{
            **_result_item(messages), "confidence": "not-a-number",
        }], {"protocol_ok": True},
    )

    with pytest.raises(ValueError, match="corrupt ready payload"):
        store.complete_extraction_job(job["id"])

    assert store.get_extraction_job(job["id"])["status"] == "dead"
    assert store.get_extraction_cursor("20002", "forward") == 0


def test_batch_orchestrator_separates_llm_contexts_before_atomic_completion(store):
    handler = _bare_handler(store)
    first = _messages(store, group_id="group-a", count=2)
    second = _messages(store, group_id="group-b", count=2)
    calls = []

    async def fake_llm(system, user):
        calls.append(user)
        return "[]"

    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002",
        messages=first + second,
        nickname="测试用户",
        existing_summary="其他会话的画像秘密",
        direction="forward",
        origin="extracted",
        llm_call=fake_llm,
    ))

    assert result["completed"] is True
    assert result["cursor_chat_id"] == second[-1]["id"]
    assert len(calls) == 2
    assert all(not ("第0条明确陈述" in call and call.count("第0条明确陈述") > 1)
               for call in calls)
    assert all("其他会话的画像秘密" not in call for call in calls)


def test_ready_batch_replays_without_calling_llm(store):
    handler = _bare_handler(store)
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [_result_item(messages)],
        {"protocol_ok": True, "outcome": "success_with_items"},
    )

    async def forbidden_llm(system, user):
        raise AssertionError("ready replay must not call LLM")

    result = asyncio.run(handler._process_extraction_batch(
        user_id="20002",
        messages=messages,
        nickname="测试用户",
        existing_summary="",
        direction="forward",
        origin="extracted",
        llm_call=forbidden_llm,
    ))

    assert result["completed"] is True
    assert result["count"] == 1
    assert len(store.query_memories("20002", trusted_only=True)) == 1


def test_memory_unprocessed_reads_database_cursor_not_process_cache(store):
    memory = MemorySystem(store=store)
    messages = _messages(store)
    store.migrate_extraction_cursors(forward={"20002": messages[1]["id"]})
    memory._last_extracted_id = {"20002": 0}

    pending = memory.get_unprocessed_messages("20002", limit=10)

    assert [item["id"] for item in pending] == [messages[2]["id"]]


def test_job_persists_exact_message_membership_for_restart(store):
    selected = _messages(store, group_id="group-a", count=2)
    store.insert_chat("other-user", "夹在范围里的别人消息", group_id="group-a")
    selected.extend(_messages(store, group_id="group-b", count=2))
    job = store.create_extraction_job("20002", selected, direction="forward")

    restored = store.get_extraction_job_messages(job["id"])

    assert [item["id"] for item in restored] == [item["id"] for item in selected]


def test_batch_orchestrator_consumes_every_message_in_stable_twenty_item_chunks(store):
    handler = _bare_handler(store)
    messages = _messages(store, group_id="group-a", count=50)
    calls = []

    async def fake_llm(system, user):
        calls.append(user)
        return "[]"

    first = asyncio.run(handler._process_extraction_batch(
        user_id="20002", messages=list(reversed(messages)), nickname="测试用户",
        existing_summary="", direction="backfill", origin="backfill",
        llm_call=fake_llm,
    ))

    assert first["completed"] is True
    assert len(calls) == 3
    assert [call.count("明确陈述") for call in calls] == [20, 20, 10]
    assert "第0条明确陈述" in calls[0]
    assert "第49条明确陈述" in calls[-1]


def test_resumable_queue_prioritizes_ready_and_reports_health(store):
    first = _messages(store, qq_id="20002")
    second = _messages(store, qq_id="20003")
    pending = store.create_extraction_job("20002", first, direction="forward")
    ready = store.create_extraction_job("20003", second, direction="forward")
    leased = store.lease_extraction_job(ready["id"])
    store.mark_extraction_job_ready(
        ready["id"], leased["lease_token"], [],
        {"protocol_ok": True, "outcome": "success_empty"},
    )

    resumable = store.list_resumable_extraction_jobs(limit=10)
    health = store.get_extraction_queue_health()

    assert [job["id"] for job in resumable[:2]] == [ready["id"], pending["id"]]
    assert health["ready"] == 1
    assert health["pending"] == 1
    assert health["total_open"] == 2


def test_empty_extraction_queue_health_has_stable_status_schema(store):
    health = store.get_extraction_queue_health()

    assert health == {
        "pending": 0, "leased": 0, "ready": 0,
        "done": 0, "dead": 0,
        "total_open": 0, "oldest_open_at": "",
    }


def test_dead_job_is_not_counted_as_runnable_and_can_be_requeued(store):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    assert store.fail_extraction_job(
        job["id"], leased["lease_token"], "invalid_json", max_attempts=1,
    ) is True

    health = store.get_extraction_queue_health()
    assert health["dead"] == 1
    assert health["total_open"] == 0
    assert store.requeue_dead_extraction_jobs(
        limit=1, retry_after_seconds=0,
    ) == 1
    assert store.get_extraction_job(job["id"])["status"] == "pending"


def test_missing_frozen_messages_are_quarantined_and_do_not_starve_worker(store):
    """冻结原文丢失时任务必须退出可运行队列，避免每轮重复阻塞。"""
    from agent.handler_autonomy import AutonomyMixin

    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    with store._connect() as conn:
        conn.execute("DELETE FROM chat_log WHERE qq_id=?", ("20002",))
        conn.commit()

    handler = object.__new__(AutonomyMixin)
    handler.memory = MemorySystem(store=store)
    handler._extracting_users = set()
    handler.metrics = _bare_handler(store).metrics

    completed = asyncio.run(handler._resume_extraction_jobs(limit=1))

    assert completed == 0
    assert store.get_extraction_job(job["id"])["status"] == "dead"
    assert store.list_resumable_extraction_jobs(limit=1) == []


def test_corrupt_ready_recovery_records_dead_lifecycle_once(store):
    """ready 载荷损坏被 Store 隔离后，恢复层仍要记录失败与 dead。"""
    from agent.handler_autonomy import AutonomyMixin
    from agent.metrics import MemoryMetrics

    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    assert leased
    assert store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [], {"protocol_ok": True},
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET result_json=? WHERE id=?",
            ('{"items":0}', job["id"]),
        )
        conn.commit()

    handler = object.__new__(AutonomyMixin)
    handler.memory = MemorySystem(store=store)
    handler._extracting_users = set()
    handler.metrics = MemoryMetrics(store)

    assert asyncio.run(handler._resume_extraction_jobs(limit=1)) == 0
    assert store.get_extraction_job(job["id"])["status"] == "dead"
    assert handler.metrics.get_current("extract_jobs_failed") == 1
    assert handler.metrics.get_current("extract_jobs_dead") == 1


def test_stale_worker_cannot_fail_a_newer_lease(store):
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET lease_token='new-owner' WHERE id=?",
            (job["id"],),
        )
        conn.commit()

    assert store.fail_extraction_job(
        job["id"], leased["lease_token"], "stale",
    ) is False
    current = store.get_extraction_job(job["id"])
    assert current["status"] == "leased"
    assert current["lease_token"] == "new-owner"


def test_embedding_backlog_can_be_drained_in_bounded_batches(store):
    memory = MemorySystem(store=store)
    for index in range(7):
        store.insert_memory(
            "20002", "fact", f"永久事实{index}", origin="manual",
        )

    class Embed:
        ready = True

        @staticmethod
        def encode(_text):
            return np.asarray([1.0, 0.0], dtype=np.float32)

    assert memory.ensure_memory_embeddings(Embed(), batch_limit=3) == 3
    assert memory.ensure_memory_embeddings(Embed(), batch_limit=3) == 3
    assert memory.ensure_memory_embeddings(Embed(), batch_limit=3) == 1
    assert memory.ensure_memory_embeddings(Embed(), batch_limit=3) == 0


def test_failed_embedding_rows_do_not_starve_later_backlog(store):
    memory = MemorySystem(store=store)
    for index in range(25):
        store.insert_memory(
            "20002", "fact", f"坏文本{index}", importance=10, origin="manual",
        )
    good_id = store.insert_memory(
        "20002", "fact", "正常文本", importance=1, origin="manual",
    )

    class Embed:
        ready = True

        @staticmethod
        def encode(text):
            if text.startswith("坏文本"):
                raise RuntimeError("synthetic poison row")
            return np.asarray([1.0, 0.0], dtype=np.float32)

    assert memory.ensure_memory_embeddings(Embed(), batch_limit=25) == 0
    assert memory.ensure_memory_embeddings(Embed(), batch_limit=25) == 1
    assert store.get_embedding(good_id) is not None
    with store._connect() as conn:
        failures = conn.execute(
            "SELECT COUNT(*) FROM memory_embedding_failures"
        ).fetchone()[0]
    assert failures == 25


def test_startup_resume_completes_ready_job_without_llm(store):
    handler = _bare_handler(store)
    handler._extracting_users = set()
    messages = _messages(store)
    job = store.create_extraction_job("20002", messages, direction="forward")
    leased = store.lease_extraction_job(job["id"])
    store.mark_extraction_job_ready(
        job["id"], leased["lease_token"], [_result_item(messages)],
        {"protocol_ok": True, "outcome": "success_with_items"},
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("ready startup recovery must not call LLM")

    handler._call_llm_light = forbidden
    completed = asyncio.run(handler._resume_extraction_jobs(limit=10))

    assert completed == 1
    assert store.get_extraction_job(job["id"])["status"] == "done"
    assert store.get_extraction_cursor("20002", "forward") == messages[-1]["id"]
