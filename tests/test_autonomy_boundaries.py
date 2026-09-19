"""P1 自治窗口和经验持久化边界。"""

import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock

from agent.conversation_tracker import ConversationTracker, FADE_PERIOD, GRACE_PERIOD
from agent.handler_autonomy import AutonomyMixin
from agent.self_state import TangTangSelf
from napcat.ws_client import SendResult


def test_autonomy_releases_claim_when_executing_transition_fails():
    from agent.interaction_contract import build_proactive_event

    released = []

    class Store:
        def get_proactive_event(self, _event_id):
            return {"status": "pending"}

        def claim_proactive_event(self, _event_id, _lease):
            return True

        def mark_proactive_event_executing(self, _event_id, _lease):
            return False

        def release_proactive_event_claim(self, event_id, lease):
            released.append((event_id, lease))
            return True

    event = build_proactive_event(
        event_id="autonomy:g1:claim-failure", source="autonomy",
        channel="group", target="g1",
    )
    fake = types.SimpleNamespace(memory=types.SimpleNamespace(store=Store()))
    available, lease = AutonomyMixin._claim_proactive_event(fake, event)

    assert available is True and lease == ""
    assert released and released[0][0] == event.event_id


def test_autonomy_reaction_protocol_consumes_prompt_enums_exactly():
    async def scenario():
        outcomes = ["warmly_received", "lukewarm", "ignored", "rejected"]
        fake = types.SimpleNamespace(
            memory=types.SimpleNamespace(short_term={
                "g1": [{"nickname": "u", "message": "reply", "time": "12:00:01"}],
            }),
            _auto_cold={},
            _last_auto_msg={"g1": "hello"},
            self_state=types.SimpleNamespace(
                drives=types.SimpleNamespace(release=lambda *_args: None),
            ),
            _save_state_kv=lambda *_args: None,
        )
        for outcome in outcomes:
            fake._call_llm_light = types.SimpleNamespace()

            async def call(_system, _prompt, value=outcome):
                return value

            fake._call_llm_light = call
            await AutonomyMixin._judge_autonomous_cold(fake, "g1", time.mktime(
                time.strptime("2026-08-26 12:00:00", "%Y-%m-%d %H:%M:%S")
            ))

        # 温暖重置，敷衍/无视递增，拒绝进入暂停哨位。
        assert fake._auto_cold["g1"] == 99

    asyncio.run(scenario())


def test_expired_window_is_not_deleted_during_grace_or_fade(monkeypatch):
    tracker = ConversationTracker(None)
    tracker.force_engage("u1", "g1")
    key = tracker._key("g1", "u1")
    tracker._engaged[key]["expires_at"] = 100.0

    monkeypatch.setattr("agent.conversation_tracker.time.time", lambda: 100.0 + GRACE_PERIOD + 1)
    tracker._cleanup()
    assert key in tracker._engaged

    monkeypatch.setattr("agent.conversation_tracker.time.time", lambda: 100.0 + FADE_PERIOD + 1)
    tracker._cleanup()
    assert key not in tracker._engaged


def test_window_capacity_supports_thousands_of_active_users():
    tracker = ConversationTracker(None)
    for i in range(1000):
        tracker.force_engage(f"u{i}", "g1")
    assert len(tracker._engaged) == 1000


def test_non_reply_experience_is_persisted_without_relationship_update(tmp_path, monkeypatch):
    state_file = tmp_path / "self.json"
    monkeypatch.setattr(TangTangSelf, "STATE_FILE", str(state_file))
    state = TangTangSelf(bot_qq="bot")
    for _ in range(5):
        state.accumulate_experience("u", "测试", reply_sent=False, message="被跳过的消息")

    assert state_file.exists()
    restored = TangTangSelf(bot_qq="bot")
    assert restored.get_recent_experiences(1)[0]["message"] == "被跳过的消息"


def test_group_autonomy_uncertain_delivery_sets_persistent_cooldown(monkeypatch):
    async def scenario():
        drives = types.SimpleNamespace(
            get_dominant=lambda: types.SimpleNamespace(
                name="social", label="社交", value=0.9, threshold=0.7,
            ),
            natural_decay=lambda *_args: None,
            get_drive_context=lambda *_args: "想说话",
            release=lambda *_args: None,
            drives={"social": types.SimpleNamespace(value=0.9)},
        )
        save = MagicMock(return_value=True)
        fake = types.SimpleNamespace(
            self_state=types.SimpleNamespace(tick=lambda: None, drives=drives),
            _last_initiative_time=0.0, _initiative_cooldown=0.0,
            private_interjection=False, autonomous_speech=True,
            active_interjection=True, _pick_initiative_group=lambda: "g1",
            _auto_pending={}, _auto_cold={}, _auto_uncertain={},
            _conv_tracker=types.SimpleNamespace(
                has_active_window=lambda _gid: False, _auto_initiated={},
            ),
            memory=types.SimpleNamespace(short_term={}, log_chat=MagicMock()),
            personality=types.SimpleNamespace(_cached_base="你是糖糖"),
            _build_initiative_topics=AsyncMock(return_value=""),
            _get_diary_fragment=lambda **_kwargs: "",
            _call_llm_light=AsyncMock(return_value="想和大家聊聊"),
            _enrich_reply=lambda text: text,
            _checked_send=AsyncMock(return_value=False),
            reply=types.SimpleNamespace(last_send_result=SendResult(
                False, False, error="NETWORK_UNCERTAIN", uncertain=True,
            )),
            _save_state_kv=save,
            _set_last_initiative=MagicMock(),
        )
        events = []
        fake._proactive_event_sink = events.append
        fake.memory.add_to_buffer = MagicMock()

        await AutonomyMixin._check_autonomous_action(fake)

        assert "g1" in fake._auto_uncertain
        save.assert_any_call("state:auto_uncertain", fake._auto_uncertain)
        fake._set_last_initiative.assert_called_once()
        assert len(events) == 1
        assert events[0].source == "autonomy"
        assert events[0].scope_id == "group:g1"
        assert events[0].payload["kind"] == "group_initiative"

    monkeypatch.setattr("agent.handler_autonomy.datetime", types.SimpleNamespace(
        now=lambda: types.SimpleNamespace(hour=12, strftime=lambda _fmt: "12:00"),
    ))
    asyncio.run(scenario())


def test_group_autonomy_persists_and_finishes_proactive_event(tmp_path, monkeypatch):
    from agent.handler_autonomy import AutonomyMixin
    from agent.store import Store

    async def scenario():
        drives = types.SimpleNamespace(
            get_dominant=lambda: types.SimpleNamespace(
                name="social", label="社交", value=0.9, threshold=0.7,
            ),
            natural_decay=lambda *_args: None,
            get_drive_context=lambda *_args: "想说话",
            release=lambda *_args: None,
            drives={"social": types.SimpleNamespace(value=0.9)},
        )
        store = Store(str(tmp_path / "autonomy.db"))
        events = []

        def sink(event):
            events.append(event)
            return store.record_proactive_event(event)

        fake = types.SimpleNamespace(
            self_state=types.SimpleNamespace(tick=lambda: None, drives=drives),
            _last_initiative_time=0.0, _initiative_cooldown=0.0,
            private_interjection=False, autonomous_speech=True,
            active_interjection=True, _pick_initiative_group=lambda: "g1",
            _auto_pending={}, _auto_cold={}, _auto_uncertain={},
            _conv_tracker=types.SimpleNamespace(
                has_active_window=lambda _gid: False, _auto_initiated={},
            ),
            memory=types.SimpleNamespace(
                store=store, short_term={}, log_chat=MagicMock(),
            ),
            personality=types.SimpleNamespace(_cached_base="你是糖糖"),
            _build_initiative_topics=AsyncMock(return_value=""),
            _get_diary_fragment=lambda **_kwargs: "",
            _call_llm_light=AsyncMock(return_value="想和大家聊聊"),
            _enrich_reply=lambda text: text,
            _checked_send=AsyncMock(return_value=True),
            reply=types.SimpleNamespace(last_send_result=SendResult(
                True, True, message_id=123,
            )),
            _save_state_kv=MagicMock(),
            _set_last_initiative=MagicMock(),
            bot_qq="bot", config={"bot": {"name": "糖糖"}},
        )
        fake._proactive_event_sink = sink
        fake._claim_proactive_event = types.MethodType(
            AutonomyMixin._claim_proactive_event, fake,
        )
        fake._finish_proactive_event = types.MethodType(
            AutonomyMixin._finish_proactive_event, fake,
        )
        fake.memory.add_to_buffer = MagicMock()

        await AutonomyMixin._check_autonomous_action(fake)

        assert len(events) == 1
        state = store.get_proactive_event(events[0].event_id)
        assert state["status"] == "confirmed"
        assert state["attempts"] == 1
        assert state["decision_run_id"]
        decision = store.get_decision_run(state["decision_run_id"])
        assert decision["event_key"] == events[0].event_id
        assert decision["scope_id"] == "group:g1"
        assert decision["decision"] == "reply"

    monkeypatch.setattr("agent.handler_autonomy.datetime", types.SimpleNamespace(
        now=lambda: types.SimpleNamespace(hour=12, strftime=lambda _fmt: "12:00"),
    ))
    asyncio.run(scenario())


def test_confirmed_autonomy_send_keeps_terminal_state_when_history_write_fails(monkeypatch):
    """发送已确认后，聊天史回写失败不能让自治回合重新变成可重试状态。"""
    async def scenario():
        drives = types.SimpleNamespace(
            get_dominant=lambda: types.SimpleNamespace(
                name="social", label="社交", value=0.9, threshold=0.7,
            ),
            natural_decay=lambda *_args: None,
            get_drive_context=lambda *_args: "想说话",
            release=lambda *_args: None,
            drives={"social": types.SimpleNamespace(value=0.9)},
        )
        save = MagicMock(return_value=True)
        setter = MagicMock()
        fake = types.SimpleNamespace(
            self_state=types.SimpleNamespace(tick=lambda: None, drives=drives),
            _last_initiative_time=0.0, _initiative_cooldown=0.0,
            private_interjection=False, autonomous_speech=True,
            active_interjection=True, _pick_initiative_group=lambda: "g1",
            _auto_pending={}, _auto_cold={}, _auto_uncertain={},
            _conv_tracker=types.SimpleNamespace(
                has_active_window=lambda _gid: False, _auto_initiated={},
            ),
            memory=types.SimpleNamespace(
                short_term={},
                log_chat=MagicMock(side_effect=RuntimeError("sqlite locked")),
            ),
            personality=types.SimpleNamespace(_cached_base="你是糖糖"),
            _build_initiative_topics=AsyncMock(return_value=""),
            _get_diary_fragment=lambda **_kwargs: "",
            _call_llm_light=AsyncMock(return_value="想和大家聊聊"),
            _enrich_reply=lambda text: text,
            _checked_send=AsyncMock(return_value=True),
            reply=types.SimpleNamespace(last_send_result=SendResult(
                True, True, message_id=123,
            )),
            _save_state_kv=save,
            _set_last_initiative=setter,
            bot_qq="bot", config={"bot": {"name": "糖糖"}},
        )
        fake._proactive_event_sink = lambda _event: None
        fake.memory.add_to_buffer = MagicMock()

        await AutonomyMixin._check_autonomous_action(fake)

        assert fake._auto_pending["g1"] > 0
        setter.assert_called_once()
        assert fake._auto_uncertain == {}

    monkeypatch.setattr("agent.handler_autonomy.datetime", types.SimpleNamespace(
        now=lambda: types.SimpleNamespace(hour=12, strftime=lambda _fmt: "12:00"),
    ))
    asyncio.run(scenario())
