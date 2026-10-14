"""H7：P5-2 的临时库 / mock 故障注入矩阵。

不读取或写入生产 ``memory.db``。提取面复用 H4 已有的真实状态机演练；
本文件补足 outbox 网络、损坏 payload、外键与实际进程终止窗口。任何断言
失败即退出非零，供主负责人用作定向红测。
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import types
from unittest.mock import AsyncMock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _UnusedDependency:
    """仅供本离线 mock 导入，任何真实网络调用均不可用。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def __call__(self, *_args, **_kwargs):
        return self

    def __getattr__(self, _name):
        return self


def _install_network_import_stubs() -> tuple[str, ...]:
    """补齐沙箱缺少的网络依赖；P5 不创建任何真实客户端。"""
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


IMPORT_STUBS = _install_network_import_stubs()

from agent.action_contract import ActionEnvelope, build_action_receipt_template
from agent.store import Store
from onebot.ws_client import NapCatClient, SendResult
from tools.extraction_resilience_h4 import run_h4_resilience


def _voice_template(action_id: str) -> dict:
    envelope = ActionEnvelope(
        action_id=action_id,
        kind="voice",
        channel="group",
        target="h7-group",
        payload={
            "text": "H7 收敛演练", "emotion": "温柔", "speed": 1.0,
            "pause": "自然",
        },
        source_id=f"h7:{action_id}",
        scope_id="h7-group",
        ordinal=0,
    )
    return build_action_receipt_template(envelope, {
        "delivery_kind": "voice", "voice_generated": True,
        "fallback_used": False,
    })


def _claim_and_pause(db_path: str, outbox_id: str, entered_path: str) -> None:
    """将状态持久化为 sending 后暂停，供父进程实际 terminate。"""
    store = Store(db_path)
    assert store.claim_send_outbox(outbox_id)
    Path(entered_path).write_text("claimed", encoding="utf-8")
    time.sleep(30)


def _settle_and_pause(db_path: str, outbox_id: str, entered_path: str) -> None:
    """在 confirmed 回执即将入库时暂停，模拟结算事务提交前进程终止。"""
    store = Store(db_path)
    original = store._enqueue_action_receipt_conn

    def pause_before_receipt(conn, receipt):
        Path(entered_path).write_text("settling", encoding="utf-8")
        time.sleep(30)
        return original(conn, receipt)

    store._enqueue_action_receipt_conn = pause_before_receipt
    assert store.settle_send_outbox(
        outbox_id, "confirmed", message_ids=(7001,),
    ) == "confirmed"


def _terminate_after_marker(process, entered: Path) -> int:
    deadline = time.monotonic() + 10
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert entered.exists(), "child did not enter injected process window"
    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive(), "terminated child did not exit"
    return int(process.exitcode)


async def _network_matrix(db_path: Path) -> dict:
    store = Store(str(db_path))
    client = NapCatClient(testing_mode=True)
    client.bind_outbox_store(store)

    reconnect_id = store.enqueue_send_outbox("group", "h7-group", "reconnect")
    client._qq_online = False
    assert await client.process_send_outbox() == 0
    assert store.get_send_outbox(reconnect_id)["status"] == "pending"
    client._qq_online = True
    client.send_group_message = AsyncMock(
        return_value=SendResult(True, True, message_id=7002),
    )
    assert await client.process_send_outbox() == 1
    assert store.get_send_outbox(reconnect_id) is None

    timeout_id = store.enqueue_send_outbox("group", "h7-group", "timeout")
    client.send_group_message = AsyncMock(return_value=SendResult(
        False, False, error="NETWORK_UNCERTAIN", uncertain=True,
    ))
    assert await client.process_send_outbox() == 1
    timeout_job = store.get_send_outbox(timeout_id)
    assert timeout_job and timeout_job["status"] == "uncertain"
    assert store.list_due_send_outbox() == []

    dead_id = store.enqueue_send_outbox("group", "h7-group", "dead-letter")
    for _ in range(3):
        assert store.claim_send_outbox(dead_id)
        assert store.fail_send_outbox(dead_id, "NETWORK", max_attempts=3)
    dead_job = store.get_send_outbox(dead_id)
    assert dead_job and dead_job["status"] == "dead"
    assert dead_job["attempts"] == 3
    assert store.list_due_send_outbox() == []
    return {
        "disconnect_reconnect": "pending_then_confirmed",
        "send_timeout": "uncertain_not_replayed",
        "dead_letter": {"status": dead_job["status"], "attempts": dead_job["attempts"]},
    }


def _data_matrix(db_path: Path) -> dict:
    store = Store(str(db_path))
    corrupt_id = store.enqueue_send_outbox("group", "h7-group", "corrupt")
    with store._connect() as conn:
        conn.execute(
            "UPDATE send_outbox SET receipt_template='{' WHERE action_id=?",
            (corrupt_id,),
        )
        conn.commit()
    assert store.claim_send_outbox(corrupt_id)
    assert store.recover_send_outbox_after_restart() == 1
    corrupt = store.get_send_outbox(corrupt_id)
    assert corrupt and corrupt["status"] == "uncertain"
    assert corrupt["last_error"] == "RECEIPT_TEMPLATE_INVALID"
    assert store.list_due_send_outbox() == []

    foreign_key_rejected = False
    with store._connect() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(
                "INSERT INTO task_action_items "
                "(task_id,ordinal,canonical_json,created_at) "
                "VALUES (999999,0,'{}','now')",
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            assert "FOREIGN KEY" in str(exc).upper()
            foreign_key_rejected = True
    assert foreign_key_rejected
    return {
        "corrupt_json": "uncertain_quarantined_not_replayed",
        "foreign_key": "rejected_before_commit",
    }


def _process_matrix(db_path: Path) -> dict:
    context = multiprocessing.get_context("spawn")

    sending_store = Store(str(db_path))
    sending_id = sending_store.enqueue_send_outbox("group", "h7-group", "sending")
    sending_marker = db_path.with_name("sending-entered")
    sending_process = context.Process(
        target=_claim_and_pause,
        args=(str(db_path), sending_id, str(sending_marker)),
    )
    sending_process.start()
    sending_exit = _terminate_after_marker(sending_process, sending_marker)
    restarted = Store(str(db_path))
    assert restarted.recover_send_outbox_after_restart() == 1
    sending = restarted.get_send_outbox(sending_id)
    assert sending and sending["status"] == "uncertain"
    assert restarted.list_due_send_outbox() == []

    settling_id = restarted.enqueue_send_outbox(
        "group", "h7-group", "settling",
        receipt_template=_voice_template("act-h7-settling"),
    )
    assert restarted.claim_send_outbox(settling_id)
    settling_marker = db_path.with_name("settling-entered")
    settling_process = context.Process(
        target=_settle_and_pause,
        args=(str(db_path), settling_id, str(settling_marker)),
    )
    settling_process.start()
    settling_exit = _terminate_after_marker(settling_process, settling_marker)
    post_settle = Store(str(db_path))
    assert post_settle.recover_send_outbox_after_restart() == 1
    settling = post_settle.get_send_outbox(settling_id)
    assert settling and settling["status"] == "uncertain"
    receipts = post_settle.lease_action_receipts("h7-group")["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "uncertain"
    assert receipts[0]["error_code"] == "PROCESS_RESTART_UNCERTAIN"
    assert post_settle.list_due_send_outbox() == []
    return {
        "sending": {"exit": sending_exit, "recovered": sending["status"]},
        "settling_precommit": {
            "exit": settling_exit, "recovered": settling["status"],
            "recovery_receipt": "uncertain",
        },
    }


async def run_p5_fault_matrix() -> dict:
    """执行完整 H7 矩阵；所有持久化均位于自动清理的临时目录。"""
    extraction = await run_h4_resilience()
    assert extraction["passed"] is True
    with tempfile.TemporaryDirectory(
            prefix="tangtang-h7-p5-", ignore_cleanup_errors=True) as root:
        root_path = Path(root)
        network = await _network_matrix(root_path / "network.db")
        data = _data_matrix(root_path / "data.db")
        process = _process_matrix(root_path / "process.db")
    return {
        "passed": True,
        "import_stubs": list(IMPORT_STUBS),
        "llm_and_extraction": extraction,
        "network": network,
        "data": data,
        "process": process,
    }


def main() -> int:
    print(json.dumps(asyncio.run(run_p5_fault_matrix()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
