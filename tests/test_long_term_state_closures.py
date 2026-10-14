"""长期状态闭环回归：提取游标、脏画像重试、反馈反思材料。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.handler_autonomy import AutonomyMixin
from agent.memory import MemorySystem


def test_unprocessed_count_uses_user_rows_not_global_id_gap(store):
    first_id = store.insert_chat("cursor-user", "第一条")
    for i in range(80):
        store.insert_chat("other-user", f"穿插消息{i}")
    store.insert_chat("cursor-user", "第二条")

    assert store.count_unprocessed_messages("cursor-user", first_id) == 1


def test_reverse_fetch_never_crosses_forward_cursor(store):
    ids = [store.insert_chat("bounded-user", f"消息{i}") for i in range(8)]

    rows = store.get_unprocessed_messages(
        "bounded-user", ids[-1] + 1, limit=20, newest_first=True,
        lower_bound_id=ids[3],
    )

    assert [row["id"] for row in rows] == list(reversed(ids[4:]))


def test_backlog_scan_batches_users_with_cursor_bounds_in_one_connection(store):
    user_a_ids = [store.insert_chat("batch-user-a", f"A{i}") for i in range(5)]
    user_b_ids = [store.insert_chat("batch-user-b", f"B{i}") for i in range(4)]
    store.insert_chat("batch-user-a", "糖糖回复", is_bot=True)

    original_connect = store._connect
    connection_count = 0

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    store._connect = counted_connect
    rows = store.get_unprocessed_backlog(
        {"batch-user-a": user_a_ids[1], "batch-user-b": user_b_ids[0]},
        before_ids={"batch-user-a": user_a_ids[-1]},
    )

    assert connection_count == 1
    assert {qq: count for qq, count, _max_id in rows} == {
        "batch-user-a": 2,
        "batch-user-b": 3,
    }


def test_backlog_snapshot_reports_capacity_and_oldest_event_in_one_connection(store):
    first = store.insert_chat(
        "snapshot-user-a", "已处理", timestamp="2026-08-26 10:00:00"
    )
    store.insert_chat(
        "snapshot-user-a", "待处理", timestamp="2026-08-26 11:00:00"
    )
    store.insert_chat(
        "snapshot-user-b", "更早的待处理", timestamp="2026-08-25 09:00:00"
    )
    store.insert_chat("snapshot-user-b", "糖糖回复", is_bot=True)
    store.migrate_extraction_cursors(forward={"snapshot-user-a": first})

    original_connect = store._connect
    connection_count = 0

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    store._connect = counted_connect
    snapshot = store.get_extraction_backlog_snapshot()

    assert connection_count == 1
    assert snapshot["total_user_messages"] == 3
    assert snapshot["backlog_messages"] == 2
    assert snapshot["backlog_users"] == 2
    assert snapshot["over_30_users"] == 0
    assert snapshot["oldest_at"] == "2026-08-25 09:00:00"
    assert [row["qq_id"] for row in snapshot["users"]] == [
        "snapshot-user-b", "snapshot-user-a",
    ]


def test_stale_health_counts_messages_instead_of_id_distance(store):
    from agent.health_check import _check_stale_extraction

    cursors = {}
    for i in range(4):
        qq_id = f"health-user-{i}"
        cursors[qq_id] = store.insert_chat(qq_id, "游标内消息")
    for i in range(80):
        store.insert_chat("health-noise", f"穿插消息{i}")
    for qq_id in cursors:
        store.insert_chat(qq_id, "仅一条待处理")

    handler = SimpleNamespace(
        memory=SimpleNamespace(store=store, _last_extracted_id=cursors)
    )
    original_connect = store._connect
    connection_count = 0

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    store._connect = counted_connect
    result = asyncio.run(_check_stale_extraction(handler))

    assert result["status"] == "ok"
    assert connection_count == 1


def test_stale_health_reports_arrival_and_service_rates_across_samples(store):
    from agent.health_check import _check_stale_extraction

    user_ids = [f"rate-user-{index}" for index in range(5)]
    max_ids = {}
    for qq_id in user_ids:
        for index in range(31):
            max_ids[qq_id] = store.insert_chat(
                qq_id, f"消息{index}", timestamp="2026-08-01 10:00:00"
            )
    handler = SimpleNamespace(memory=SimpleNamespace(store=store))

    first = asyncio.run(_check_stale_extraction(handler))
    assert first["status"] == "warn"
    assert "state=warming" in first["message"]
    assert "155条/5人" in first["message"]

    handler._extraction_backlog_sample["captured_at"] = time.time() - 3600
    store.migrate_extraction_cursors(forward={user_ids[0]: max_ids[user_ids[0]]})
    for index in range(4):
        store.insert_chat(user_ids[1], f"新到消息{index}")

    second = asyncio.run(_check_stale_extraction(handler))

    assert second["status"] == "warn"
    assert "state=draining" in second["message"]
    assert "到达4.0/h" in second["message"]
    assert "消化31.0/h" in second["message"]


def test_stale_health_warns_only_after_eligible_tail_proves_stopped(store):
    from agent.health_check import _check_stale_extraction

    for index in range(3):
        store.insert_chat(
            "aged-tail-user",
            f"旧消息{index}",
            timestamp="2026-07-01 10:00:00",
        )
    handler = SimpleNamespace(memory=SimpleNamespace(store=store))

    warming = asyncio.run(_check_stale_extraction(handler))
    handler._extraction_backlog_sample["captured_at"] = time.time() - 3600
    stopped = asyncio.run(_check_stale_extraction(handler))

    assert warming["status"] == "ok"
    assert "到期3" in warming["message"]
    assert stopped["status"] == "warn"
    assert "state=stopped" in stopped["message"]


def test_stale_extraction_scans_historical_backlog_not_only_recent_users(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("old-user", "旧用户")
    for i in range(12):
        store.insert_chat(
            "old-user", f"历史消息{i}", timestamp="2026-01-01 10:00:00"
        )

    class Harness(AutonomyMixin):
        pass

    handler = Harness()
    handler.memory = memory
    handler.bot_qq = "bot"
    handler.owner_qq = ""
    handler._extracting_users = set()
    handler._extraction_counter = {}
    handler.metrics = SimpleNamespace(incr=lambda *_args, **_kwargs: None)
    handler._extract_memories_with_llm = AsyncMock()

    admitted = asyncio.run(handler._maybe_extract_stale(
        max_per_cycle=6, max_open=6,
    ))

    assert admitted == 1
    assert store.get_extraction_queue_health()["pending"] == 1
    assert store.list_resumable_extraction_jobs(limit=2)[0]["qq_id"] == "old-user"
    handler._extract_memories_with_llm.assert_not_awaited()


def test_stale_admission_is_bounded_owner_first_then_oldest(store):
    memory = MemorySystem(store=store)
    users = [
        ("new-user", "2026-08-20 10:00:00"),
        ("owner-user", "2026-08-21 10:00:00"),
        ("old-user", "2026-08-01 10:00:00"),
    ]
    for qq_id, timestamp in users:
        for index in range(12):
            store.insert_chat(qq_id, f"历史消息{index}", timestamp=timestamp)

    class Harness(AutonomyMixin):
        pass

    handler = Harness()
    handler.memory = memory
    handler.bot_qq = "bot"
    handler.owner_qq = "owner-user"
    handler._extracting_users = set()
    handler._extraction_counter = {}
    handler.metrics = SimpleNamespace(incr=lambda *_args, **_kwargs: None)

    admitted = asyncio.run(handler._maybe_extract_stale(
        max_per_cycle=2, max_open=2,
    ))
    jobs = store.list_resumable_extraction_jobs(limit=10)

    assert admitted == 2
    assert [job["qq_id"] for job in jobs] == ["owner-user", "old-user"]
    assert asyncio.run(handler._maybe_extract_stale(
        max_per_cycle=2, max_open=2,
    )) == 0


def test_dirty_profile_enters_autonomous_retry_queue(store):
    memory = MemorySystem(store=store)
    store.get_or_create_person("dirty-user", "待修复")
    store.update_person("dirty-user", notes="仍可读但已经过期的画像", notes_dirty=1)
    for i in range(11):
        store.insert_memory(
            "dirty-user", "fact", f"用于重合成的有效事实{i}", confidence=0.9
        )

    class Harness(AutonomyMixin):
        pass

    handler = Harness()
    handler.memory = memory
    handler._extracting_users = set()
    handler._resynthesize_profile_later = AsyncMock()
    handler._synthesize_profile_task = AsyncMock()
    handler._safe_task = lambda coro, name="": asyncio.create_task(coro)

    async def run():
        await handler._maybe_synthesize_stale_profiles()
        await asyncio.sleep(0)

    asyncio.run(run())
    handler._resynthesize_profile_later.assert_awaited_once_with("dirty-user")
    handler._synthesize_profile_task.assert_not_awaited()


def test_feedback_becomes_sourced_reflection_material_for_current_user(store):
    from agent.handler import MessageHandler

    store.record_feedback(
        "我刚才说得太满了", "你又在瞎编",
        "feedback-user", "group-1", sentiment="negative", confidence=0.9,
        direction_verified=True,
    )
    store.record_feedback(
        "别人的回复", "别人的反应",
        "other-user", "group-2", sentiment="positive", confidence=0.9,
    )

    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(store=store)
    context = handler._get_feedback_reflection_context("feedback-user")

    assert "来源: feedback#" in context
    assert "group-1" in context
    assert "我刚才说得太满了" in context
    assert "你又在瞎编" in context
    assert "别人的回复" not in context
    assert "positive" not in context and "negative" not in context
    assert store.kv_get("feedback:last_consumed")


def test_feedback_reflection_cursor_consumes_once_and_migrates_timestamp(store):
    from agent.handler import MessageHandler

    store.record_feedback(
        "第一句糖糖回复", "第一句用户反馈",
        "feedback-user", "group-1", direction_verified=True,
    )
    store.kv_set("feedback:last_consumed", "2026-09-03 12:00")
    handler = object.__new__(MessageHandler)
    handler.memory = SimpleNamespace(store=store)

    first = handler._get_feedback_reflection_context("feedback-user")
    cursor_after_first = store.kv_get("feedback:last_consumed")
    repeated = handler._get_feedback_reflection_context("feedback-user")

    store.record_feedback(
        "第二句糖糖回复", "第二句用户反馈",
        "feedback-user", "group-1", direction_verified=True,
    )
    second = handler._get_feedback_reflection_context("feedback-user")

    assert "第一句用户反馈" in first
    assert cursor_after_first.isdigit()
    assert repeated == ""
    assert "第二句用户反馈" in second
    assert store.kv_get("feedback:last_consumed").isdigit()


def test_unverified_group_feedback_stays_out_of_reflection(store):
    store.record_feedback(
        "这条回复不能确定对象", "路人的下一句话",
        "feedback-user", "group-1", direction_verified=False,
    )
    store.record_feedback(
        "这条回复对象明确", "原目标用户的反馈",
        "feedback-user", "group-1", direction_verified=True,
    )

    rows = store.get_recent_feedback_reflections("feedback-user")

    assert [row["bot_reply"] for row in rows] == ["这条回复对象明确"]


def test_group_feedback_candidate_is_bound_to_original_target():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler._last_bot_reply = {
        "group-1": {
            "reply": "糖糖刚才的回复",
            "time": time.time(),
            "target_user": "target-user",
        }
    }

    assert handler._take_feedback_candidate("group-1", "passer-by", 60) is None
    assert "group-1" in handler._last_bot_reply
    result = handler._take_feedback_candidate("group-1", "target-user", 60)
    assert result is not None
    assert result[0] == "糖糖刚才的回复"
    assert "group-1" not in handler._last_bot_reply


def test_perception_persists_direction_verification():
    from agent.perception import PerceptionEngine

    class CaptureStore:
        def __init__(self):
            self.kwargs = None

        def record_feedback(self, *args, **kwargs):
            self.kwargs = kwargs

    capture = CaptureStore()

    async def llm_call(_system, _prompt):
        return "positive"

    engine = PerceptionEngine(capture, llm_call)
    asyncio.run(engine.evaluate(
        "糖糖的回复", "明确对象的反馈", "target-user", "group-1",
        direction_verified=True,
    ))

    assert capture.kwargs["direction_verified"] is True


def test_group_and_private_paths_both_inject_feedback_reflection():
    source = open("agent/handler.py", encoding="utf-8").read()

    assert source.count('backgrounds.append(("反思", feedback_reflection))') == 2


def test_feedback_budget_keeps_source_reply_and_reaction():
    from agent import protocols

    block = protocols.feedback_reflection_block([{
        "id": 7,
        "timestamp": "2026-08-26 12:00:00",
        "group_id": "group-1",
        "bot_reply": "糖糖原话开始" + "长" * 180,
        "user_reaction": "用户反应开始" + "长" * 180,
    }])
    fitted = protocols.fit_backgrounds([("反思", block)])

    assert "feedback#7" in fitted[0][1]
    assert "糖糖原话开始" in fitted[0][1]
    assert "用户反应开始" in fitted[0][1]
