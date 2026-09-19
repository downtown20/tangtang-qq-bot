"""记忆访问边界回归测试。

P0A 目标：身份主体和会话作用域必须在所有记忆/聊天工具进入数据层前统一校验。
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import numpy as np

from agent.memory import MemorySystem
from agent.memory_access import authorize_memory_access


def _handler(store):
    from agent.handler import MessageHandler

    handler = object.__new__(MessageHandler)
    handler.owner_qq = "10001"
    handler.bot_qq = "90000"
    handler.memory = MemorySystem(store=store)
    handler.embed_engine = None
    handler.reranker = None
    handler.reflection = None
    handler.napcat = AsyncMock()
    handler.self_state = MagicMock()
    return handler


class TestMemoryAccessPolicy:
    def test_private_raw_history_is_self_only_for_ordinary_user(self):
        decision = authorize_memory_access(
            "get_recent_messages",
            {"subject_qq": "30003"},
            scope_id="_private_20002",
            current_user="20002",
            owner_qq="10001",
        )
        assert decision is not None
        assert decision.allowed is False
        assert decision.disclosure == "denied"

    def test_private_trusted_memory_of_third_party_is_recognition_only(self):
        decision = authorize_memory_access(
            "search_facts",
            {"subject_qq": "30003"},
            scope_id="_private_20002",
            current_user="20002",
            owner_qq="10001",
        )
        assert decision is not None
        assert decision.allowed is True
        assert decision.subject_qq == "30003"
        assert decision.disclosure == "recognition"

    def test_owner_can_read_explicit_third_party_subject(self):
        decision = authorize_memory_access(
            "search_chat_history",
            {"subject_qq": "30003", "query": "昨天"},
            scope_id="_private_10001",
            current_user="10001",
            owner_qq="10001",
        )
        assert decision is not None
        assert decision.allowed is True
        assert decision.subject_qq == "30003"
        assert decision.disclosure == "full"

    def test_group_subject_defaults_to_current_user_and_current_group(self):
        decision = authorize_memory_access(
            "search_memories",
            {"query": "喜欢什么"},
            scope_id="778899",
            current_user="20002",
            owner_qq="10001",
        )
        assert decision is not None
        assert decision.allowed is True
        assert decision.subject_qq == "20002"
        assert decision.group_id == "778899"

    def test_group_resource_cannot_switch_group_id(self):
        decision = authorize_memory_access(
            "get_group_activity",
            {"group_id": "other-group"},
            scope_id="778899",
            current_user="20002",
            owner_qq="10001",
        )
        assert decision is not None
        assert decision.allowed is False


class TestScopedStoreQueries:
    def test_trusted_memories_can_be_limited_to_current_group(self, store):
        for group_id, value in (
            ("group-a", "当前群事实"),
            ("group-b", "其他群秘密"),
            ("", "私聊秘密"),
        ):
            chat_id = store.insert_chat("20002", value, group_id=group_id)
            store.insert_memory(
                "20002", "fact", value, origin="extracted",
                source_group_id=group_id, evidence_ids=str(chat_id),
                evidence_quote=value, claim_type="stated",
            )

        rows = store.query_memories(
            "20002", trusted_only=True, source_group_id="group-a",
        )

        assert [row["value"] for row in rows] == ["当前群事实"]

    def test_recent_messages_can_be_limited_to_current_group(self, store):
        store.insert_chat("20002", "当前群消息", group_id="group-a")
        store.insert_chat("20002", "其他群秘密", group_id="group-b")
        store.insert_chat("20002", "私聊秘密", group_id="")

        rows = store.get_user_recent_messages("20002", limit=10, group_id="group-a")

        assert any("当前群消息" in row for row in rows)
        assert all("其他群秘密" not in row and "私聊秘密" not in row for row in rows)

    def test_message_count_can_be_limited_to_current_group(self, store):
        store.insert_chat("20002", "当前群一", group_id="group-a")
        store.insert_chat("20002", "当前群二", group_id="group-a")
        store.insert_chat("20002", "其他群", group_id="group-b")

        stats = store.count_user_messages("20002", group_id="group-a")

        assert stats["total_messages"] == 2

    def test_last_conversation_can_be_limited_to_current_group(self, store):
        store.insert_chat("20002", "当前群提问", group_id="group-a")
        store.insert_chat("20002", "当前群回答", group_id="group-a", is_bot=True)
        store.insert_chat("20002", "其他群秘密", group_id="group-b")
        store.insert_chat("20002", "其他群回答", group_id="group-b", is_bot=True)

        rows = store.get_last_conversation(
            "90000", "20002", limit=20, group_id="group-a"
        )

        joined = "\n".join(rows)
        assert "当前群提问" in joined and "当前群回答" in joined
        assert "其他群秘密" not in joined and "其他群回答" not in joined

    def test_messages_by_date_uses_production_bot_reply_ownership(self, store):
        store.insert_chat(
            "20002", "用户提问", group_id="group-a", is_bot=False,
            timestamp="2026-08-26 10:00:00",
        )
        store.insert_chat(
            "20002", "糖糖回答", group_id="group-a", is_bot=True,
            timestamp="2026-08-26 10:01:00",
        )
        store.insert_chat(
            "30003", "第三人回答", group_id="group-a", is_bot=True,
            timestamp="2026-08-26 10:02:00",
        )

        rows = store.get_messages_by_date(
            "20002", "2026-08-26", include_bot_replies=True,
            group_id="group-a", bot_qq="90000",
        )

        assert [row["message"] for row in rows] == ["用户提问", "糖糖回答"]

    def test_group_correction_only_changes_current_scope(self, store):
        group_id = store.insert_memory(
            "20002", "fact", "住在旧地址", origin="manual",
            source_group_id="group-a",
        )
        private_id = store.insert_memory(
            "20002", "fact", "住在旧地址", origin="manual",
            source_group_id="",
        )

        result = MemorySystem(store=store).correct_memory(
            "20002", "旧地址", "住在新地址",
            source_group_id="group-a",
        )

        assert result["matched"] == 1
        rows = {
            row["id"]: row for row in store.query_memories(
                "20002", include_retracted=True,
            )
        }
        assert rows[group_id]["status"] == "retracted"
        assert rows[private_id]["status"] == "active"
        corrected = store.get_memory_by_id(result["corrected_id"])
        assert corrected["source_group_id"] == "group-a"


class TestAutonomyMemoryScope:
    def test_self_memory_recall_uses_bounded_worker(self, monkeypatch):
        """自忆召回必须经过统一有界阻塞门，避免默认线程池失控。"""
        from agent.handler import MessageHandler

        handler = object.__new__(MessageHandler)
        handler.bot_qq = "90000"
        handler.embed_engine = None
        calls = []

        async def bounded(operation, func, *args, **kwargs):
            calls.append(operation)
            return func(*args, **{
                key: value for key, value in kwargs.items()
                if key not in {"logger", "log_prefix"}
            })

        monkeypatch.setattr("agent.handler.run_bounded_blocking", bounded)
        handler.memory = SimpleNamespace(
            recall=lambda *args, **kwargs: [],
            _format_self_memories=lambda values: "",
        )

        # 仅检查异步方法的门控契约；不触碰真实数据库。
        assert inspect.iscoroutinefunction(MessageHandler._get_self_memory_context)
        result = asyncio.run(handler._get_self_memory_context(
            target_qq="20002", source_group_id="group-a",
        ))

        assert result == ""
        assert calls == ["memory.self_recall.recent"]

    def test_group_initiative_topics_do_not_leak_private_memory(self, store):
        private_chat = store.insert_chat(
            "20002", "只在私聊说的医疗秘密", group_id="",
        )
        store.insert_memory(
            "20002", "event", "只在私聊说的医疗秘密",
            cognitive="episodic", origin="extracted",
            source_group_id="", evidence_ids=str(private_chat),
            evidence_quote="只在私聊说的医疗秘密", claim_type="stated",
        )
        handler = _handler(store)
        handler.memory.add_to_buffer("group-a", "20002", "测试用户", "群里聊点别的")

        topics = asyncio.run(handler._build_initiative_topics("group-a"))

        assert "医疗秘密" not in topics

    def test_self_memory_context_is_bound_to_user_and_conversation_scope(self, store):
        for group_id, value in (
            ("group-a", "只在A群答应的提醒"),
            ("", "只在私聊答应的诊断提醒"),
        ):
            chat_id = store.insert_chat(
                "20002", value, group_id=group_id, is_bot=True,
            )
            store.insert_memory(
                "90000", "promise", value, origin="self",
                target_qq="20002", source_group_id=group_id,
                evidence_ids=str(chat_id),
            )
        handler = _handler(store)

        group_context = asyncio.run(handler._get_self_memory_context(
            target_qq="20002", message="提醒", source_group_id="group-a",
        ))
        private_context = asyncio.run(handler._get_self_memory_context(
            target_qq="20002", message="提醒", source_group_id="",
        ))

        assert "只在A群" in group_context and "诊断提醒" not in group_context
        assert "诊断提醒" in private_context and "只在A群" not in private_context

    def test_group_reply_path_passes_group_scope_to_raw_user_history(self):
        import inspect
        from agent.handler import MessageHandler

        source = inspect.getsource(MessageHandler.handle_group_message)

        assert (
            "get_user_recent_messages, user_id, 15, group_id" in source
        ), "群回复路径读取历史时必须显式传当前 group_id"


class TestMemoryToolAccessIntegration:
    def test_group_memory_search_does_not_leak_other_scopes(self, store):
        for group_id, value in (
            ("group-a", "当前群喜欢苹果"),
            ("group-b", "其他群喜欢苹果的秘密"),
            ("", "私聊喜欢苹果的秘密"),
        ):
            chat_id = store.insert_chat("20002", value, group_id=group_id)
            store.insert_memory(
                "20002", "like", value, origin="extracted",
                source_group_id=group_id, evidence_ids=str(chat_id),
                evidence_quote=value, claim_type="stated",
            )
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "search_memories",
            {"query": "喜欢苹果"},
            scope_id="group-a",
            current_user="20002",
        ))

        assert "当前群喜欢苹果" in out
        assert "其他群" not in out and "私聊" not in out

    def test_group_history_never_falls_back_to_private_vector_index(self, store):
        class Embed:
            ready = True

            @staticmethod
            def encode(_text):
                return np.asarray([1.0, 0.0], dtype=np.float32)

        private_chat = store.insert_chat("20002", "私聊里的秘密")
        store.index_chat(
            private_chat, "20002", "私聊里的秘密",
            np.asarray([1.0, 0.0], dtype=np.float32),
        )
        handler = _handler(store)
        handler.embed_engine = Embed()

        out = asyncio.run(handler._execute_tool(
            "search_chat_history",
            {"query": "私聊里的秘密"},
            scope_id="group-a",
            current_user="20002",
        ))

        assert "私聊里的秘密" not in out

    def test_chat_history_does_not_rederive_subject_from_free_text(self, store):
        handler = _handler(store)
        handler._search_people = lambda _query: "LEAKED_THIRD_PARTY_PROFILE"

        out = asyncio.run(handler._execute_tool(
            "search_chat_history",
            {"query": "第三人"},
            scope_id="group-a",
            current_user="20002",
        ))

        assert "LEAKED_THIRD_PARTY_PROFILE" not in out

    def test_private_recent_messages_cannot_read_third_party(self, store):
        store.insert_chat("30003", "第三人的私聊秘密")
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "get_recent_messages",
            {"subject_qq": "30003"},
            scope_id="_private_20002",
            current_user="20002",
        ))

        assert "隐私" in out
        assert "第三人的私聊秘密" not in out

    def test_high_intimacy_does_not_bypass_third_party_fact_privacy(self, store):
        store.get_or_create_person("20002", "来访者")
        store.set_intimacy("20002", 100)
        store.get_or_create_person("30003", "第三人")
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "search_facts",
            {"subject_qq": "30003", "query": "全部"},
            scope_id="_private_20002",
            current_user="20002",
        ))

        assert "详细" in out and "隐私" in out

    def test_group_recent_messages_only_returns_current_group(self, store):
        store.insert_chat("20002", "当前群内容", group_id="group-a")
        store.insert_chat("20002", "其他群秘密", group_id="group-b")
        store.insert_chat("20002", "私聊秘密")
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "get_recent_messages",
            {"subject_qq": "20002", "limit": 10},
            scope_id="group-a",
            current_user="20002",
        ))

        assert "当前群内容" in out
        assert "其他群秘密" not in out and "私聊秘密" not in out

    def test_relation_nickname_cannot_bypass_private_third_party_gate(self, store):
        store.get_or_create_person("30003", "第三人")
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "search_relations",
            {"name": "第三人"},
            scope_id="_private_20002",
            current_user="20002",
        ))

        assert "隐私" in out

    def test_group_activity_cannot_switch_to_another_group(self, store):
        handler = _handler(store)

        out = asyncio.run(handler._execute_tool(
            "get_group_activity",
            {"group_id": "group-b"},
            scope_id="group-a",
            current_user="20002",
        ))

        assert "当前群" in out or "不能" in out
        handler.napcat.get_group_activity.assert_not_called()

    def test_group_cast_does_not_embed_third_party_profile(self, store):
        store.get_or_create_person("30003", "第三人")
        store.update_person("30003", notes="第三人的私密画像")
        handler = _handler(store)

        cast = handler._build_cast_context([
            {"qq_id": "30003", "nickname": "第三人"},
        ], "group-a")

        assert "第三人" in cast
        assert "私密画像" not in cast

    def test_memory_command_searches_only_callers_trusted_memories(self, store):
        from agent.handler_commands import CommandRouter

        memory = MemorySystem(store=store)
        store.insert_memory("20002", "fact", "本人确认的月桂记忆", origin="manual")
        store.insert_memory("30003", "fact", "第三人的月桂秘密", origin="manual")
        router = CommandRouter(SimpleNamespace(
            memory=memory, embed_engine=None, reranker=None,
        ))

        out = asyncio.run(router._cmd_memory("20002", "搜索 月桂"))

        assert "本人确认的月桂记忆" in out
        assert "第三人的月桂秘密" not in out

    def test_no_relevant_candidates_does_not_inject_profile(self, store):
        store.get_or_create_person("20002", "本人")
        store.update_person(
            "20002", notes="不相关的合成画像",
            notes_trust_level="manual",
        )
        handler = _handler(store)

        out, selected = asyncio.run(handler._build_semantic_memories(
            "20002", [], "量子纠缠",
        ))

        assert out == ""
        assert selected is None

    def test_get_message_accepts_current_group_and_rejects_other_group(self, store):
        handler = _handler(store)
        handler.napcat.get_msg.side_effect = [
            {
                "message_id": 1, "group_id": "group-a", "sender_nickname": "甲",
                "sender_card": "", "content": "当前群原文",
            },
            {
                "message_id": 2, "group_id": "group-b", "sender_nickname": "乙",
                "sender_card": "", "content": "其他群秘密",
            },
        ]

        same = asyncio.run(handler._execute_tool(
            "get_message", {"message_id": 1},
            scope_id="group-a", current_user="20002",
        ))
        other = asyncio.run(handler._execute_tool(
            "get_message", {"message_id": 2},
            scope_id="group-a", current_user="20002",
        ))

        assert "当前群原文" in same
        assert "其他群秘密" not in other and "不能跨群" in other


def test_napcat_get_msg_preserves_group_scope():
    from napcat.ws_client import NapCatClient

    client = object.__new__(NapCatClient)
    client._call_api = AsyncMock(return_value={
        "status": "ok",
        "data": {
            "message_id": 7, "group_id": 778899,
            "sender": {"user_id": 20002, "nickname": "甲"},
            "message": [{"type": "text", "data": {"text": "原文"}}],
        },
    })
    client._extract_text = lambda _message: "原文"

    result = asyncio.run(client.get_msg(7))

    assert result["group_id"] == "778899"
