import pytest


def _event():
    from agent.interaction_contract import ProactiveEvent

    return ProactiveEvent(
        event_id="autonomy:group:g1:1",
        source="autonomy",
        scope_id="group:g1",
        channel="group",
        target="g1",
        payload={"kind": "group_initiative"},
    )


def test_proactive_decision_is_terminal_persisted_and_lease_bound(tmp_path):
    from agent.proactive_decision import (
        finalize_proactive_decision,
        start_proactive_decision,
    )
    from agent.store import Store

    store = Store(str(tmp_path / "decision.db"))
    event = _event()
    store.record_proactive_event(event)
    assert store.claim_proactive_event(event.event_id, "lease-1") is True
    assert store.mark_proactive_event_executing(event.event_id, "lease-1") is True

    started = start_proactive_decision(event, model="test")
    finished, bound = finalize_proactive_decision(
        store, event, "lease-1", started,
        reply="想和大家聊聊", responded=True,
    )

    assert bound is True
    assert finished.status == "completed"
    assert finished.decision == "reply"
    assert store.get_decision_run(finished.run_id)["scope_id"] == "group:g1"
    state = store.get_proactive_event(event.event_id)
    assert state["status"] == "decided"
    assert state["decision_run_id"] == finished.run_id


def test_proactive_silence_is_a_completed_skip_decision(tmp_path):
    from agent.proactive_decision import (
        finalize_proactive_decision,
        start_proactive_decision,
    )
    from agent.store import Store

    store = Store(str(tmp_path / "decision.db"))
    event = _event()
    store.record_proactive_event(event)
    assert store.claim_proactive_event(event.event_id, "lease-1") is True
    assert store.mark_proactive_event_executing(event.event_id, "lease-1") is True

    finished, bound = finalize_proactive_decision(
        store, event, "lease-1", start_proactive_decision(event),
        reply="", responded=False,
    )

    assert bound is True
    assert finished.status == "completed"
    assert finished.decision == "skip"
    assert store.get_proactive_event(event.event_id)["status"] == "decided"


def test_proactive_llm_failure_is_recorded_without_decided_state(tmp_path):
    from agent.proactive_decision import (
        finalize_proactive_decision,
        start_proactive_decision,
    )
    from agent.store import Store

    store = Store(str(tmp_path / "decision.db"))
    event = _event()
    store.record_proactive_event(event)
    assert store.claim_proactive_event(event.event_id, "lease-1") is True
    assert store.mark_proactive_event_executing(event.event_id, "lease-1") is True

    finished, bound = finalize_proactive_decision(
        store, event, "lease-1", start_proactive_decision(event),
        error_code="LLM_UNAVAILABLE",
    )

    assert bound is False
    assert finished.status == "failed"
    assert finished.error_code == "LLM_UNAVAILABLE"
    assert store.get_proactive_event(event.event_id)["status"] == "executing"
