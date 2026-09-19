"""
定时任务系统测试（2026-08-15/16）

08-15 事故：糖糖答应「明天早上九点在群里叫我」但什么都没记录——tasks 表空。
根因①系统 parse 不认中文数字；②LLM 无定时工具；③任务系统不支持群提醒。
08-16 范式转换（教训表 #24）：自然语言意图解析**唯一决策源是 LLM 工具**
（set_reminder / group_say_later）——系统 precise 层只允许动作确认类命令，
时间/位置/内容解析不可回系统层（TestPreciseCommandsBoundary 机器闸门）。
"""

import asyncio
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.tasks import TaskManager
from napcat.ws_client import SendResult


class TestGroupReminder:
    def _make(self, store):
        async def _ok(*a, **k):
            return True
        nap = types.SimpleNamespace(
            send_private_message=_ok, send_group_message=_ok)
        return TaskManager(store, nap)

    def test_add_at_group_id_persisted(self, store):
        tm = self._make(store)
        tid = tm.add_at("09:00", "起床改备注", "12345",
                        date_offset=1, group_id="10005")
        tasks = store.list_tasks("12345")
        assert any(t["id"] == tid and t.get("group_id") == "10005"
                   for t in tasks), "群提醒任务的 group_id 没有持久化"

    def test_private_task_has_no_group(self, store):
        tm = self._make(store)
        tid = tm.add(minutes=30, description="喝水", owner_qq="12345")
        tasks = store.list_tasks("12345")
        hit = [t for t in tasks if t["id"] == tid]
        assert hit and not hit[0].get("group_id")


def test_stop_waits_for_task_loop_to_finish():
    """TaskManager.stop 返回时，自己创建的循环必须已完成取消。"""
    tm = TaskManager(None, None)

    async def worker():
        await asyncio.sleep(60)

    async def scenario():
        tm._running = True
        tm._task = asyncio.create_task(worker())
        task = tm._task
        await tm.stop()
        return task.done(), task.cancelled(), tm._task

    done, cancelled, current = asyncio.run(scenario())
    assert done is True
    assert cancelled is True
    assert current is None


def test_check_and_send_slow_store_probe_does_not_block_event_loop():
    """到期任务扫描的慢 SQLite 调用必须让前台事件循环继续调度。"""
    class SlowStore:
        def get_due_tasks(self):
            time.sleep(0.08)
            return []

    tm = TaskManager(SlowStore(), types.SimpleNamespace())

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            await tm._check_and_send()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return ticks

    assert asyncio.run(scenario()) > 0


def test_task_store_io_waits_for_cancelled_thread_before_propagating():
    """取消提醒循环时，底层 Store 线程必须先释放连接。"""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_store():
        started.set()
        release.wait(timeout=2)
        finished.set()
        return True

    tm = TaskManager(None, None)

    async def scenario():
        task = asyncio.create_task(tm._run_store_io("cancel_probe", slow_store))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert finished.is_set()

    asyncio.run(scenario())


class TestReminderComposition:
    """2026-08-18 小闹钟事故回归：备忘是 LLM 写给自己的指令
    （「主动找米雪儿报到…提醒她：那个计划不作数」），原样发给对方
    = 对方收到一份看不懂的指令备忘。到点必须由 LLM 改写成自然消息。"""

    def _make(self, store, llm_call):
        sent = []
        async def _priv(qq, msg):
            sent.append(("priv", qq, msg)); return True
        async def _group(gid, msg):
            sent.append(("group", gid, msg)); return True
        nap = types.SimpleNamespace(send_private_message=_priv, send_group_message=_group)
        return TaskManager(store, nap, llm_call=llm_call), sent

    @staticmethod
    def _queued_text(store):
        rows = store.list_due_send_outbox()
        assert len(rows) == 1
        return rows[0]["message"]

    def test_llm_composes_natural_message(self, store):
        """有 LLM → 发出去的是改写后的自然消息，不是原始指令备忘"""
        async def fake_llm(system, user):
            assert "主动找米雪儿报到" in user  # 备忘进了提示词
            return "早上好呀，我来报到啦～昨晚睡得好吗？那个坏结局计划不作数了哦，难受了先来找我"
        tm, sent = self._make(store, fake_llm)
        store.create_task("10003", "主动找米雪儿报到，问昨晚睡得好不好、今天状态怎么样。提醒她：那个计划不作数，先来找我。",
                          "2020-01-01 00:00")
        asyncio.run(tm._check_and_send())
        assert sent == []  # TaskManager 只冻结，网络首发归 outbox worker
        queued = self._queued_text(store)
        assert "主动找米雪儿报到" not in queued      # 指令备忘不外泄
        assert "报到" in queued                       # 语义传达到了

    def test_llm_empty_falls_back_to_raw(self, store):
        """LLM 空回复 → 原样兜底（降级链，宁可发原始备忘也不静默丢弃）"""
        async def fake_llm(system, user):
            return ""
        tm, sent = self._make(store, fake_llm)
        store.create_task("12345", "起床改备注", "2020-01-01 00:00")
        asyncio.run(tm._check_and_send())
        assert sent == []
        assert "起床改备注" in self._queued_text(store)

    def test_no_llm_raw_backward_compat(self, store):
        """未注入 LLM（旧构造方式）→ 原样发送，行为不变"""
        sent = []
        async def _priv(qq, msg):
            sent.append(msg); return True
        nap = types.SimpleNamespace(send_private_message=_priv,
                                    send_group_message=_priv)
        tm = TaskManager(store, nap)
        store.create_task("12345", "喝水", "2020-01-01 00:00")
        asyncio.run(tm._check_and_send())
        assert sent == []
        assert "喝水" in self._queued_text(store)

    def test_unconfirmed_delivery_does_not_mark_task_done(self, store):
        async def _uncertain(*_args, **_kwargs):
            return SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")

        nap = types.SimpleNamespace(
            send_private_message=_uncertain,
            send_group_message=_uncertain,
        )
        tm = TaskManager(store, nap)
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")

        asyncio.run(tm._check_and_send())
        job = store.list_due_send_outbox()[0]
        assert store.claim_send_outbox(job["action_id"])
        assert store.settle_send_outbox(
            job["action_id"], "uncertain",
            error_code="MESSAGE_ID_UNCONFIRMED",
        ) == "uncertain"

        row = [t for t in store.list_tasks("12345") if t["id"] == task_id]
        assert row and row[0]["status"] == "uncertain"
        # 不确定结果不能由每分钟循环盲目重放；Phase 2 提供带 attempt
        # 与核验结论的显式 API 前，旧 bool retry 也必须 fail-closed。
        assert not any(t["id"] == task_id for t in store.get_due_tasks())
        assert tm.retry(task_id, "12345") is False
        with store._connect() as conn:
            generations = conn.execute(
                "SELECT generation,state FROM task_action_attempts "
                "WHERE task_id=? ORDER BY generation", (task_id,),
            ).fetchall()
        assert generations == [(0, "uncertain")]
        assert not any(t["id"] == task_id for t in store.get_due_tasks())

    def test_task_is_atomically_claimed_before_sending(self, store):
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")

        assert store.claim_task_for_send(task_id) is True
        assert store.claim_task_for_send(task_id) is False
        assert not any(t["id"] == task_id for t in store.get_due_tasks())

    def test_restart_quarantines_interrupted_send_instead_of_replaying(self, store):
        calls = 0

        async def _must_not_send(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("TaskManager must not send text directly")

        nap = types.SimpleNamespace(
            send_private_message=_must_not_send,
            send_group_message=_must_not_send,
        )
        tm = TaskManager(store, nap)
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")

        asyncio.run(tm._check_and_send())
        job = store.list_due_send_outbox()[0]
        assert store.claim_send_outbox(job["action_id"])

        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "sending"
        assert store.recover_send_outbox_after_restart() == 1
        assert store.recover_sending_tasks() == 0
        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "uncertain"

        asyncio.run(tm._check_and_send())
        assert calls == 0

    def test_known_send_failure_releases_claim_for_later_retry(self, store):
        async def _failed(*_args, **_kwargs):
            return SendResult(False, False, error="OFFLINE", retryable=True)

        nap = types.SimpleNamespace(
            send_private_message=_failed,
            send_group_message=_failed,
        )
        tm = TaskManager(store, nap)
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")

        asyncio.run(tm._check_and_send())
        job = store.list_due_send_outbox()[0]
        assert store.claim_send_outbox(job["action_id"])
        assert store.settle_send_outbox(
            job["action_id"], "failed", error_code="OFFLINE",
            max_attempts=3,
        ) == "pending"

        assert not any(t["id"] == task_id for t in store.get_due_tasks())
        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "sending"
        assert store.get_send_outbox(job["action_id"])["status"] == "pending"

    def test_send_exception_is_uncertain_and_not_replayed(self, store):
        async def _lost_response(*_args, **_kwargs):
            raise ConnectionError("response lost after request")

        nap = types.SimpleNamespace(
            send_private_message=_lost_response,
            send_group_message=_lost_response,
        )
        tm = TaskManager(store, nap)
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")

        asyncio.run(tm._check_and_send())
        job = store.list_due_send_outbox()[0]
        assert store.claim_send_outbox(job["action_id"])
        assert store.settle_send_outbox(
            job["action_id"], "uncertain",
            error_code="SEND_RESULT_LOST",
        ) == "uncertain"

        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "uncertain"
        assert not any(t["id"] == task_id for t in store.get_due_tasks())

    def test_malformed_typed_payload_is_frozen_not_downgraded_to_text(self, store):
        sent = []

        async def _priv(*args, **kwargs):
            sent.append((args, kwargs))
            return SendResult(True, True)

        nap = types.SimpleNamespace(
            send_private_message=_priv,
            send_group_message=_priv,
        )
        tm = TaskManager(store, nap)
        task_id = store.create_task(
            "12345", "不要把内部指令发出去", "2020-01-01 00:00",
            action_payload='{"voice_text": "你好", "unexpected": true}',
        )

        asyncio.run(tm._check_and_send())

        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "uncertain"
        assert sent == []

    def test_cancel_does_not_mutate_task_while_send_is_inflight(self, store):
        task_id = store.create_task("12345", "喝水", "2020-01-01 00:00")
        assert store.claim_task_for_send(task_id) is True

        assert store.cancel_task(task_id, "12345") is False
        row = next(t for t in store.list_tasks("12345") if t["id"] == task_id)
        assert row["status"] == "sending"


class TestPreciseCommandsBoundary:
    """2026-08-16 范式转换机器闸门（教训表 #24）：系统 precise 层只允许
    动作确认类命令（撤回/撤销/草稿审核）——自然语言意图（时间/位置/内容）
    的解析是 LLM 的决策域。现场事故：「5分钟后在这里叫我」被 delayed_say
    正则抢答误发默认群；「明天早上九点」不认中文数字承诺落空。"""

    @staticmethod
    def _parse(text):
        from agent.handler import MessageHandler
        return MessageHandler._parse_precise_commands(object.__new__(MessageHandler), text)

    def test_natural_language_intent_not_intercepted(self):
        for t in ("5分钟后在这里叫我", "5分钟后提醒我喝水",
                  "明天早上九点在群里叫我", "5分钟后去群里说晚安"):
            assert self._parse(t) is None, f"系统层仍在解析自然语言意图: {t}"

    def test_no_intent_parsing_branches_in_source(self):
        """机器闸门：意图解析分支不可复活——函数体（去 docstring）内出现
        时间量词/提醒词/意图动作即红灯"""
        src = Path(__file__).parent.parent / "agent" / "handler.py"
        text = src.read_text(encoding="utf-8")
        m = re.search(r"def _parse_precise_commands.*?(?=\n    (?:async )?def |\nclass )",
                      text, re.S)
        assert m, "找不到 _parse_precise_commands"
        body = m.group(0)
        body = re.sub(r'""".*?"""', "", body, flags=re.S)  # 历史事故叙述在 docstring 里
        for forbidden in ("delayed_say", "delayed_remind_me", "提醒我", "分钟"):
            assert forbidden not in body, f"precise 层出现意图解析标记: {forbidden}"

    def test_confirmation_commands_still_work(self):
        for t, act in (("撤回消息", "recall_msg"), ("发错了", "recall_msg"),
                       ("撤销设置", "undo")):
            r = self._parse(t)
            assert r and r["action"] == act, f"动作确认命令被误伤: {t}"

    def test_draft_commands_require_draft(self):
        """2026-08-16 事故回归：无待审草稿时，「改一下X」「好的」「算了」
        是普通对话——系统不许抢答（「改一下语言模板」曾被 (.*) 吞成草稿修改，
        意见征集消息被吞）。有草稿时才生效。"""
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h._pending_pm = None
        for t in ("改一下语言模板我就不求啥了", "好的", "算了", "发吧",
                  "改一下语气温柔一点", "ok"):
            assert MessageHandler._parse_precise_commands(h, t) is None, f"无草稿却抢答: {t}"
        # 有草稿 → 草稿命令正常生效
        h._pending_pm = {"qq": "123", "message": "草稿", "user_id": "1"}
        r = MessageHandler._parse_precise_commands(h, "改一下语气温柔一点")
        assert r and r["action"] == "revise_pending"
        r = MessageHandler._parse_precise_commands(h, "发吧")
        assert r and r["action"] == "send_pending"
        r = MessageHandler._parse_precise_commands(h, "算了")
        assert r and r["action"] == "cancel_pending"

    def test_llm_tools_exist(self):
        """意图解析的正解：LLM 工具必须存在且带「答应的事要记下来」语义"""
        src = Path(__file__).parent.parent / "agent" / "handler.py"
        text = src.read_text(encoding="utf-8")
        assert '"set_reminder"' in text, "set_reminder 工具丢失"
        assert '"group_say_later"' in text, "group_say_later 工具丢失"
