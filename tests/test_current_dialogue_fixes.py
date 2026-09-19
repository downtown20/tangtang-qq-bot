"""当前对话问题的回归测试：历史证据、工具收尾和自我记忆隔离。"""

import asyncio
import types
from unittest.mock import AsyncMock

from agent.handler import MessageHandler
from agent.memory import MemorySystem


def test_history_queries_can_include_group_and_bot_messages(store):
    store.insert_chat("u1", "用户说要吃饭", group_id="g1", is_bot=False,
                      timestamp="2026-08-25 12:00:00")
    # 生产写法：bot 回复仍绑定对话对象 qq_id，以 is_bot_reply 区分说话者。
    store.insert_chat("u1", "我明天提醒你吃饭", group_id="g1", is_bot=True,
                      timestamp="2026-08-25 12:01:00")

    rows = store.get_messages_by_date(
        "u1", "2026-08-25", limit=20, include_bot_replies=True,
        group_id="g1", bot_qq="bot"
    )
    assert [r["message"] for r in rows] == ["用户说要吃饭", "我明天提醒你吃饭"]
    assert rows[1]["is_bot_reply"] is True

    hits = store.search_chat_keywords(
        "", ["提醒"], limit=20, group_id="g1", include_bot_replies=True
    )
    assert any(h["message"] == "我明天提醒你吃饭" and h["is_bot_reply"] for h in hits)


def test_keyword_history_projection_includes_chat_log_source_id(store):
    chat_id = store.insert_chat(
        "u1", "带来源的原文", group_id="g1", timestamp="2026-08-25 12:02:00",
    )
    hits = store.search_chat_keywords("u1", ["来源"], group_id="g1")

    assert hits and hits[0]["chat_log_id"] == chat_id


def test_self_memory_recall_is_scoped_to_target_user(store):
    memory = MemorySystem(store=store)
    source_a = store.insert_chat("甲", "糖糖给甲唱歌", group_id="g1", is_bot=True)
    source_b = store.insert_chat("乙", "糖糖给乙唱歌", group_id="g1", is_bot=True)
    memory.remember_self("bot", "给甲唱歌", target_qq="甲", group_id="g1",
                         evidence_ids=str(source_a))
    memory.remember_self("bot", "给乙唱歌", target_qq="乙", group_id="g1",
                         evidence_ids=str(source_b))

    rows = store.query_memories("bot", target_qq="甲")
    assert [r["value"] for r in rows] == ["给甲唱歌"]


def test_self_memory_rejects_missing_or_mismatched_source_evidence(store):
    memory = MemorySystem(store=store)

    assert memory.remember_self(
        "bot", "没有这条原话", target_qq="甲", group_id="g1", evidence_ids="999"
    ) == 0

    user_chat = store.insert_chat("甲", "用户说的话", group_id="g1", is_bot=False)
    assert memory.remember_self(
        "bot", "不能绑定用户原话", target_qq="甲", group_id="g1",
        evidence_ids=str(user_chat),
    ) == 0
    assert store.query_memories("bot", include_retracted=True) == []


def test_tool_progress_text_is_not_sent_as_final_reply(monkeypatch):
    class Resp:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class LLM:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 4:
                tc_next = [{"id": f"c{self.calls + 1}", "type": "function", "function": {
                    "name": "search_facts", "arguments": "{}"
                }}]
                return Resp({"choices": [{"message": {
                    "role": "assistant", "content": "让我换个方式，继续查记录：",
                    "tool_calls": tc_next,
                }}]})
            return Resp({"choices": [{"message": {
                "role": "assistant", "content": "最终核查结果：没有证据"
            }}]})

    tc = [{"id": "c1", "type": "function", "function": {
        "name": "search_facts", "arguments": "{}"
    }}]
    fake = types.SimpleNamespace(llm=LLM())
    fake._stream_deepseek = AsyncMock(return_value=(
        "让我换个方式，直接查记录：",
        {"role": "assistant", "content": "让我换个方式，直接查记录：", "tool_calls": tc},
        0.1,
    ))
    fake._execute_tool = AsyncMock(return_value="(没有找到相关记录)")
    import agent.skills as skills_mod
    monkeypatch.setattr(skills_mod, "get_method_type", lambda name: "agent")

    result = asyncio.run(MessageHandler._call_deepseek(
        fake, "sys", "user", tools=[{"function": {"name": "search_facts"}}],
        config={"api_key": "k", "model": "m", "base_url": "http://fake"}
    ))
    assert result == "最终核查结果：没有证据"
