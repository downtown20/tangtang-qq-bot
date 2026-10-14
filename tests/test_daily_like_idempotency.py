import asyncio
from unittest.mock import AsyncMock


def test_daily_like_batch_is_at_most_once_per_target_and_day():
    from agent.handler import MessageHandler

    state = {}
    liker = AsyncMock()
    liker.daily_like_targets.return_value = {"liked": 1, "failed": 0, "capped": 0}

    handler = object.__new__(MessageHandler)
    handler.album_liker = liker
    handler._load_state_kv = lambda key, default: state.get(key, default)
    handler._save_state_kv = lambda key, value: state.__setitem__(key, value) or True

    async def run():
        first = await handler._run_daily_like_once(
            "2026-08-28", ["u1", "u2"], 10,
        )
        second = await handler._run_daily_like_once(
            "2026-08-28", ["u1", "u2"], 10,
        )
        return first, second

    first, second = asyncio.run(run())

    assert first["attempted"] == 2
    assert second["attempted"] == 0
    assert liker.daily_like_targets.await_count == 2
    assert all(call.args[0] in (["u1"], ["u2"]) for call in liker.daily_like_targets.await_args_list)


def test_daily_like_marks_target_before_external_call_when_call_raises():
    from agent.handler import MessageHandler

    state = {}
    liker = AsyncMock()
    liker.daily_like_targets.side_effect = RuntimeError("transport lost")

    handler = object.__new__(MessageHandler)
    handler.album_liker = liker
    handler._load_state_kv = lambda key, default: state.get(key, default)
    handler._save_state_kv = lambda key, value: state.__setitem__(key, value) or True

    result = asyncio.run(handler._run_daily_like_once("2026-08-28", ["u1"], 10))

    assert result["attempted"] == 1
    assert result["failed"] == 1
    assert state["state:daily_like_runs"]["2026-08-28"]["u1"]["status"] == "failed"


def test_daily_like_quota_error_is_expected_not_a_background_failure():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-28 13:44:00+0800 | boot=abc cid=- | 糖糖.Album | WARNING | "
        "  ✗ 每日点赞失败: OIDB error 20003 今日同一好友点赞数已达上限",
        "2026-08-28 13:44:01+0800 | boot=abc cid=- | 糖糖.Album | INFO | "
        "每日定时点赞完成: 成功0, 失败1, 配额1",
    ])
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"].get("album_failures", 0) == 0
    assert parsed["events"]["album_quota_exhausted"] == 1
    assert parsed["events"].get("background_task_failures", 0) == 0
    assert report["domains"]["album"]["status"] == "PASS"
    assert report["domains"]["background_tasks"]["status"] != "FAIL"


def _snapshot():
    return {
        "captured_at": "2026-08-28T13:45:00+08:00",
        "boot_ids": ["abc"],
        "latest_boot_id": "abc",
        "latest_boot_at": "2026-08-28T13:43:59+08:00",
        "metrics": {},
        "db": {
            "memory": {"max_memory_id": 1, "max_chat_log_id": 1},
            "queue": {"pending": 0, "leased": 0, "ready": 0, "dead": 0},
            "outbox": {"open": 0, "uncertain": 0, "dead": 0},
        },
    }
