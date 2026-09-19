"""图片分享配额、模糊发送与顺序游标的持久化边界。"""

import asyncio
import hashlib
from unittest.mock import AsyncMock

from agent.image_share import ImageShareScheduler, _contains_skip_text, _load_seq_state
from agent.store import Store
from onebot.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


def _scheduler(tmp_path, send, *, max_per_day=3):
    return ImageShareScheduler(
        config={
            "enabled": True,
            "groups": ["g1"],
            "use_local_only": True,
            "local_dir": str(tmp_path),
            "play_mode": "sequential",
            "max_per_day": max_per_day,
        },
        send_group_msg=send,
        get_group_ids=lambda: ["g1"],
    )


def test_confirmed_quota_survives_restart(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"image")
    send = AsyncMock(return_value=SendResult(True, True, message_id=1))
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())

    first = _scheduler(tmp_path, send, max_per_day=1)
    assert _run(first._share_one()) is True

    restarted = _scheduler(tmp_path, send, max_per_day=1)
    assert restarted._sent_today == 1
    assert _run(restarted._share_one()) is False
    assert send.await_count == 1


def test_uncertain_attempt_pauses_automatic_sharing_and_survives_restart(
        tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"image")
    send = AsyncMock(return_value=SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    ))
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())

    first = _scheduler(tmp_path, send)
    assert _run(first._share_one()) is False
    assert first._uncertain_today == 1
    assert _run(first._share_one()) is False

    restarted = _scheduler(tmp_path, send)
    assert restarted._uncertain_today == 1
    assert _run(restarted._share_one()) is False
    assert send.await_count == 1


def test_send_attempt_is_persisted_before_await_to_bound_crash_replay(
        tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"image")

    async def crash(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())
    first = _scheduler(tmp_path, crash, max_per_day=1)

    try:
        _run(first._share_one())
    except asyncio.CancelledError:
        pass

    restarted_send = AsyncMock(return_value=SendResult(True, True, message_id=2))
    restarted = _scheduler(tmp_path, restarted_send, max_per_day=1)
    assert restarted._attempts_today == 1
    assert _run(restarted._share_one()) is False
    restarted_send.assert_not_awaited()


def test_same_instance_inflight_send_is_not_started_twice(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"image")
    send = AsyncMock(return_value=SendResult(True, True, message_id=3))
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())
    scheduler = _scheduler(tmp_path, send, max_per_day=3)
    scheduler._inflight = True

    assert _run(scheduler._share_one()) is False
    send.assert_not_awaited()


def test_sequential_cursor_advances_only_after_confirmed_delivery(
        tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"a")
    (tmp_path / "b.png").write_bytes(b"b")
    send = AsyncMock(side_effect=[
        SendResult(False, False, error="BOT_ERROR"),
        SendResult(True, True, message_id=2),
    ])
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())
    scheduler = _scheduler(tmp_path, send, max_per_day=3)

    assert _run(scheduler._share_one()) is False
    assert _load_seq_state(tmp_path) in {None, 0}
    assert _run(scheduler._share_one()) is True
    assert _load_seq_state(tmp_path) == 1
    assert "a.png" in send.await_args_list[0].args[1]
    assert "a.png" in send.await_args_list[1].args[1]


def test_image_share_filter_is_case_insensitive_for_english_terms():
    """英文广告词大小写变化也必须被统一过滤。"""
    assert _contains_skip_text("APP promotion") is True
    assert _contains_skip_text("app promotion") is True
    assert _contains_skip_text("clean anime illustration") is False


def test_image_share_filter_tolerates_missing_or_non_string_descriptions():
    """第三方 API 缺少或错误类型的描述不应让筛选流程崩溃。"""
    assert _contains_skip_text(None) is False
    assert _contains_skip_text(12345) is False


def test_image_share_freezes_caption_in_single_image_child_and_skips_prior_receipts(
        tmp_path, monkeypatch):
    """每群冻结一个含配文的 image child，终局回执不得重放。"""
    image = tmp_path / "a.png"
    image.write_bytes(b"frozen-image")
    store = Store(str(tmp_path / "receipt.db"))
    send = AsyncMock(side_effect=[
        SendResult(True, True, message_id=101),
        SendResult(True, True, message_id=102),
    ])
    scheduler = ImageShareScheduler(
        config={
            "enabled": True,
            "groups": ["g1", "g2"],
            "use_local_only": True,
            "local_dir": str(tmp_path),
            "max_per_day": 3,
        },
        send_group_msg=send,
        get_group_ids=lambda: ["g1", "g2"],
        enrich=lambda text, group_id: f"{text}-{group_id}",
        receipt_store=store,
    )
    scheduler._pick_local_image = lambda: (image, "配文")
    scheduler._cleanup_old_files = lambda **_kwargs: None
    monkeypatch.setattr("agent.image_share.asyncio.sleep", AsyncMock())

    assert _run(scheduler._share_one()) is True

    expected_digest = hashlib.sha256(b"frozen-image").hexdigest()
    first_g1 = store.lease_action_receipts("g1")["receipts"]
    first_g2 = store.lease_action_receipts("g2")["receipts"]
    for group_id, receipts in (("g1", first_g1), ("g2", first_g2)):
        assert [(receipt["kind"], receipt["ordinal"]) for receipt in receipts] == [
            ("image", 0),
        ]
        assert receipts[0]["status"] == "confirmed"
        assert receipts[0]["identity_payload"] == {
            "asset_ref": "a.png",
            "asset_sha256": expected_digest,
            "asset_valid": True,
            "library_id": str(tmp_path.resolve()),
            "caption": f"配文-{group_id}",
        }
        assert receipts[0]["actual"] == {
            "delivery_kind": "image",
            "text": f"配文-{group_id}",
            "asset_ref": "a.png",
            "asset_sha256": expected_digest,
            "library_id": str(tmp_path.resolve()),
            "partial_delivery": False,
        }

    assert send.await_count == 2
    for group_id, call in zip(("g1", "g2"), send.await_args_list):
        assert call.args[0] == group_id
        assert call.args[1].startswith(f"配文-{group_id}\n[CQ:image,file=file:///")

    # 模拟调度器在同一已持久化 source 上恢复：receipt 是权威事实，不能再次
    # 触发任何平台发送。
    scheduler._attempts_today = 0
    scheduler._sent_today = 0
    assert _run(scheduler._share_one()) is True
    assert send.await_count == 2
