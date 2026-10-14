"""辅助主动发送模块的送达确认契约回归测试。

网关 ``ok=True`` 只代表请求被接受；只有 ``delivered=True`` 才能提交
去重、配额、任务完成和活动状态等业务状态。
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.catch_up import CatchUpManager, CatchUpSummary
from agent.daily_report import DailyReportScheduler
from agent.image_share import ImageShareScheduler
from agent.opinion import OpinionManager
from agent.personalization import BirthdayGreeter
from agent.scheduler import CronScheduler
from agent.store import Store
from onebot.ws_client import SendResult


def _uncertain() -> SendResult:
    return SendResult(ok=True, delivered=False, message_id=0, error="ack timeout")


async def _no_sleep(*_args, **_kwargs):
    return None


def test_scheduler_fire_emits_proactive_event_before_external_send():
    events = []
    order = []

    async def send_group(_group_id, _message):
        order.append("send")
        return SendResult(True, True, message_id=1)

    async def llm(*_args, **_kwargs):
        order.append("llm")
        return "提醒一下～"

    def on_event(event):
        order.append("event")
        events.append(event)

    scheduler = CronScheduler(
        send_group_msg=send_group,
        send_private_msg=send_group,
        llm_caller=llm,
        get_group_ids=lambda: [],
        proactive_event_sink=on_event,
    )

    asyncio.run(scheduler._fire({
        "id": 42,
        "type": "once",
        "text": "喝水",
        "group_id": "g1",
        "last_attempt_at": "2026-08-29 17:00",
    }))

    assert len(events) == 1
    event = events[0]
    assert event.source == "scheduler"
    assert event.scope_id == "group:g1"
    assert event.target == "g1"
    assert event.payload["task_id"] == "42"
    assert event.idempotency_key == event.event_id
    assert order == ["event", "llm", "send"]


def test_scheduler_restart_sentinel_does_not_emit_proactive_event():
    """系统重启哨兵没有 QQ 外部副作用，不应制造主动事件记录。"""
    events = []
    restart = AsyncMock()

    scheduler = CronScheduler(
        send_group_msg=AsyncMock(),
        send_private_msg=AsyncMock(),
        llm_caller=AsyncMock(return_value="不应调用"),
        get_group_ids=lambda: ["g1", "g2"],
        restart_callback=restart,
        proactive_event_sink=events.append,
    )

    result = asyncio.run(scheduler._fire({
        "id": 43,
        "type": "daily",
        "text": "__RESTART__",
        "group_id": "",
        "last_attempt_at": "2026-08-31 06:00",
    }))

    assert result == "confirmed"
    assert events == []
    restart.assert_awaited_once()


def test_catch_up_records_uncertain_without_claiming_delivery_or_replaying(tmp_path, monkeypatch):
    async def llm(*_args, **_kwargs):
        return "收到啦～"

    async def send(*_args, **_kwargs):
        return _uncertain()

    manager = CatchUpManager(
        store=None,
        short_term={},
        llm_caller=llm,
        config={},
        get_allowed_groups=lambda: ["g1"],
        bot_qq="bot",
        send_group_msg=send,
    )
    manager.SENT_FILE = str(tmp_path / "caught.json")
    manager._sent = {}
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1",
            summary="",
            message_count=1,
            from_time="",
            to_time="",
            missed_mentions=[
                {"qq_id": "u1", "nickname": "小蓝", "timestamp": "2026-08-27 12:00:00", "message": "在吗"}
            ],
        )
    }
    manager._generate_missed_reply = AsyncMock(return_value="收到啦～")
    monkeypatch.setattr("agent.catch_up.asyncio.sleep", _no_sleep)

    asyncio.run(manager.reply_to_missed_mentions())

    assert manager._already_caught_up("g1", "u1", "2026-08-27 12:00:00")
    key = manager._sent_key("g1", "u1", "2026-08-27 12:00:00")
    assert manager._sent[key]["state"] == "uncertain"
    assert Path(manager.SENT_FILE).exists()


def test_daily_report_does_not_mark_uncertain_group(tmp_path, monkeypatch):
    async def send(*_args, **_kwargs):
        return _uncertain()

    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(return_value="日报"),
        send_group_msg=send,
        get_group_ids=lambda: ["g1"],
        get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    scheduler._generate_report = AsyncMock(return_value="日报")
    scheduler._save_state = lambda: True
    monkeypatch.setattr("agent.daily_report.asyncio.sleep", _no_sleep)

    asyncio.run(scheduler._broadcast())

    assert scheduler._sent_today == []


def test_image_share_quota_only_counts_confirmed_delivery(tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    image.write_bytes(b"not decoded; only path is needed")

    async def send(*_args, **_kwargs):
        return _uncertain()

    scheduler = ImageShareScheduler(
        config={
            "enabled": True,
            "groups": ["g1"],
            "use_local_only": True,
            "local_dir": str(tmp_path),
            "max_per_day": 3,
        },
        send_group_msg=send,
        get_group_ids=lambda: ["g1"],
    )
    scheduler._pick_local_image = lambda: (image, "一张图")
    scheduler._cleanup_old_files = lambda **_kwargs: None
    monkeypatch.setattr("agent.image_share.asyncio.sleep", _no_sleep)

    delivered = asyncio.run(scheduler._share_one())

    assert delivered is False
    assert scheduler._sent_today == 0


def test_daily_report_corrupt_state_fails_closed_without_overwrite(
        tmp_path, monkeypatch):
    import agent.daily_report as daily_module

    state_file = tmp_path / "daily.json"
    state_file.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(daily_module, "STATE_FILE", str(state_file))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=send,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    scheduler._load_state()
    scheduler._generate_report = AsyncMock(return_value="今日播报")

    asyncio.run(scheduler._broadcast())

    assert scheduler._state_corrupt is True
    send.assert_not_awaited()
    assert state_file.read_text(encoding="utf-8") == "{bad"


def test_catch_up_corrupt_sent_state_fails_closed_without_overwrite(
        tmp_path, monkeypatch):
    sent_file = tmp_path / "caught.json"
    sent_file.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(CatchUpManager, "SENT_FILE", str(sent_file))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    manager = CatchUpManager(
        store=SimpleNamespace(), short_term={}, llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], bot_qq="bot", send_group_msg=send,
    )
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1", summary="摘要", message_count=1,
            from_time="", to_time="", missed_mentions=[{
                "qq_id": "u1", "nickname": "小蓝", "message": "糖糖？",
                "timestamp": "2026-08-27 10:00:30",
            }],
        )
    }
    manager._generate_missed_reply = AsyncMock(return_value="看到啦～")

    asyncio.run(manager.reply_to_missed_mentions())

    assert manager._sent_corrupt is True
    send.assert_not_awaited()
    assert sent_file.read_text(encoding="utf-8") == "{bad"


def test_scheduler_corrupt_state_fails_closed_without_overwrite(
        tmp_path, monkeypatch):
    import agent.scheduler as scheduler_module

    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tasks_file))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = CronScheduler(
        send_group_msg=send, send_private_msg=send,
        llm_caller=AsyncMock(return_value="提醒"), get_group_ids=lambda: ["g1"],
    )

    task_id = scheduler.ensure_daily("__RESTART__", 6, 0, group_id="")
    asyncio.run(scheduler._tick())

    assert scheduler._state_corrupt is True
    assert task_id == 0
    send.assert_not_awaited()
    assert tasks_file.read_text(encoding="utf-8") == "{bad"


def test_image_share_corrupt_state_fails_closed_without_overwrite(
        tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    state_file = tmp_path / "share-state.json"
    state_file.write_text("{bad", encoding="utf-8")
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = ImageShareScheduler(
        config={
            "enabled": True, "groups": ["g1"], "use_local_only": True,
            "local_dir": str(tmp_path), "state_file": str(state_file),
        },
        send_group_msg=send, get_group_ids=lambda: ["g1"],
    )
    scheduler._pick_local_image = lambda: (image, "一张图")

    delivered = asyncio.run(scheduler._share_one())

    assert delivered is False
    assert scheduler._state_corrupt is True
    send.assert_not_awaited()
    assert state_file.read_text(encoding="utf-8") == "{bad"


def test_image_share_seq_cursor_failure_freezes_restart_replay(
        tmp_path, monkeypatch):
    import agent.image_share as image_module

    image = tmp_path / "a.png"
    image.write_bytes(b"image")
    config = {
        "enabled": True, "groups": ["g1"], "use_local_only": True,
        "local_dir": str(tmp_path), "play_mode": "sequential",
        "max_per_day": 3,
    }
    first_send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    monkeypatch.setattr(image_module, "_save_seq_state", lambda *_args: False)
    monkeypatch.setattr("agent.image_share.asyncio.sleep", _no_sleep)
    first = ImageShareScheduler(config, first_send, lambda: ["g1"])

    assert asyncio.run(first._share_one()) is True
    assert first._inflight is True
    first_send.assert_awaited_once()

    second_send = AsyncMock(return_value=SendResult(True, True, message_id=2))
    restored = ImageShareScheduler(config, second_send, lambda: ["g1"])
    assert restored._uncertain_today > 0
    assert asyncio.run(restored._share_one()) is False
    second_send.assert_not_awaited()


def test_opinion_invite_message_is_recorded_only_after_confirmation(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))

    async def llm(*_args, **_kwargs):
        return "想邀请你来聊聊这个话题呀～"

    async def send(*_args, **_kwargs):
        return _uncertain()

    manager = OpinionManager(store, llm, send, bot_qq="bot")
    campaign_id = store.create_opinion_campaign("话题")
    store.add_opinion_participant(campaign_id, "u1", "小蓝", status="queued")

    asyncio.run(manager._invite(campaign_id, "话题", "u1", "小蓝"))

    messages = store.get_opinion_messages(campaign_id)
    assert not any(m["qq_id"] == "u1" and m["is_bot"] for m in messages)
    assert store.get_opinion_participant(campaign_id, "u1")["status"] == "invite_uncertain"


def test_opinion_invite_send_exception_is_frozen_as_uncertain(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))

    async def send(*_args, **_kwargs):
        raise RuntimeError("response lost after POST")

    manager = OpinionManager(
        store, AsyncMock(return_value="想邀请你来聊聊这个话题呀～"), send,
        bot_qq="bot",
    )
    campaign_id = store.create_opinion_campaign("话题")
    store.add_opinion_participant(campaign_id, "u1", "小蓝", status="queued")

    asyncio.run(manager._invite_all(campaign_id, "话题", [("u1", "小蓝")]))

    participant = store.get_opinion_participant(campaign_id, "u1")
    assert participant["status"] == "invite_uncertain"
    assert not any(m["qq_id"] == "u1" and m["is_bot"]
                   for m in store.get_opinion_messages(campaign_id))


def test_opinion_campaign_with_no_targets_leaves_no_open_campaign(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    manager = OpinionManager(
        store, AsyncMock(return_value="邀请文案"), AsyncMock(return_value=True),
        bot_qq="bot",
    )
    manager._pick_targets = AsyncMock(return_value=[])

    result = asyncio.run(manager.start_campaign("无人可邀请"))

    assert "error" in result
    assert store.get_open_opinion_campaign() is None


def test_opinion_restart_recovers_only_queued_invite_once(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    campaign_id = store.create_opinion_campaign("话题")
    store.add_opinion_participant(
        campaign_id, "queued-user", "排队用户", status="queued",
    )
    store.add_opinion_participant(
        campaign_id, "uncertain-user", "未确认用户", status="invite_uncertain",
    )
    sent = []

    async def send(qq_id, _text):
        sent.append(qq_id)
        return True

    async def run():
        manager = OpinionManager(
            store, AsyncMock(return_value="想邀请你聊聊这个话题呀～"), send,
            bot_qq="bot", invite_retry_delay=0,
        )
        # 恢复必须由 QQ lifecycle online gate 显式触发，构造阶段不抢跑。
        await manager.recover_queued_invitations()
        for _ in range(10):
            if not manager._background_tasks:
                break
            await asyncio.gather(*tuple(manager._background_tasks))
            await asyncio.sleep(0)
        return manager

    manager = asyncio.run(run())

    assert sent == ["queued-user"]
    assert store.get_opinion_participant(
        campaign_id, "queued-user",
    )["status"] == "pending"
    assert store.get_opinion_participant(
        campaign_id, "uncertain-user",
    )["status"] == "invite_uncertain"
    assert not manager._background_tasks


def test_opinion_background_invite_task_is_tracked_until_completion(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    release = asyncio.Event()

    async def send(*_args, **_kwargs):
        await release.wait()
        return True

    async def run():
        manager = OpinionManager(
            store, AsyncMock(return_value="想邀请你聊聊这个话题呀～"), send,
            bot_qq="bot", invite_retry_delay=0,
        )
        result = await manager.start_campaign("话题", targets=["u1"])
        await asyncio.sleep(0)
        assert manager._background_tasks
        release.set()
        await asyncio.gather(*tuple(manager._background_tasks))
        await asyncio.sleep(0)
        assert not manager._background_tasks
        return result

    result = asyncio.run(run())
    assert store.get_opinion_participant(
        result["campaign_id"], "u1",
    )["status"] == "pending"


def test_opinion_idle_window_closure_is_independent_of_thank_you_delivery(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))

    async def llm(*_args, **_kwargs):
        return "keep"

    async def send(*_args, **_kwargs):
        return _uncertain()

    manager = OpinionManager(store, llm, send, bot_qq="bot")
    manager._export_markdown = lambda *a, **k: str(tmp_path / "opinion.md")
    campaign_id = store.create_opinion_campaign("话题")
    store.add_opinion_participant(campaign_id, "u1", "小蓝")
    store.update_opinion_participant(campaign_id, "u1", "participating")
    store.add_opinion_message(campaign_id, "u1", "小蓝", "我的意见")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE opinion_messages SET timestamp='2020-01-01 00:00:00' WHERE campaign_id=?",
            (campaign_id,),
        )
        conn.commit()

    asyncio.run(manager.auto_close_stale(minutes=1))

    # done 描述用户的意见窗口已因超时关闭，不是「致谢已送达」；QQ 回执
    # 不应反向篡改这一业务事实。致谢的不确定发送由网关指标单独记录。
    assert store.get_opinion_participant(campaign_id, "u1")["status"] == "done"


def test_scheduler_migrates_duplicate_restart_tasks_and_ensure_is_idempotent(
    tmp_path, monkeypatch,
):
    import agent.scheduler as scheduler_module

    tasks_file = tmp_path / "tasks.json"
    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tasks_file))
    tasks_file.write_text(json.dumps([
        {
            "id": 1, "type": "daily", "text": "__RESTART__",
            "hour": 6, "minute": 0, "group_id": "",
        },
        {
            "id": 2, "type": "daily", "text": "__RESTART__",
            "hour": 6, "minute": 0, "group_id": "",
        },
        {
            "id": 3, "type": "daily", "text": "普通提醒",
            "hour": 6, "minute": 0, "group_id": "g1",
        },
    ], ensure_ascii=False), encoding="utf-8")

    scheduler = CronScheduler(
        send_group_msg=AsyncMock(), send_private_msg=AsyncMock(),
        llm_caller=AsyncMock(), get_group_ids=lambda: [],
    )

    restart_tasks = [
        task for task in scheduler._tasks if task.get("text") == "__RESTART__"
    ]
    assert [task["id"] for task in restart_tasks] == [1]
    assert scheduler.ensure_daily("__RESTART__", 6, 0, group_id="") == 1
    assert len(scheduler._tasks) == 2
    assert len([
        task for task in json.loads(tasks_file.read_text(encoding="utf-8"))
        if task.get("text") == "__RESTART__"
    ]) == 1


def test_catch_up_marks_inflight_before_request_and_restart_freezes_it(tmp_path, monkeypatch):
    from agent.catch_up import CatchUpManager, CatchUpSummary

    sent_file = tmp_path / "catch-up.json"
    monkeypatch.setattr(CatchUpManager, "SENT_FILE", str(sent_file))

    async def crash(*_args, **_kwargs):
        raise asyncio.CancelledError()

    manager = CatchUpManager(
        store=SimpleNamespace(), short_term={}, llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], bot_qq="bot", send_group_msg=crash,
    )
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1", summary="摘要", message_count=1,
            from_time="", to_time="", missed_mentions=[{
                "qq_id": "u1", "nickname": "小蓝", "message": "糖糖？",
                "timestamp": "2026-08-27 10:00:30",
            }],
        )
    }
    manager._generate_missed_reply = AsyncMock(return_value="看到啦～")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.reply_to_missed_mentions())

    key = manager._sent_key("g1", "u1", "2026-08-27 10:00:30")
    assert manager._sent[key]["state"] == "sending"

    restored = CatchUpManager(
        store=SimpleNamespace(), short_term={}, llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], bot_qq="bot", send_group_msg=AsyncMock(),
    )
    assert restored._sent[key]["state"] == "uncertain"
    restored._summaries = manager._summaries
    restored._generate_missed_reply = manager._generate_missed_reply
    asyncio.run(restored.reply_to_missed_mentions())
    restored._send_group_msg.assert_not_awaited()


def test_catch_up_does_not_send_when_inflight_marker_cannot_be_persisted(
        tmp_path, monkeypatch):
    sent_file = tmp_path / "catch-up.json"
    monkeypatch.setattr(CatchUpManager, "SENT_FILE", str(sent_file))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    manager = CatchUpManager(
        store=SimpleNamespace(), short_term={}, llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], bot_qq="bot", send_group_msg=send,
    )
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1", summary="摘要", message_count=1,
            from_time="", to_time="", missed_mentions=[{
                "qq_id": "u1", "nickname": "小蓝", "message": "糖糖？",
                "timestamp": "2026-08-27 10:00:30",
            }],
        )
    }
    manager._generate_missed_reply = AsyncMock(return_value="看到啦～")
    manager._save_sent = lambda: False

    asyncio.run(manager.reply_to_missed_mentions())

    send.assert_not_awaited()
    assert manager._sent == {}


def test_concurrent_catch_up_calls_claim_each_mention_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(CatchUpManager, "SENT_FILE", str(tmp_path / "caught.json"))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    manager = CatchUpManager(
        store=SimpleNamespace(), short_term={}, llm_caller=AsyncMock(), config={},
        get_allowed_groups=lambda: ["g1"], bot_qq="bot", send_group_msg=send,
    )
    manager._summaries = {
        "g1": CatchUpSummary(
            group_id="g1", summary="摘要", message_count=1,
            from_time="", to_time="", missed_mentions=[{
                "qq_id": "u1", "nickname": "小蓝", "message": "糖糖？",
                "timestamp": "2026-08-27 10:00:30",
            }],
        )
    }
    entered = 0
    release = asyncio.Event()

    async def generate(*_args):
        nonlocal entered
        entered += 1
        if entered == 2:
            release.set()
        await release.wait()
        return "看到啦～"

    manager._generate_missed_reply = generate
    monkeypatch.setattr("agent.catch_up.asyncio.sleep", _no_sleep)

    async def scenario():
        await asyncio.gather(
            manager.reply_to_missed_mentions(),
            manager.reply_to_missed_mentions(),
        )

    asyncio.run(scenario())

    assert entered == 2
    send.assert_awaited_once()


def test_daily_report_marks_inflight_before_request_and_restart_freezes_it(
        tmp_path, monkeypatch):
    import agent.daily_report as daily_module
    from agent.daily_report import DailyReportScheduler

    monkeypatch.setattr(daily_module, "STATE_FILE", str(tmp_path / "daily.json"))

    async def crash(*_args, **_kwargs):
        raise asyncio.CancelledError()

    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=crash,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    scheduler._last_date = datetime.now().strftime("%Y-%m-%d")
    scheduler._generate_report = AsyncMock(return_value="今日播报")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler._broadcast())

    restored = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=AsyncMock(),
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    restored._load_state()
    assert restored._uncertain_today == ["g1"]
    assert restored._sent_today == []


def test_daily_report_does_not_send_when_inflight_marker_cannot_be_persisted(
        tmp_path, monkeypatch):
    import agent.daily_report as daily_module

    monkeypatch.setattr(daily_module, "STATE_FILE", str(tmp_path / "daily.json"))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=send,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    scheduler._generate_report = AsyncMock(return_value="今日播报")
    scheduler._save_state = lambda: False

    asyncio.run(scheduler._broadcast())

    send.assert_not_awaited()
    assert scheduler._sending_today == []


def test_concurrent_daily_broadcasts_claim_each_group_only_once(tmp_path, monkeypatch):
    import agent.daily_report as daily_module

    monkeypatch.setattr(daily_module, "STATE_FILE", str(tmp_path / "daily.json"))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = DailyReportScheduler(
        config={"enabled": True, "groups": ["g1"]},
        llm_caller=AsyncMock(), send_group_msg=send,
        get_group_ids=lambda: ["g1"], get_stats=lambda: {},
        get_weather=AsyncMock(return_value="晴"),
    )
    entered = 0
    release = asyncio.Event()

    async def generate():
        nonlocal entered
        entered += 1
        if entered == 2:
            release.set()
        await release.wait()
        return "今日播报"

    scheduler._generate_report = generate
    monkeypatch.setattr("agent.daily_report.asyncio.sleep", _no_sleep)

    async def scenario():
        await asyncio.gather(scheduler._broadcast(), scheduler._broadcast())

    asyncio.run(scenario())

    assert entered == 2
    send.assert_awaited_once()


def test_birthday_marks_inflight_before_request_and_restart_freezes_it(tmp_path):
    from agent.personalization import BirthdayGreeter

    state = {}

    class StoreStub:
        def get_today_birthdays(self):
            return [{"qq_id": "u1", "nickname": "小蓝"}]

        def kv_set(self, key, value):
            state[key] = value

        def kv_get(self, key):
            return state.get(key)

        def get_or_create_person(self, qq_id):
            return {"qq_id": qq_id, "nickname": "小蓝"}

    async def crash(*_args, **_kwargs):
        raise asyncio.CancelledError()

    today = datetime.now().strftime("%Y-%m-%d")
    async def empty_llm(*_args, **_kwargs):
        return ""

    greeter = BirthdayGreeter(crash, StoreStub(), empty_llm, lambda: ["g1"])
    greeter._last_date = today
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(greeter._check_birthdays())

    restored = BirthdayGreeter(AsyncMock(), StoreStub(), empty_llm, lambda: ["g1"])
    restored._last_date = today
    restored._load_sent_today()
    assert "u1" in restored._uncertain_today
    assert "u1" not in restored._sent_today


def test_cron_marks_inflight_before_request_and_restart_freezes_it(tmp_path, monkeypatch):
    import agent.scheduler as scheduler_module
    from agent.scheduler import CronScheduler

    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tmp_path / "tasks.json"))

    async def crash(*_args, **_kwargs):
        raise asyncio.CancelledError()

    scheduler = CronScheduler(
        send_group_msg=crash, send_private_msg=crash,
        llm_caller=AsyncMock(return_value="提醒"), get_group_ids=lambda: ["g1"],
    )
    task = {
        "id": 1, "type": "once", "text": "喝水",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "group_id": "g1", "user_id": "",
    }
    scheduler._tasks = [task]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scheduler._tick())

    restarted = CronScheduler(
        send_group_msg=AsyncMock(), send_private_msg=AsyncMock(),
        llm_caller=AsyncMock(return_value="提醒"), get_group_ids=lambda: ["g1"],
    )
    assert restarted._tasks[0]["status"] == "uncertain"
    assert restarted._tasks[0]["last_attempt_at"] == datetime.now().strftime("%Y-%m-%d %H:%M")


def test_scheduler_keeps_one_shot_when_delivery_uncertain(tmp_path, monkeypatch):
    async def send(*_args, **_kwargs):
        return _uncertain()

    scheduler = CronScheduler(
        send_group_msg=send,
        send_private_msg=send,
        llm_caller=AsyncMock(return_value="提醒一下～"),
        get_group_ids=lambda: [],
    )
    scheduler._tasks = [
        {
            "id": 1,
            "type": "once",
            "text": "提醒",
            "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "group_id": "g1",
            "user_id": "",
        }
    ]
    scheduler._save = lambda: True

    asyncio.run(scheduler._tick())

    assert len(scheduler._tasks) == 1


def test_scheduler_send_exception_is_uncertain_not_failed(tmp_path, monkeypatch):
    import agent.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tmp_path / "tasks.json"))

    async def send(*_args, **_kwargs):
        raise RuntimeError("response lost after POST")

    scheduler = CronScheduler(
        send_group_msg=send,
        send_private_msg=send,
        llm_caller=AsyncMock(return_value="提醒一下～"),
        get_group_ids=lambda: [],
    )
    scheduler._tasks = [{
        "id": 2,
        "type": "once",
        "text": "提醒",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "group_id": "g1",
        "user_id": "",
    }]

    asyncio.run(scheduler._tick())

    assert scheduler._tasks[0]["status"] == "uncertain"


def test_scheduler_does_not_fire_when_sending_marker_cannot_be_persisted(
        tmp_path, monkeypatch):
    import agent.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "TASKS_FILE", str(tmp_path / "tasks.json"))
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    scheduler = CronScheduler(
        send_group_msg=send, send_private_msg=send,
        llm_caller=AsyncMock(return_value="提醒一下～"),
        get_group_ids=lambda: [],
    )
    scheduler._tasks = [{
        "id": 3, "type": "once", "text": "提醒",
        "fire_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "group_id": "g1", "user_id": "",
    }]
    scheduler._save = lambda: False

    asyncio.run(scheduler._tick())

    send.assert_not_awaited()
    assert scheduler._tasks[0].get("status") is None
    assert scheduler._tasks[0].get("last_attempt_at") is None


class _BirthdayStore:
    def __init__(self):
        self.values = {}

    def get_today_birthdays(self):
        return [{"qq_id": "u1", "nickname": "小蓝"}]

    def get_or_create_person(self, qq_id):
        return {"nickname": "小蓝"}

    def kv_get(self, key):
        return self.values.get(key)

    def kv_set(self, key, value):
        self.values[key] = value


def test_birthday_persists_and_binds_proactive_decision_before_send():
    """生日祝福必须先形成来源事实和终态决策，才允许碰 QQ 发送。"""
    order = []
    events = []
    decisions = []

    class Store(_BirthdayStore):
        def record_proactive_event(self, event):
            order.append("record")
            events.append(event)
            return {"status": "pending"}

        def claim_proactive_event(self, event_id, lease_token):
            order.append("claim")
            return True

        def mark_proactive_event_executing(self, event_id, lease_token):
            order.append("executing")
            return True

        def record_decision_run(self, run):
            order.append("decision")
            decisions.append(run)

        def mark_proactive_event_decided(self, event_id, lease_token, run_id):
            order.append("bind")
            return True

        def finish_proactive_event(self, event_id, lease_token, status, *, error_code=""):
            order.append("finish")
            return True

    async def llm(*_args, **_kwargs):
        order.append("llm")
        return "生日快乐～"

    async def send(*_args, **_kwargs):
        order.append("send")
        return SendResult(True, True, message_id=1)

    greeter = BirthdayGreeter(
        send_group_msg=send,
        store=Store(),
        llm_caller=llm,
        get_group_ids=lambda: ["g1"],
    )

    asyncio.run(greeter._check_birthdays())

    assert [(event.source, event.channel, event.target) for event in events] == [
        ("birthday", "group", "g1"),
    ]
    assert decisions[0].event_key == events[0].event_id
    assert decisions[0].status == "completed"
    assert order == ["record", "claim", "executing", "llm", "decision", "bind", "send", "finish"]


def test_birthday_not_marked_when_all_group_sends_uncertain():
    store = _BirthdayStore()

    async def send(*_args, **_kwargs):
        return _uncertain()

    greeter = BirthdayGreeter(
        send_group_msg=send,
        store=store,
        llm_caller=AsyncMock(return_value="生日快乐～"),
        get_group_ids=lambda: ["g1"],
    )

    asyncio.run(greeter._check_birthdays())

    state = json.loads(store.kv_get("birthday_sent"))
    assert state["sent"] == []


def test_birthday_send_exception_freezes_and_does_not_try_another_group():
    store = _BirthdayStore()
    attempts = []

    async def send(group_id, _message):
        attempts.append(group_id)
        raise RuntimeError("response lost after POST")

    greeter = BirthdayGreeter(
        send_group_msg=send,
        store=store,
        llm_caller=AsyncMock(return_value="生日快乐～"),
        get_group_ids=lambda: ["g1", "g2"],
    )

    asyncio.run(greeter._check_birthdays())

    state = json.loads(store.kv_get("birthday_sent"))
    assert state["sent"] == []
    assert state["uncertain"] == ["u1"]
    assert attempts == ["g1"]
