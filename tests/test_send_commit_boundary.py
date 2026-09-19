"""发送尝试与成功状态提交必须由真实发送结果分隔。"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from napcat.ws_client import SendResult


def _checked_send_harness(send_result):
    from agent.handler import MessageHandler
    from agent.self_check import ReplySelfCheck

    handler = object.__new__(MessageHandler)
    handler.self_check = ReplySelfCheck()
    handler.reply = SimpleNamespace(send=AsyncMock(return_value=send_result))
    handler.memory = SimpleNamespace(
        store=SimpleNamespace(record_reply_metric=MagicMock())
    )
    handler._allowed_groups = set()
    return handler


def test_failed_send_does_not_enter_sent_history():
    handler = _checked_send_harness(False)

    sent = asyncio.run(handler._checked_send("group", "group-1", "正常回复文本"))

    assert sent is False
    assert list(handler.self_check._sent_history.get("group-1", [])) == []
    handler.memory.store.record_reply_metric.assert_not_called()


def test_successful_send_commits_sent_history():
    handler = _checked_send_harness(True)

    sent = asyncio.run(handler._checked_send("group", "group-1", "正常回复文本"))

    assert sent is True
    assert list(handler.self_check._sent_history["group-1"]) == ["正常回复文本"]
    handler.memory.store.record_reply_metric.assert_called_once()


def test_confirmed_media_fallback_commits_only_the_actual_text_payload():
    handler = _checked_send_harness(True)
    handler.reply.last_confirmed_payload = "正文"

    sent = asyncio.run(handler._checked_send(
        "group", "group-1", "正文[CQ:image,file=broken.jpg]",
    ))

    assert sent is True
    assert list(handler.self_check._sent_history["group-1"]) == ["正文"]
    metric = handler.memory.store.record_reply_metric.call_args.kwargs
    assert metric["reply_len"] == 2


def test_unconfirmed_send_does_not_commit_sent_history_or_metrics():
    handler = _checked_send_harness(
        SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")
    )

    sent = asyncio.run(handler._checked_send("group", "group-1", "正常回复文本"))

    assert sent is False
    assert list(handler.self_check._sent_history.get("group-1", [])) == []
    handler.memory.store.record_reply_metric.assert_not_called()


def test_failed_self_check_fallback_is_not_recorded_when_send_fails():
    handler = _checked_send_harness(False)

    sent = asyncio.run(handler._checked_send("group", "group-1", "嗯"))

    assert sent is False
    assert list(handler.self_check._sent_history.get("group-1", [])) == []


def test_successful_self_check_fallback_is_recorded_as_the_actual_text():
    handler = _checked_send_harness(True)

    sent = asyncio.run(handler._checked_send("group", "group-1", "嗯"))

    assert sent is False
    history = list(handler.self_check._sent_history["group-1"])
    assert len(history) == 1
    assert history[0] != "嗯"


def test_reply_metric_write_does_not_block_event_loop():
    from agent.handler import MessageHandler
    from agent.self_check import ReplySelfCheck

    thread_ids = []

    def slow_metric(**_kwargs):
        thread_ids.append(threading.get_ident())
        time.sleep(0.08)

    handler = object.__new__(MessageHandler)
    handler.self_check = ReplySelfCheck()
    handler.reply = SimpleNamespace(send=AsyncMock(return_value=True))
    handler.memory = SimpleNamespace(
        store=SimpleNamespace(record_reply_metric=slow_metric)
    )
    handler._allowed_groups = set()
    main_thread = threading.get_ident()

    async def run():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            assert await handler._checked_send(
                "group", "group-1", "正常回复文本",
            ) is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks

    ticks = asyncio.run(run())
    assert ticks > 0
    assert thread_ids
    assert all(thread_id != main_thread for thread_id in thread_ids)


def test_partial_group_send_is_not_a_successful_commit(reply_pipeline, mock_napcat,
                                                         monkeypatch):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.side_effect = [True, False]
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    assert mock_napcat.send_group_message.await_count == 2


def test_split_send_stops_after_first_known_failure(reply_pipeline, mock_napcat):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.return_value = SendResult(
        False, False, error="BOT_ERROR", retryable=False,
    )

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    mock_napcat.send_group_message.assert_awaited_once_with("group-1", "第一段")
    assert reply_pipeline.last_send_result.delivery_state == "failed"


def test_split_send_stops_after_first_uncertain_result(reply_pipeline, mock_napcat):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.return_value = SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    )

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    mock_napcat.send_group_message.assert_awaited_once_with("group-1", "第一段")
    assert reply_pipeline.last_send_result.delivery_state == "uncertain"


def test_partial_split_delivery_is_quarantined_with_confirmed_ids(
        reply_pipeline, mock_napcat):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.side_effect = [
        SendResult(True, True, message_id=11),
        SendResult(False, False, error="BOT_ERROR", retryable=False),
    ]

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    detail = reply_pipeline.last_send_result
    assert detail.delivery_state == "uncertain"
    assert detail.uncertain is True
    assert detail.chunk_ids == (11,)
    assert "PARTIAL" in detail.error


def test_partial_split_delivery_is_never_marked_retryable(
        reply_pipeline, mock_napcat):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.side_effect = [
        SendResult(True, True, message_id=11),
        SendResult(False, False, error="NETWORK", retryable=True),
    ]

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    assert reply_pipeline.last_send_result.uncertain is True
    assert reply_pipeline.last_send_result.retryable is False


def test_send_exception_after_confirmed_segment_preserves_partial_evidence(
        reply_pipeline, mock_napcat):
    reply_pipeline.maybe_split_reply = lambda _reply: ["第一段", "第二段"]
    mock_napcat.send_group_message.side_effect = [
        SendResult(True, True, message_id=9),
        RuntimeError("response lost after POST"),
    ]

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    assert reply_pipeline.last_send_result.delivery_state == "uncertain"
    assert reply_pipeline.last_send_result.chunk_ids == (9,)


def test_unconfirmed_group_send_is_not_a_successful_pipeline_result(reply_pipeline,
                                                                      mock_napcat):
    mock_napcat.send_group_message.return_value = SendResult(
        True, False, error="MESSAGE_ID_UNCONFIRMED"
    )

    sent = asyncio.run(reply_pipeline.send("group", "group-1", "完整回复"))

    assert sent is False
    assert reply_pipeline.last_send_result.delivered is False


def test_media_network_failure_does_not_trigger_second_fallback_send(
        reply_pipeline, mock_napcat):
    mock_napcat.send_group_message.return_value = SendResult(
        False, False, error="NETWORK", retryable=True,
    )

    sent = asyncio.run(reply_pipeline.send(
        "group", "group-1", "正文[CQ:image,file=file:///tmp/a.jpg]",
    ))

    assert sent is False
    mock_napcat.send_group_message.assert_awaited_once()


def test_media_ambiguous_timeout_does_not_trigger_duplicate_fallback(
        reply_pipeline, mock_napcat):
    mock_napcat.send_group_message.return_value = SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    )

    sent = asyncio.run(reply_pipeline.send(
        "group", "group-1", "正文[CQ:image,file=file:///tmp/a.jpg]",
    ))

    assert sent is False
    assert reply_pipeline.last_send_result.delivery_state == "uncertain"
    mock_napcat.send_group_message.assert_awaited_once()


def test_confirmed_media_rejection_fallback_exposes_actual_confirmed_payload(
        reply_pipeline, mock_napcat, monkeypatch):
    mock_napcat.send_group_message.side_effect = [
        SendResult(False, False, error="BOT_ERROR", retryable=False),
        SendResult(True, True, message_id=7),
    ]
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())

    sent = asyncio.run(reply_pipeline.send(
        "group", "group-1", "正文[CQ:image,file=broken.jpg]",
    ))

    assert sent is True
    assert reply_pipeline.last_confirmed_payload == "正文"


def test_group_and_private_intimacy_commit_only_after_success():
    source = open("agent/handler.py", encoding="utf-8").read()

    assert "if ok:\n            self._add_intimacy_with_milestone(user_id, 1, group_id)" in source
    assert "if ok:\n            self._add_intimacy_with_milestone(user_id, 2)" in source
