"""生产运行观察器回归：无样本不假绿，只读、按区间增量判定。"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace


def _snapshot(*, metrics=None, backlog=0, total_messages=0,
              oldest_at="", queue=None, outbox=None,
              eligible_backlog=None, eligible_oldest_at=""):
    backlog_snapshot = {
        "backlog_messages": backlog,
        "backlog_users": 1 if backlog else 0,
        "total_user_messages": total_messages,
        "oldest_at": oldest_at,
        "over_30_users": 0,
    }
    if eligible_backlog is not None:
        backlog_snapshot.update({
            "eligible_messages": eligible_backlog,
            "eligible_users": 1 if eligible_backlog else 0,
            "eligible_oldest_at": eligible_oldest_at,
            "deferred_messages": max(0, backlog - eligible_backlog),
        })
    return {
        "captured_at": "2026-08-27T16:00:00+08:00",
        "database": {
            "ok": True,
            "metrics": metrics or {},
            "backlog": backlog_snapshot,
            "queue": queue or {"total_open": 0, "dead": 0},
            "outbox": outbox or {
                "open": 0, "uncertain": 0, "dead": 0,
            },
            "memory": {
                "active": 100,
                "trusted": 20,
                "trust_levels": {"verified": 20},
                "reasoning_leaks_today": 0,
                "duplicate_groups_today": 0,
            },
        },
        "services": {
            "napcat": {"required": True, "up": True},
            "reverse_ws": {"required": True, "up": True},
        },
        "errors": [],
    }


def test_port_probe_uses_local_listener_table_without_opening_socket(monkeypatch):
    """反向 WS 探针不能用裸 TCP 连接制造 400 日志。"""
    from tools import runtime_observer

    class Listener:
        laddr = SimpleNamespace(port=3001)
        status = "LISTEN"

    fake_psutil = SimpleNamespace(net_connections=lambda kind: [Listener()])
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    def fail_socket(*_args, **_kwargs):
        raise AssertionError("listener-table probe must not open a socket")

    monkeypatch.setattr(runtime_observer.socket, "create_connection", fail_socket)
    assert runtime_observer._port_open(3001) is True


def test_zero_traffic_is_insufficient_never_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["dialog"]["status"] == "INSUFFICIENT"
    assert report["domains"]["voice"]["status"] == "INSUFFICIENT"
    assert report["domains"]["vision"]["status"] == "INSUFFICIENT"
    assert report["domains"]["sticker"]["status"] == "INSUFFICIENT"
    assert report["domains"]["llm_tools"]["status"] == "INSUFFICIENT"
    assert report["domains"]["runtime_logs"]["status"] == "INSUFFICIENT"
    assert report["domains"]["background_tasks"]["status"] == "INSUFFICIENT"
    assert report["overall"] == "INSUFFICIENT"
    assert report["coverage"] == "INSUFFICIENT"


def test_memory_evidence_exposes_active_untrusted_inventory():
    """未验证记忆会被 trusted_only 排除，但存量必须在观察报告中可见。"""
    from tools.runtime_observer import evaluate_observation

    report = evaluate_observation(
        [_snapshot(), _snapshot()], {"events": {}},
    )
    evidence = report["domains"]["memory"]["evidence"]

    assert evidence["trusted_memories"] == 20
    assert evidence["untrusted_active_memories"] == 80


def test_proactive_decision_domain_exposes_no_sample_as_insufficient():
    from tools.runtime_observer import evaluate_observation

    report = evaluate_observation(
        [_snapshot(), _snapshot()], {"events": {}},
    )
    domain = report["domains"]["proactive_decisions"]
    assert domain["status"] == "INSUFFICIENT"
    assert domain["evidence"]["decision_runs_delta"] == 0
    assert domain["evidence"]["proactive_events_delta"] == 0


def test_proactive_decision_domain_passes_bound_sample():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T16:05:00+08:00"
    before["database"].update({
        "decision_runs": {
            "completed": 0, "failed": 0, "total": 0,
            "unknown": 0, "corrupt": 0, "schema_missing": False,
        },
        "proactive": {
            "total": 0, "pending": 0, "claimed": 0, "executing": 0,
            "decided": 0, "confirmed": 0, "failed": 0, "uncertain": 0,
            "skipped": 0, "unknown": 0, "invalid_payload": 0,
            "open": 0, "schema_missing": False,
        },
    })
    after["database"].update({
        "decision_runs": {
            "completed": 1, "failed": 0, "total": 1,
            "unknown": 0, "corrupt": 0, "schema_missing": False,
        },
        "proactive": {
            "total": 1, "pending": 0, "claimed": 0, "executing": 0,
            "decided": 0, "confirmed": 1, "failed": 0, "uncertain": 0,
            "bound": 1,
            "proactive_decision_runs": 1,
            "skipped": 0, "unknown": 0, "invalid_payload": 0,
            "open": 0, "schema_missing": False,
        },
    })

    domain = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["proactive_decisions"]
    assert domain["status"] == "PASS"
    assert domain["evidence"]["decision_runs_delta"] == 1
    assert domain["evidence"]["proactive_decision_runs_delta"] == 1
    assert domain["evidence"]["proactive_events_delta"] == 1
    assert domain["evidence"]["proactive_bound_delta"] == 1


def test_proactive_decision_domain_ignores_foreground_decision_run():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    before["database"]["decision_runs"] = {
        "completed": 0, "failed": 0, "total": 0,
        "unknown": 0, "corrupt": 0, "schema_missing": False,
    }
    after["database"]["decision_runs"] = {
        "completed": 1, "failed": 0, "total": 1,
        "unknown": 0, "corrupt": 0, "schema_missing": False,
    }
    before["database"]["proactive"] = {
        "total": 0, "bound": 0, "proactive_decision_runs": 0,
        "unknown": 0, "invalid_payload": 0, "schema_missing": False,
    }
    after["database"]["proactive"] = {
        "total": 0, "bound": 0, "proactive_decision_runs": 0,
        "unknown": 0, "invalid_payload": 0, "schema_missing": False,
    }

    domain = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["proactive_decisions"]
    assert domain["status"] == "INSUFFICIENT"
    assert domain["evidence"]["decision_runs_delta"] == 1
    assert domain["evidence"]["proactive_decision_runs_delta"] == 0


def test_proactive_decision_domain_fails_on_new_uncertain_or_corrupt_rows():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T16:05:00+08:00"
    before["database"]["decision_runs"] = {
        "completed": 0, "failed": 0, "total": 0,
        "unknown": 0, "corrupt": 0, "schema_missing": False,
    }
    before["database"]["proactive"] = {
        "total": 0, "uncertain": 0, "unknown": 0,
        "proactive_decision_runs": 0,
        "proactive_decision_runs_corrupt": 0,
        "proactive_decision_runs_unknown": 0,
        "invalid_payload": 0, "schema_missing": False,
    }
    after["database"]["decision_runs"] = {
        "completed": 0, "failed": 0, "total": 1,
        "unknown": 0, "corrupt": 1, "schema_missing": False,
    }
    after["database"]["proactive"] = {
        "total": 1, "uncertain": 1, "unknown": 0,
        "proactive_decision_runs": 1,
        "proactive_decision_runs_corrupt": 1,
        "proactive_decision_runs_unknown": 0,
        "invalid_payload": 0, "schema_missing": False,
    }

    domain = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["proactive_decisions"]
    assert domain["status"] == "FAIL"
    assert "new_decision_run_corrupt" in domain["evidence"]["violations"]
    assert "new_proactive_uncertain" in domain["evidence"]["violations"]


def test_dialog_uses_persisted_window_funnel_metrics():
    """窗口验收必须看到候选→决策漏斗，不能只数 reply/skip 日志。"""
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "window_candidate": 10,
        "window_entry_direct": 3,
        "window_continuation": 7,
        "window_routed": 2,
        "window_decisions_total": 8,
        "window_decisions_reply": 6,
        "window_decisions_skip": 2,
        "window_fade_after_silence": 1,
    })
    after = _snapshot(metrics={
        "window_candidate": 14,
        "window_entry_direct": 4,
        "window_continuation": 10,
        "window_routed": 3,
        "window_decisions_total": 12,
        "window_decisions_reply": 9,
        "window_decisions_skip": 3,
        "window_fade_after_silence": 1,
    })
    after["captured_at"] = "2026-08-27T16:36:00+08:00"

    report = evaluate_observation([before, after], {"events": {
        "window_reply": 3, "window_skip": 1,
    }})
    dialog = report["domains"]["dialog"]

    assert dialog["status"] == "PASS"
    assert dialog["evidence"]["candidate"] == 4
    assert dialog["evidence"]["entry_direct"] == 1
    assert dialog["evidence"]["continuation"] == 3
    assert dialog["evidence"]["routed"] == 1
    assert dialog["evidence"]["decisions_total"] == 4
    assert dialog["evidence"]["reply"] == 3
    assert dialog["evidence"]["skip"] == 1


def test_dialog_funnel_inconsistency_is_not_allowed_to_pass():
    """决策数超过候选数意味着指标接线/窗口协议漂移，必须显式失败。"""
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "window_candidate": 5,
        "window_entry_direct": 2,
        "window_continuation": 3,
        "window_decisions_total": 5,
        "window_decisions_reply": 4,
        "window_decisions_skip": 1,
    })
    after = _snapshot(metrics={
        "window_candidate": 6,
        "window_entry_direct": 3,
        "window_continuation": 3,
        "window_decisions_total": 7,
        "window_decisions_reply": 5,
        "window_decisions_skip": 2,
    })
    after["captured_at"] = "2026-08-27T17:00:00+08:00"

    dialog = evaluate_observation(
        [before, after], {"events": {"window_reply": 1, "window_skip": 1}},
    )["domains"]["dialog"]

    assert dialog["status"] == "FAIL"
    assert "decisions_exceed_candidates" in dialog["evidence"]["violations"]


def test_dialog_durable_schema_missing_cannot_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "window_candidate": 1,
        "window_entry_direct": 1,
        "window_decisions_total": 1,
        "window_decisions_reply": 1,
    })
    after = _snapshot(metrics={
        "window_candidate": 2,
        "window_entry_direct": 2,
        "window_decisions_total": 2,
        "window_decisions_reply": 2,
    })
    after["database"]["window"] = {
        "event_count": 0, "max_id": 0, "invalid_events": 0,
        "future_events": 0, "schema_missing": True,
    }
    dialog = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["dialog"]
    assert dialog["status"] == "FAIL"
    assert dialog["evidence"]["durable_window"]["schema_missing"] is True


def test_optional_untouched_features_do_not_block_core_health_pass():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot(backlog=1, total_messages=10,
                       oldest_at="2026-08-27 15:00:00", metrics={
        "gateway_send_attempts": 10,
        "gateway_send_confirmed": 10,
    })
    after = _snapshot(backlog=0, total_messages=11, metrics={
        "gateway_send_attempts": 11,
        "gateway_send_confirmed": 11,
    })
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-2 | 糖糖.SnowLuma | INFO | 📩 [私聊] masked",
        "2026-08-27 16:00:03+0800 | boot=abc cid=evt-3 | 糖糖.SnowLuma | INFO | 💬 [群:masked] masked",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #1",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #2",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #3",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:05:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:15:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:20:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:25:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:35:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:45:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:55:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:05:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:15:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:20:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:25:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:35:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:45:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:55:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:58:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:59:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
    ])
    report = evaluate_observation([before, after], logs)

    assert report["overall"] == "PASS"
    assert report["coverage"] == "INSUFFICIENT"


def test_interval_uses_baseline_delta_not_today_cumulative():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "gateway_send_attempts": 100,
        "gateway_send_confirmed": 90,
        "gateway_send_uncertain": 7,
        "gateway_send_failed": 3,
    })
    after = _snapshot(metrics={
        "gateway_send_attempts": 104,
        "gateway_send_confirmed": 94,
        "gateway_send_uncertain": 7,
        "gateway_send_failed": 3,
    })
    report = evaluate_observation([before, after], {"events": {}})

    sending = report["domains"]["sending"]
    assert sending["status"] == "PASS"
    assert sending["evidence"]["attempts"] == 4
    assert sending["evidence"]["confirmed"] == 4
    assert sending["evidence"]["uncertain"] == 0


def test_send_uncertain_dead_or_drop_prevents_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "gateway_send_attempts": 4,
        "gateway_send_confirmed": 4,
        "gateway_send_uncertain": 0,
        "gateway_send_failed": 0,
        "gateway_events_dropped_global": 0,
    })
    after = _snapshot(
        metrics={
            "gateway_send_attempts": 6,
            "gateway_send_confirmed": 5,
            "gateway_send_uncertain": 1,
            "gateway_send_failed": 0,
            "gateway_events_dropped_global": 1,
        },
        outbox={"open": 0, "uncertain": 1, "dead": 0},
    )
    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["sending"]["status"] == "FAIL"
    assert report["domains"]["qq_gateway"]["status"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_open_outbox_prevents_sending_false_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "gateway_send_attempts": 10,
        "gateway_send_confirmed": 10,
    })
    after = _snapshot(
        metrics={
            "gateway_send_attempts": 11,
            "gateway_send_confirmed": 11,
        },
        outbox={
            "open": 1, "pending": 0, "sending": 1,
            "uncertain": 0, "dead": 0,
        },
    )

    sending = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["sending"]

    assert sending["status"] == "FAIL"
    assert sending["evidence"]["outbox_open"] == 1


def test_new_inbound_inbox_failure_prevents_gateway_false_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["database"]["inbox"] = {
        "received": 0, "claimed": 0, "executing": 0, "processed": 10,
        "failed": 0, "uncertain": 0, "unknown": 0,
    }
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    after["database"]["inbox"] = {
        "received": 0, "claimed": 0, "executing": 0, "processed": 11,
        "failed": 1, "uncertain": 0, "unknown": 0,
    }

    gateway = evaluate_observation([before, after], {"events": {
        "inbound_private": 1, "inbound_group": 1,
    }})["domains"]["qq_gateway"]

    assert gateway["status"] == "FAIL"
    assert gateway["evidence"]["inbox_new_failed"] == 1


def test_historical_inbox_failure_does_not_block_current_gateway_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["database"]["inbox"] = {
        "received": 0, "claimed": 0, "executing": 0, "processed": 10,
        "failed": 1, "uncertain": 1, "unknown": 0,
    }
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    after["database"]["inbox"] = {
        "received": 0, "claimed": 0, "executing": 0, "processed": 11,
        "failed": 1, "uncertain": 1, "unknown": 0,
    }

    gateway = evaluate_observation([before, after], {"events": {
        "inbound_private": 1,
        "inbound_group": 1,
    }})["domains"]["qq_gateway"]

    assert gateway["status"] == "PASS"
    assert gateway["evidence"]["inbox_new_failed"] == 0
    assert gateway["evidence"]["inbox_new_uncertain"] == 0


def test_unflushed_send_failure_log_prevents_false_pass():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot(metrics={
        "gateway_send_attempts": 10,
        "gateway_send_confirmed": 10,
    })
    after = _snapshot(metrics={
        "gateway_send_attempts": 11,
        "gateway_send_confirmed": 11,
    })
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    unflushed_failure = parse_log_events([
        "2026-08-27 16:59:59+0800 | boot=abc cid=evt-1 | 糖糖.Handler | WARNING | "
        "📤 回复发送未确认: target=redacted",
    ])

    report = evaluate_observation([before, after], unflushed_failure)
    assert report["domains"]["sending"]["status"] == "FAIL"
    assert report["domains"]["sending"]["evidence"]["log_uncertain"] == 1


def test_suspicious_send_logs_cannot_be_counted_as_confirmed_only():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | "
        "糖糖.SnowLuma | WARNING | ⚠️ 群100发送可疑: msg_id=0",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | "
        "糖糖.SnowLuma | INFO | 📤 群临时会话已发送 → 200 (msg_id=0)",
    ])

    assert parsed["events"]["send_uncertain_log"] == 2
    assert parsed["events"].get("send_confirmed_log", 0) == 0
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)
    assert report["domains"]["sending"]["status"] == "FAIL"


def test_metric_total_delta_survives_daily_counter_reset():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(metrics={
        "gateway_send_attempts": 100,
        "gateway_send_confirmed": 100,
    })
    before["database"]["metrics_total"] = {
        "gateway_send_attempts": 1000,
        "gateway_send_confirmed": 1000,
    }
    after = _snapshot(metrics={
        "gateway_send_attempts": 2,
        "gateway_send_confirmed": 2,
    })
    after["captured_at"] = "2026-08-28T01:00:00+08:00"
    after["database"]["metrics_total"] = {
        "gateway_send_attempts": 1002,
        "gateway_send_confirmed": 1002,
    }

    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["sending"]["status"] == "PASS"
    assert report["domains"]["sending"]["evidence"]["attempts"] == 2


def test_any_metrics_total_regression_breaks_integrity_and_domain_pass():
    """计数器回退不能被 ``max(0, delta)`` 静默成零。"""
    from tools.runtime_observer import evaluate_observation

    metric_names = (
        "gateway_send_attempts",
        "gateway_send_confirmed",
        "gateway_send_uncertain",
        "gateway_send_failed",
        "gateway_events_dropped_global",
        "gateway_events_dropped_scopes",
        "gateway_events_dropped_scope_queue",
        "gateway_callback_errors",
    )
    for regressed_name in metric_names:
        before = _snapshot()
        after = _snapshot()
        after["captured_at"] = "2026-08-27T17:00:00+08:00"
        before["database"]["metrics_total"] = {
            name: 100 for name in metric_names
        }
        after["database"]["metrics_total"] = {
            name: 99 if name == regressed_name else 100
            for name in metric_names
        }
        # 成功日志会让旧实现把 sending 判成 PASS；完整性闸门必须压住它。
        report = evaluate_observation(
            [before, after], {"events": {"send_confirmed_log": 1}},
        )

        assert report["domains"]["observer_integrity"]["status"] == "FAIL", regressed_name
        affected = (
            "sending"
            if regressed_name.startswith("gateway_send_")
            else "qq_gateway"
        )
        assert report["domains"][affected]["status"] != "PASS", regressed_name


def test_memory_capacity_requires_two_samples_and_reports_draining():
    from tools.runtime_observer import evaluate_observation

    one = _snapshot(backlog=20, total_messages=100, oldest_at="2026-08-20 10:00:00")
    report = evaluate_observation([one], {"events": {}})
    assert report["domains"]["memory"]["status"] == "INSUFFICIENT"

    two = _snapshot(backlog=15, total_messages=105, oldest_at="2026-08-21 10:00:00")
    two["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([one, two], {"events": {}})
    memory = report["domains"]["memory"]

    assert memory["status"] == "PASS"
    assert memory["evidence"]["arrivals"] == 5
    assert memory["evidence"]["serviced"] == 10
    assert memory["evidence"]["service_rate_per_hour"] > memory["evidence"]["arrival_rate_per_hour"]


def test_memory_report_exposes_extraction_lifecycle_and_quality_deltas():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(
        backlog=20, total_messages=100, oldest_at="2026-08-20 10:00:00",
        metrics={
            "extract_jobs_created": 1,
            "extract_jobs_admitted": 1,
            "extract_job_llm_started": 1,
            "extract_job_llm_succeeded": 1,
            "extract_jobs_ready": 1,
            "extract_jobs_completed": 0,
            "extract_attempts": 1,
            "extract_memories_total": 2,
        },
    )
    after = _snapshot(
        backlog=15, total_messages=105, oldest_at="2026-08-21 10:00:00",
        metrics={
            "extract_jobs_created": 3,
            "extract_jobs_admitted": 3,
            "extract_job_llm_started": 3,
            "extract_job_llm_succeeded": 3,
            "extract_jobs_ready": 3,
            "extract_jobs_completed": 2,
            "extract_attempts": 3,
            "extract_memories_total": 7,
        },
    )
    after["captured_at"] = "2026-08-27T17:00:00+08:00"

    memory = evaluate_observation([before, after], {
        "events": {},
    })["domains"]["memory"]
    lifecycle = memory["evidence"]["extraction_lifecycle"]

    assert lifecycle["jobs_created"] == 2
    assert lifecycle["llm_started"] == 2
    assert lifecycle["llm_succeeded"] == 2
    assert lifecycle["attempts"] == 2
    assert lifecycle["memories"] == 5
    assert lifecycle["warnings"] == []


def test_memory_backlog_must_actually_drain_after_minimum_window():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(backlog=100, total_messages=1000, oldest_at="2026-08-20 10:00:00")
    after = _snapshot(backlog=100, total_messages=1000, oldest_at="2026-08-20 10:00:00")
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "FAIL"


def test_memory_deferred_tail_is_not_misreported_as_stopped_worker():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(
        backlog=100,
        total_messages=1000,
        oldest_at="2026-08-27 15:00:00",
        eligible_backlog=0,
    )
    after = _snapshot(
        backlog=100,
        total_messages=1000,
        oldest_at="2026-08-27 15:00:00",
        eligible_backlog=0,
    )
    after["captured_at"] = "2026-08-27T17:00:00+08:00"

    report = evaluate_observation([before, after], {"events": {}})

    memory = report["domains"]["memory"]
    assert memory["status"] == "INSUFFICIENT"
    assert memory["evidence"]["eligible_backlog_after"] == 0
    assert memory["evidence"]["deferred_backlog_after"] == 100


def test_memory_eligible_tail_must_drain_after_minimum_window():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(
        backlog=10,
        total_messages=1000,
        oldest_at="2026-08-01 10:00:00",
        eligible_backlog=10,
        eligible_oldest_at="2026-08-01 10:00:00",
    )
    after = _snapshot(
        backlog=10,
        total_messages=1000,
        oldest_at="2026-08-01 10:00:00",
        eligible_backlog=10,
        eligible_oldest_at="2026-08-01 10:00:00",
    )
    after["captured_at"] = "2026-08-27T17:00:00+08:00"

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["memory"]["status"] == "FAIL"
    assert report["domains"]["memory"]["evidence"]["eligible_backlog_after"] == 10

    balanced = _snapshot(backlog=100, total_messages=1005, oldest_at="2026-08-20 10:00:00")
    balanced["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, balanced], {"events": {}})
    assert report["domains"]["memory"]["status"] == "FAIL"


def test_memory_capacity_short_window_is_insufficient_not_false_failure():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(backlog=100, total_messages=1000, oldest_at="2026-08-20 10:00:00")
    after = _snapshot(backlog=100, total_messages=1000, oldest_at="2026-08-20 10:00:00")
    after["captured_at"] = "2026-08-27T16:01:00+08:00"
    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "INSUFFICIENT"


def test_memory_hygiene_uses_observation_delta_not_existing_today_count():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(backlog=1, total_messages=10,
                       oldest_at="2026-08-27 15:00:00")
    before["database"]["memory"]["reasoning_leaks_today"] = 2
    before["database"]["memory"]["duplicate_groups_today"] = 1
    after = _snapshot(backlog=0, total_messages=11)
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    after["database"]["memory"]["reasoning_leaks_today"] = 2
    after["database"]["memory"]["duplicate_groups_today"] = 1

    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "PASS"
    assert report["domains"]["memory"]["evidence"]["new_reasoning_leaks"] == 0
    assert report["domains"]["memory"]["evidence"]["new_duplicate_groups"] == 0

    after["database"]["memory"]["reasoning_leaks_today"] = 3
    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "FAIL"
    assert report["domains"]["memory"]["evidence"]["new_reasoning_leaks"] == 1


def test_memory_hygiene_accumulates_across_daily_counter_reset():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["captured_at"] = "2026-08-27T23:50:00+08:00"
    before["database"]["memory"]["reasoning_leaks_today"] = 5
    middle = _snapshot()
    middle["captured_at"] = "2026-08-27T23:55:00+08:00"
    middle["database"]["memory"]["reasoning_leaks_today"] = 6
    after = _snapshot()
    after["captured_at"] = "2026-08-28T00:05:00+08:00"
    after["database"]["memory"]["reasoning_leaks_today"] = 1

    report = evaluate_observation([before, middle, after], {"events": {}})
    memory = report["domains"]["memory"]
    assert memory["status"] == "FAIL"
    assert memory["evidence"]["new_reasoning_leaks"] == 2


def test_memory_hygiene_uses_monotonic_row_boundary_across_midnight():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["captured_at"] = "2026-08-27T23:55:00+08:00"
    before["database"]["memory"].update({
        "max_id": 100,
        "new_reasoning_leaks": 0,
        "new_duplicate_rows": 0,
    })
    middle = _snapshot()
    middle["captured_at"] = "2026-08-27T23:59:00+08:00"
    middle["database"]["memory"].update({
        "max_id": 101,
        "new_reasoning_leaks": 1,
        "new_duplicate_rows": 0,
    })
    after = _snapshot()
    after["captured_at"] = "2026-08-28T00:05:00+08:00"
    after["database"]["memory"].update({
        "max_id": 101,
        "new_reasoning_leaks": 0,
        "new_duplicate_rows": 0,
    })

    report = evaluate_observation([before, middle, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "FAIL"
    assert report["domains"]["memory"]["evidence"]["new_reasoning_leaks"] == 1


def test_gateway_flap_in_any_snapshot_prevents_pass():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    middle = _snapshot()
    middle["captured_at"] = "2026-08-27T16:30:00+08:00"
    middle["services"]["napcat"]["up"] = False
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"

    report = evaluate_observation([before, middle, after], {"events": {}})
    gateway = report["domains"]["qq_gateway"]
    assert gateway["status"] == "FAIL"
    assert gateway["evidence"]["down_samples"]["napcat"] == 1


def test_local_dependency_failure_does_not_mislabel_qq_gateway():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    before["captured_at"] = "2026-08-30T12:00:00+08:00"
    after["captured_at"] = "2026-08-30T12:05:00+08:00"
    before["services"]["gpt_sovits"] = {"required": True, "up": True}
    after["services"]["gpt_sovits"] = {"required": True, "up": False}

    report = evaluate_observation([before, after], {"events": {
        "inbound_private": 1,
        "inbound_group": 1,
    }})

    assert report["domains"]["qq_gateway"]["status"] == "PASS"
    assert report["domains"]["qq_gateway"]["evidence"]["required_down"] == []
    assert report["domains"]["local_services"]["status"] == "FAIL"
    assert report["domains"]["local_services"]["evidence"]["required_down"] == [
        "gpt_sovits",
    ]
    assert report["overall"] == "FAIL"


def test_parse_log_events_keeps_only_structured_counts_and_latency():
    from tools.runtime_observer import parse_log_events

    raw_qq = "10001"
    raw_text = "这是不应进报告的私聊原文"
    lines = [
        f"2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.SnowLuma | INFO | 📩 [私聊] @{raw_qq}: {raw_text}",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:00:03+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 📝 流式完成: 12字 (2.4s, stop, 思考10字)",
        "2026-08-27 16:00:03+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:00:04+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎤 语音已发送",
    ]

    parsed = parse_log_events(lines)
    rendered = json.dumps(parsed, ensure_ascii=False)

    assert parsed["events"]["inbound_private"] == 1
    assert parsed["events"]["llm_started"] == 1
    assert parsed["events"]["llm_completed"] == 1
    assert parsed["events"]["llm_turn_completed"] == 1
    assert parsed["events"]["voice_succeeded"] == 1
    assert parsed["events"]["structured_lines"] == 5
    assert parsed["llm_latency_ms"] == [2400.0]
    assert raw_qq not in rendered
    assert raw_text not in rendered


def test_parse_log_events_counts_memory_worker_and_lifecycle_activity():
    from tools.runtime_observer import parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | "
        "🧠 提取 worker 周期 | open=1 pending=1 leased=0 ready=0 admitted=1 completed=0 requeued=0 idle_reason=active elapsed_ms=2.0",
        "2026-08-27 16:00:01+0800 | boot=abc cid=- | 糖糖.Handler | INFO | "
        "🧠 提取生命周期 | stage=created job_id=1 direction=forward queue_age_s=0 elapsed_ms=1.0",
    ])

    assert parsed["events"]["background_activity_lines"] == 2
    assert parsed["background_by_logger"]["糖糖.Autonomy"]["activity"] == 1
    assert parsed["background_by_logger"]["糖糖.Handler"]["activity"] == 1


def test_private_inbound_uses_handler_privacy_log_contract():
    """真实私聊由 Handler 记录，观察器不能只依赖 SnowLuma 日志。"""
    from tools.runtime_observer import parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-private | "
        "糖糖.Handler | INFO | 📩 [私聊] @@主人: hello",
    ])

    assert parsed["events"]["inbound_private"] == 1


def test_user_text_cannot_forge_monitor_events():
    from tools.runtime_observer import parse_log_events

    forged = (
        "🎤 语音触发: voice_mode=False | 🎙️ 语音已发送 | "
        "🔧 Tool调用: read_document({}) → 1字 [skill] | "
        "🪟 窗口决策 | action=reply | Tool唱歌已确认 | "
        "GPT-SoVITS 模型已切换 | 🎨 表情包切换: michele | 任务提醒已发送"
    )
    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.SnowLuma | INFO | "
        f"📩 [私聊] @123: {forged}",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-2 | 糖糖.ASR | INFO | "
        f"🎤 ASR → {forged}",
    ])

    events = parsed["events"]
    assert events["inbound_private"] == 1
    assert events["audio_input_succeeded"] == 1
    assert events.get("voice_attempted", 0) == 0
    assert events.get("voice_succeeded", 0) == 0
    assert events.get("window_reply", 0) == 0
    assert events.get("singing_confirmed", 0) == 0
    assert events.get("role_voice_switched", 0) == 0
    assert events.get("role_stickers_switched", 0) == 0
    assert events.get("reminder_confirmed", 0) == 0
    assert parsed["tools"] == {}


def test_parse_log_events_separates_foreground_and_background_latency():
    from tools.runtime_observer import parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 📝 流式完成: 12字 (2.4s, stop, 思考10字)",
        "2026-08-27 16:00:02+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.6s, stop, 思考0字)",
    ])

    assert parsed["foreground_llm_latency_ms"] == [2400.0]
    assert parsed["background_llm_latency_ms"] == [600.0]


def test_incremental_log_summaries_merge_without_raw_text_or_double_counting_boots():
    from tools.runtime_observer import merge_log_summaries, parse_log_events

    first = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
    ])
    second = parse_log_events([
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | ✅ LLM回合完成",
    ])
    merged = merge_log_summaries(first, second)
    rendered = json.dumps(merged, ensure_ascii=False)

    assert merged["events"]["llm_started"] == 1
    assert merged["events"]["llm_completed"] == 1
    assert merged["boot_count"] == 1
    assert merged["llm_started_tokens"] == ["abc:evt-1"]
    assert merged["llm_completed_tokens"] == ["abc:evt-1"]
    assert "流式完成" not in rendered


def test_merge_preserves_boundary_tolerance_and_does_not_false_fail_llm():
    """尾部追读时，观察窗口边界内的收尾不得被误报为孤儿完成。"""
    from tools.runtime_observer import (
        evaluate_observation, merge_log_summaries, parse_log_events,
    )

    propagated = merge_log_summaries(
        {"correlation_boundary_tolerant": True},
        parse_log_events([]),
    )
    assert propagated["correlation_boundary_tolerant"] is True

    boundary = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-boundary | 糖糖.Handler | INFO | "
        "📝 流式完成: 2字 (1.0s, stop, 思考0字)",
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-boundary | 糖糖.Handler | INFO | "
        "✅ LLM回合完成",
    ])
    boundary["correlation_boundary_tolerant"] = propagated[
        "correlation_boundary_tolerant"
    ]

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], boundary)
    llm = report["domains"]["llm"]

    # 窗口起点附近的孤立收尾证据只能算样本不足，不能误报真实故障。
    assert llm["status"] == "INSUFFICIENT"
    assert llm["evidence"]["boundary_unmatched_completion"] == 1
    assert llm["evidence"]["unmatched_completion"] == 0


def test_log_cursor_integrity_issues_are_carried_into_observation_summary(tmp_path):
    from tools.runtime_observer import (
        LogCursor, evaluate_observation, merge_log_summaries, parse_log_events,
    )

    log = tmp_path / "tangtang.log"
    log.write_text("已读\n", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)
    cursor.read_new()
    rotated = tmp_path / "tangtang.log.1"
    log.replace(rotated)
    log.write_text("新行\n", encoding="utf-8")
    cursor.read_new()

    summary = merge_log_summaries(parse_log_events([]))
    for name, count in cursor.issues.items():
        summary["events"][f"log_{name}"] = count
    assert summary["events"].get("log_rotation_tail_missing", 0) == 0
    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    summary["events"]["log_rotation_tail_missing"] = 1
    report = evaluate_observation([before, after], summary)
    assert report["domains"]["observer_integrity"]["status"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_llm_unfinished_foreground_call_is_not_masked_by_other_completions():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    events = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-a | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:00:03+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
    ])
    report = evaluate_observation([before, after], events)
    assert report["domains"]["llm"]["status"] == "FAIL"
    assert report["domains"]["llm"]["evidence"]["unfinished_foreground"] == 1


def test_llm_error_burst_or_slow_foreground_p95_fails_slo():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    lines = []
    for index, seconds in enumerate((1.0, 1.2, 1.3, 1.5, 120.0)):
        lines.extend([
            f"2026-08-27 16:00:0{index}+0800 | boot=abc cid=evt-{index} | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
            f"2026-08-27 16:00:1{index}+0800 | boot=abc cid=evt-{index} | 糖糖.Handler | INFO | 📝 流式完成: 2字 ({seconds}s, stop, 思考0字)",
            f"2026-08-27 16:00:1{index}+0800 | boot=abc cid=evt-{index} | 糖糖.Handler | INFO | ✅ LLM回合完成",
        ])
    lines.extend([
        "2026-08-27 16:01:00+0800 | boot=abc cid=evt-x | httpx | INFO | HTTP Request: POST https://api.deepseek.com/v1/chat/completions HTTP/1.1 429 Too Many Requests",
        "2026-08-27 16:01:01+0800 | boot=abc cid=evt-x | httpx | INFO | HTTP Request: POST https://api.deepseek.com/v1/chat/completions HTTP/1.1 429 Too Many Requests",
        "2026-08-27 16:01:02+0800 | boot=abc cid=evt-x | httpx | INFO | HTTP Request: POST https://api.deepseek.com/v1/chat/completions HTTP/1.1 429 Too Many Requests",
        "2026-08-27 16:01:03+0800 | boot=abc cid=evt-x | httpx | INFO | HTTP Request: POST https://api.deepseek.com/v1/chat/completions HTTP/1.1 500 Internal Server Error",
        "2026-08-27 16:01:04+0800 | boot=abc cid=evt-x | httpx | INFO | HTTP Request: POST https://api.deepseek.com/v1/chat/completions HTTP/1.1 503 Service Unavailable",
    ])
    report = evaluate_observation([before, after], parse_log_events(lines))
    llm = report["domains"]["llm"]
    assert llm["status"] == "FAIL"
    assert llm["evidence"]["foreground_p95_ms"] == 120000.0
    assert llm["evidence"]["transport_errors"] == 5


def test_llm_unmatched_completion_and_fallback_latency_are_not_hidden():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    lines = [
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-orphan | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-fallback | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:00:46+0800 | boot=abc cid=evt-fallback | 糖糖.Handler | INFO | 📝 流式完成(降级非流式): 2字 (45.0s, stop)",
        "2026-08-27 16:00:46+0800 | boot=abc cid=evt-fallback | 糖糖.Handler | INFO | ✅ LLM回合完成",
    ]
    parsed = parse_log_events(lines)
    assert parsed["llm_latency_ms"] == [45000.0]
    report = evaluate_observation([before, after], parsed)
    assert report["domains"]["llm"]["status"] == "FAIL"
    assert report["domains"]["llm"]["evidence"]["unmatched_completion"] == 1


def test_completion_only_llm_without_stream_latency_is_insufficient():
    """只有回合收尾标记、没有流式延迟时不能冒充完整 LLM 样本。"""
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Handler | INFO | ✅ LLM回合完成",
    ])
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert report["domains"]["llm"]["status"] == "INSUFFICIENT"
    assert report["domains"]["llm"]["evidence"]["completion_without_latency"] is True
    assert report["domains"]["llm"]["evidence"]["samples"] == 0


def test_latency_sample_cap_makes_llm_insufficient_instead_of_false_pass():
    from tools.runtime_observer import (
        MAX_LATENCY_SAMPLES, evaluate_observation, merge_log_summaries,
    )

    summary = merge_log_summaries({
        "events": {"llm_turn_completed": 1},
        "llm_latency_ms": [1000.0] * (MAX_LATENCY_SAMPLES + 1),
        "foreground_llm_latency_ms": [1000.0] * (MAX_LATENCY_SAMPLES + 1),
    })
    assert summary["latency_samples_dropped"] > 0

    report = evaluate_observation(
        [_snapshot(), _snapshot()], summary,
    )

    assert report["domains"]["llm"]["status"] == "INSUFFICIENT"


def test_complete_histogram_keeps_llm_pass_after_parse_merge_latency_cap():
    """原始延迟列表可截断，但完整直方图与配对证据仍足以验收。"""
    from tools.runtime_observer import (
        MAX_LATENCY_SAMPLES, evaluate_observation, merge_log_summaries,
        parse_log_events,
    )

    def latency_lines(start: int, count: int):
        for index in range(start, start + count):
            yield (
                f"2026-08-27 16:00:00+0800 | boot=abc cid=evt-{index} | "
                "糖糖.Handler | INFO | 🔄 正在调用LLM生成回复..."
            )
            yield (
                f"2026-08-27 16:00:01+0800 | boot=abc cid=evt-{index} | "
                "糖糖.Handler | INFO | 📝 流式完成: 2字 (1.0s, stop, 思考0字)"
            )
            yield (
                f"2026-08-27 16:00:01+0800 | boot=abc cid=evt-{index} | "
                "糖糖.Handler | INFO | ✅ LLM回合完成"
            )

    first_count = MAX_LATENCY_SAMPLES // 2
    second_count = MAX_LATENCY_SAMPLES - first_count + 1
    merged = merge_log_summaries(
        parse_log_events(latency_lines(0, first_count)),
        parse_log_events(latency_lines(first_count, second_count)),
    )

    assert merged["latency_samples_dropped"] > 0
    assert merged["latency_histogram_complete"] is True
    assert sum(merged["latency_histograms"]["foreground"]) == MAX_LATENCY_SAMPLES + 1
    assert len(merged["foreground_llm_latency_ms"]) == MAX_LATENCY_SAMPLES
    assert merged["llm_started_tokens"] == merged["llm_completed_tokens"]

    report = evaluate_observation([_snapshot(), _snapshot()], merged)

    assert report["domains"]["llm"]["status"] == "PASS"
    assert report["domains"]["llm"]["evidence"]["latency_histogram_complete"] is True
    assert report["domains"]["llm"]["evidence"]["samples"] == MAX_LATENCY_SAMPLES + 1


def test_voice_requires_same_correlation_to_finish():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-a | 糖糖.Handler | INFO | 🎤 语音触发: voice_mode=False, tool=True",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | 🎙️ 语音发送: 情绪=温柔 音色=x 语速=1.00 停顿=自然",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | 🎤 语音已发送",
    ])
    report = evaluate_observation([before, after], parsed)
    assert report["domains"]["voice"]["status"] == "FAIL"
    assert report["domains"]["voice"]["evidence"]["unfinished"] == 1


def test_voice_engine_failure_log_cannot_be_hidden_by_fallback_path():
    """Voice 层重试耗尽时，即使 Handler 未再写失败行也必须判语音失败。"""
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-v | 糖糖.Voice | WARNING | "
        "GPT-SoVITS 3次尝试均失败: timeout",
    ])
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["voice_failed"] == 1
    assert report["domains"]["voice"]["status"] == "FAIL"


def test_sticker_domain_requires_delivery_log_not_only_tool_call():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    tool_only = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🔧 Tool调用: send_stickers({'emotion': '开心'}) → 14字 [behavior]",
    ])
    report = evaluate_observation([before, after], tool_only)
    assert report["domains"]["sticker"]["status"] == "FAIL"


def test_sticker_action_plan_confirmation_is_delivery_evidence():
    """现行贴图链路为 ActionPlan 文案，观察器不得把真实送达漏算。"""
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🔧 Tool调用: send_stickers({'emotion': '开心'}) → 14字 [behavior]",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🎨 Sticker ActionPlan → 群123 (attempted=1 confirmed=1 uncertain=0 failed=0 plan=x)",
    ])
    report = evaluate_observation([before, after], parsed)
    assert report["domains"]["sticker"]["status"] == "PASS"

    delivered = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🔧 Tool调用: send_stickers({'emotion': '开心'}) → 14字 [behavior]",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🎨 Sticker Tool → 群123 (attempted=1 confirmed=1 uncertain=0 failed=0)",
    ])
    report = evaluate_observation([before, after], delivered)
    assert report["domains"]["sticker"]["status"] == "PASS"
    assert report["domains"]["vision"]["status"] == "INSUFFICIENT"
    assert report["domains"]["llm_tools"]["status"] == "PASS"

    partially_delivered = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-a | 糖糖.Handler | INFO | "
        "🔧 Tool调用: send_stickers({'emotion': '开心'}) → 14字 [behavior]",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | "
        "🔧 Tool调用: send_stickers({'emotion': '温柔'}) → 14字 [behavior]",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | "
        "🎨 Sticker Tool → 群masked (attempted=1 confirmed=1 uncertain=0 failed=0)",
    ])
    report = evaluate_observation([before, after], partially_delivered)
    assert report["domains"]["sticker"]["status"] == "FAIL"


def test_vision_sticker_and_scheduled_features_do_not_mask_each_other():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    evidence = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🖼 按需识图: 成功",
        "2026-08-27 16:00:01+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 📋 任务提醒已发送: redacted",
    ])
    report = evaluate_observation([before, after], evidence)

    assert report["domains"]["vision"]["status"] == "PASS"
    assert report["domains"]["sticker"]["status"] == "INSUFFICIENT"
    assert report["domains"]["reminders"]["status"] == "PASS"
    assert report["domains"]["birthdays"]["status"] == "INSUFFICIENT"
    assert report["domains"]["image_shares"]["status"] == "INSUFFICIENT"


def test_text_reminder_confirmation_uses_durable_fact_without_legacy_log(
        tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    db = tmp_path / "memory.db"
    store = Store(str(db))
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    before = capture_snapshot(db)

    task_id = store.create_task(
        "1001", "内部备忘", "2020-01-01 00:00",
        action_payload={"text": "到点提醒"},
    )
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "到点提醒")
    assert store.claim_send_outbox(action["outbox_id"])
    assert store.settle_send_outbox(
        action["outbox_id"], "confirmed", message_ids=[7001],
    ) in {"confirmed", "confirmed_duplicate"}
    after = capture_snapshot(db)

    reminders = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["reminders"]
    assert reminders["status"] == "PASS"
    assert reminders["evidence"]["durable_confirmed"] == 1
    assert reminders["evidence"]["log_confirmed"] == 0


def test_knowledge_requires_structured_success_not_only_tool_return():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    tool_only = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🔧 Tool调用: search_knowledge({'query': 'x'}) → 20字 [skill]",
    ])
    report = evaluate_observation([before, after], tool_only)
    assert report["domains"]["knowledge_tools"]["status"] == "FAIL"

    empty = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "📚 Knowledge Tool | tool=search_knowledge outcome=empty",
    ])
    report = evaluate_observation([before, after], empty)
    assert report["domains"]["knowledge_tools"]["status"] == "INSUFFICIENT"

    success = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "📚 Knowledge Tool | tool=read_document outcome=success",
    ])
    report = evaluate_observation([before, after], success)
    assert report["domains"]["knowledge_tools"]["status"] == "PASS"

    partially_observed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-a | 糖糖.Handler | INFO | "
        "🔧 Tool调用: search_knowledge({'query': 'x'}) → 20字 [skill]",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | "
        "🔧 Tool调用: read_document({'document': 'x'}) → 20字 [skill]",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-b | 糖糖.Handler | INFO | "
        "📚 Knowledge Tool | tool=read_document outcome=success",
    ])
    report = evaluate_observation([before, after], partially_observed)
    assert report["domains"]["knowledge_tools"]["status"] == "FAIL"


def test_knowledge_retrieval_trace_is_aggregated_without_query_text():
    from tools.runtime_observer import (
        evaluate_observation, merge_log_summaries, parse_log_events,
    )

    lines = [
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Knowledge | INFO | "
        "📚 Knowledge Retrieval | hits=3 sources=2 paths=keyword:3,fts:3,semantic:2 latency_ms=8.1 "
        "trace=[{\"match_type\":\"fts+keyword\",\"rerank_score\":0.875},{\"match_type\":\"semantic\",\"rerank_score\":null}]",
        "2026-08-27 16:00:01+0800 | boot=abc cid=- | 糖糖.Knowledge | INFO | "
        "📚 Knowledge Retrieval | hits=0 sources=0 paths=none latency_ms=1.2",
    ]

    parsed = parse_log_events(lines)
    trace = parsed["knowledge_retrieval"]
    assert trace["calls"] == 2
    assert trace["hits"] == 3
    assert trace["empty"] == 1
    assert trace["sources"] == 2
    assert trace["path_counts"] == {
        "keyword": 1, "fts": 1, "semantic": 1,
    }
    assert trace["match_type_counts"] == {
        "fts+keyword": 1, "semantic": 1,
    }
    assert trace["rerank_score"] == [0.875]
    assert trace["latency_ms"] == [8.1, 1.2]

    merged = merge_log_summaries(parsed, parse_log_events(lines[:1]))
    merged_trace = merged["knowledge_retrieval"]
    assert merged_trace["calls"] == 3
    assert merged_trace["hits"] == 6
    assert merged_trace["empty"] == 1
    assert merged_trace["path_counts"] == {
        "keyword": 2, "fts": 2, "semantic": 2,
    }
    assert merged_trace["match_type_counts"] == {
        "fts+keyword": 2, "semantic": 2,
    }
    assert merged_trace["rerank_score"] == [0.875, 0.875]

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], merged)
    evidence = report["domains"]["knowledge_tools"]["evidence"]
    assert evidence["retrieval_trace_calls"] == 3
    assert evidence["retrieval_trace_hits"] == 6
    assert evidence["retrieval_trace_empty"] == 1
    assert evidence["retrieval_trace_latency_p95_ms"] == 8.1
    assert evidence["retrieval_trace_match_types"] == {
        "fts+keyword": 2, "semantic": 2,
    }
    assert evidence["retrieval_trace_rerank_score_p95"] == 0.875


def test_gpt_ready_child_exit_metadata_is_deduplicated_and_reported():
    from tools.runtime_observer import (
        evaluate_observation, merge_log_summaries, parse_log_events,
    )

    lines = [
        "2026-08-31 10:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 就绪后进程退出 (code=1) pid=123 lifetime=312.4s，尝试一次自动恢复",
        "2026-08-31 10:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | ERROR | "
        "🔧 GPT-SoVITS 就绪后进程意外退出 (code=1) pid=123 lifetime=312.4s，最后输出:",
    ]

    parsed = parse_log_events(lines)
    assert parsed["events"]["gpt_ready_child_exits"] == 1
    assert parsed["gpt_exit_codes"] == {"1": 1}
    assert parsed["gpt_exit_lifetime_max_s"] == 312.4
    assert parsed["gpt_exit_last"] == {
        "timestamp": "2026-08-31 10:00:00+0800",
        "pid": "123",
        "code": "1",
        "lifetime_s": 312.4,
    }

    merged = merge_log_summaries(parsed)
    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-31T10:01:00+08:00"
    report = evaluate_observation([before, after], merged)
    evidence = report["domains"]["local_services"]["evidence"]
    assert evidence["gpt_ready_child_exits"] == 1
    assert evidence["gpt_exit_codes"] == {"1": 1}
    assert evidence["gpt_exit_lifetime_max_s"] == 312.4


def test_runtime_log_errors_fail_but_clean_structured_log_passes():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    clean = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | httpx | INFO | heartbeat",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #1",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #2",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #3",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:05:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:15:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:20:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:25:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:35:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:45:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:55:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:05:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:10:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:15:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:20:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:25:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:30:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:35:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:40:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:45:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:50:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:55:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
        "2026-08-27 16:58:00+0800 | boot=abc cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:59:00+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
    ])
    report = evaluate_observation([before, after], clean)
    assert report["domains"]["runtime_logs"]["status"] == "PASS"
    assert report["domains"]["background_tasks"]["status"] == "PASS"

    failed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Test | ERROR | synthetic failure",
    ])
    report = evaluate_observation([before, after], failed)
    assert report["domains"]["runtime_logs"]["status"] == "INSUFFICIENT"


def test_background_tasks_without_background_sample_are_insufficient():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    traffic_only = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.SnowLuma | INFO | "
        "💬 [群:masked] 普通消息",
    ])
    report = evaluate_observation([before, after], traffic_only)
    assert report["domains"]["background_tasks"]["status"] == "INSUFFICIENT"


def test_background_failure_prefixes_override_startup_activity():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    lines = [
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Autonomy | WARNING | 🔥 自治循环异常: synthetic",
        "2026-08-27 16:00:01+0800 | boot=abc cid=- | 糖糖.Tasks | WARNING | 任务检查异常: synthetic",
        "2026-08-27 16:00:02+0800 | boot=abc cid=- | 糖糖.Scheduler | ERROR | 定时调度检查出错",
        "2026-08-27 16:00:03+0800 | boot=abc cid=- | 糖糖.ImageShare | ERROR | 图片分享出错",
    ]
    parsed = parse_log_events(lines)

    assert parsed["events"]["background_task_failures"] == len(lines)
    report = evaluate_observation([before, after], parsed)
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_tts_startup_repair_is_recovered_only_after_ready_evidence():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    recovered = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…",
        "2026-08-27 16:00:10+0800 | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
    ])
    unresolved = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…",
    ])

    recovered_report = evaluate_observation(
        [_snapshot(), _snapshot()], recovered,
    )
    unresolved_report = evaluate_observation(
        [_snapshot(), _snapshot()], unresolved,
    )

    assert recovered["events"]["tts_failure_observed"] == 1
    assert recovered["events"]["tts_failure_recovered"] == 1
    assert recovered_report["domains"]["background_tasks"]["status"] != "FAIL"
    assert recovered_report["domains"]["background_tasks"]["evidence"][
        "recovered_failures"
    ] == 1
    assert unresolved_report["domains"]["background_tasks"]["status"] == "FAIL"


def test_tts_startup_timeout_stays_failed_even_if_an_old_service_was_ready():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 15:59:00+0800 | boot=old cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
        "2026-08-27 16:00:00+0800 | boot=new cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 启动超时 (60s)，语音可能不可用",
    ])

    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["tts_failure_observed"] == 1
    assert parsed["events"]["tts_failure_recovered"] == 0
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_tts_ready_before_repair_in_same_boot_cannot_mask_failure():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 15:59:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…",
    ])

    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["tts_failure_recovered"] == 0
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_tts_repair_pairing_survives_log_chunk_boundary():
    from tools.runtime_observer import (
        evaluate_observation, merge_log_summaries, parse_log_events,
    )

    started = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…",
    ])
    recovered = parse_log_events([
        "2026-08-27 16:00:10+0800 | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
    ])

    merged = merge_log_summaries(started, recovered)
    report = evaluate_observation([_snapshot(), _snapshot()], merged)

    assert merged["events"]["tts_failure_recovered"] == 1
    assert report["domains"]["background_tasks"]["status"] != "FAIL"


def test_one_tts_ready_recovers_all_failures_in_same_repair_episode():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 进程异常，强制重启…",
        "2026-08-27 16:01:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 启动超时 (60s)，语音可能不可用",
        "2026-08-27 16:02:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
    ])

    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["tts_failure_observed"] == 2
    assert parsed["events"]["tts_failure_recovered"] == 2
    assert report["domains"]["background_tasks"]["status"] != "FAIL"


def test_tts_recovery_cannot_hide_unrelated_background_failure():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
        "🔧 GPT-SoVITS 端口有响应但 TTS 不健康，杀掉重建…",
        "2026-08-27 16:00:10+0800 | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
        "🔧 GPT-SoVITS 已就绪 (4s)",
        "2026-08-27 16:00:20+0800 | boot=abc cid=- | 糖糖.Tasks | WARNING | "
        "任务检查异常: synthetic",
    ])

    report = evaluate_observation([_snapshot(), _snapshot()], parsed)
    evidence = report["domains"]["background_tasks"]["evidence"]

    assert evidence["raw_failures"] == 2
    assert evidence["recovered_failures"] == 1
    assert evidence["failures"] == 1
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_tts_episode_summary_matches_full_parse_across_three_chunks():
    from itertools import product

    from tools.runtime_observer import merge_log_summaries, parse_log_events

    def line(index, event):
        timestamp = f"2026-08-27 16:00:{index:02d}+0800"
        if event == "failure":
            return (
                f"{timestamp} | boot=abc cid=- | 糖糖.ServiceMgr | WARNING | "
                "🔧 GPT-SoVITS 启动超时 (60s)，语音可能不可用"
            )
        return (
            f"{timestamp} | boot=abc cid=- | 糖糖.ServiceMgr | INFO | "
            "🔧 GPT-SoVITS 已就绪 (4s)"
        )

    for length in range(1, 6):
        for sequence in product(("failure", "ready"), repeat=length):
            lines = [line(index, event) for index, event in enumerate(sequence)]
            expected = parse_log_events(lines)
            expected_state = expected["tts_repairs_by_boot"]["abc"]
            for first in range(length + 1):
                for second in range(first, length + 1):
                    merged = merge_log_summaries(
                        parse_log_events(lines[:first]),
                        parse_log_events(lines[first:second]),
                        parse_log_events(lines[second:]),
                    )
                    assert merged["events"]["tts_failure_observed"] == expected[
                        "events"
                    ]["tts_failure_observed"]
                    assert merged["events"]["tts_failure_recovered"] == expected[
                        "events"
                    ]["tts_failure_recovered"]
                    assert merged["tts_repairs_by_boot"]["abc"] == expected_state


def test_album_warning_failure_fails_album_and_background_domains():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Album | WARNING | ✗ 每日点赞失败: synthetic",
    ])
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["album_failures"] == 1
    assert report["domains"]["album"]["status"] == "FAIL"
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_health_check_warning_fails_health_check_and_background_domains():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    parsed = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.HealthCheck | WARNING | 🩺 [warn] stale_extraction: synthetic",
    ])
    report = evaluate_observation([_snapshot(), _snapshot()], parsed)

    assert parsed["events"]["health_check_failures"] == 1
    assert report["domains"]["health_check"]["status"] == "FAIL"
    assert report["domains"]["background_tasks"]["status"] == "FAIL"


def test_early_background_startup_without_late_activity_is_not_pass():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    parsed = parse_log_events([
        # 观察期只在开头看到启动，末段没有任何循环活性。
        "2026-08-27 16:00:01+0800 | boot=abc cid=- | 糖糖.Scheduler | INFO | ⏰ 定时调度器已启动 (1个任务)",
        "2026-08-27 16:59:59+0800 | boot=abc cid=evt-1 | 糖糖.SnowLuma | INFO | 💬 [群:masked] 普通消息",
    ])

    report = evaluate_observation([before, after], parsed)

    assert report["domains"]["background_tasks"]["status"] != "PASS"


def test_tasks_and_scheduler_max_heartbeat_gap_fails_even_with_fresh_endpoints():
    """开头/结尾两次心跳不能掩盖中间超合同的长空窗。"""
    from tools.runtime_observer import evaluate_observation, parse_log_events

    def timestamp(minutes: int) -> str:
        return (
            f"2026-08-27 {16 + minutes // 60:02d}:{minutes % 60:02d}:00+0800"
        )

    heartbeat_prefixes = {
        "糖糖.Autonomy": "🫀 自治循环心跳",
        "糖糖.Tasks": "🫀 任务提醒循环心跳",
        "糖糖.Scheduler": "🫀 定时调度心跳",
        "糖糖.Handler": "🫀 反思循环心跳",
    }
    for target in ("糖糖.Tasks", "糖糖.Scheduler"):
        lines = []
        # 其余必需循环保持合同内的采样间隔，避免“缺样本”掩盖目标空窗。
        schedules = {
            "糖糖.Autonomy": range(0, 121, 30),
            "糖糖.Handler": range(0, 121, 60),
            "糖糖.Tasks": range(0, 121, 5),
            "糖糖.Scheduler": range(0, 121, 5),
        }
        schedules[target] = (0, 120)
        for logger_name, offsets in schedules.items():
            prefix = heartbeat_prefixes[logger_name]
            for offset in offsets:
                lines.append(
                    f"{timestamp(offset)} | boot=abc cid=- | {logger_name} | INFO | {prefix}"
                )

        before = _snapshot()
        after = _snapshot()
        after["captured_at"] = "2026-08-27T18:00:00+08:00"
        report = evaluate_observation([before, after], parse_log_events(lines))

        assert report["domains"]["background_tasks"]["status"] == "FAIL", target


def test_old_boot_heartbeats_cannot_vouch_for_new_boot():
    from tools.runtime_observer import (
        evaluate_observation, merge_log_summaries, parse_log_events,
    )

    old_boot = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=old cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:30:00+0800 | boot=old cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:50:00+0800 | boot=old cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳",
        "2026-08-27 16:58:00+0800 | boot=old cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:59:00+0800 | boot=old cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
    ])
    # 新进程已经产生日志，但没有任何新的后台心跳。
    new_boot = parse_log_events([
        "2026-08-27 16:59:59+0800 | boot=new cid=- | httpx | INFO | health probe",
    ])
    merged = merge_log_summaries(old_boot, new_boot)

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], merged)
    background = report["domains"]["background_tasks"]

    assert background["status"] != "PASS"
    assert all(
        item["latest_boot_match"] is False
        for item in background["evidence"]["required_heartbeats"].values()
    )


def test_multiple_boots_make_ordinary_stability_observation_insufficient():
    """普通 soak 跨过重启时，不能继续证明单实例稳定运行。"""
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=old cid=- | httpx | INFO | first boot",
        "2026-08-27 16:30:00+0800 | boot=new cid=- | httpx | INFO | second boot",
    ])

    report = evaluate_observation([before, after], logs)
    stability = report["domains"]["process_stability"]

    assert stability["status"] == "INSUFFICIENT"
    assert stability["evidence"]["boot_count"] == 2
    assert stability["evidence"]["observed_restarts"] == 1
    assert stability["evidence"]["expected_restarts"] is None
    assert report["overall"] != "PASS"


def test_log_cursor_anchors_the_boot_visible_at_tail_start(tmp_path):
    from tools.runtime_observer import LogCursor

    log = tmp_path / "tangtang.log"
    log.write_text(
        "2026-08-27 16:00:00+0800 | boot=old cid=- | httpx | INFO | baseline\n",
        encoding="utf-8",
    )

    cursor = LogCursor(log, start_at_end=True)

    assert cursor.initial_boot_id == "old"
    assert cursor.initial_boot_at == "2026-08-27 16:00:00+0800"
    assert cursor.read_new() == []


def test_silent_initial_boot_is_counted_when_new_boot_first_writes_in_window():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:30:00+0800 | boot=new cid=- | httpx | INFO | first visible line",
    ])
    logs.update({
        "baseline_required": True,
        "initial_boot_id": "old",
        "initial_boot_at": "2026-08-27 15:59:00+0800",
    })

    report = evaluate_observation([before, after], logs)

    assert report["domains"]["process_stability"]["status"] == "INSUFFICIENT"
    assert report["domains"]["process_stability"]["evidence"]["observed_restarts"] == 1


def test_stale_tail_boot_cannot_anchor_observation_boundary():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    logs = parse_log_events([
        "2026-08-27 16:01:00+0800 | boot=new cid=- | httpx | INFO | cold start",
    ])
    logs.update({
        "baseline_required": True,
        "initial_boot_id": "dead-old",
        "initial_boot_at": "2026-08-27 15:49:59+0800",
    })

    report = evaluate_observation([before, after], logs)

    evidence = report["domains"]["process_stability"]["evidence"]
    assert evidence["baseline_complete"] is False
    assert report["domains"]["process_stability"]["status"] == "INSUFFICIENT"


def test_tail_boot_at_freshness_boundary_is_a_valid_anchor():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    logs = parse_log_events([
        "2026-08-27 16:00:01+0800 | boot=current cid=- | httpx | INFO | active",
    ])
    logs.update({
        "baseline_required": True,
        "initial_boot_id": "current",
        "initial_boot_at": "2026-08-27 15:50:00+0800",
    })

    report = evaluate_observation([before, after], logs)

    evidence = report["domains"]["process_stability"]["evidence"]
    assert evidence["baseline_complete"] is True
    assert evidence["baseline_age_seconds"] == 600.0
    assert report["domains"]["process_stability"]["status"] == "PASS"


def test_future_tail_timestamp_cannot_be_treated_as_fresh_baseline():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    logs = parse_log_events([
        "2026-08-27 16:00:01+0800 | boot=current cid=- | httpx | INFO | active",
    ])
    logs.update({
        "baseline_required": True,
        "initial_boot_id": "current",
        "initial_boot_at": "2026-08-27 16:00:01+0800",
    })

    report = evaluate_observation([before, after], logs)

    evidence = report["domains"]["process_stability"]["evidence"]
    assert evidence["baseline_complete"] is False
    assert evidence["baseline_age_seconds"] == -1.0


def test_exact_restart_count_without_latest_boot_business_recovery_is_insufficient():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=old cid=- | httpx | INFO | first boot",
        "2026-08-27 16:30:00+0800 | boot=new cid=- | httpx | INFO | recovered boot",
    ])
    logs["expected_restarts"] = 1

    report = evaluate_observation([before, after], logs)

    stability = report["domains"]["process_stability"]
    assert stability["status"] == "INSUFFICIENT"
    assert stability["evidence"]["latest_boot_recovery"]["inbound"] is False
    assert stability["evidence"]["latest_boot_recovery"]["llm_completed"] is False
    assert stability["evidence"]["latest_boot_recovery"]["send_confirmed"] is False


def test_explicit_restart_recovery_passes_only_with_latest_boot_end_to_end_evidence():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot(metrics={
        "gateway_send_attempts": 0,
        "gateway_send_confirmed": 0,
    })
    after = _snapshot(metrics={
        "gateway_send_attempts": 1,
        "gateway_send_confirmed": 1,
    })
    after["captured_at"] = "2026-08-27T16:36:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=old cid=- | httpx | INFO | baseline",
        "2026-08-27 16:30:00+0800 | boot=new cid=evt-1 | 糖糖.Handler | INFO | 📩 [私聊] masked",
        "2026-08-27 16:31:00+0800 | boot=new cid=evt-1 | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:31:01+0800 | boot=new cid=evt-1 | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
        "2026-08-27 16:31:01+0800 | boot=new cid=evt-1 | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:31:02+0800 | boot=new cid=evt-1 | 糖糖.SnowLuma | INFO | 📤 私聊已发送 → masked (msg_id=1)",
        "2026-08-27 16:32:00+0800 | boot=new cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #1",
        "2026-08-27 16:32:00+0800 | boot=new cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
    ])
    logs["expected_restarts"] = 1

    report = evaluate_observation([before, after], logs)

    stability = report["domains"]["process_stability"]
    assert stability["status"] == "PASS", stability
    assert all(stability["evidence"]["latest_boot_recovery"].values())


def test_restart_recovery_cannot_join_unrelated_correlation_ids():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot(metrics={
        "gateway_send_attempts": 0,
        "gateway_send_confirmed": 0,
    })
    after = _snapshot(metrics={
        "gateway_send_attempts": 1,
        "gateway_send_confirmed": 1,
    })
    after["captured_at"] = "2026-08-27T16:36:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=old cid=- | httpx | INFO | baseline",
        "2026-08-27 16:30:00+0800 | boot=new cid=inbound-only | 糖糖.Handler | INFO | 📩 [私聊] masked",
        "2026-08-27 16:31:00+0800 | boot=new cid=llm-only | 糖糖.Handler | INFO | 🔄 正在调用LLM生成回复...",
        "2026-08-27 16:31:01+0800 | boot=new cid=llm-only | 糖糖.Handler | INFO | 📝 流式完成: 2字 (0.5s, stop, 思考0字)",
        "2026-08-27 16:31:01+0800 | boot=new cid=llm-only | 糖糖.Handler | INFO | ✅ LLM回合完成",
        "2026-08-27 16:31:02+0800 | boot=new cid=send-only | 糖糖.SnowLuma | INFO | 📤 私聊已发送 → masked (msg_id=1)",
        "2026-08-27 16:32:00+0800 | boot=new cid=- | 糖糖.Autonomy | INFO | 🫀 自治循环心跳 #1",
        "2026-08-27 16:32:00+0800 | boot=new cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Handler | INFO | 🫀 反思循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Tasks | INFO | 🫀 任务提醒循环心跳",
        "2026-08-27 16:35:00+0800 | boot=new cid=- | 糖糖.Scheduler | INFO | 🫀 定时调度心跳",
    ])
    logs["expected_restarts"] = 1

    report = evaluate_observation([before, after], logs)

    stability = report["domains"]["process_stability"]
    assert stability["status"] == "INSUFFICIENT"
    assert stability["evidence"]["latest_boot_recovery"]["same_turn"] is False


def test_missing_planned_restart_is_insufficient():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=only cid=- | httpx | INFO | baseline",
    ])
    logs["expected_restarts"] = 1

    report = evaluate_observation([before, after], logs)

    assert report["domains"]["process_stability"]["status"] == "INSUFFICIENT"


def test_extra_restart_fails_explicit_recovery_observation():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    logs = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=first cid=- | httpx | INFO | first boot",
        "2026-08-27 16:20:00+0800 | boot=second cid=- | httpx | INFO | expected restart",
        "2026-08-27 16:40:00+0800 | boot=third cid=- | httpx | INFO | extra restart",
    ])
    logs["expected_restarts"] = 1

    report = evaluate_observation([before, after], logs)

    assert report["domains"]["process_stability"]["status"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_memory_id_regression_is_a_hard_failure():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(backlog=20, total_messages=100, oldest_at="2026-08-20 10:00:00")
    before["database"]["memory"]["max_id"] = 900
    before["database"]["memory"]["id_high_water"] = 900
    after = _snapshot(backlog=10, total_messages=105, oldest_at="2026-08-21 10:00:00")
    after["database"]["memory"]["max_id"] = 12
    after["database"]["memory"]["id_high_water"] = 12
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["memory"]["status"] == "FAIL"
    assert report["domains"]["memory"]["evidence"]["memory_id_regression"] is True


def test_memory_id_high_water_regression_is_failure_even_when_max_id_is_stable():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["database"]["memory"].update({"max_id": 1000, "id_high_water": 1000})
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    after["database"]["memory"].update({"max_id": 1000, "id_high_water": 900})

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["memory"]["status"] == "FAIL"


def test_chat_log_high_water_regression_fails_integrity_and_memory():
    """聊天记录水位回退不能被稳定的记忆水位或指标计数掩盖。"""
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(
        backlog=20, total_messages=100, oldest_at="2026-08-27 16:00:00",
    )
    after = _snapshot(
        backlog=10, total_messages=105, oldest_at="2026-08-27 16:30:00",
    )
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    for snapshot in (before, after):
        snapshot["database"]["memory"].update({
            "max_id": 1000,
            "id_high_water": 1000,
        })
        snapshot["database"]["metrics_total"] = {
            "gateway_send_attempts": 100,
        }
    before["database"]["chat_id_high_water"] = 100
    after["database"]["chat_id_high_water"] = 99

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["observer_integrity"]["status"] != "PASS"
    assert report["domains"]["memory"]["status"] != "PASS"
    assert report["domains"]["memory"]["evidence"]["chat_log_id_regressions"] == 1


def test_memory_max_id_deletion_is_not_failure_when_id_high_water_holds():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    before["database"]["memory"].update({"max_id": 1000, "id_high_water": 1000})
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    # 合法的清理/撤回可能使当前 MAX(id) 下降；高水位不能下降才是回退判据。
    after["database"]["memory"].update({"max_id": 900, "id_high_water": 1000})

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["memory"]["status"] != "FAIL"


def test_total_user_messages_regression_is_not_counted_as_zero_arrivals():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot(
        backlog=100, total_messages=1000, oldest_at="2026-08-20 10:00:00",
    )
    before["database"]["memory"].update({"max_id": 1000, "id_high_water": 1000})
    after = _snapshot(
        backlog=10, total_messages=900, oldest_at="2026-08-21 10:00:00",
    )
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    after["database"]["memory"].update({"max_id": 1000, "id_high_water": 1000})

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["memory"]["status"] == "FAIL"


def test_file_truncation_makes_observer_integrity_fail():
    from tools.runtime_observer import evaluate_observation

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation(
        [before, after], {"events": {"log_file_truncations": 1}},
    )
    assert report["domains"]["observer_integrity"]["status"] == "FAIL"


def test_log_file_disappearance_after_observation_fails_integrity(tmp_path):
    from tools.runtime_observer import (
        LogCursor, evaluate_observation, merge_log_summaries, parse_log_events,
    )

    log = tmp_path / "tangtang.log"
    log.write_text("已读\n", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)
    assert cursor.read_new() == ["已读"]
    log.unlink()
    # 单次缺失可能只是轮转窗口，先挂起；最终收尾时才确认证据缺口。
    assert cursor.read_new() == []
    assert cursor.issues.get("file_missing", 0) == 0
    assert cursor.read_new(final=True) == []
    assert cursor.issues["file_missing"] == 1

    summary = merge_log_summaries(parse_log_events([]))
    for name, count in cursor.issues.items():
        summary["events"][f"log_{name}"] = count
    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], summary)

    assert report["domains"]["observer_integrity"]["status"] == "FAIL"


def test_log_file_disappearance_then_recovery_is_not_integrity_failure(tmp_path):
    """轮转期间短暂缺失、随后恢复时不应制造假红。"""
    from tools.runtime_observer import (
        LogCursor, evaluate_observation, merge_log_summaries, parse_log_events,
    )

    log = tmp_path / "tangtang.log"
    rotated = tmp_path / "tangtang.log.2026-08-27"
    log.write_text("已读\n", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)
    assert cursor.read_new() == ["已读"]

    # 模拟 rename 与新文件创建之间的轮询间隙；旧文件仍可被轮转扫描找到。
    log.replace(rotated)
    assert cursor.read_new() == []
    assert cursor.issues.get("file_missing", 0) == 0
    log.write_text("恢复后\n", encoding="utf-8")
    assert cursor.read_new() == ["恢复后"]
    assert cursor.issues.get("file_missing", 0) == 0
    assert cursor.issues.get("rotation_tail_missing", 0) == 0

    summary = merge_log_summaries(parse_log_events([]))
    for name, count in cursor.issues.items():
        summary["events"][f"log_{name}"] = count
    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], summary)

    assert report["domains"]["observer_integrity"]["status"] == "PASS"


def test_log_cursor_handles_partial_utf8_and_truncation(tmp_path):
    from tools.runtime_observer import LogCursor

    log = tmp_path / "tangtang.log"
    log.write_bytes(b"")
    cursor = LogCursor(log, start_at_end=False)

    encoded = "糖糖已上线\n".encode("utf-8")
    log.write_bytes(encoded[:-2])
    assert cursor.read_new() == []
    with log.open("ab") as fh:
        fh.write(encoded[-2:])
    assert cursor.read_new() == ["糖糖已上线"]

    log.write_text("新启动\n", encoding="utf-8")
    assert cursor.read_new() == ["新启动"]


def test_log_cursor_reads_complete_line_without_trailing_newline(tmp_path):
    from tools.runtime_observer import LogCursor

    log = tmp_path / "tangtang.log"
    log.write_text("最后一条完整日志", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)

    assert cursor.read_new(final=True) == ["最后一条完整日志"]


def test_log_cursor_detects_midnight_file_replacement(tmp_path):
    from tools.runtime_observer import LogCursor

    log = tmp_path / "tangtang.log"
    rotated = tmp_path / "tangtang.log.2026-08-27"
    log.write_text("旧行\n", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)
    assert cursor.read_new() == ["旧行"]

    log.replace(rotated)
    log.write_text("新日志第一行\n", encoding="utf-8")

    assert cursor.read_new() == ["新日志第一行"]


def test_log_cursor_drains_unread_rotated_tail_before_new_file(tmp_path):
    from tools.runtime_observer import LogCursor

    log = tmp_path / "tangtang.log"
    rotated = tmp_path / "tangtang.log.2026-08-27"
    log.write_text("已经读过\n", encoding="utf-8")
    cursor = LogCursor(log, start_at_end=False)
    assert cursor.read_new() == ["已经读过"]

    with log.open("a", encoding="utf-8") as handle:
        handle.write("轮转前未读尾部\n")
    log.replace(rotated)
    log.write_text("轮转后的新行\n", encoding="utf-8")

    assert cursor.read_new() == ["轮转前未读尾部", "轮转后的新行"]


def test_audio_singing_and_role_switch_need_real_success_evidence():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    events = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.ASR | INFO | 🎤 ASR → 我想听歌",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎤 Tool唱歌已确认: 测试歌 (rvc)",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-1 | 糖糖.Voice | INFO | 🎙️ GPT-SoVITS 模型已切换: michele",
        "2026-08-27 16:00:03+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎨 表情包切换: michele | dir=x count=235",
    ])
    report = evaluate_observation([before, after], events)

    assert report["domains"]["audio_input"]["status"] == "PASS"
    assert report["domains"]["singing"]["status"] == "PASS"
    assert report["domains"]["role_switch"]["status"] == "PASS"


def test_direct_voice_command_is_counted_without_double_counting_normal_voice():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    direct = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🎙️ 语音发送: 情绪=温柔 音色=x 语速=1.00 停顿=自然",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎤 语音已发送",
    ])
    report = evaluate_observation([before, after], direct)
    assert report["domains"]["voice"]["status"] == "PASS"
    assert report["domains"]["voice"]["evidence"]["attempted"] == 1

    normal = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎤 语音触发: voice_mode=False, tool=True",
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎙️ 语音发送: 情绪=温柔 音色=x 语速=1.00 停顿=自然",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | 🎤 语音已发送",
    ])
    report = evaluate_observation([before, after], normal)
    assert report["domains"]["voice"]["evidence"]["attempted"] == 1


def test_role_switch_same_weight_profile_uses_completed_sticker_step():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    same_weight_switch = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.Handler | INFO | "
        "🎨 表情包切换: murasame | dir=x count=100",
    ])
    report = evaluate_observation([before, after], same_weight_switch)
    assert report["domains"]["role_switch"]["status"] == "PASS"

    startup_weight_load = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=- | 糖糖.Voice | INFO | "
        "🎙️ GPT-SoVITS 模型已切换: v4",
    ])
    report = evaluate_observation([before, after], startup_weight_load)
    assert report["domains"]["role_switch"]["status"] == "INSUFFICIENT"


def test_audio_and_singing_failures_cannot_be_masked_by_successes():
    from tools.runtime_observer import evaluate_observation, parse_log_events

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    events = parse_log_events([
        "2026-08-27 16:00:00+0800 | boot=abc cid=evt-1 | 糖糖.ASR | INFO | 🎤 ASR → 成功样本",
        "2026-08-27 16:00:01+0800 | boot=abc cid=evt-2 | 糖糖.ASR | ERROR | ❌ 语音识别失败: timeout",
        "2026-08-27 16:00:02+0800 | boot=abc cid=evt-3 | 糖糖.Handler | INFO | 🎤 Tool唱歌已确认: 成功样本",
        "2026-08-27 16:00:03+0800 | boot=abc cid=evt-4 | 糖糖.Handler | WARNING | 🎤 Tool唱歌音频未确认: state=uncertain",
    ])
    report = evaluate_observation([before, after], events)

    assert report["domains"]["audio_input"]["status"] == "FAIL"
    assert report["domains"]["singing"]["status"] == "FAIL"


def test_read_only_store_never_creates_or_writes_database(tmp_path):
    from agent.store import ReadOnlyStore, Store

    missing = tmp_path / "missing.db"
    ro_missing = ReadOnlyStore(str(missing))
    try:
        ro_missing.get_global_stats()
    except sqlite3.OperationalError:
        pass
    assert not missing.exists()

    db = tmp_path / "memory.db"
    writable = Store(str(db))
    writable.insert_chat("u1", "hello", is_bot=False)
    readonly = ReadOnlyStore(str(db))

    assert readonly.get_global_stats()["chat_count"] == 1
    try:
        readonly.insert_chat("u2", "must fail", is_bot=False)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("ReadOnlyStore unexpectedly allowed a write")
    assert writable.get_global_stats()["chat_count"] == 1


def test_read_only_store_lists_inbound_review_facts_without_message_body(tmp_path):
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "memory.db"
    writable = Store(str(db))
    event_key = "v2:group:100:1700000000:200:300"
    writable.register_inbound_event(event_key, "group")
    writable.claim_inbound_event(event_key)
    writable.mark_inbound_event_executing(event_key)
    writable.fail_inbound_event(event_key, "RuntimeError")
    writable.insert_chat(
        "200", "private body must not be returned", group_id="100",
        is_bot=False, timestamp="2023-11-14 22:13:20", event_key=event_key,
    )

    rows = ReadOnlyStore(str(db)).list_inbound_events(
        statuses=("failed", "uncertain"), limit=10,
    )

    assert rows == [{
        "event_key": event_key,
        "event_type": "group",
        "status": "failed",
        "attempts": 1,
        "last_error": "RuntimeError",
        "received_at": rows[0]["received_at"],
        "updated_at": rows[0]["updated_at"],
        "chat_log_id": rows[0]["chat_log_id"],
        "chat_log_present": True,
        "chat_log_is_bot": False,
        "chat_log_timestamp": "2023-11-14 22:13:20",
    }]
    assert "private body" not in json.dumps(rows, ensure_ascii=False)


def test_capture_snapshot_redacts_user_rows_and_outbox_payload(tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    db = tmp_path / "memory.db"
    store = Store(str(db))
    store.insert_chat("raw-user-id", "raw private message", is_bot=False)
    store.enqueue_send_outbox(
        "private", "raw-target-id", "raw outbound message",
        error="timeout",
    )
    store.kv_set(
        "metric:2026-08-27:users_zero_memory_list",
        "10001",
    )
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)

    snapshot = capture_snapshot(db)
    rendered = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["database"]["ok"] is True
    assert snapshot["database"]["backlog"]["backlog_messages"] == 1
    assert "users" not in snapshot["database"]["backlog"]
    assert snapshot["database"]["outbox"]["open"] == 1
    assert "raw-user-id" not in rendered
    assert "raw private message" not in rendered
    assert "raw-target-id" not in rendered
    assert "raw outbound message" not in rendered
    assert "10001" not in rendered


def test_capture_snapshot_separates_eligible_and_deferred_memory_tails(
        tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    db = tmp_path / "memory.db"
    store = Store(str(db))
    for index in range(3):
        store.insert_chat(
            "aged-user", f"旧消息{index}",
            timestamp="2026-08-01 09:00:00",
        )
    store.insert_chat(
        "recent-user", "刚来的消息",
        timestamp="2026-08-28 08:30:00",
    )
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)

    snapshot = capture_snapshot(
        db,
        now=datetime.fromisoformat("2026-08-28T09:00:00+08:00"),
    )
    backlog = snapshot["database"]["backlog"]

    assert backlog["backlog_messages"] == 4
    assert backlog["eligible_messages"] == 3
    assert backlog["eligible_users"] == 1
    assert backlog["deferred_messages"] == 1
    assert "users" not in backlog


def test_capture_snapshot_marks_configured_gpt_sovits_required(tmp_path, monkeypatch):
    """已启用的 GPT-SoVITS 不能被观察器当作可选服务而漏报退出。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    db = tmp_path / "memory.db"
    Store(str(db))
    config = tmp_path / "config.yaml"
    config.write_text(
        "voice:\n  enabled: true\n  provider: gpt-sovits\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: False)

    snapshot = capture_snapshot(db, config_path=config)

    assert snapshot["services"]["gpt_sovits"] == {
        "required": True, "up": False,
    }


def test_capture_snapshot_keeps_disabled_voice_optional(tmp_path, monkeypatch):
    """关闭语音时不应凭空要求 GPT-SoVITS 在线。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    db = tmp_path / "memory.db"
    Store(str(db))
    config = tmp_path / "config.yaml"
    config.write_text(
        "voice:\n  enabled: false\n  provider: gpt-sovits\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: False)

    snapshot = capture_snapshot(db, config_path=config)

    assert snapshot["services"]["gpt_sovits"] == {
        "required": False, "up": False,
    }


def test_required_local_services_fails_open_on_invalid_yaml(tmp_path):
    """配置损坏不能让只读观察器整体退出。"""
    from tools.runtime_observer import _required_local_services

    config = tmp_path / "broken.yaml"
    config.write_text("voice: [", encoding="utf-8")

    assert _required_local_services(config) == {
        "napcat": True,
        "reverse_ws": True,
        "gpt_sovits": False,
        "diffsinger": False,
        "cosyvoice": False,
    }


def test_snapshot_unknown_enums_are_redacted_and_cannot_false_pass(tmp_path, monkeypatch):
    """未知枚举只允许汇总为 unknown，原值不得进入快照或健康结论。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    db = tmp_path / "memory.db"
    store = Store(str(db))
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    before = capture_snapshot(db)

    memory_id = store.insert_memory("u1", "fact", "safe value")
    job = store.create_extraction_job(
        "u1", [{"id": 1, "group_id": ""}],
    )
    action_id = store.enqueue_send_outbox(
        "private", "safe-target", "safe outbound message",
    )
    secrets = {
        "trust": "SECRET_TRUST_LEVEL",
        "queue": "SECRET_JOB_STATUS",
        "outbox": "SECRET_OUTBOX_STATUS",
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE memories SET trust_level=? WHERE id=?",
            (secrets["trust"], memory_id),
        )
        conn.execute(
            "UPDATE extraction_jobs SET status=? WHERE id=?",
            (secrets["queue"], int(job["id"])),
        )
        conn.execute(
            "UPDATE send_outbox SET status=? WHERE action_id=?",
            (secrets["outbox"], action_id),
        )

    after = capture_snapshot(db)
    rendered = json.dumps(after, ensure_ascii=False)

    assert all(secret not in rendered for secret in secrets.values())
    assert after["database"]["memory"]["trust_levels"]["unknown"] == 1
    assert after["database"]["memory"]["trusted"] == 0
    assert after["database"]["queue"]["unknown"] == 1
    assert after["database"]["queue"]["total_open"] == 0
    assert after["database"]["outbox"]["unknown"] == 1
    assert after["database"]["outbox"]["open"] == 0
    assert after["database"]["outbox"]["needs_review"] == 0

    report = evaluate_observation([before, after], {"events": {}})

    assert report["domains"]["memory"]["status"] != "PASS"
    assert report["domains"]["sending"]["status"] != "PASS"


def test_known_confirmed_local_failures_are_explicit_and_fail_sending_domain(
        tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    db = tmp_path / "memory.db"
    store = Store(str(db))
    outbox_id = store.enqueue_send_outbox("group", "g1", "safe message")
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "UPDATE send_outbox SET status='confirmed_unaccounted',"
            "confirmed_message_ids='[88]',confirmed_at='2026-08-27 16:30:00',"
            "projection_error='PROJECTION_LOCAL_ERROR' WHERE action_id=?",
            (outbox_id,),
        )
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)

    after = capture_snapshot(db)
    before = _snapshot(metrics={
        "gateway_send_attempts": 1, "gateway_send_confirmed": 1,
    })
    after["database"]["metrics"] = {
        "gateway_send_attempts": 2, "gateway_send_confirmed": 2,
    }

    assert after["database"]["outbox"]["confirmed_unaccounted"] == 1
    assert after["database"]["outbox"]["unknown"] == 0
    assert after["database"]["outbox"]["needs_review"] == 1
    report = evaluate_observation([before, after], {"events": {}})
    assert report["domains"]["sending"]["status"] == "FAIL"
    assert report["domains"]["sending"]["evidence"][
        "outbox_confirmed_unaccounted"
    ] == 1


def test_task_action_invariant_drift_is_visible_without_payload_leak(
        tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    db = tmp_path / "memory.db"
    store = Store(str(db))
    task_id = store.create_task(
        "raw-owner", "raw-description", "2020-01-01 00:00",
        action_payload={"text": "raw-frozen-message"},
    )
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_text_action(task_id, "raw-frozen-message")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER trg_task_action_task_status_guard")
        conn.execute("DROP TRIGGER trg_task_action_outbox_delete_requires_confirmation")
        conn.execute(
            "DELETE FROM send_outbox WHERE action_id=?", (action["outbox_id"],),
        )
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)

    after = capture_snapshot(db)
    rendered = json.dumps(after, ensure_ascii=False)
    assert after["database"]["outbox"]["open"] == 0
    assert after["database"]["outbox"]["linked_invariant_violations"] == 1
    assert "raw-owner" not in rendered
    assert "raw-description" not in rendered
    assert "raw-frozen-message" not in rendered

    before = _snapshot(metrics={
        "gateway_send_attempts": 1, "gateway_send_confirmed": 1,
    })
    after["database"]["metrics"] = {
        "gateway_send_attempts": 2, "gateway_send_confirmed": 2,
    }
    sending = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["sending"]
    assert sending["status"] == "FAIL"
    assert sending["evidence"]["linked_invariant_violations"] == 1


def test_bare_sending_task_is_visible_without_user_or_payload_leak(
        tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    db = tmp_path / "memory.db"
    store = Store(str(db))
    task_id = store.create_task(
        "secret-owner", "secret-description", "2020-01-01 00:00",
        action_payload={"text": "secret-payload"},
    )
    assert store.claim_task_for_send(task_id)
    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)

    before = capture_snapshot(db)
    after = capture_snapshot(db)
    rendered = json.dumps(after, ensure_ascii=False)
    assert after["database"]["outbox"]["bare_sending_tasks"] == 1
    assert after["database"]["outbox"]["needs_review"] >= 1
    assert "secret-owner" not in rendered
    assert "secret-description" not in rendered
    assert "secret-payload" not in rendered

    before["database"]["metrics"] = {
        "gateway_send_attempts": 1, "gateway_send_confirmed": 1,
    }
    after["database"]["metrics"] = {
        "gateway_send_attempts": 2, "gateway_send_confirmed": 2,
    }
    sending = evaluate_observation(
        [before, after], {"events": {}},
    )["domains"]["sending"]
    assert sending["status"] == "FAIL"
    assert sending["evidence"]["bare_sending_tasks"] == 1
    assert sending["evidence"]["persistent_bare_sending_tasks"] == 1


def test_snapshot_metric_integrity_rejects_garbage_and_negative_values(tmp_path, monkeypatch):
    """指标值不是非负整数时只留计数证据，不得泄露原值或假绿。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot, evaluate_observation

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    captured_at = datetime.fromisoformat("2026-08-27T17:00:00+08:00")
    cases = (
        ("garbage", "garbage", "invalid_values"),
        ("negative", "-7", "negative_values"),
    )
    for suffix, raw_value, evidence_key in cases:
        db = tmp_path / f"{suffix}.db"
        store = Store(str(db))
        store.kv_set("metric:2026-08-27:gateway_send_attempts", raw_value)

        before = capture_snapshot(db, now=captured_at)
        after = capture_snapshot(db, now=captured_at)
        rendered = json.dumps(after, ensure_ascii=False)
        metric_integrity = after["database"]["metric_integrity"]

        assert raw_value not in rendered
        assert metric_integrity[evidence_key] == 1
        assert sum(metric_integrity.values()) == 1

        report = evaluate_observation([before, after], {"events": {}})

        assert report["domains"]["observer_integrity"]["status"] == "FAIL", suffix
        assert report["domains"]["sending"]["status"] != "PASS", suffix


def test_snapshot_accepts_metrics_latest_mirror_without_double_counting(tmp_path, monkeypatch):
    """MemoryMetrics 的 latest 镜像是合法键，不应制造非法指标或重复总计。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    db = tmp_path / "memory.db"
    store = Store(str(db))
    store.kv_set("metric:2026-08-27:gateway_send_attempts", "7")
    store.kv_set("metric:latest:gateway_send_attempts", "7")

    snapshot = capture_snapshot(
        db, now=datetime.fromisoformat("2026-08-27T17:00:00+08:00"),
    )

    assert snapshot["database"]["metric_integrity"] == {
        "invalid_values": 0, "negative_values": 0,
    }
    assert snapshot["database"]["metrics"]["gateway_send_attempts"] == 7
    assert snapshot["database"]["metrics_total"]["gateway_send_attempts"] == 7


def test_snapshot_includes_window_funnel_metrics(tmp_path, monkeypatch):
    """E1 窗口指标必须进入生产观察快照，不能只在 /状态 中可见。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    db = tmp_path / "memory.db"
    store = Store(str(db))
    names = (
        "window_candidate", "window_entry_direct", "window_continuation",
        "window_routed", "window_decisions_total", "window_decisions_reply",
        "window_decisions_skip", "window_fade_after_silence",
    )
    for index, name in enumerate(names, start=1):
        store.kv_set(f"metric:2026-08-27:{name}", str(index))

    snapshot = capture_snapshot(
        db, now=datetime.fromisoformat("2026-08-27T17:00:00+08:00"),
    )

    assert {name: snapshot["database"]["metrics"][name] for name in names} == {
        name: index for index, name in enumerate(names, start=1)
    }
    assert {name: snapshot["database"]["metrics_total"][name] for name in names} == {
        name: index for index, name in enumerate(names, start=1)
    }


def test_snapshot_includes_extraction_job_lifecycle_metrics(tmp_path, monkeypatch):
    """job 级提取计数必须进入只读快照，不能只停留在 /状态。"""
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    db = tmp_path / "memory.db"
    store = Store(str(db))
    names = (
        "extract_attempts",
        "extract_jobs_created", "extract_jobs_lease_acquired",
        "extract_jobs_ready", "extract_jobs_completed",
        "extract_job_llm_started", "extract_job_llm_succeeded",
    )
    for index, name in enumerate(names, start=1):
        store.kv_set(f"metric:2026-08-27:{name}", str(index))

    snapshot = capture_snapshot(
        db, now=datetime.fromisoformat("2026-08-27T17:00:00+08:00"),
    )

    assert {name: snapshot["database"]["metrics"][name] for name in names} == {
        name: index for index, name in enumerate(names, start=1)
    }
    assert {name: snapshot["database"]["metrics_total"][name] for name in names} == {
        name: index for index, name in enumerate(names, start=1)
    }


def test_snapshot_includes_inbound_inbox_health(tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    db = tmp_path / "memory.db"
    store = Store(str(db))
    assert store.register_inbound_event("evt:ok", "group") == "received"
    assert store.claim_inbound_event("evt:ok")
    assert store.mark_inbound_event_executing("evt:ok")
    assert store.complete_inbound_event("evt:ok")
    assert store.register_inbound_event("evt:failed", "private") == "received"
    assert store.claim_inbound_event("evt:failed")
    assert store.mark_inbound_event_executing("evt:failed")
    assert store.fail_inbound_event("evt:failed", "RuntimeError")

    inbox = capture_snapshot(db)["database"]["inbox"]

    assert inbox["processed"] == 1
    assert inbox["failed"] == 1
    assert inbox["needs_review"] == 1


def test_snapshot_exposes_deidentified_inbound_decision_lifecycle_counts(
        tmp_path, monkeypatch):
    """观察快照须能证明入站事实已接到决策，且不泄露关联主键或正文。"""
    from agent.interaction_contract import DecisionRun
    from agent.store import Store
    from tools.runtime_observer import capture_snapshot

    monkeypatch.setattr("tools.runtime_observer._port_open", lambda _port: True)
    db = tmp_path / "memory.db"
    store = Store(str(db))
    event_key = "v2:group:g1:1700000000:u1:9"
    assert store.register_inbound_event(event_key, "group") == "received"
    assert store.claim_inbound_event(event_key)
    assert store.mark_inbound_event_executing(event_key)
    assert store.complete_inbound_event(event_key)
    store.insert_chat(
        "u1", "private body must not be exported", group_id="g1",
        is_bot=False, event_key=event_key,
    )
    store.record_decision_run(DecisionRun.start(
        run_id="run-1", event_key=event_key, scope_id="group:g1",
        correlation_id="cid-1",
    ).finish(decision="reply"))

    lifecycle = capture_snapshot(db)["database"]["interaction_lifecycle"]

    assert lifecycle == {
        "inbound_chat_links": 1,
        "inbound_decision_runs": 1,
        "inbound_confirmed_action_receipts": 0,
        "complete_lifecycles": 0,
        "schema_missing": False,
    }
    serialized = json.dumps(lifecycle, ensure_ascii=False)
    assert event_key not in serialized
    assert "private body" not in serialized
    assert "u1" not in serialized and "g1" not in serialized


def test_snapshot_daily_hygiene_uses_captured_local_date(tmp_path):
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "memory.db"
    store = Store(str(db))
    store.insert_memory("u1", "fact", "SKIP internal reasoning", timestamp="2026-08-27 23:59")
    store.insert_memory("u1", "fact-copy", "SKIP internal reasoning", timestamp="2026-08-27 23:59")
    store.insert_memory("u1", "next-day", "normal", timestamp="2026-08-28 00:01")

    readonly = ReadOnlyStore(str(db))
    previous = readonly.get_runtime_observer_snapshot(
        datetime.fromisoformat("2026-08-27T23:59:00+08:00"),
    )
    current = readonly.get_runtime_observer_snapshot(
        datetime.fromisoformat("2026-08-28T00:05:00+08:00"),
    )

    assert previous["memory"]["counter_day"] == "2026-08-27"
    assert previous["memory"]["reasoning_leaks_today"] == 2
    assert previous["memory"]["duplicate_groups_today"] == 1
    assert current["memory"]["counter_day"] == "2026-08-28"
    assert current["memory"]["reasoning_leaks_today"] == 0
    assert current["memory"]["duplicate_groups_today"] == 0


def test_snapshot_treats_unverified_memory_as_known_review_state(tmp_path):
    """unverified 是提取结果的合法待复核层，不应被观察器报成 unknown。"""
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "memory.db"
    Store(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memories "
            "(qq_id,key,value,timestamp,trust_level,status) "
            "VALUES ('u1','fact','待复核事实','2026-08-28 08:00','unverified','active')"
        )
        conn.commit()

    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime.fromisoformat("2026-08-28T09:00:00+08:00"),
    )

    assert snapshot["memory"]["trust_levels"]["unverified"] == 1
    assert snapshot["memory"]["trust_levels"]["unknown"] == 0


def test_snapshot_includes_durable_window_integrity(tmp_path):
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "memory.db"
    store = Store(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO conversation_window_events "
            "(event_key,domain_action_id,scope_id,channel,actor_kind,"
            "conversation_user_id,group_id,chat_log_id,text,occurred_at) "
            "VALUES ('e1','a1','wrong','group','bot','u1','g1',1,'x','2099-01-01 00:00:00')"
        )
    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime.fromisoformat("2026-08-28T09:00:00+08:00"),
    )
    assert snapshot["window"]["event_count"] == 1
    assert snapshot["window"]["invalid_events"] == 1
    assert snapshot["window"]["future_events"] == 1


def test_snapshot_legacy_database_without_window_table_is_explicitly_safe(tmp_path):
    from agent.store import ReadOnlyStore, Store

    db = tmp_path / "legacy.db"
    Store(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE conversation_window_events")
    snapshot = ReadOnlyStore(str(db)).get_runtime_observer_snapshot(
        datetime.fromisoformat("2026-08-28T09:00:00+08:00"),
    )
    assert snapshot["window"]["schema_missing"] is True
    assert snapshot["window"]["event_count"] == 0


def test_report_contains_feature_domains_and_three_state_explanation():
    from tools.runtime_observer import evaluate_observation, render_markdown

    before = _snapshot()
    after = _snapshot()
    after["captured_at"] = "2026-08-27T17:00:00+08:00"
    report = evaluate_observation([before, after], {"events": {}})
    rendered = render_markdown(report)

    assert "QQ接入" in rendered
    assert "记忆" in rendered
    assert "窗口对话" in rendered
    assert "语音" in rendered
    assert "识图" in rendered
    assert "贴图" in rendered
    assert "知识工具" in rendered
    assert "自治" in rendered
    assert "提醒" in rendered
    assert "生日祝福" in rendered
    assert "定时图片" in rendered
    assert "INSUFFICIENT" in rendered
    assert "不代表故障" in rendered


def test_main_returns_documented_observer_error_for_invalid_output_path(tmp_path, monkeypatch):
    from agent.store import Store
    from tools.runtime_observer import main

    db = tmp_path / "memory.db"
    Store(str(db))
    log = tmp_path / "tangtang.log"
    log.write_text("", encoding="utf-8")
    bad_output = tmp_path / "not-a-directory"
    bad_output.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(
        "tools.runtime_observer._parse_args",
        lambda: SimpleNamespace(
            duration_hours=0.0,
            interval_seconds=1.0,
            db=str(db),
            log=str(log),
            output_dir=str(bad_output),
            from_start=False,
            once=True,
            expected_restarts=None,
        ),
    )

    assert main() == 4


def test_main_rejects_from_start_in_restart_recovery_mode(monkeypatch):
    from tools.runtime_observer import main

    monkeypatch.setattr(
        "tools.runtime_observer._parse_args",
        lambda: SimpleNamespace(
            duration_hours=0.0,
            interval_seconds=1.0,
            db="unused.db",
            log="unused.log",
            output_dir="unused",
            from_start=True,
            once=True,
            expected_restarts=1,
        ),
    )

    assert main() == 4
