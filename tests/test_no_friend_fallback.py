"""
非好友私聊兜底测试（2026-08-15）

现场：自治插话选中「我是白熊二」→ QQ 拒绝（result=16，请先添加对方为好友）。
根因：send_private_message 的群临时会话兜底需要 group_id，自治路径没传。
修复三件：pipeline 传 group_id / 失败记录 6h 非好友冷却 / 自治 gate 跳过冷却对象。
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline
from onebot.ws_client import NapCatClient


def _pipeline(napcat):
    p = object.__new__(ReplyPipeline)
    p.napcat = napcat
    p.reply = None
    return p


class TestGroupIdThreading:
    def test_private_send_passes_group_id(self):
        """非好友时 napcat 需要 group_id 才能走群临时会话兜底"""
        napcat = types.SimpleNamespace(send_private_message=AsyncMock(return_value=True),
                                       send_group_message=AsyncMock(return_value=True))
        p = _pipeline(napcat)
        asyncio.run(p.send("private", "123", "你好呀", group_id="456"))
        kwargs = napcat.send_private_message.call_args.kwargs
        assert kwargs.get("group_id") == "456", kwargs

    def test_group_send_ignores_group_id_param(self):
        napcat = types.SimpleNamespace(send_group_message=AsyncMock(return_value=True))
        p = _pipeline(napcat)
        asyncio.run(p.send("group", "456", "大家好"))
        assert napcat.send_group_message.called


class TestNoFriendCooldown:
    def _client(self):
        cl = NapCatClient()
        cl._qq_online = True
        cl._ws_connected = True
        return cl

    def test_friend_rejection_marks_cooldown(self):
        cl = self._client()
        cl._call_api = AsyncMock(return_value={
            "status": "failed", "retcode": 100,
            "msg": "send private message rejected: result=16 err=发送失败，请先添加对方为好友",
            "wording": "send private message rejected: result=16 err=发送失败，请先添加对方为好友",
        })
        ok = asyncio.run(cl.send_private_message("10002", "醒着没？"))
        assert bool(ok) is False
        assert "10002" in cl._no_friend_until
        assert cl._no_friend_until["10002"] > 0

    def test_other_error_no_cooldown(self):
        """非好友之外的失败（如风控）不标记冷却——冷却表只服务好友问题"""
        cl = self._client()
        cl._call_api = AsyncMock(return_value={
            "status": "failed", "retcode": 200, "msg": "rate limited", "wording": "rate limited",
        })
        asyncio.run(cl.send_private_message("10002", "hi"))
        assert "10002" not in cl._no_friend_until


class TestGateSkipsNoFriend:
    def _handler(self, napcat):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.napcat = napcat
        h.bot_qq = "10000"
        h._private_blacklist = set()
        h.reply_only_to = []
        h._last_private_init = {}
        h._care_due = {}
        # 2026-08-16 观察回路：意愿分住在关系场
        h.self_state = types.SimpleNamespace(relationships={})
        h._pending_seek = {}
        return h

    def test_gate_skips_cooldown_user(self):
        import time as _t
        napcat = types.SimpleNamespace(
            _no_friend_until={"10002": _t.time() + 3600})
        h = self._handler(napcat)
        assert h._private_gate_ok("10002", _t.time()) is False

    def test_gate_passes_normal_user(self):
        import time as _t
        napcat = types.SimpleNamespace(_no_friend_until={})
        h = self._handler(napcat)
        # 沉默 3 天 → 72h 冷却，从未插话过 → 通过
        h._days_silent = lambda qq, now: 3.0
        assert h._private_gate_ok("123", _t.time()) is True
