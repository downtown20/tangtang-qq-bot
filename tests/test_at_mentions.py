"""2026-08-16 @ 代指解析——「你认识@一个包子 是什么时候」被答成主人自己的事故修复

根因：ws_client._extract_text 对 at 段「静默跳过」——@ 对象从消息文本里消失，
LLM 只能猜问的是谁。修复三层：at 段转 @名字 / @QQxxxx 兜底解析 / 提及身份映射块。
"""
from unittest.mock import MagicMock

import pytest

from agent import protocols
from agent.memory import MemorySystem


@pytest.fixture
def memory(store):
    return MemorySystem(store=store)


class TestExtractTextAtSegment:
    def _extract(self, segments):
        from napcat.ws_client import NapCatClient
        return NapCatClient._extract_text(object(), segments)

    def test_at_with_name_kept(self):
        out = self._extract([
            {"type": "at", "data": {"qq": "12801301", "name": "一个包子"}},
            {"type": "text", "data": {"text": " 你认识 是什么时候？"}},
        ])
        assert "@一个包子" in out

    def test_at_without_name_fallback_qq(self):
        out = self._extract([
            {"type": "at", "data": {"qq": "12801301"}},
            {"type": "text", "data": {"text": " 你认识 是什么时候？"}},
        ])
        assert "@QQ12801301" in out


class TestHandlerAtHelpers:
    def _handler(self, memory):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = memory
        h.bot_qq = "10000"
        return h

    def test_nick_for_qq_buffer_first(self, memory):
        h = self._handler(memory)
        memory.add_to_buffer("999", "12801301", "一个包子", "大家好")
        assert h._nick_for_qq("12801301", "999") == "一个包子"

    def test_nick_for_qq_people_fallback(self, memory):
        h = self._handler(memory)
        memory.store.get_or_create_person("12801301", "一个包子")
        assert h._nick_for_qq("12801301", "998") == "一个包子"

    def test_nick_for_qq_unknown_returns_qq(self, memory):
        h = self._handler(memory)
        assert h._nick_for_qq("1234567", "998") == "1234567"

    def test_mention_context_filters_and_maps(self, memory):
        h = self._handler(memory)
        memory.store.get_or_create_person("12801301", "一个包子")
        raw = "[CQ:at,qq=10000][CQ:at,qq=12801301][CQ:at,qq=9999]"
        out = h._mention_context(raw, speaker_qq="88888")
        assert "@一个包子(QQ12801301)" in out
        assert "10000" not in out      # @糖糖自己过滤
        assert "9999" not in out       # 未建档的随机号过滤（防 people 污染）

    def test_mention_context_empty(self, memory):
        h = self._handler(memory)
        assert h._mention_context("", "1") == ""


class TestGroupCardDisplay:
    def test_mention_context_prefers_card(self, memory):
        """群内 @ 映射优先群名片（2026-08-16 群昵称需求）"""
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = memory
        h.bot_qq = "10000"
        memory.store.get_or_create_person("12801301", "一个包子")
        memory.store.upsert_group_member("999", "12801301", card="包子老师")
        out = h._mention_context("[CQ:at,qq=12801301]", speaker_qq="88888", group_id="999")
        assert "@包子老师(QQ12801301)" in out

    def test_mention_context_falls_back_to_qq_nickname(self, memory):
        """无群名片回退 QQ 昵称"""
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = memory
        h.bot_qq = "10000"
        memory.store.get_or_create_person("12801301", "一个包子")
        out = h._mention_context("[CQ:at,qq=12801301]", speaker_qq="88888", group_id="999")
        assert "@一个包子(QQ12801301)" in out
