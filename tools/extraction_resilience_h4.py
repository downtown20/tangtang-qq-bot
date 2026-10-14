"""H4：临时库提取故障与崩溃恢复验收。

不读取或写入生产 ``memory.db``。该模块被 pytest 回归复用，也可独立运行，
用于证明现有持久化提取状态机在小批量多生产者、故障和进程终止后可收敛。
"""

from __future__ import annotations

import asyncio
from collections import Counter
import gc
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import types


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _UnusedDependency:
    """仅让隔离测试导入未走到的网络模块；任何实际调用都会返回空 stub。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def __call__(self, *_args, **_kwargs):
        return type(self)()

    def __getattr__(self, _name):
        return type(self)()


def _install_isolated_import_stubs() -> tuple[str, ...]:
    """补齐当前沙箱缺失、但 H4 不会调用的 HTTP/WebSocket 导入。

    ``MessageHandler`` 顶层同时承载网络回复功能和本批要测的提取状态机。
    本地 Python 缺少可选网络包时，仅为模块导入安装空壳；提取演练不会创建
    HTTP/WebSocket client。实际运行环境有依赖时不写入 ``sys.modules``。
    """
    installed = []
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        httpx = types.ModuleType("httpx")
        httpx.AsyncClient = _UnusedDependency
        httpx.Client = _UnusedDependency
        httpx.Timeout = _UnusedDependency
        httpx.HTTPError = Exception
        httpx.RequestError = Exception
        sys.modules["httpx"] = httpx
        installed.append("httpx")
    try:
        import websockets  # noqa: F401
    except ModuleNotFoundError:
        websockets = types.ModuleType("websockets")
        websockets.WebSocketClientProtocol = _UnusedDependency
        websockets.connect = _UnusedDependency()
        sys.modules["websockets"] = websockets
        installed.append("websockets")
    return tuple(installed)


IMPORT_STUBS = _install_isolated_import_stubs()

from agent.handler import MessageHandler
from agent.memory import MemorySystem
from agent.store import Store
from tools.memory_llm_soak import SoakConfig, run_simulated_soak


class _MetricsStub:
    """只满足生产提取器的遥测接口；不替代任何生产逻辑。"""

    def __init__(self):
        self.counters: Counter[str] = Counter()

    def incr(self, name: str, delta: int = 1) -> None:
        self.counters[name] += int(delta)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _messages(store: Store, qq_id: str, *, count: int = 3) -> list[dict]:
    rows = []
    for index in range(count):
        timestamp = f"2026-09-03 10:00:0{index}"
        chat_id = store.insert_chat(
            qq_id,
            f"H4 故障注入消息 {index}",
            group_id="h4-group",
            is_bot=False,
            timestamp=timestamp,
        )
        rows.append({
            "id": chat_id,
            "message": f"H4 故障注入消息 {index}",
            "timestamp": timestamp,
            "is_bot_reply": 0,
            "group_id": "h4-group",
        })
    return rows


def _item(messages: list[dict]) -> dict:
    return {
        "type": "note",
        "cognitive": "semantic",
        "value": "H4 崩溃恢复验收事实",
        "importance": 5,
        "confidence": 0.95,
        "claim_type": "stated",
        "evidence_quote": messages[0]["message"],
        "evidence_ids": [messages[0]["id"]],
        "source_group_id": messages[0]["group_id"],
    }


def _handler(store: Store) -> MessageHandler:
    handler = MessageHandler.__new__(MessageHandler)
    handler.memory = MemorySystem(store=store)
    handler.metrics = _MetricsStub()
    handler._extracting_users = set()
    return handler


def _success_llm(messages: list[dict]):
    async def call(_system_prompt: str, _user_message: str, **_kwargs) -> str:
        return json.dumps([_item(messages)], ensure_ascii=False)

    return call


async def _process(handler: MessageHandler, qq_id: str, messages: list[dict], llm_call) -> dict:
    return await handler._process_extraction_batch(
        user_id=qq_id,
        messages=messages,
        nickname=qq_id,
        existing_summary="",
        direction="forward",
        origin="extracted",
        llm_call=llm_call,
    )


def _terminate_during_commit(db_path: str, job_id: int, entered_path: str) -> None:
    """子进程在 SQLite 事务内暂停，供父进程真实 terminate。"""
    store = Store(db_path)

    def pause_before_commit(*_args, **_kwargs):
        Path(entered_path).write_text("entered", encoding="utf-8")
        time.sleep(30)
        return 0

    store._apply_extraction_item = pause_before_commit
    store.complete_extraction_job(job_id, origin="extracted")


async def _fault_matrix(db_path: Path) -> dict:
    store = Store(str(db_path))
    cases: dict[str, object] = {
        "429": RuntimeError("simulated HTTP 429"),
        "5xx": RuntimeError("simulated HTTP 503"),
        "timeout": asyncio.TimeoutError("simulated LLM timeout"),
        "invalid_json": '{"items":[',
        "empty": "",
        "disconnect": ConnectionError("simulated connection reset"),
    }
    result: dict[str, dict] = {}
    for name, outcome in cases.items():
        qq_id = f"h4-{name}"
        messages = _messages(store, qq_id)
        handler = _handler(store)

        async def injected(_system: str, _user: str, value=outcome):
            if isinstance(value, BaseException):
                raise value
            return value

        first = await _process(handler, qq_id, messages, injected)
        job = store.create_extraction_job(
            qq_id, messages, direction="forward", protocol_version="memory-v1",
        )
        current = store.get_extraction_job(job["id"])
        assert first["completed"] is False
        assert current["status"] == "pending"
        assert current["attempts"] == 1
        replay = await _process(handler, qq_id, messages, _success_llm(messages))
        done = store.get_extraction_job(job["id"])
        assert replay["completed"] is True
        assert done["status"] == "done"
        assert done["attempts"] == 2
        assert len(store.query_memories(qq_id, trusted_only=True)) == 1
        result[name] = {
            "first_status": current["status"],
            "recovered_status": done["status"],
            "attempts": done["attempts"],
        }
    return result


async def _retry_cooldown(db_path: Path) -> dict:
    store = Store(str(db_path))
    qq_id = "h4-retry-budget"
    messages = _messages(store, qq_id)
    handler = _handler(store)

    async def always_503(_system: str, _user: str) -> str:
        raise RuntimeError("simulated persistent HTTP 503")

    for _ in range(5):
        result = await _process(handler, qq_id, messages, always_503)
        assert result["completed"] is False

    job = store.create_extraction_job(
        qq_id, messages, direction="forward", protocol_version="memory-v1",
    )
    dead = store.get_extraction_job(job["id"])
    assert dead["status"] == "dead"
    assert dead["attempts"] == 5
    assert store.requeue_dead_extraction_jobs(limit=1, retry_after_seconds=3600) == 0
    assert store.requeue_dead_extraction_jobs(limit=1, retry_after_seconds=0) == 1
    replay = await _process(handler, qq_id, messages, _success_llm(messages))
    done = store.get_extraction_job(job["id"])
    assert replay["completed"] is True
    assert done["status"] == "done"
    assert done["attempts"] == 1
    return {
        "dead_after_attempts": dead["attempts"],
        "cooldown_blocks_requeue": True,
        "recovered_status": done["status"],
    }


async def _half_write_recovery(db_path: Path) -> dict:
    store = Store(str(db_path))
    qq_id = "h4-half-write"
    messages = _messages(store, qq_id)
    job = store.create_extraction_job(
        qq_id, messages, direction="forward", protocol_version="memory-v1",
    )
    lease = store.lease_extraction_job(job["id"])
    assert lease and store.mark_extraction_job_ready(
        job["id"], lease["lease_token"], [_item(messages)], {"protocol_ok": True},
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET result_json=? WHERE id=?",
            ('{"items":[', job["id"]),
        )
        conn.commit()

    restarted = Store(str(db_path))
    try:
        restarted.complete_extraction_job(job["id"], origin="extracted")
    except ValueError as exc:
        assert "corrupt ready payload" in str(exc)
    else:
        raise AssertionError("half-written ready payload must be quarantined")
    dead = restarted.get_extraction_job(job["id"])
    assert dead["status"] == "dead"
    assert restarted.get_extraction_cursor(qq_id, "forward") == 0
    assert restarted.query_memories(qq_id, trusted_only=True) == []
    assert restarted.requeue_dead_extraction_jobs(limit=1, retry_after_seconds=0) == 1
    handler = _handler(restarted)
    replay = await _process(handler, qq_id, messages, _success_llm(messages))
    done = restarted.get_extraction_job(job["id"])
    assert replay["completed"] is True
    assert done["status"] == "done"
    assert len(restarted.query_memories(qq_id, trusted_only=True)) == 1
    return {"quarantined_status": dead["status"], "recovered_status": done["status"]}


def _kill_and_recover(db_path: Path) -> dict:
    store = Store(str(db_path))
    qq_id = "h4-kill"
    messages = _messages(store, qq_id)
    job = store.create_extraction_job(
        qq_id, messages, direction="forward", protocol_version="memory-v1",
    )
    lease = store.lease_extraction_job(job["id"])
    assert lease and store.mark_extraction_job_ready(
        job["id"], lease["lease_token"], [_item(messages)], {"protocol_ok": True},
    )

    entered = db_path.with_suffix(".entered")
    process = multiprocessing.get_context("spawn").Process(
        target=_terminate_during_commit,
        args=(str(db_path), int(job["id"]), str(entered)),
    )
    process.start()
    deadline = time.monotonic() + 10
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert entered.exists(), "child did not enter the commit transaction"
    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive(), "terminated commit child did not exit"

    restarted = Store(str(db_path))
    before = restarted.get_extraction_job(job["id"])
    assert before["status"] == "ready"
    assert restarted.query_memories(qq_id, trusted_only=True) == []
    completed = restarted.complete_extraction_job(job["id"], origin="extracted")
    repeated = restarted.complete_extraction_job(job["id"], origin="extracted")
    after = restarted.get_extraction_job(job["id"])
    rows = restarted.query_memories(qq_id, trusted_only=True)
    assert completed["status"] == repeated["status"] == after["status"] == "done"
    assert len(rows) == 1
    assert restarted.get_extraction_cursor(qq_id, "forward") == messages[-1]["id"]
    return {
        "terminated_exitcode": process.exitcode,
        "status_after_kill": before["status"],
        "status_after_restart": after["status"],
        "trusted_memories": len(rows),
    }


async def _expired_lease_recovery(db_path: Path) -> dict:
    store = Store(str(db_path))
    qq_id = "h4-expired-lease"
    messages = _messages(store, qq_id)
    job = store.create_extraction_job(
        qq_id, messages, direction="forward", protocol_version="memory-v1",
    )
    lease = store.lease_extraction_job(job["id"])
    assert lease and lease["status"] == "leased"
    # 临时库中模拟进程被杀后租约自然到期；不缩短生产租约。
    with store._connect() as conn:
        conn.execute(
            "UPDATE extraction_jobs SET lease_until=? WHERE id=?",
            ("2000-01-01 00:00:00", job["id"]),
        )
        conn.commit()

    restarted = _handler(Store(str(db_path)))
    restarted._call_llm_light = _success_llm(messages)
    completed = await restarted._resume_extraction_jobs(limit=1)
    current = restarted.memory.store.get_extraction_job(job["id"])
    assert completed == 1
    assert current["status"] == "done"
    assert current["attempts"] == 2
    assert len(restarted.memory.store.query_memories(qq_id, trusted_only=True)) == 1
    return {
        "status_before_restart": "leased_expired",
        "status_after_restart": current["status"],
        "attempts": current["attempts"],
    }


async def run_h4_resilience() -> dict:
    """执行 H4 全矩阵；任何断言失败即代表回归红灯。"""
    fair_config = SoakConfig(
        jobs=24,
        users=12,
        producer_count=4,
        worker_count=1,
        arrival_jobs_per_second=80.0,
        messages_per_job=2,
        base_latency_seconds=0.0,
        jitter_seconds=0.0,
        rate_limit_rate=0.0,
        server_error_rate=0.0,
        timeout_rate=0.0,
        invalid_json_rate=0.0,
        empty_rate=0.0,
        slow_rate=0.0,
        slow_seconds=0.0,
        drain_timeout_seconds=20.0,
        sample_interval_seconds=0.01,
        probe_interval_seconds=0.01,
    )
    fair = await run_simulated_soak(fair_config)
    assert fair["passed"] is True
    assert fair["jobs_created"] == fair["jobs_done"] == 24
    assert fair["users_covered"] == 12
    assert fair["config"]["worker_count"] == 1

    with tempfile.TemporaryDirectory(
            prefix="tangtang-h4-resilience-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        faults = await _fault_matrix(root / "faults.db")
        cooldown = await _retry_cooldown(root / "cooldown.db")
        half_write = await _half_write_recovery(root / "half-write.db")
        kill = _kill_and_recover(root / "kill.db")
        lease = await _expired_lease_recovery(root / "lease.db")
        await asyncio.sleep(0.1)
        gc.collect()

    return {
        "passed": True,
        "fairness": {
            "jobs": fair["jobs_done"],
            "users_covered": fair["users_covered"],
            "worker_count": fair["config"]["worker_count"],
            "integrity": fair["integrity"],
        },
        "faults": faults,
        "retry_cooldown": cooldown,
        "half_write": half_write,
        "kill_recovery": kill,
        "expired_lease_recovery": lease,
    }


def main() -> int:
    print(json.dumps(asyncio.run(run_h4_resilience()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
