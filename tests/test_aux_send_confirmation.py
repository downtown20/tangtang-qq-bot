"""后台发送任务只能在明确送达后提交成功状态。"""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from napcat.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


def test_catch_up_persists_uncertain_and_does_not_blindly_replay(tmp_path, monkeypatch):
    from agent.catch_up import CatchUpManager, CatchUpSummary

    sent_file = tmp_path / "catch_up_sent.json"
    monkeypatch.setattr(CatchUpManager, "SENT_FILE", str(sent_file))
    send = AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    ))
    manager = CatchUpManager(
        store=SimpleNamespace(), short_term={},
        llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"],
        send_group_msg=send,
    )
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1", summary="摘要", message_count=1,
            from_time="2026-08-27 10:00:00", to_time="2026-08-27 10:01:00",
            missed_mentions=[{
                "qq_id": "u1", "nickname": "小蓝", "message": "糖糖？",
                "timestamp": "2026-08-27 10:00:30",
            }],
        )
    }
    manager._generate_missed_reply = AsyncMock(return_value="刚回来，看到啦～")

    _run(manager.reply_to_missed_mentions())
    _run(manager.reply_to_missed_mentions())

    assert send.await_count == 1
    key = manager._sent_key("g1", "u1", "2026-08-27 10:00:30")
    assert manager._sent[key]["state"] == "uncertain"
    assert sent_file.exists()

    restored = CatchUpManager(
        store=SimpleNamespace(), short_term={},
        llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], send_group_msg=send,
    )
    assert restored._already_caught_up("g1", "u1", "2026-08-27 10:00:30") is True
    assert restored._sent[key]["state"] == "uncertain"


def test_daily_report_persists_uncertain_without_claiming_sent(tmp_path, monkeypatch):
    import agent.daily_report as daily_report
    from agent.daily_report import DailyReportScheduler

    monkeypatch.setattr(daily_report, "STATE_FILE", str(tmp_path / "daily.json"))
    send = AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    ))
    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=send,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    scheduler._last_date = datetime.now().strftime("%Y-%m-%d")
    scheduler._generate_report = AsyncMock(return_value="今日日报")
    monkeypatch.setattr("agent.daily_report.asyncio.sleep", AsyncMock())

    _run(scheduler._broadcast())
    _run(scheduler._broadcast())

    assert send.await_count == 1
    assert scheduler._sent_today == []
    assert scheduler._uncertain_today == ["g1"]

    restored = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=send,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    restored._load_state()
    assert restored._uncertain_today == ["g1"]


def test_image_share_quota_only_counts_confirmed_delivery(tmp_path, monkeypatch):
    from agent.image_share import ImageShareScheduler

    image = tmp_path / "sample.png"
    image.write_bytes(b"not-a-real-image-needed-for-send-contract")
    send = AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    ))
    scheduler = ImageShareScheduler(
        config={"enabled": True, "max_per_day": 3},
        send_group_msg=send, get_group_ids=lambda: ["g1"],
    )
    scheduler._pick_local_image = lambda: (image, "配文")
    scheduler._cleanup_old_files = lambda keep=20: None
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())

    _run(scheduler._share_one())

    assert send.await_count == 1
    assert scheduler._sent_today == 0


def test_opinion_invite_is_recorded_only_after_confirmed_delivery(store):
    from agent.opinion import OpinionManager

    send = AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    ))
    manager = OpinionManager(
        store=store, llm_caller=AsyncMock(return_value="想听听你的想法呀～"),
        send_private=send, personality_base="你是糖糖", bot_qq="bot",
        invite_retry_delay=0,
    )
    campaign_id = store.create_opinion_campaign("话题")
    store.add_opinion_participant(campaign_id, "u1", "小蓝", status="queued")

    _run(manager._invite(campaign_id, "话题", "u1", "小蓝"))

    messages = store.get_opinion_messages(campaign_id)
    assert not any(m["is_bot"] for m in messages)
    assert store.get_opinion_participant(campaign_id, "u1")["status"] == "invite_uncertain"


def test_once_scheduler_keeps_uncertain_task_and_does_not_repeat_same_minute(
        tmp_path, monkeypatch):
    import agent.scheduler as scheduler_module
    from agent.scheduler import CronScheduler

    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tmp_path / "tasks.json"))
    send = AsyncMock(return_value=SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED",
    ))
    scheduler = CronScheduler(
        send_group_msg=send, send_private_msg=send,
        llm_caller=AsyncMock(return_value="到点啦～"),
        get_group_ids=lambda: ["g1"],
    )
    task = {
        "id": 1, "type": "once", "text": "喝水",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "group_id": "g1", "user_id": "",
    }
    scheduler._tasks = [task]

    _run(scheduler._tick())
    _run(scheduler._tick())

    assert send.await_count == 1
    assert scheduler._tasks[0]["status"] == "uncertain"
    assert "未确认" in scheduler.list_tasks()
