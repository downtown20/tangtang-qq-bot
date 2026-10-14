import asyncio
import json

import pytest

from tools import memory_llm_soak
from tools.memory_llm_soak import SoakConfig, run_simulated_soak


def test_simulated_soak_drains_concurrent_faulted_workload():
    config = SoakConfig(
        jobs=12,
        users=6,
        producer_count=4,
        worker_count=2,
        arrival_jobs_per_second=200,
        messages_per_job=10,
        base_latency_seconds=0.001,
        jitter_seconds=0,
        slow_seconds=0.01,
        drain_timeout_seconds=8,
        sample_interval_seconds=0.01,
        probe_interval_seconds=0.005,
        max_foreground_wait_p95_ms=250,
        fault_script=(
            "429", "ok", "5xx", "ok", "invalid", "ok", "slow",
        ),
        rate_limit_rate=0,
        server_error_rate=0,
        timeout_rate=0,
        invalid_json_rate=0,
        empty_rate=0,
        slow_rate=0,
        seed=20260827,
    )

    result = asyncio.run(run_simulated_soak(config))

    assert result["passed"] is True
    assert result["jobs_created"] == result["jobs_done"] == 12
    assert result["queue_final"]["total_open"] == 0
    assert result["queue_final"].get("dead", 0) == 0
    assert result["integrity"] == {
        "cross_scope_leaks": 0,
        "duplicate_memories": 0,
        "cursor_mismatches": 0,
        "verified_memories": 12,
        "verified_evidence_links": 12,
        "memories_without_verified_evidence": 0,
    }
    assert result["users_requested"] == result["users_covered"] == 6
    assert result["injection"]["jobs_created"] == 12
    assert result["drain"]["jobs_done_after_injection"] >= 0
    assert result["faults"] == {
        "429": 1,
        "5xx": 1,
        "timeout": 0,
        "invalid": 1,
        "empty": 0,
        "slow": 1,
            # slow 是一次成功调用，只计入 slow，不再额外计入 ok。
            "ok": 11,
    }
    assert result["samples"]


def test_simulated_soak_reports_capacity_failure_when_drain_budget_is_too_small():
    config = SoakConfig(
        jobs=4,
        users=4,
        producer_count=2,
        worker_count=1,
        arrival_jobs_per_second=500,
        messages_per_job=10,
        base_latency_seconds=0.2,
        drain_timeout_seconds=0.02,
        sample_interval_seconds=0.005,
        probe_interval_seconds=0.005,
        seed=7,
    )

    result = asyncio.run(run_simulated_soak(config))

    assert result["passed"] is False
    assert result["jobs_done"] < result["jobs_created"]
    assert result["queue_final"]["total_open"] > 0


def test_simulated_soak_fails_when_requested_users_are_not_covered():
    result = asyncio.run(run_simulated_soak(SoakConfig(
        jobs=2,
        users=3,
        producer_count=1,
        worker_count=1,
        arrival_jobs_per_second=500,
        messages_per_job=1,
        base_latency_seconds=0,
        jitter_seconds=0,
        drain_timeout_seconds=2,
    )))

    assert result["jobs_done"] == 2
    assert result["users_covered"] == 2
    assert result["users_requested"] == 3
    assert result["passed"] is False


def test_simulated_soak_fails_when_jobs_finish_without_verified_memories(monkeypatch):
    async def no_evidence(_self, _system_prompt, _user_message):
        return json.dumps([{
            "type": "note",
            "value": "没有原文证据的伪记忆",
            "confidence": 0.95,
            "evidence_ids": [],
        }], ensure_ascii=False)

    monkeypatch.setattr(memory_llm_soak._FaultInjectingLLM, "__call__", no_evidence)
    result = asyncio.run(run_simulated_soak(SoakConfig(
        jobs=2,
        users=2,
        producer_count=1,
        worker_count=1,
        arrival_jobs_per_second=500,
        messages_per_job=1,
        base_latency_seconds=0,
        drain_timeout_seconds=2,
    )))

    assert result["jobs_done"] == 2
    assert result["integrity"]["verified_memories"] == 0
    assert result["integrity"]["verified_evidence_links"] == 0
    assert result["passed"] is False


def test_simulated_soak_does_not_bypass_dead_letter_cooldown():
    result = asyncio.run(run_simulated_soak(SoakConfig(
        jobs=1,
        users=1,
        producer_count=1,
        worker_count=1,
        arrival_jobs_per_second=500,
        messages_per_job=1,
        base_latency_seconds=0,
        jitter_seconds=0,
        # 该断言验证 dead-letter 上限/冷却，不验证亚秒级吞吐。全量套件在
        # Windows 上会让同步 SQLite 调度出现长尾；给 5 次无延迟失败一个
        # 有界但不脆弱的收口预算，容量短窗口另由下一测试覆盖。
        drain_timeout_seconds=4.0,
        fault_script=("invalid",) * 8,
    )))

    assert result["passed"] is False
    assert result["queue_final"]["dead"] == 1
    assert result["faults"]["invalid"] == 5


def test_simulated_soak_rejects_unknown_fault_script():
    with pytest.raises(ValueError, match="unsupported fault_script outcome"):
        asyncio.run(run_simulated_soak(SoakConfig(
            jobs=1, users=1, fault_script=("typo",),
        )))


# ═══════════════════════════════════════════════════════
# 偶发失败根因回归（2026-08-28）：foreground_lock_wait_ms 把「事件循环被
# 同步 SQLite 阻塞」误计为「LLM 锁等待」——锁空闲时 acquire 的恢复由事件
# 循环调度，SQLite busy_timeout 竞争可致恢复延迟 1.6s，p95 偶发超 250ms。
# ═══════════════════════════════════════════════════════

class _SlowRecoveryFakeLock:
    """锁实际空闲，但 acquire 的恢复被事件循环调度延迟——
    模拟 worker 的同步 SQLite 写阻塞事件循环时的旧 probe 误报。"""

    def __init__(self, recovery_seconds):
        self._recovery = recovery_seconds

    def locked(self):
        return False

    async def acquire(self):
        await asyncio.sleep(self._recovery)
        return True

    def release(self):
        pass


def test_foreground_wait_zero_when_lock_free_despite_blocked_loop():
    """根因回归：锁空闲时测量必须为 0——事件循环调度延迟
    （同步 SQLite 阻塞）不得计入前台 LLM 锁等待。"""
    from tools.memory_llm_soak import _measure_foreground_wait
    lock = _SlowRecoveryFakeLock(recovery_seconds=0.3)
    wait_ms = asyncio.run(_measure_foreground_wait(lock))
    assert wait_ms == 0.0


def test_foreground_wait_measures_real_contention():
    """锁忙时仍测量真实等待（修复不吞真实竞争样本）"""
    from tools.memory_llm_soak import _measure_foreground_wait

    async def scenario():
        lock = asyncio.Lock()
        async def holder():
            await lock.acquire()
            await asyncio.sleep(0.05)
            lock.release()
        task = asyncio.create_task(holder())
        await asyncio.sleep(0.01)  # holder 已持锁
        wait_ms = await _measure_foreground_wait(lock)
        await task
        return wait_ms

    wait_ms = asyncio.run(scenario())
    assert 30 <= wait_ms <= 70  # 真实锁等待 ~50ms


def test_sample_write_failure_fails_acceptance_and_closes_file(monkeypatch, tmp_path):
    class BrokenSamplesFile:
        def __init__(self):
            self.closed = False

        def write(self, _value):
            raise OSError("disk full")

        def flush(self):
            pass

        def close(self):
            self.closed = True

    broken = BrokenSamplesFile()
    original_open = memory_llm_soak.Path.open

    def patched_open(path, *args, **kwargs):
        if path == tmp_path / "samples.jsonl":
            return broken
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(memory_llm_soak.Path, "open", patched_open)
    result = asyncio.run(run_simulated_soak(SoakConfig(
        jobs=2,
        users=2,
        producer_count=1,
        worker_count=1,
        arrival_jobs_per_second=10,
        messages_per_job=1,
        base_latency_seconds=0.01,
        jitter_seconds=0,
        drain_timeout_seconds=2,
        sample_interval_seconds=0.001,
        samples_path=str(tmp_path / "samples.jsonl"),
    )))

    assert result["passed"] is False
    assert any("disk full" in error for error in result["errors"])
    assert broken.closed is True
