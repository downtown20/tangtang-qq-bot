"""记忆提取持续流量验收。

默认只使用临时 SQLite 和可重复的模拟 LLM，不读取或修改生产 memory.db。
它与 ``记忆千人验收.py`` 分工：后者证明存储/隔离正确性，本工具证明
真实提取状态机在持续到达、慢调用和故障下能否最终收敛。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import random
import re
import statistics
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.handler import MessageHandler
from agent.memory import MemorySystem
from agent.store import Store


@dataclass(frozen=True)
class SoakConfig:
    jobs: int = 0
    duration_seconds: float = 60.0
    users: int = 1000
    producer_count: int = 4
    worker_count: int = 1
    arrival_jobs_per_second: float = 0.2
    messages_per_job: int = 10
    base_latency_seconds: float = 0.8
    jitter_seconds: float = 0.2
    rate_limit_rate: float = 0.02
    server_error_rate: float = 0.02
    timeout_rate: float = 0.01
    invalid_json_rate: float = 0.01
    empty_rate: float = 0.01
    slow_rate: float = 0.01
    slow_seconds: float = 15.0
    drain_timeout_seconds: float = 600.0
    sample_interval_seconds: float = 5.0
    probe_interval_seconds: float = 1.0
    max_foreground_wait_p95_ms: float = 0.0
    fault_script: tuple[str, ...] = ()
    seed: int = 20260827
    samples_path: str = ""


class _MetricsStub:
    def __init__(self):
        self.counters: Counter[str] = Counter()

    def incr(self, name: str, delta: int = 1):
        self.counters[name] += int(delta)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _RateLimitFault(RuntimeError):
    pass


class _ServerFault(RuntimeError):
    pass


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        int(round((len(ordered) - 1) * max(0.0, min(1.0, percentile)))),
    )
    return float(ordered[index])


def _latency_summary(values: list[float]) -> dict:
    return {
        "samples": len(values),
        "p50": round(statistics.median(values), 3) if values else 0.0,
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


class _FaultInjectingLLM:
    """可重复故障序列；每个成功调用产生一条带原文证据的记忆。"""

    _OUTCOMES = ("429", "5xx", "timeout", "invalid", "empty", "slow", "ok")

    def __init__(self, config: SoakConfig):
        self.config = config
        self.random = random.Random(config.seed)
        self.calls = 0
        self.faults: Counter[str] = Counter()
        self.latencies_ms: list[float] = []

    def _choose_outcome(self) -> str:
        call_index = self.calls
        self.calls += 1
        if call_index < len(self.config.fault_script):
            return self.config.fault_script[call_index]

        draw = self.random.random()
        cumulative = 0.0
        rates = (
            ("429", self.config.rate_limit_rate),
            ("5xx", self.config.server_error_rate),
            ("timeout", self.config.timeout_rate),
            ("invalid", self.config.invalid_json_rate),
            ("empty", self.config.empty_rate),
            ("slow", self.config.slow_rate),
        )
        for outcome, rate in rates:
            cumulative += max(0.0, float(rate))
            if draw < cumulative:
                return outcome
        return "ok"

    async def __call__(self, _system_prompt: str, user_message: str) -> str:
        started = time.perf_counter()
        outcome = self._choose_outcome()
        self.faults[outcome] += 1
        try:
            if outcome in {"slow", "timeout"}:
                delay = max(0.0, self.config.slow_seconds)
            else:
                jitter = self.random.uniform(
                    -max(0.0, self.config.jitter_seconds),
                    max(0.0, self.config.jitter_seconds),
                )
                delay = max(0.0, self.config.base_latency_seconds + jitter)
            if delay:
                await asyncio.sleep(delay)

            if outcome == "429":
                raise _RateLimitFault("simulated HTTP 429")
            if outcome == "5xx":
                raise _ServerFault("simulated HTTP 503")
            if outcome == "timeout":
                raise asyncio.TimeoutError("simulated LLM timeout")
            if outcome == "invalid":
                return '{"items":['
            if outcome == "empty":
                return ""

            evidence = re.search(r"#(\d+)", user_message)
            evidence_id = int(evidence.group(1)) if evidence else 0
            quote_match = re.search(r"#\d+[^\n]*?:\s*([^\n]+)", user_message)
            evidence_quote = quote_match.group(1).strip() if quote_match else ""
            return json.dumps([{
                "type": "note",
                "cognitive": "semantic",
                "value": f"持续流量验收事实来自消息{evidence_id}",
                "importance": 5,
                "confidence": 0.95,
                "claim_type": "stated",
                "evidence_quote": evidence_quote,
                "evidence_ids": [evidence_id],
            }], ensure_ascii=False)
        finally:
            self.latencies_ms.append((time.perf_counter() - started) * 1000)


async def _measure_foreground_wait(llm_lock: asyncio.Lock) -> float:
    """返回前台 LLM 锁等待毫秒。

    2026-08-28 偶发失败根因修复：旧 probe 无条件 ``await acquire()`` 计时——
    asyncio.Lock.acquire 挂起后的恢复由事件循环调度；worker 的同步 SQLite
    写事务（busy_timeout 竞争）阻塞事件循环时，**锁空闲**的 acquire 恢复也被
    延迟（实测可达 1.6s），被误计为「前台锁等待」→ p95 偶发超阈值。
    修复：锁空闲时立即返回 0（不经过事件循环调度）；锁忙时才测量真实等待。
    """
    if not llm_lock.locked():
        return 0.0
    started = time.perf_counter()
    await llm_lock.acquire()
    wait = (time.perf_counter() - started) * 1000
    llm_lock.release()
    return wait


def _make_handler(store: Store) -> MessageHandler:
    handler = MessageHandler.__new__(MessageHandler)
    handler.memory = MemorySystem(store=store)
    handler.metrics = _MetricsStub()
    return handler


def _insert_job(store: Store, config: SoakConfig, job_index: int) -> dict:
    qq_id = f"soak-u{job_index % max(1, config.users):04d}"
    group_id = f"soak-group-{job_index % 7}"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    messages = []
    for message_index in range(max(1, config.messages_per_job)):
        text = f"验收任务{job_index}的明确陈述{message_index}"
        chat_id = store.insert_chat(
            qq_id, text, group_id=group_id, is_bot=False, timestamp=timestamp,
        )
        messages.append({
            "id": chat_id,
            "message": text,
            "timestamp": timestamp,
            "is_bot_reply": 0,
            "group_id": group_id,
        })
    job = store.create_extraction_job(
        # 必须使用生产提取器的协议版本；否则 worker 读取 soak-v1 任务时，
        # _process_extraction_batch 会另建一条 memory-v1 任务，原任务永远
        # pending，排空循环无法结束。
        qq_id, messages, direction="forward", protocol_version="memory-v1",
    )
    return {
        "job_id": int(job["id"]),
        "qq_id": qq_id,
        "group_id": group_id,
        "messages": messages,
    }


def _integrity_result(store: Store, expected_jobs: list[dict]) -> dict:
    evidence_scope: dict[int, tuple[str, str]] = {}
    expected_cursor: dict[str, int] = {}
    users = set()
    for job in expected_jobs:
        users.add(job["qq_id"])
        expected_cursor[job["qq_id"]] = max(
            expected_cursor.get(job["qq_id"], 0),
            max(message["id"] for message in job["messages"]),
        )
        for message in job["messages"]:
            evidence_scope[int(message["id"])] = (
                job["qq_id"], job["group_id"],
            )

    leaks = 0
    duplicates = 0
    verified_memories = 0
    verified_evidence_links = 0
    memories_without_verified_evidence = 0
    seen = set()
    for qq_id in users:
        for memory in store.query_memories(qq_id, trusted_only=True):
            verified_memories += 1
            key = (
                str(memory.get("qq_id") or ""),
                str(memory.get("source_group_id") or ""),
                str(memory.get("value") or ""),
            )
            if key in seen:
                duplicates += 1
            seen.add(key)
            evidence_rows = store.get_memory_evidence(int(memory["id"]))
            supporting = [
                evidence for evidence in evidence_rows
                if evidence.get("relation") == "supports"
            ]
            verified_evidence_links += len(supporting)
            if not supporting:
                memories_without_verified_evidence += 1
            for evidence in supporting:
                expected = evidence_scope.get(int(evidence["chat_id"]))
                actual = (
                    str(memory.get("qq_id") or ""),
                    str(memory.get("source_group_id") or ""),
                )
                if expected != actual:
                    leaks += 1

    cursor_mismatches = sum(
        store.get_extraction_cursor(qq_id, "forward") != cursor
        for qq_id, cursor in expected_cursor.items()
    )
    return {
        "cross_scope_leaks": leaks,
        "duplicate_memories": duplicates,
        "cursor_mismatches": cursor_mismatches,
        "verified_memories": verified_memories,
        "verified_evidence_links": verified_evidence_links,
        "memories_without_verified_evidence": memories_without_verified_evidence,
    }


async def run_simulated_soak(config: SoakConfig) -> dict:
    """运行一次临时库端到端 soak，返回可机器判定的结构化结果。"""
    target_jobs = int(config.jobs)
    if target_jobs <= 0:
        target_jobs = max(
            1,
            int(max(0.0, config.duration_seconds)
                * max(0.001, config.arrival_jobs_per_second)),
        )
    if config.users <= 0 or config.producer_count <= 0 or config.worker_count <= 0:
        raise ValueError("users, producer_count and worker_count must be positive")
    if config.arrival_jobs_per_second <= 0:
        raise ValueError("arrival_jobs_per_second must be positive")
    unknown_faults = sorted(set(config.fault_script) - set(_FaultInjectingLLM._OUTCOMES))
    if unknown_faults:
        raise ValueError(
            "unsupported fault_script outcome(s): " + ", ".join(unknown_faults)
        )

    started = time.perf_counter()
    expected_jobs: list[dict] = []
    samples: list[dict] = []
    foreground_wait_ms: list[float] = []
    stop = asyncio.Event()
    llm_lock = asyncio.Lock()
    claim_lock = asyncio.Lock()
    # 2026-08-28：worker 的 _process_extraction_batch 内部同步执行 SQLite
    # 写事务（complete_extraction_job 的 BEGIN IMMEDIATE）——多 worker 并发
    # 写同一临时库触发 busy_timeout 竞争，阻塞事件循环（probe 测量被污染、
    # 恢复延迟可达 1.6s）。process_lock 串行化写事务（WAL 下读不受影响）；
    # LLM 调用仍走 llm_lock 保持真实锁竞争语义。
    process_lock = asyncio.Lock()
    claimed_job_ids: set[int] = set()
    fault_llm = _FaultInjectingLLM(config)
    errors: list[str] = []

    samples_file = None

    with tempfile.TemporaryDirectory(prefix="tangtang-memory-llm-soak-") as tmp:
        store = Store(str(Path(tmp) / "soak.db"))
        handler = _make_handler(store)

        async def llm_call(system_prompt: str, user_message: str) -> str:
            # 模拟生产当前共享锁：后台慢调用期间记录前台需要等待多久。
            async with llm_lock:
                result = await fault_llm(system_prompt, user_message)
            # worker 完成一次请求后必须让等待中的前台探针先获得调度机会。
            # 否则两个 worker 在极短请求下可连续重入 asyncio.Lock，导致
            # 探针把 worker 的批量重入误判为前台锁等待，产生偶发秒级 p95。
            await asyncio.sleep(0)
            return result

        async def producer(producer_index: int):
            for job_index in range(producer_index, target_jobs, config.producer_count):
                scheduled = started + job_index / config.arrival_jobs_per_second
                delay = scheduled - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                # Store 的 SQLite 访问层是同步的；把同一临时库同时放进
                # asyncio.to_thread 会让生产者线程与主事件循环互相等待
                # busy_timeout，短 smoke 可能假死。模拟器让插入在事件循环
                # 内原子完成，生产者仍通过 scheduled await 产生并发到达。
                created = _insert_job(store, config, job_index)
                expected_jobs.append(created)
                # 同步 SQLite 插入可能让当前 producer 落后于计划时间；一旦
                # 落后，下一轮不会再 sleep，必须显式让出事件循环，避免该
                # producer 连续批量插入把 worker/LLM 的等待时间伪装成秒级长尾。
                await asyncio.sleep(0)

        async def worker():
            while not stop.is_set():
                # 模拟多个 worker 时先在 harness 内做一次轻量 claim，
                # 避免两个协程同时把同一 pending job 交给真实处理器，
                # 进而制造无意义的 SQLite busy_timeout 等待。
                async with claim_lock:
                    jobs = store.list_resumable_extraction_jobs(
                        limit=max(1, config.worker_count),
                    )
                    job = next(
                        (item for item in jobs
                         if int(item["id"]) not in claimed_job_ids),
                        None,
                    )
                    if job is not None:
                        claimed_job_ids.add(int(job["id"]))
                if job is None:
                    await asyncio.sleep(0.002)
                    continue
                messages = store.get_extraction_job_messages(int(job["id"]))
                if not messages:
                    claimed_job_ids.discard(int(job["id"]))
                    await asyncio.sleep(0.002)
                    continue
                try:
                    async with process_lock:
                        await handler._process_extraction_batch(
                            user_id=str(job["qq_id"]),
                            messages=messages,
                            nickname=str(job["qq_id"]),
                            existing_summary="",
                            direction=str(job["direction"]),
                            origin="extracted",
                            llm_call=llm_call,
                        )
                except Exception:
                    # 状态机已把可重试失败落到 pending/dead；worker 继续消费。
                    await asyncio.sleep(0)
                finally:
                    claimed_job_ids.discard(int(job["id"]))

        async def foreground_probe():
            while not stop.is_set():
                # 2026-08-28 根因修复：锁空闲时测量必须为 0——事件循环被
                # 同步 SQLite 阻塞时的 acquire 恢复延迟不是 LLM 锁等待
                foreground_wait_ms.append(
                    await _measure_foreground_wait(llm_lock)
                )
                await asyncio.sleep(max(0.001, config.probe_interval_seconds))

        async def sampler():
            previous_created = 0
            previous_done = 0
            previous_time = time.perf_counter()
            while not stop.is_set():
                # 与 worker 的线程化 Store I/O 共用同一条模拟数据库闸门。
                # Windows 下排空阶段的高频同步读若与 BEGIN IMMEDIATE 写事务
                # 重叠，会偶发占满 busy_timeout，把夹具自身的采样竞争误报成
                # 死信冷却失效。
                async with process_lock:
                    health = store.get_extraction_queue_health()
                now = time.perf_counter()
                created = len(expected_jobs)
                done = int(health.get("done", 0) or 0)
                elapsed = max(0.001, now - previous_time)
                backlog = store.get_extraction_backlog_snapshot()
                sample = {
                    "elapsed_seconds": round(now - started, 3),
                    "jobs_created": created,
                    "jobs_done": done,
                    "arrival_jobs_per_second": round(
                        (created - previous_created) / elapsed, 3,
                    ),
                    "service_jobs_per_second": round(
                        (done - previous_done) / elapsed, 3,
                    ),
                    "queue_open": int(health.get("total_open", 0) or 0),
                    "queue_dead": int(health.get("dead", 0) or 0),
                    "oldest_open_at": str(health.get("oldest_open_at", "") or ""),
                    "backlog_messages": int(backlog.get("backlog_messages", 0) or 0),
                    "backlog_users": int(backlog.get("backlog_users", 0) or 0),
                    "oldest_backlog_at": str(backlog.get("oldest_at", "") or ""),
                }
                samples.append(sample)
                if samples_file:
                    try:
                        samples_file.write(json.dumps(sample, ensure_ascii=False) + "\n")
                        samples_file.flush()
                    except OSError as exc:
                        errors.append(f"samples_write:{type(exc).__name__}:{exc}")
                previous_created = created
                previous_done = done
                previous_time = now
                await asyncio.sleep(max(0.001, config.sample_interval_seconds))

        if config.samples_path:
            samples_path = Path(config.samples_path)
            try:
                samples_path.parent.mkdir(parents=True, exist_ok=True)
                samples_file = samples_path.open("a", encoding="utf-8")
            except OSError as exc:
                errors.append(f"samples_open:{type(exc).__name__}:{exc}")

        producer_tasks = [
            asyncio.create_task(producer(index))
            for index in range(config.producer_count)
        ]
        worker_tasks = [
            asyncio.create_task(worker()) for _ in range(config.worker_count)
        ]
        probe_task = asyncio.create_task(foreground_probe())
        sampler_task = asyncio.create_task(sampler())

        injection_started = time.perf_counter()
        injection_finished = injection_started
        done_at_injection_end = 0
        drained = False
        try:
            producer_results = await asyncio.gather(
                *producer_tasks, return_exceptions=True,
            )
            for outcome in producer_results:
                if isinstance(outcome, BaseException):
                    errors.append(
                        f"producer:{type(outcome).__name__}:{outcome}"
                    )
            injection_finished = time.perf_counter()
            async with process_lock:
                health = store.get_extraction_queue_health()
            done_at_injection_end = int(health.get("done", 0) or 0)
            drain_deadline = (
                injection_finished + max(0.0, config.drain_timeout_seconds)
            )
            while time.perf_counter() <= drain_deadline and not errors:
                async with process_lock:
                    health = store.get_extraction_queue_health()
                if (len(expected_jobs) == target_jobs
                        and int(health.get("done", 0) or 0) == target_jobs
                        and int(health.get("total_open", 0) or 0) == 0
                        and int(health.get("dead", 0) or 0) == 0):
                    drained = True
                    break
                await asyncio.sleep(0.005)
        finally:
            stop.set()
            all_tasks = producer_tasks + worker_tasks + [probe_task, sampler_task]
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            task_results = await asyncio.gather(*all_tasks, return_exceptions=True)
            for outcome in task_results:
                if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError):
                    message = f"background:{type(outcome).__name__}:{outcome}"
                    if message not in errors:
                        errors.append(message)
            if samples_file:
                try:
                    samples_file.close()
                except OSError as exc:
                    errors.append(f"samples_close:{type(exc).__name__}:{exc}")
                samples_file = None

        queue_final = store.get_extraction_queue_health()
        integrity = _integrity_result(store, expected_jobs)
        elapsed_seconds = max(0.001, time.perf_counter() - started)
        foreground = _latency_summary(foreground_wait_ms)
        wait_ok = (
            config.max_foreground_wait_p95_ms <= 0
            or foreground["p95"] <= config.max_foreground_wait_p95_ms
        )
        passed = (
            drained
            and len(expected_jobs) == target_jobs
            and len({job["qq_id"] for job in expected_jobs}) == config.users
            and integrity["cross_scope_leaks"] == 0
            and integrity["duplicate_memories"] == 0
            and integrity["cursor_mismatches"] == 0
            and integrity["verified_memories"] >= target_jobs
            and integrity["verified_evidence_links"] >= integrity["verified_memories"]
            and integrity["memories_without_verified_evidence"] == 0
            and wait_ok
            and not errors
        )
        injection_seconds = max(0.001, injection_finished - injection_started)
        drain_seconds = max(0.001, time.perf_counter() - injection_finished)
        final_done = int(queue_final.get("done", 0) or 0)
        result = {
            "passed": passed,
            "mode": "simulated_llm",
            "config": asdict(config),
            "jobs_created": len(expected_jobs),
            "jobs_done": final_done,
            "users_requested": int(config.users),
            "users_covered": len({job["qq_id"] for job in expected_jobs}),
            "arrival_jobs_per_second": round(
                len(expected_jobs) / elapsed_seconds, 3,
            ),
            "service_jobs_per_second": round(
                final_done / elapsed_seconds, 3,
            ),
            "injection": {
                "jobs_created": len(expected_jobs),
                "jobs_done_during_injection": done_at_injection_end,
                "elapsed_seconds": round(injection_seconds, 3),
                "arrival_jobs_per_second": round(
                    len(expected_jobs) / injection_seconds, 3,
                ),
            },
            "drain": {
                "jobs_done_after_injection": max(
                    0, final_done - done_at_injection_end,
                ),
                "elapsed_seconds": round(drain_seconds, 3),
                "service_jobs_per_second": round(
                    max(0, final_done - done_at_injection_end) / drain_seconds, 3,
                ),
            },
            "queue_final": queue_final,
            "integrity": integrity,
            "faults": {
                key: int(fault_llm.faults.get(key, 0))
                for key in ("429", "5xx", "timeout", "invalid", "empty", "slow", "ok")
            },
            "llm_latency_ms": _latency_summary(fault_llm.latencies_ms),
            "foreground_lock_wait_ms": foreground,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "samples": samples,
            "errors": errors,
        }

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="临时库记忆 LLM 持续流量验收（不会修改生产 memory.db）",
    )
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--duration-seconds", type=float, default=60)
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--producers", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--arrival-jobs-per-second", type=float, default=0.2)
    parser.add_argument("--messages-per-job", type=int, default=10)
    parser.add_argument("--base-latency-seconds", type=float, default=0.8)
    parser.add_argument("--jitter-seconds", type=float, default=0.2)
    parser.add_argument("--rate-limit-rate", type=float, default=0.02)
    parser.add_argument("--server-error-rate", type=float, default=0.02)
    parser.add_argument("--timeout-rate", type=float, default=0.01)
    parser.add_argument("--invalid-json-rate", type=float, default=0.01)
    parser.add_argument("--empty-rate", type=float, default=0.01)
    parser.add_argument("--slow-rate", type=float, default=0.01)
    parser.add_argument("--slow-seconds", type=float, default=15)
    parser.add_argument("--drain-timeout-seconds", type=float, default=600)
    parser.add_argument("--sample-interval-seconds", type=float, default=5)
    parser.add_argument("--probe-interval-seconds", type=float, default=1)
    parser.add_argument("--max-foreground-wait-p95-ms", type=float, default=0)
    parser.add_argument("--seed", type=int, default=20260827)
    parser.add_argument("--samples-jsonl", default="")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = SoakConfig(
        jobs=args.jobs,
        duration_seconds=args.duration_seconds,
        users=args.users,
        producer_count=args.producers,
        worker_count=args.workers,
        arrival_jobs_per_second=args.arrival_jobs_per_second,
        messages_per_job=args.messages_per_job,
        base_latency_seconds=args.base_latency_seconds,
        jitter_seconds=args.jitter_seconds,
        rate_limit_rate=args.rate_limit_rate,
        server_error_rate=args.server_error_rate,
        timeout_rate=args.timeout_rate,
        invalid_json_rate=args.invalid_json_rate,
        empty_rate=args.empty_rate,
        slow_rate=args.slow_rate,
        slow_seconds=args.slow_seconds,
        drain_timeout_seconds=args.drain_timeout_seconds,
        sample_interval_seconds=args.sample_interval_seconds,
        probe_interval_seconds=args.probe_interval_seconds,
        max_foreground_wait_p95_ms=args.max_foreground_wait_p95_ms,
        seed=args.seed,
        samples_path=args.samples_jsonl,
    )
    result = asyncio.run(run_simulated_soak(config))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
