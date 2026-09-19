"""
⏰ 定时任务调度器 — 支持一次性延迟和周期性日程

asyncio 定时器 + JSON 持久化（GreetingScheduler 已退役 2026-08-15）。
支持：一次性延迟、每日重复、每周重复。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from napcat.ws_client import send_delivery_state

from .async_io import run_bounded_store_io
from .interaction_contract import ProactiveEvent, build_proactive_event
from .proactive_decision import (
    finalize_proactive_decision,
    start_proactive_decision,
)

logger = logging.getLogger("糖糖.Scheduler")

TASKS_FILE = ".scheduled_tasks.json"


def _finalize_proactive_decision_sync(store, event, lease_token, run, **kwargs):
    """在线程中执行 DecisionRun 落盘，同时保留显式生产调用接线。"""
    return finalize_proactive_decision(
        store,
        event,
        lease_token,
        run,
        **kwargs,
    )


class CronScheduler:
    """轻量 cron 调度器（精度：分钟级）"""

    def __init__(
        self,
        send_group_msg: Callable[..., Awaitable[bool]],
        send_private_msg: Callable[..., Awaitable[bool]],
        llm_caller: Callable[..., Awaitable[str]],
        get_group_ids: Callable[[], list[str]],
        restart_callback: Callable[[], Awaitable[None]] | None = None,
        enrich: Callable[[str, str], str] | None = None,
        proactive_event_sink: Callable[[ProactiveEvent], None] | None = None,
        proactive_event_store: object | None = None,
    ):
        self._send_group = send_group_msg
        self._send_private = send_private_msg
        self._llm = llm_caller
        self._get_groups = get_group_ids
        self._restart = restart_callback
        self._enrich = enrich
        self._proactive_event_sink = proactive_event_sink
        # 仅依赖主动事件的窄存储契约（record/claim/executing/finish）；
        # 未传入时保持旧测试和离线调用的行为，不会凭空改变发送语义。
        self._proactive_event_store = proactive_event_store
        self._tasks: list[dict] = []
        self._task: asyncio.Task | None = None
        self._state_corrupt = False
        self._load()

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把主动事件协调的同步 Store 调用移出调度事件循环。"""
        return await run_bounded_store_io(
            operation,
            func,
            *args,
            logger=logger,
            log_prefix="⏰ 调度器 Store SQLite 调用较慢",
            **kwargs,
        )

    # ═══════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════

    def add_one_shot(self, text: str, delay_minutes: int,
                     group_id: str = "", user_id: str = "") -> int:
        """添加一次性任务：delay_minutes 分钟后执行"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝创建一次性任务")
            return 0
        fire_at = datetime.now() + timedelta(minutes=delay_minutes)
        tid = int(datetime.now().timestamp() * 1000)  # 用毫秒时间戳当 ID
        task = {
            "id": tid,
            "type": "once",
            "text": text,
            "fire_at": fire_at.strftime("%Y-%m-%d %H:%M"),
            "group_id": group_id,
            "user_id": user_id,
        }
        self._tasks.append(task)
        self._save()
        logger.info(f"⏰ 一次性任务 #{tid}: {delay_minutes}分钟后 → {text[:40]}")
        return tid

    def add_daily(self, text: str, hour: int, minute: int,
                  group_id: str = "") -> int:
        """添加每日重复任务"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝创建每日任务")
            return 0
        tid = int(datetime.now().timestamp() * 1000)
        task = {
            "id": tid,
            "type": "daily",
            "text": text,
            "hour": hour,
            "minute": minute,
            "group_id": group_id,
        }
        self._tasks.append(task)
        self._save()
        logger.info(f"⏰ 每日任务 #{tid}: {hour:02d}:{minute:02d} → {text[:40]}")
        return tid

    def ensure_daily(self, text: str, hour: int, minute: int,
                     group_id: str = "") -> int:
        """确保同一条每日任务只存在一份，并返回其 ID。"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝覆盖原文件")
            return 0
        for task in self._tasks:
            if (
                task.get("type") == "daily"
                and task.get("text") == text
                and task.get("hour") == hour
                and task.get("minute") == minute
                and task.get("group_id", "") == group_id
            ):
                return int(task["id"])
        return self.add_daily(text, hour, minute, group_id=group_id)

    def add_weekly(self, text: str, weekday: int, hour: int, minute: int,
                   group_id: str = "") -> int:
        """添加每周重复任务。weekday: 0=周一, 6=周日"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝创建每周任务")
            return 0
        tid = int(datetime.now().timestamp() * 1000)
        task = {
            "id": tid,
            "type": "weekly",
            "text": text,
            "weekday": weekday,
            "hour": hour,
            "minute": minute,
            "group_id": group_id,
        }
        self._tasks.append(task)
        self._save()
        logger.info(f"⏰ 每周任务 #{tid}: 周{weekday+1} {hour:02d}:{minute:02d} → {text[:40]}")
        return tid

    def remove_task(self, task_id: int) -> bool:
        """删除任务"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝修改原文件")
            return False
        for t in self._tasks:
            if t["id"] == task_id:
                self._tasks.remove(t)
                self._save()
                return True
        return False

    def list_tasks(self) -> str:
        """返回任务列表的格式化字符串"""
        if self._state_corrupt:
            return "⚠️ 定时任务状态文件损坏，自动执行已暂停，请先人工修复"
        if not self._tasks:
            return "没有定时任务喵～"
        lines = ["⏰ 定时任务列表："]
        for t in sorted(self._tasks, key=lambda x: x.get("id", 0)):
            tid = t["id"]
            ttype = t["type"]
            if ttype == "once":
                desc = f"一次性 — {t['fire_at']}"
            elif ttype == "daily":
                desc = f"每日 {t['hour']:02d}:{t['minute']:02d}"
            elif ttype == "weekly":
                wd = ["一", "二", "三", "四", "五", "六", "日"][t['weekday']]
                desc = f"每周{wd} {t['hour']:02d}:{t['minute']:02d}"
            else:
                desc = str(ttype)
            target = f"群{t['group_id']}" if t.get("group_id") else "私聊"
            state = t.get("status", "")
            state_label = {
                "sending": "（发送中断，等待重启核验）",
                "uncertain": "（发送未确认，已暂停重放）",
                "failed": "（发送失败）",
            }.get(state, "")
            lines.append(f"  #{tid} {desc} → {target}: {t['text'][:40]}{state_label}")
        return "\n".join(lines)

    # ═══════════════════════════════════════
    # 调度循环
    # ═══════════════════════════════════════

    def start(self):
        """启动调度器"""
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，调度器保持停用")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(f"⏰ 定时调度器已启动 ({len(self._tasks)}个任务)")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        """每 30 秒检查一次是否有任务到期"""
        while True:
            try:
                await asyncio.sleep(30)
                await self._tick()
                logger.info("🫀 定时调度心跳")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("定时调度检查出错")
                await asyncio.sleep(30)

    async def _tick(self):
        """检查并触发到期的任务"""
        if self._state_corrupt:
            return
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        for t in list(self._tasks):
            # 循环每 30 秒运行；同一分钟的 daily/weekly 任务只能尝试一次。
            if t.get("last_attempt_at") == minute_key:
                continue
            ttype = t["type"]
            due = False
            if ttype == "once":
                fire_str = t.get("fire_at", "")
                if fire_str and fire_str == now.strftime("%Y-%m-%d %H:%M"):
                    due = True
            elif ttype == "daily":
                if now.hour == t["hour"] and now.minute == t["minute"]:
                    due = True
            elif ttype == "weekly":
                if now.weekday() == t["weekday"] and now.hour == t["hour"] and now.minute == t["minute"]:
                    due = True
            if not due:
                continue

            # 外部副作用前先写入稳定占位。若进程在 _fire 中崩溃，
            # _load 会把 sending 隔离为 uncertain，避免一次性消息重放。
            previous_last_attempt = t.get("last_attempt_at")
            previous_status = t.get("status")
            t["last_attempt_at"] = minute_key
            t["status"] = "sending"
            if not self._save():
                if previous_last_attempt is None:
                    t.pop("last_attempt_at", None)
                else:
                    t["last_attempt_at"] = previous_last_attempt
                if previous_status is None:
                    t.pop("status", None)
                else:
                    t["status"] = previous_status
                logger.error("⏰ 无法持久化发送占位，跳过任务 #%s", t["id"])
                continue
            state = await self._fire(t)
            t["status"] = state
            # 一次性任务只有 confirmed 才能从持久队列删除；uncertain/failed
            # 留在列表供查询，但不会因时间已过而自动重放。
            if ttype == "once" and state == "confirmed":
                index = self._tasks.index(t)
                self._tasks.remove(t)
                if not self._save():
                    t["status"] = "sending"
                    self._tasks.insert(index, t)
                    logger.error(
                        "⏰ 任务 #%s 已送达但终态落盘失败，保留 sending",
                        t["id"],
                    )
            elif not self._save():
                # 发送前的 sending 已经稳定落盘；终态写失败时内存也保持
                # sending，使当前进程和重启恢复都不会盲目重放。
                t["status"] = "sending"
                logger.error(
                    "⏰ 任务 #%s 终态落盘失败，保留 sending", t["id"],
                )

    async def _fire(self, task: dict) -> str:
        """执行任务并返回 confirmed / uncertain / failed。"""
        text = task["text"]
        group_id = task.get("group_id", "")
        user_id = task.get("user_id", "")

        # 系统级重启哨兵不会发送 QQ 消息，也不应创建主动事件或进入
        # 事件租约流程。否则 SystemExit 发生在终态落盘前，重启后会凭空
        # 留下 PROCESS_RESTARTED_DURING_EVENT/uncertain 记录。
        if text.strip() == "__RESTART__":
            logger.info(f"⏰ 定时重启任务触发 #{task['id']}")
            if self._restart:
                try:
                    await self._restart()
                except Exception:
                    logger.exception("⏰ 定时重启回调失败")
                    return "uncertain"
                return "confirmed"
            return "failed"

        # 先记录“主动入口已产生”的事实，再生成文本或触碰 QQ；这不是送达
        # 回执，confirmed/uncertain/failed 仍只由下方实际发送结果决定。
        if group_id:
            targets = [("group", str(group_id))]
        elif user_id:
            targets = [("private", str(user_id))]
        else:
            targets = [("group", str(gid)) for gid in self._get_groups()]
        occurrence = str(
            task.get("last_attempt_at") or task.get("fire_at") or "manual"
        )
        prepared_events: list[tuple[ProactiveEvent, str]] = []
        for channel, target in targets:
            event = build_proactive_event(
                event_id=f"scheduler:{task.get('id')}:{channel}:{target}:{occurrence}",
                source="scheduler",
                channel=channel,
                target=target,
                idempotency_key=(
                    f"scheduler:{task.get('id')}:{channel}:{target}:{occurrence}"
                ),
                payload={
                    "kind": "scheduled_task",
                    "task_id": str(task.get("id", "")),
                    "task_type": str(task.get("type", "")),
                    "source_text": text,
                    "occurrence": occurrence,
                },
            )
            sink = self._proactive_event_sink
            stored = None
            if sink is not None:
                try:
                    stored = await self._run_store_io(
                        "scheduler.proactive_event_sink", sink, event,
                    )
                except Exception:
                    logger.warning(
                        "⏰ ProactiveEvent 记录失败，不影响定时任务执行",
                        exc_info=True,
                    )
            store = self._proactive_event_store
            if store is None:
                # 没有持久事件协调器时保留旧的直接发送路径；有协调器时
                # 必须先拥有租约，避免来源已重复但 QQ 发送被再次触碰。
                prepared_events.append((event, ""))
                continue
            if not isinstance(stored, dict):
                try:
                    stored = await self._run_store_io(
                        "scheduler.get_proactive_event",
                        store.get_proactive_event,
                        event.event_id,
                    )
                except Exception:
                    stored = None
            if not stored:
                logger.error(
                    "⏰ ProactiveEvent 未持久化，拒绝发送 event_id=%s",
                    event.event_id,
                )
                continue
            lease_token = f"scheduler:{uuid.uuid4().hex}"
            try:
                claimed = bool(await self._run_store_io(
                    "scheduler.claim_proactive_event",
                    store.claim_proactive_event,
                    event.event_id,
                    lease_token,
                ))
                executing = claimed and bool(await self._run_store_io(
                    "scheduler.mark_proactive_event_executing",
                    store.mark_proactive_event_executing,
                    event.event_id,
                    lease_token,
                ))
            except Exception:
                logger.error(
                    "⏰ ProactiveEvent claim/executing 失败，拒绝发送 event_id=%s",
                    event.event_id,
                    exc_info=True,
                )
                continue
            if not executing:
                if claimed:
                    release = getattr(store, "release_proactive_event_claim", None)
                    if callable(release):
                        try:
                            await self._run_store_io(
                                "scheduler.release_proactive_event_claim",
                                release,
                                event.event_id,
                                lease_token,
                            )
                        except Exception:
                            logger.error(
                                "⏰ ProactiveEvent claim 释放失败 event_id=%s",
                                event.event_id, exc_info=True,
                            )
                logger.info(
                    "⏰ ProactiveEvent 非 pending，跳过重复执行 event_id=%s",
                    event.event_id,
                )
                continue
            prepared_events.append((event, lease_token))

        if self._proactive_event_store is not None and not prepared_events:
            # 事件已被其他 worker/本次重入领取，不能再为同一来源调用 LLM
            # 或触碰 QQ；失败状态由外层任务持久化，后续不会按时间盲重放。
            return "failed"

        logger.info(f"⏰ 触发任务 #{task['id']}: {text[:40]}")

        # 每个目标事件各自绑定一个决策回合；同一条润色文本可发送给多个
        # 目标，但决策事实不能跨 scope 共用。无持久协调器时保留旧兼容路径。
        decision_runs = {
            event.event_id: start_proactive_decision(event)
            for event, lease_token in prepared_events
            if lease_token
        }

        # 用 LLM 润色（对一次性任务更自然）
        try:
            msg = await self._llm(
                "你是一只有责任心的猫娘——小糖糖。你要帮群友传达一条提醒/消息。"
                "自然地用猫娘的语气说，不要生硬地复读原文。1-2句话。",
                f"你要传达的内容：{text}"
            )
            msg = msg.strip().strip('"').strip("'")
            if len(msg) < 3:
                msg = text
        except Exception as exc:
            if self._proactive_event_store is not None:
                for event, lease_token in prepared_events:
                    run = decision_runs.get(event.event_id)
                    if run is not None:
                        try:
                            await self._run_store_io(
                                "scheduler.finalize_proactive_decision_error",
                                _finalize_proactive_decision_sync,
                                self._proactive_event_store,
                                event,
                                lease_token,
                                run,
                                error_code=type(exc).__name__,
                            )
                        except Exception:
                            logger.error(
                                "⏰ 主动 DecisionRun 失败落盘异常 event_id=%s",
                                event.event_id, exc_info=True,
                            )
                    await self._finish_proactive_event(
                        event, lease_token, "failed", error_code="LLM_UNAVAILABLE",
                    )
                # 有持久事件协调器时禁止绕过 LLM 决策直接发送原文。
                return "failed"
            msg = text

        decision_events: list[tuple[ProactiveEvent, str]] = []
        for event, lease_token in prepared_events:
            run = decision_runs.get(event.event_id)
            if run is None:
                decision_events.append((event, lease_token))
                continue
            try:
                terminal, bound = await self._run_store_io(
                    "scheduler.finalize_proactive_decision",
                    _finalize_proactive_decision_sync,
                    self._proactive_event_store,
                    event,
                    lease_token,
                    run,
                    reply=msg,
                    responded=True,
                )
            except Exception:
                logger.error(
                    "⏰ 主动 DecisionRun 绑定异常 event_id=%s",
                    event.event_id, exc_info=True,
                )
                await self._finish_proactive_event(
                    event, lease_token, "failed", error_code="DECISION_BIND_ERROR",
                )
                continue
            if terminal.status != "completed" or not bound:
                await self._finish_proactive_event(
                    event, lease_token, "failed", error_code="DECISION_BIND_FAILED",
                )
                continue
            decision_events.append((event, lease_token))

        if self._proactive_event_store is not None and not decision_events:
            return "failed"

        # 2026-08-16 发送链路审计：LLM 润色的提醒文本发送前过清洗+贴图解析
        if self._enrich:
            msg = self._enrich(msg, group_id)
            if not msg:
                for event, lease_token in decision_events:
                    await self._finish_proactive_event(
                        event, lease_token, "skipped", error_code="EMPTY_MESSAGE",
                    )
                return "failed"

        try:
            states: list[str] = []
            for index, (event, lease_token) in enumerate(decision_events):
                try:
                    if event.channel == "private":
                        result = await self._send_private(event.target, msg)
                    else:
                        result = await self._send_group(event.target, msg)
                    state = send_delivery_state(result)
                except Exception:
                    # POST 可能已到达而响应丢失，不能把它当成可安全重放的失败。
                    state = "uncertain"
                    logger.exception(
                        "⏰ ProactiveEvent 发送响应丢失 event_id=%s",
                        event.event_id,
                    )
                states.append(state)
                await self._finish_proactive_event(event, lease_token, state)
                if not group_id and not user_id and index + 1 < len(prepared_events):
                    await asyncio.sleep(random.uniform(1, 3))
            if states and all(state == "confirmed" for state in states):
                logger.info(f"⏰ 任务 #{task['id']} 已确认送达")
                return "confirmed"
            if any(state in ("confirmed", "uncertain") for state in states):
                logger.warning(f"⏰ 任务 #{task['id']} 发送未确认: {states}")
                return "uncertain"
            logger.warning(f"⏰ 任务 #{task['id']} 发送失败: {states}")
            return "failed"
        except Exception:
            # 外部发送回调抛异常时，POST 可能已经到达 QQ 网关。不能把它
            # 降级为确定失败后允许重放；冻结为 uncertain 交给人工核验。
            logger.exception(f"执行任务 #{task['id']} 响应丢失，冻结为未确认")
            return "uncertain"

    async def _finish_proactive_event(
            self, event: ProactiveEvent, lease_token: str, status: str,
            *, error_code: str = "",
    ) -> None:
        """提交事件终态；无持久协调器时是兼容性空操作。"""
        store = self._proactive_event_store
        if store is None or not lease_token:
            return
        try:
            await self._run_store_io(
                "scheduler.finish_proactive_event",
                store.finish_proactive_event,
                event.event_id,
                lease_token,
                status,
                error_code=error_code,
            )
        except Exception:
            logger.error(
                "⏰ ProactiveEvent 终态落盘失败 event_id=%s status=%s",
                event.event_id, status, exc_info=True,
            )

    # ═══════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════

    def _save(self) -> bool:
        if self._state_corrupt:
            logger.error("⏰ 定时任务状态损坏，拒绝覆盖原文件")
            return False
        tmp_file = f"{TASKS_FILE}.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self._tasks, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, TASKS_FILE)
            return True
        except OSError:
            logger.exception("⏰ 保存定时任务失败")
            try:
                if os.path.isfile(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass
            return False

    def _load(self):
        if not os.path.exists(TASKS_FILE):
            return
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError("tasks state must be a list")
            for task in loaded:
                if not isinstance(task, dict):
                    raise ValueError("task must be an object")
                if task.get("type") not in ("once", "daily", "weekly"):
                    raise ValueError("invalid task type")
                if "id" not in task or not isinstance(task.get("text"), str):
                    raise ValueError("task id/text missing")
            self._tasks = loaded
            changed = False
            normalized_tasks: list[dict] = []
            restart_seen = False
            duplicate_restarts = 0
            for task in self._tasks:
                is_restart = (
                    task.get("type") == "daily"
                    and task.get("text") == "__RESTART__"
                    and task.get("hour") == 6
                    and task.get("minute") == 0
                    and not task.get("group_id")
                )
                if is_restart and restart_seen:
                    duplicate_restarts += 1
                    changed = True
                    continue
                if is_restart:
                    restart_seen = True
                if task.get("status") == "sending":
                    task["status"] = "uncertain"
                    changed = True
                normalized_tasks.append(task)
            self._tasks = normalized_tasks
            if duplicate_restarts:
                logger.warning(
                    "⏰ 已清理 %d 条重复的每日重启任务", duplicate_restarts
                )
            if changed:
                # 进程重启后发送结果不可证实，持久化隔离状态。
                self._save()
            self._state_corrupt = False
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            self._tasks = []
            self._state_corrupt = True
            logger.exception("⏰ 定时任务状态损坏，保留原文件并暂停自动执行")


# ═══════════════════════════════════════════════════════════════════
# 2026-08-16 范式转换（教训表 #24）：parse_natural_schedule 已删除——
# 自然语言定时意图一律由 LLM 工具（set_reminder / group_say_later）决定；
# /定时 命令只保留管理操作（列表/删除），创建提醒请直接说自然语言。
# ═══════════════════════════════════════════════════════════════════
