"""定时媒体任务闸门回归。"""

import asyncio
import json
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 工作区内嵌 Python 只带标准库；正式 pytest 环境会使用真实依赖。
for dependency in ("websockets", "httpx"):
    try:
        __import__(dependency)
    except ModuleNotFoundError:
        sys.modules[dependency] = types.ModuleType(dependency)

from agent.tasks import TaskManager


def test_media_gate_off_warns_once_without_claim_or_send():
    """v2 媒体任务在闸门关闭时保持 pending，且只告警一次。"""
    task = {
        "id": 42,
        "owner_qq": "1001",
        "description": "发一张开心贴图",
        "remind_at": "2026-09-03 00:00",
        "status": "pending",
        "group_id": "",
        "action_payload": json.dumps({
            "action_version": 2,
            "sticker_emotion": "开心",
        }, ensure_ascii=False),
    }

    class Store:
        claim_calls = 0

        def get_due_tasks(self):
            return [dict(task)] if task["status"] == "pending" else []

        def claim_task_for_send(self, task_id):
            self.claim_calls += 1
            task["status"] = "sending"
            return True

    sent = []

    async def send(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    store = Store()
    napcat = types.SimpleNamespace(
        _task_media_action_outbox_enabled=False,
        send_private_message=send,
        send_group_message=send,
    )
    manager = TaskManager(store, napcat)

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    capture = Capture()
    task_logger = logging.getLogger("糖糖.Tasks")
    task_logger.addHandler(capture)
    try:
        asyncio.run(manager._check_and_send())
        asyncio.run(manager._check_and_send())
    finally:
        task_logger.removeHandler(capture)

    gate_warnings = [
        message for message in capture.messages
        if "tasks.media_action_outbox_enabled" in message
    ]
    assert gate_warnings == [
        "📋 媒体任务因闸门关闭而跳过: "
        "task_id=42 gate=tasks.media_action_outbox_enabled"
    ]
    assert task["status"] == "pending"
    assert store.claim_calls == 0
    assert sent == []


if __name__ == "__main__":
    test_media_gate_off_warns_once_without_claim_or_send()
    print("E1_GATE_TEST_PASS")
