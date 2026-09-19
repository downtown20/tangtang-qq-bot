"""贴图动作的送达结果不得把已尝试记成已成功。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent.handler import MessageHandler
from napcat.ws_client import SendResult


def test_sticker_batch_reports_confirmed_uncertain_and_failed_separately():
    async def scenario():
        handler = object.__new__(MessageHandler)
        handler.napcat = type("NapCatStub", (), {})()
        handler.napcat.send_group_message = AsyncMock(side_effect=[
            SendResult(True, True, message_id=1),
            SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED"),
            SendResult(False, False, error="BOT_ERROR"),
        ])

        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_sticker_batch(
                "100", ["a", "b", "c"], private=False,
            )
        return stats

    assert asyncio.run(scenario()) == {
        "attempted": 3,
        "confirmed": 1,
        "uncertain": 1,
        "failed": 1,
    }


def test_sticker_batch_classifies_ok_false_uncertain_as_uncertain():
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.napcat = SimpleNamespace(send_group_message=AsyncMock(
        return_value=SendResult(False, False, error="NETWORK_UNCERTAIN", uncertain=True)
    ))

    with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
        stats = asyncio.run(handler._send_sticker_batch("g1", ["[CQ:image,file=x]"], False))

    assert stats["confirmed"] == 0
    assert stats["uncertain"] == 1
    assert stats["failed"] == 0


def test_sticker_actions_freeze_each_asset_and_persist_receipts():
    async def scenario():
        handler = object.__new__(MessageHandler)
        handler.testing_mode = True
        handler._current_sticker_role = "michele"
        handler.stickers = SimpleNamespace(sticker_dir="stickers_michele")
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(
            send_group_message=AsyncMock(side_effect=[
                SendResult(True, True, message_id=11),
                SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED"),
            ])
        )
        intents = [{
            "emotion": "开心",
            "paths": ["asset-a", "asset-b"],
            "role_id": "michele",
            "library_id": "stickers_michele:v3",
        }]
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_sticker_actions(
                "100", ["asset-a", "asset-b"], private=False,
                action_source_id="100:42", conversation_user_id="u1",
                group_id="100", source_chat_id=42,
                sticker_intents=intents,
            )
        return stats, handler

    stats, handler = asyncio.run(scenario())
    assert stats["attempted"] == 2
    assert stats["confirmed"] == 1
    assert stats["uncertain"] == 1
    assert stats["failed"] == 0
    assert handler._persist_terminal_action_receipt.call_count == 2
    envelopes = [call.args[0] for call in handler._persist_terminal_action_receipt.call_args_list]
    assert [e.ordinal for e in envelopes] == [0, 1]
    assert [e.payload["asset_ref"] for e in envelopes] == ["asset-a", "asset-b"]
    assert all(e.payload["role_id"] == "michele" for e in envelopes)
    assert all(e.payload["library_id"] == "stickers_michele:v3" for e in envelopes)


def test_sticker_actions_continue_after_dispatch_exception():
    async def scenario():
        handler = object.__new__(MessageHandler)
        handler.testing_mode = True
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir="stickers")
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        sender = AsyncMock(side_effect=[
            RuntimeError("gateway disconnected"),
            SendResult(True, True, message_id=12),
        ])
        handler.napcat = SimpleNamespace(send_group_message=sender)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_sticker_actions(
                "100", ["asset-a", "asset-b"], private=False,
                action_source_id="100:43", conversation_user_id="u1",
                group_id="100", source_chat_id=43,
                sticker_intents=[],
            )
        return stats, handler

    stats, handler = asyncio.run(scenario())
    assert stats["attempted"] == 2
    assert stats["confirmed"] == 1
    assert stats["uncertain"] == 1
    assert stats["failed"] == 0
    assert handler._persist_terminal_action_receipt.call_count == 2


def test_sticker_actions_reject_asset_outside_library_without_sending(tmp_path):
    async def scenario():
        library = tmp_path / "stickers"
        outside = tmp_path / "outside.png"
        library.mkdir()
        outside.write_bytes(b"not in library")
        cq = f"[CQ:image,file=file:///{outside.as_posix()}]"
        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=library)
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        sender = AsyncMock(return_value=SendResult(True, True, message_id=99))
        handler.napcat = SimpleNamespace(send_group_message=sender)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_sticker_actions(
                "100", [cq], private=False,
                action_source_id="100:44", conversation_user_id="u1",
                group_id="100", source_chat_id=44,
                sticker_intents=[{
                    "emotion": "开心", "paths": [cq],
                    "role_id": "default", "library_id": str(library),
                }],
            )
        return stats, sender, handler

    stats, sender, handler = asyncio.run(scenario())
    assert stats == {"attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1}
    sender.assert_not_awaited()
    receipt = handler._persist_terminal_action_receipt.call_args.args[0]
    assert receipt.payload["asset_valid"] is False
    assert receipt.payload["asset_sha256"] == ""


def test_sticker_actions_empty_input_is_a_noop():
    async def scenario():
        handler = object.__new__(MessageHandler)
        handler._send_sticker_batch = AsyncMock()
        return await handler._send_sticker_actions(
            "100", [], private=False, action_source_id="100:45",
        )

    assert asyncio.run(scenario()) == {
        "attempted": 0, "confirmed": 0, "uncertain": 0, "failed": 0,
    }


def test_image_actions_freeze_file_identity_and_fail_closed_on_asset_drift(tmp_path):
    """图片 child 使用 frozen asset；同路径文件被替换后不得继续 QQ 发送。"""
    async def scenario():
        image = tmp_path / "draw.png"
        image.write_bytes(b"first-image")
        digest = __import__("hashlib").sha256(image.read_bytes()).hexdigest()
        handler = object.__new__(MessageHandler)
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        sender = AsyncMock(return_value=SendResult(True, True, message_id=601))
        handler.napcat = SimpleNamespace(send_group_message=sender)
        image_action = {
            "asset_ref": "draw.png", "asset_sha256": digest,
            "asset_valid": True, "library_id": str(tmp_path.resolve()),
            "source_skill": "generate_image", "ordinal": 2,
        }
        first = await handler._send_image_actions(
            "g1", [image_action], private=False, action_source_id="g1:501",
        )
        image.write_bytes(b"replacement-image")
        second = await handler._send_image_actions(
            "g1", [image_action], private=False, action_source_id="g1:501",
        )
        return first, second, sender, handler

    first, second, sender, handler = asyncio.run(scenario())
    assert first == {"attempted": 1, "confirmed": 1, "uncertain": 0, "failed": 0}
    assert second == {"attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1}
    assert sender.await_count == 1
    envelope = handler._persist_terminal_action_receipt.call_args_list[0].args[0]
    assert envelope.kind == "image"
    assert envelope.ordinal == 2


def test_sticker_actions_reject_non_image_transport_reference():
    async def scenario():
        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir="stickers")
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        sender = AsyncMock(return_value=SendResult(True, True, message_id=100))
        handler.napcat = SimpleNamespace(send_group_message=sender)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_sticker_actions(
                "100", ["[CQ:record,file=file:///tmp/x.mp3]"], private=False,
                action_source_id="100:46", conversation_user_id="u1",
                group_id="100", source_chat_id=46,
            )
        return stats, sender

    stats, sender = asyncio.run(scenario())
    assert stats["failed"] == 1
    sender.assert_not_awaited()


def test_cg_actions_use_stable_asset_and_receipt_for_group(tmp_path):
    """CG 贴图走 ActionPlan，且同一来源不会因重试随机换图。"""
    async def scenario():
        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        first = cg_dir / "a.png"
        second = cg_dir / "b.png"
        first.write_bytes(b"cg-a")
        second.write_bytes(b"cg-b")

        sent = []

        async def _send(group_id, message, **kwargs):
            sent.append((group_id, message, kwargs))
            return SendResult(True, True, message_id=101)

        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir,
            _cache=[str(first), str(second)],
            has_stickers=lambda: True,
            random_sticker=lambda: (_ for _ in ()).throw(
                AssertionError("CG must not re-randomize when cache is available")
            ),
        )
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(send_group_message=_send)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:source-1",
                conversation_user_id="u1", group_id="g1", source_chat_id=77,
            )
        return stats, sent, handler

    stats, sent, handler = asyncio.run(scenario())
    assert stats == {"attempted": 1, "confirmed": 1, "uncertain": 0, "failed": 0}
    assert len(sent) == 1
    group_id, message, kwargs = sent[0]
    assert group_id == "g1"
    assert message.startswith("[CQ:image,file=file:///")
    template = kwargs["receipt_template"]
    assert template["kind"] == "sticker"
    assert template["identity_payload"]["library_id"] == "stickers_cg"
    assert template["identity_payload"]["asset_ref"] in {"a.png", "b.png"}
    assert len(template["identity_payload"]["asset_sha256"]) == 64
    receipt = handler._persist_terminal_action_receipt.call_args.args[0]
    assert receipt.payload["library_id"] == "stickers_cg"
    assert receipt.payload["asset_sha256"] == template["identity_payload"]["asset_sha256"]


def test_cg_actions_private_uncertain_is_frozen_and_not_replayed(tmp_path):
    """CG 私聊异常只产生 uncertain child，不再次随机/重复发送。"""
    async def scenario():
        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        asset = cg_dir / "only.webp"
        asset.write_bytes(b"cg-only")
        calls = []

        async def _send(_user_id, _message, **_kwargs):
            calls.append(1)
            raise RuntimeError("response lost")

        handler = object.__new__(MessageHandler)
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir,
            _cache=[str(asset)],
            has_stickers=lambda: True,
            random_sticker=lambda: (_ for _ in ()).throw(
                AssertionError("CG must not call random_sticker")
            ),
        )
        handler._current_sticker_role = "default"
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(send_private_message=_send)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            first = await handler._send_cg_actions(
                "u1", private=True, action_source_id="_private_u1:source-2",
                conversation_user_id="u1",
            )
            second = await handler._send_cg_actions(
                "u1", private=True, action_source_id="_private_u1:source-2",
                conversation_user_id="u1",
            )
        return first, second, calls, handler

    first, second, calls, handler = asyncio.run(scenario())
    assert first["uncertain"] == 1 and first["failed"] == 0
    assert second["uncertain"] == 1 and second["failed"] == 0
    # 当前 fake 没有 mailbox store，故无法在本测试中读取 prior receipt；
    # 仍验证每次产生的冻结身份完全相同，真实 Store 会据此跳过重发。
    receipts = [call.args[0] for call in handler._persist_terminal_action_receipt.call_args_list]
    assert len(receipts) == 2
    assert receipts[0].action_id == receipts[1].action_id
    assert receipts[0].payload["asset_ref"] == receipts[1].payload["asset_ref"]
    assert calls == [1, 1]


def test_cg_action_prior_confirmed_receipt_skips_dispatch(tmp_path):
    """重启后同一来源读取 confirmed receipt，不重复投递 CG。"""
    async def scenario():
        from agent.action_contract import finalize_action_receipt_template

        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        asset = cg_dir / "stable.png"
        asset.write_bytes(b"cg-stable")
        sent = []
        prior = {}

        async def _send(_group_id, _message, **kwargs):
            sent.append(kwargs)
            return SendResult(True, True, message_id=202)

        def _get_receipts(_scope_id, action_ids):
            return {action_id: prior[action_id] for action_id in action_ids if action_id in prior}

        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir, _cache=[str(asset)], has_stickers=lambda: True,
            random_sticker=lambda: (_ for _ in ()).throw(AssertionError("must use stable cache")),
        )
        handler.memory = SimpleNamespace(store=SimpleNamespace(get_action_receipts=_get_receipts))
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(send_group_message=_send)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            first = await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:source-3",
                conversation_user_id="u1", group_id="g1",
            )
            template = sent[0]["receipt_template"]
            prior[template["action_id"]] = finalize_action_receipt_template(
                template, status="confirmed", message_ids=[202],
            )
            second = await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:source-3",
                conversation_user_id="u1", group_id="g1",
            )
        return first, second, sent

    first, second, sent = asyncio.run(scenario())
    assert first["confirmed"] == 1
    assert second["confirmed"] == 1
    assert len(sent) == 1  # prior confirmed child is terminal; no second QQ POST


def test_cg_action_invalid_asset_is_failed_without_transport(tmp_path):
    async def scenario():
        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"outside")
        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir, _cache=[str(outside)], has_stickers=lambda: True,
            random_sticker=lambda: None,
        )
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        sender = AsyncMock(return_value=SendResult(True, True, message_id=303))
        handler.napcat = SimpleNamespace(send_private_message=sender)
        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            stats = await handler._send_cg_actions(
                "u1", private=True, action_source_id="_private_u1:source-4",
                conversation_user_id="u1",
            )
        receipt = handler._persist_terminal_action_receipt.call_args.args[0]
        return stats, receipt

    stats, receipt = asyncio.run(scenario())
    assert stats == {"attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1}
    assert receipt.payload["library_id"] == "stickers_cg"
    assert receipt.payload["asset_valid"] is False


def test_cg_source_mapping_survives_cache_changes_and_reuses_frozen_asset(tmp_path):
    """同一 source 已冻结 CG 后，图库增删不得将其漂移到另一资产。"""
    async def scenario():
        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        first = cg_dir / "first.png"
        second = cg_dir / "second.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        values = {}
        sent = []

        class Store:
            def kv_get(self, key):
                return values.get(key)

            def kv_set(self, key, value):
                values[key] = value

        async def _send(_group_id, message, **kwargs):
            sent.append((message, kwargs["receipt_template"]))
            return SendResult(True, True, message_id=515)

        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir, _cache=[str(first), str(second)],
            has_stickers=lambda: True,
            random_sticker=lambda: (_ for _ in ()).throw(AssertionError("must use cache")),
        )
        handler.memory = SimpleNamespace(store=Store())
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(send_group_message=_send)

        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:cache-drift",
                conversation_user_id="u1", group_id="g1",
            )
            selected = sent[0][0]
            handler.cg_stickers._cache = [
                str(second if selected.endswith("first.png]") else first)
            ]
            await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:cache-drift",
                conversation_user_id="u1", group_id="g1",
            )
        return values, sent

    values, sent = asyncio.run(scenario())
    assert values, "首次选择必须持久化 source→asset 冻结映射"
    assert sent[1][0] == sent[0][0]
    assert sent[1][1]["action_id"] == sent[0][1]["action_id"]


def test_cg_source_mapping_rejects_changed_frozen_asset_instead_of_drifting(tmp_path):
    """冻结文件同路径被替换后，不能借 cache 改投另一张 CG。"""
    async def scenario():
        cg_dir = tmp_path / "stickers_cg"
        cg_dir.mkdir()
        first = cg_dir / "first.png"
        second = cg_dir / "second.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        values = {}
        sent = []

        class Store:
            def kv_get(self, key):
                return values.get(key)

            def kv_set(self, key, value):
                values[key] = value

        async def _send(_group_id, message, **kwargs):
            sent.append(message)
            return SendResult(True, True, message_id=616)

        handler = object.__new__(MessageHandler)
        handler._current_sticker_role = "default"
        handler.stickers = SimpleNamespace(sticker_dir=tmp_path / "stickers")
        handler.cg_stickers = SimpleNamespace(
            sticker_dir=cg_dir, _cache=[str(first), str(second)],
            has_stickers=lambda: True,
            random_sticker=lambda: (_ for _ in ()).throw(AssertionError("must use cache")),
        )
        handler.memory = SimpleNamespace(store=Store())
        handler._persist_terminal_action_receipt = Mock(return_value=True)
        handler.napcat = SimpleNamespace(send_group_message=_send)

        with patch("agent.handler.asyncio.sleep", new=AsyncMock()):
            await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:asset-replaced",
                conversation_user_id="u1", group_id="g1",
            )
            selected = first if sent[0].endswith("first.png]") else second
            other = second if selected == first else first
            selected.write_bytes(b"replacement")
            handler.cg_stickers._cache = [str(other)]
            stats = await handler._send_cg_actions(
                "g1", private=False, action_source_id="g1:asset-replaced",
                conversation_user_id="u1", group_id="g1",
            )
        return stats, sent

    stats, sent = asyncio.run(scenario())
    assert stats == {"attempted": 1, "confirmed": 0, "uncertain": 0, "failed": 1}
    assert len(sent) == 1
