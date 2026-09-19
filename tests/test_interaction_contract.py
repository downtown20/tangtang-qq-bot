"""P0-1 统一交互契约：上下文、决策回合和主动事件只承载事实。"""

from dataclasses import FrozenInstanceError

import pytest


def test_chat_context_freezes_scope_history_and_source_events():
    from agent.interaction_contract import ChatContext

    context = ChatContext(
        scope_id="group:g1",
        channel="group",
        actor_id="u1",
        event_key="v2:group:g1:100:u1:9",
        current_message="你好",
        source_event_keys=("v2:group:g1:100:u1:9",),
        history_messages=({"role": "user", "content": "之前"},),
        trusted_memory_ids=(12,),
        media_refs=("image:abc",),
    )

    assert context.to_dict()["scope_id"] == "group:g1"
    assert context.to_dict()["source_event_keys"] == ["v2:group:g1:100:u1:9"]
    with pytest.raises(TypeError):
        context.history_messages[0]["content"] = "被改写"
    with pytest.raises(FrozenInstanceError):
        context.source_event_keys += ("other",)


def test_inbound_event_preserves_raw_and_typed_evidence():
    from agent.interaction_contract import InboundEvent

    event = InboundEvent(
        event_id="evt-1", event_key="v2:private:u1:100:u1:9",
        channel="private", scope_id="private:u1", actor_id="u1",
        raw_message="你好[CQ:image,file=a.jpg]", segments='[{"type":"text"}]',
        message_id=9, received_at="2026-08-29T16:00:00+08:00",
    )
    assert event.to_dict()["raw_message"].endswith("file=a.jpg]")
    with pytest.raises(ValueError, match="message_id"):
        InboundEvent(
            event_id="evt-2", event_key="k", channel="private",
            scope_id="private:u1", actor_id="u1", message_id=0,
        )


@pytest.mark.parametrize("channel,scope_id", [
    ("group", "private:u1"),
    ("private", "group:g1"),
])
def test_chat_context_rejects_cross_scope_identity(channel, scope_id):
    from agent.interaction_contract import ChatContext

    with pytest.raises(ValueError, match="scope"):
        ChatContext(
            scope_id=scope_id,
            channel=channel,
            actor_id="u1",
            event_key="event-1",
            current_message="hi",
        )


def test_decision_run_is_append_only_fact_and_validates_terminal_decision():
    from agent.interaction_contract import DecisionRun

    run = DecisionRun.start(
        run_id="run-1", event_key="event-1", scope_id="group:g1",
        correlation_id="cid-1", model="deepseek-chat",
    )
    completed = run.finish(decision="skip", tool_calls=("skip_response",))
    assert completed.status == "completed"
    assert completed.decision == "skip"
    assert completed.to_dict()["tool_calls"] == ["skip_response"]
    with pytest.raises(ValueError, match="decision"):
        run.finish(decision="reply", tool_calls=("skip_response",))
    with pytest.raises(ValueError, match="terminal"):
        completed.finish(decision="reply")
    failed = run.fail(error_code="llm_timeout")
    assert failed.status == "failed" and failed.decision == ""


def test_proactive_event_requires_durable_source_and_scope():
    from agent.interaction_contract import ProactiveEvent, build_proactive_event

    event = ProactiveEvent(
        event_id="pro-1",
        source="scheduler",
        scope_id="private:u1",
        channel="private",
        target="u1",
        payload={"task_id": "task-1"},
    )
    assert event.to_dict()["source"] == "scheduler"
    with pytest.raises(TypeError):
        event.payload["task_id"] = "tampered"
    with pytest.raises(ValueError, match="target"):
        ProactiveEvent(
            event_id="pro-2", source="autonomy", scope_id="private:u1",
            channel="private", target="u2",
        )


def test_build_proactive_event_defaults_idempotency_to_event_id():
    from agent.interaction_contract import build_proactive_event

    event = build_proactive_event(
        event_id="autonomy:group:g1:attempt-1",
        source="autonomy",
        channel="group",
        target="g1",
        payload={"kind": "group_initiative"},
    )

    assert event.idempotency_key == event.event_id
    assert event.to_dict()["payload"] == {"kind": "group_initiative"}


def test_context_builder_preserves_chat_context_boundary():
    from agent.context_builder import ContextBuilder
    from agent.interaction_contract import ChatContext, InboundEvent

    event = InboundEvent(
        event_id="v2:group:g1:10:u1:2", event_key="v2:group:g1:10:u1:2",
        channel="group", scope_id="group:g1", actor_id="u1",
        raw_message="hello", message_id=2, received_at="1970-01-01 00:00:10",
    )
    context = ChatContext(
        scope_id=event.scope_id, channel=event.channel, actor_id=event.actor_id,
        event_key=event.event_key, current_message="hello",
        history_messages=({"role": "user", "content": "before"},),
        trusted_memory_ids=(7,), received_at=event.received_at,
    )
    result = ContextBuilder().build(
        user_id="u1", nickname="tester", message="hello",
        system_prompt_base="base", history_messages=[], group_id="g1",
        chat_context=context,
    )

    assert result.chat_context is context
    assert result.chat_context.to_dict()["scope_id"] == "group:g1"


def test_context_builder_rejects_legacy_arguments_that_cross_chat_scope():
    from agent.context_builder import ContextBuilder
    from agent.interaction_contract import ChatContext

    context = ChatContext(
        scope_id="private:u1", channel="private", actor_id="u1",
        event_key="ephemeral:e1", current_message="hello",
    )
    with pytest.raises(ValueError, match="scope"):
        ContextBuilder().build(
            user_id="u1", nickname="tester", message="hello",
            system_prompt_base="base", history_messages=[], group_id="g1",
            chat_context=context,
        )


def test_handler_context_rejects_cross_scope_v2_source_key():
    """来源键带有 v2 作用域时，不能把另一群的事实绑定到本回合。"""
    from agent.handler import _chat_context_for_message

    msg = {
        "group_id": "g1", "user_id": "u1", "message_id": 22,
        "time": 100, "raw_message": "hello", "message": "hello",
        "_source_event_keys": ["v2:group:g2:100:u9:99"],
    }
    with pytest.raises(ValueError, match="source_event_keys scope"):
        _chat_context_for_message(
            "group", msg, current_message="hello", history_messages=[]
        )


@pytest.mark.parametrize("reply,responded,tools,expected", [
    ("好的", True, (), "reply"),
    ("", False, ("skip_response",), "skip"),
    ("", True, ("send_voice",), "action"),
    ("", True, (), ""),
])
def test_decision_outcome_only_records_observed_result(reply, responded, tools, expected):
    from agent.interaction_contract import classify_decision_outcome

    assert classify_decision_outcome(
        reply=reply, responded=responded, tool_calls=tools,
    ) == expected


def test_handler_llm_boundary_attaches_decision_run(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agent.handler import MessageHandler
    from agent.interaction_contract import ChatContext

    monkeypatch.setattr("agent.skills.list_skills", lambda: [])
    monkeypatch.setattr("agent.skills.build_tool_definitions", lambda: [])
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = False
    handler.cg_stickers = None
    handler.songs = None
    handler.llm_config = {"model": "test-model"}
    handler.memory = SimpleNamespace(store=None)

    async def fake_llm(_system, _user, **_kwargs):
        return "收到啦"

    handler._call_llm = fake_llm
    context = ChatContext(
        scope_id="group:g1", channel="group", actor_id="u1",
        event_key="event-1", current_message="你好",
    )
    reply, actions = asyncio.run(MessageHandler._call_llm_with_skills(
        handler, "system", "你好", voice_scope="g1", current_user="u1",
        chat_context=context,
    ))

    run = actions["_decision_run"]
    assert reply == "收到啦"
    assert run.status == "completed"
    assert run.decision == "reply"
    assert run.scope_id == "group:g1"
    assert run.event_key == "event-1"
    assert run.model == "test-model"


def test_handler_llm_boundary_persists_terminal_decision_run(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agent.handler import MessageHandler
    from agent.interaction_contract import ChatContext
    from agent.store import Store

    monkeypatch.setattr("agent.skills.list_skills", lambda: [])
    monkeypatch.setattr("agent.skills.build_tool_definitions", lambda: [])
    store = Store(str(tmp_path / "decision.db"))
    handler = object.__new__(MessageHandler)
    handler.voice_enabled = False
    handler.cg_stickers = None
    handler.songs = None
    handler.llm_config = {"model": "test-model"}
    handler.memory = SimpleNamespace(store=store)

    async def fake_llm(_system, _user, **_kwargs):
        return "收到啦"

    handler._call_llm = fake_llm
    context = ChatContext(
        scope_id="group:g1", channel="group", actor_id="u1",
        event_key="event-1", current_message="你好",
    )
    reply, actions = asyncio.run(MessageHandler._call_llm_with_skills(
        handler, "system", "你好", voice_scope="g1", current_user="u1",
        chat_context=context,
    ))

    run = actions["_decision_run"]
    assert reply == "收到啦"
    persisted = store.get_decision_run(run.run_id)
    assert persisted is not None
    assert persisted["run_id"] == run.run_id
    assert persisted["event_key"] == run.event_key
    assert persisted["scope_id"] == run.scope_id
    assert persisted["status"] == run.status
    assert persisted["decision"] == run.decision
    assert persisted["tool_calls"] == list(run.tool_calls)
