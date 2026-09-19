"""
引用解析修复测试（2026-08-15）

现场事故：主人引用糖糖 5 小时前的「糖糖有点想你了喵」回「你说的！」——
OneBot reply 段不带原文，旧逻辑只给占位提示，LLM 猜错被引用对象，直接开唱《稻香》。
_resolve_quote_prefix：按 [CQ:reply,id=xxx] 调 get_msg 拉原文注入。
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import (
    MessageHandler,
    _auto_window_quote_allowed,
    _fmt_quote_time,
)
from agent import protocols

BOT_QQ = "10000"


class TestAutoWindowQuoteGate:
    def test_only_recent_quote_of_bot_message_opens_window(self):
        now = 1000.0
        assert _auto_window_quote_allowed(
            auto_time=950.0,
            now=now,
            raw="[CQ:reply,id=1]回应",
            quoted={"sender_qq": BOT_QQ},
            bot_qq=BOT_QQ,
        )

    def test_quote_of_other_member_does_not_open_window(self):
        assert not _auto_window_quote_allowed(
            auto_time=950.0,
            now=1000.0,
            raw="[CQ:reply,id=1]回应",
            quoted={"sender_qq": "10001"},
            bot_qq=BOT_QQ,
        )

    def test_missing_quote_or_expired_window_does_not_open_window(self):
        assert not _auto_window_quote_allowed(
            auto_time=950.0,
            now=1000.0,
            raw="[CQ:reply,id=1]回应",
            quoted=None,
            bot_qq=BOT_QQ,
        )
        assert not _auto_window_quote_allowed(
            auto_time=800.0,
            now=1000.0,
            raw="[CQ:reply,id=1]回应",
            quoted={"sender_qq": BOT_QQ},
            bot_qq=BOT_QQ,
        )


def _make_handler():
    h = object.__new__(MessageHandler)
    h.bot_qq = BOT_QQ
    return h


class TestResolveQuotePrefix:
    def test_placeholder_resolved_via_get_msg(self):
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value={
            "sender_qq": "10001", "sender_nickname": "忽热忽冷",
            "content": "我睡觉去了，现在才起来",
        }))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "[回复了上面的消息]你说的！", "[CQ:reply,id=123456]你说的！", "忽热忽冷"))
        assert "引用的消息是" in prefix  # 2026-08-15：自然语言承载，不再是【注意】注释块
        assert "我睡觉去了" in prefix
        assert "忽热忽冷" in prefix
        assert clean == "你说的！"

    def test_own_message_flagged(self):
        """被引用的是糖糖自己说过的话 → 明确提示（「你说的！」场景）"""
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value={
            "sender_qq": BOT_QQ, "sender_nickname": "蓝发小妹",
            "content": "主人～在忙吗？糖糖有点想你了喵。",
        }))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "[回复了上面的消息]你说的！", "[CQ:reply,id=777]你说的！", "忽热忽冷"))
        assert "糖糖你之前自己说过的话" in prefix
        assert "糖糖有点想你了" in prefix
        assert clean == "你说的！"
        assert status == protocols.QUOTE_RESOLVED

    def test_fetch_failure_falls_back_to_ask_hint(self):
        """原文取不到 → 提示 LLM 结合上下文判断，无法确定就反问，不要猜（2026-08-15 兜底）"""
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value=None))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "[回复了上面的消息]你说的！", "[CQ:reply,id=999]你说的！", "忽热忽冷"))
        assert "被引用的原文没取到" in prefix
        assert "你说的是哪句" in prefix
        assert "不要猜" in prefix
        assert clean == "你说的！"
        assert status == protocols.QUOTE_MISSING

    def test_empty_content_falls_back_to_ask_hint(self):
        """get_msg 成功但内容为空（撤回/删除）→ 同样走反问兜底"""
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value={
            "sender_qq": "10001", "sender_nickname": "忽热忽冷", "content": "",
        }))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "[回复了上面的消息]你说的！", "[CQ:reply,id=999]你说的！", "忽热忽冷"))
        assert "你说的是哪句" in prefix

    def test_already_resolved_content_no_fetch(self):
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value=None))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "[引用内容：主人～在忙吗？]你说的！", "[CQ:reply,id=1]你说的！", "忽热忽冷"))
        assert "主人～在忙吗？" in prefix
        assert clean == "你说的！"
        assert status == protocols.QUOTE_RESOLVED
        assert not h.napcat.get_msg.called

    def test_reference_without_quote_asks_instead_of_guessing(self):
        """指代词 + 完全看不到引用（SnowLuma 私聊引用不转发，2026-08-15 事故#2）→
        注入「先问再答」，禁止猜"""
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value=None))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "调用你的知识库回答这个问题", "", "忽热忽冷"))
        assert "指代" in prefix
        assert "不要猜" in prefix
        assert "你指的是哪句" in prefix
        assert clean == "调用你的知识库回答这个问题"
        assert not h.napcat.get_msg.called  # 没有 id 可拉，不调 API

    def test_reference_word_list_no_false_positive(self):
        """普通消息不含指代词 → 不注入（「今天天气不错」「他来了」不触发）"""
        h = _make_handler()
        for plain in ("今天天气不错", "他来了", "其他安排呢", "好的呀"):
            prefix, _, status = asyncio.run(h._resolve_quote_prefix(plain, "", "忽热忽冷"))
            assert prefix == "", f"误报: {plain!r} → {prefix!r}"
            assert status == protocols.QUOTE_NONE

    def test_reference_without_quote_group_text_clean(self):
        """指代注入时 clean_text 保持原样（消息本身继续正常处理）"""
        h = _make_handler()
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "你怎么还在查记忆系统这件事上", "", "忽热忽冷"))
        assert prefix != ""
        assert clean == "你怎么还在查记忆系统这件事上"

    def test_no_quote_no_prefix(self):
        h = _make_handler()
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value=None))
        prefix, clean, status = asyncio.run(h._resolve_quote_prefix(
            "你说的！", "你说的！", "忽热忽冷"))
        assert prefix == ""
        assert clean == "你说的！"
        assert status == protocols.QUOTE_NONE

    def test_quote_time_injected(self):
        """引用原文注入时间戳——LLM 知道这是 5 小时前的话（2026-08-15 时间意识）"""
        h = _make_handler()
        ts = int(time.time()) - 5 * 3600
        h.napcat = types.SimpleNamespace(get_msg=AsyncMock(return_value={
            "sender_qq": "10001", "sender_nickname": "忽热忽冷",
            "content": "主人～在忙吗？糖糖有点想你了喵。", "time": ts,
        }))
        prefix, _, status = asyncio.run(h._resolve_quote_prefix(
            "[回复了上面的消息]你说的！", "[CQ:reply,id=777]你说的！", "忽热忽冷"))
        assert "5小时前" in prefix
        assert "（" in prefix  # 绝对时间 + 相对时间都给了
        assert status == protocols.QUOTE_RESOLVED


class TestFmtQuoteTime:
    def test_just_now(self):
        assert "刚刚" in _fmt_quote_time(int(time.time()) - 10)

    def test_minutes(self):
        assert "3分钟前" in _fmt_quote_time(int(time.time()) - 3 * 60)

    def test_hours(self):
        assert "7小时前" in _fmt_quote_time(int(time.time()) - 7 * 3600)

    def test_yesterday(self):
        assert "昨天" in _fmt_quote_time(int(time.time()) - 26 * 3600)

    def test_days(self):
        assert "3天前" in _fmt_quote_time(int(time.time()) - 3 * 86400)

    def test_zero_returns_empty(self):
        assert _fmt_quote_time(0) == ""
