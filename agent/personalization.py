"""
🎂 生日祝福 — 从记忆 people.birthday 字段读取，当天主动在群里祝福
💭 个人偏好 — 分析群友聊天记录，记住每个人喜欢/讨厌的话题
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from collections import defaultdict
from datetime import datetime

from onebot.ws_client import send_delivery_state

from .async_io import run_bounded_store_io
from .interaction_contract import build_proactive_event
from .proactive_decision import finalize_proactive_decision, start_proactive_decision

logger = logging.getLogger("糖糖.Personalization")


# ═══════════════════════════════════════════════════════════
# 生日祝福 — 每天检查是否有群友过生日，有的话在群里发 LLM 生成的自然祝福
# ═══════════════════════════════════════════════════════════


class BirthdayGreeter:
    """每天检查是否有群友过生日，有的话在群里发 LLM 生成的自然祝福"""

    def __init__(self, send_group_msg, store, llm_caller, get_group_ids):
        """
        store: Store 实例（用于 birthday 字段查询）
        llm_caller: async (system, user) -> str
        """
        self._send = send_group_msg
        self._store = store
        self._llm = llm_caller
        self._get_groups = get_group_ids
        self._sent_today: set[str] = set()  # qq_id 今天已祝福过的
        self._uncertain_today: set[str] = set()  # 已接受但未确认；当天不自动重放
        self._sending_today: set[str] = set()  # 发送中崩溃；重启转 uncertain
        self._last_date: str = ""
        self._task: asyncio.Task | None = None

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把生日状态与人物档案的同步 Store 调用移出事件循环。"""
        return await run_bounded_store_io(
            operation,
            func,
            *args,
            logger=logger,
            log_prefix="🎂 生日 Store SQLite 调用较慢",
            **kwargs,
        )

    def start(self):
        """启动定时检查（每2小时查一次）"""
        self._task = asyncio.create_task(self._loop())
        logger.info("🎂 生日祝福检查已启动")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    _STATE_KEY = "birthday_sent"

    def _save_sent_today(self):
        if self._store:
            self._store.kv_set(self._STATE_KEY, json.dumps(
                {
                    "date": self._last_date,
                    "sent": list(self._sent_today),
                    "uncertain": list(self._uncertain_today),
                    "sending": list(self._sending_today),
                },
                ensure_ascii=False
            ))

    def _load_sent_today(self):
        if not self._store:
            return
        raw = self._store.kv_get(self._STATE_KEY)
        self._restore_sent_today(raw)

    def _restore_sent_today(self, raw):
        if raw:
            try:
                data = json.loads(raw)
                if data.get("date") == self._last_date:
                    self._sent_today.update(data.get("sent", []))
                    self._uncertain_today.update(data.get("uncertain", []))
                    # sending 表示外部请求可能已执行；恢复为不确定，
                    # 不能在重启后再次祝福同一个人。
                    self._uncertain_today.update(data.get("sending", []))
            except json.JSONDecodeError:
                pass

    async def _save_sent_today_async(self):
        if self._store:
            await self._run_store_io(
                "personalization.kv_set_birthday_state",
                self._store.kv_set,
                self._STATE_KEY,
                json.dumps(
                    {
                        "date": self._last_date,
                        "sent": list(self._sent_today),
                        "uncertain": list(self._uncertain_today),
                        "sending": list(self._sending_today),
                    },
                    ensure_ascii=False,
                ),
            )

    async def _load_sent_today_async(self):
        if not self._store:
            return
        raw = await self._run_store_io(
            "personalization.kv_get_birthday_state",
            self._store.kv_get,
            self._STATE_KEY,
        )
        self._restore_sent_today(raw)

    async def _loop(self):
        await asyncio.sleep(60)  # 启动后等1分钟再查
        while True:
            try:
                await self._check_birthdays()
                logger.info("🫀 生日检查心跳")
                await asyncio.sleep(7200)  # 每2小时
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("生日检查出错")
                await asyncio.sleep(3600)

    async def _check_birthdays(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_date:
            self._sent_today.clear()
            self._uncertain_today.clear()
            self._sending_today.clear()
            self._last_date = today
            await self._load_sent_today_async()  # 从磁盘恢复今天的已发记录

        # 方案 A：从 people 表 birthday 字段查（精确、可靠）
        try:
            today_bdays = await self._run_store_io(
                "personalization.get_today_birthdays",
                self._store.get_today_birthdays,
            )
        except Exception:
            today_bdays = []

        for person in today_bdays:
            qq_id = person["qq_id"]
            if (qq_id not in self._sent_today
                    and qq_id not in self._uncertain_today
                    and qq_id not in self._sending_today):
                self._sending_today.add(qq_id)
                await self._save_sent_today_async()
                state = await self._send_greeting(qq_id, person.get("nickname", qq_id))
                if state == "confirmed":
                    self._sending_today.discard(qq_id)
                    self._sent_today.add(qq_id)
                    await self._save_sent_today_async()
                elif state == "uncertain":
                    self._sending_today.discard(qq_id)
                    self._uncertain_today.add(qq_id)
                    await self._save_sent_today_async()
                else:
                    self._sending_today.discard(qq_id)
                    await self._save_sent_today_async()

        # 方案 B（记忆搜索兜底）已移除——搜索 %03-15% 可能命中"3月15号去看电影"等非生日内容
        # 生日只从 people.birthday 字段读取，通过 /生日 设置 命令写入，100% 可靠

    async def _send_greeting(self, qq_id: str, nickname: str) -> str:
        """LLM 生成生日祝福，返回 confirmed / uncertain / failed。"""
        groups = self._get_groups()
        if not groups:
            return "failed"

        # 尝试获取该群友的记忆增强祝福
        name = nickname or qq_id
        try:
            person = await self._run_store_io(
                "personalization.get_or_create_person",
                self._store.get_or_create_person,
                qq_id,
            )
            name = person.get("nickname", name)
        except Exception:
            pass

        system = (
            "你是一只有爱心又调皮的猫娘——小糖糖。今天是群友的生日，"
            "你要在群里送上生日祝福。语气要温暖可爱，带一点猫娘特有的俏皮。"
            "不是复读模板，要让人感觉你真的记得ta的生日。2-3句话。"
        )
        user = f"今天是群友「{name}」的生日！请在群里为ta送上祝福～"

        async def _generate_message() -> str:
            if not self._llm:
                return ""
            try:
                reply = await self._llm(system, user)
                reply = reply.strip().strip('"').strip("'")
                if reply and len(reply) >= 5:
                    return reply
            except Exception:
                pass
            return ""

        # 新 Store 已具备主动事件状态机时，生日也必须先持久化来源、取得
        # 租约、写入终态 DecisionRun，之后才能请求 LLM 或触碰 QQ。旧测试桩
        # 与历史调用方没有该接口时保留原有直发兼容，不能把升级变成静默停服。
        event_store = self._store
        event_methods = (
            "record_proactive_event", "claim_proactive_event",
            "mark_proactive_event_executing", "finish_proactive_event",
            "record_decision_run", "mark_proactive_event_decided",
        )
        managed_events = all(
            callable(getattr(event_store, method, None))
            for method in event_methods
        )
        if managed_events:
            today = self._last_date or datetime.now().strftime("%Y-%m-%d")
            for gid in groups:
                event = build_proactive_event(
                    event_id=f"birthday:{today}:{qq_id}:group:{gid}",
                    source="birthday",
                    channel="group",
                    target=str(gid),
                    payload={"kind": "birthday_greeting", "date": today},
                )
                try:
                    stored = await self._run_store_io(
                        "birthday.record_proactive_event",
                        event_store.record_proactive_event, event,
                    )
                except Exception:
                    logger.exception("🎂 生日 ProactiveEvent 落盘失败")
                    return "failed"
                status = str((stored or {}).get("status") or "")
                if status == "confirmed":
                    return "confirmed"
                if status in {"uncertain", "claimed", "executing", "decided", "skipped"}:
                    return "uncertain"
                if status != "pending":
                    logger.error("🎂 生日 ProactiveEvent 非法状态: %s", status or "missing")
                    return "failed"

                lease_token = f"birthday:{uuid.uuid4().hex}"
                try:
                    claimed = bool(await self._run_store_io(
                        "birthday.claim_proactive_event",
                        event_store.claim_proactive_event, event.event_id, lease_token,
                    ))
                    executing = claimed and bool(await self._run_store_io(
                        "birthday.mark_proactive_event_executing",
                        event_store.mark_proactive_event_executing,
                        event.event_id, lease_token,
                    ))
                except Exception:
                    logger.exception("🎂 生日 ProactiveEvent claim/executing 失败")
                    return "failed"
                if not executing:
                    if claimed and callable(getattr(event_store, "release_proactive_event_claim", None)):
                        await self._run_store_io(
                            "birthday.release_proactive_event_claim",
                            event_store.release_proactive_event_claim,
                            event.event_id, lease_token,
                        )
                    return "failed"

                run = start_proactive_decision(event)
                msg = await _generate_message()
                if not msg:
                    await self._run_store_io(
                        "birthday.finalize_proactive_decision_empty",
                        finalize_proactive_decision,
                        event_store, event, lease_token, run,
                        error_code="EMPTY_LLM_REPLY",
                    )
                    await self._run_store_io(
                        "birthday.finish_proactive_event_empty",
                        event_store.finish_proactive_event,
                        event.event_id, lease_token, "failed",
                        error_code="EMPTY_LLM_REPLY",
                    )
                    return "failed"
                terminal, bound = await self._run_store_io(
                    "birthday.finalize_proactive_decision",
                    finalize_proactive_decision,
                    event_store, event, lease_token, run,
                    reply=msg, responded=True,
                )
                if terminal.status != "completed" or not bound:
                    await self._run_store_io(
                        "birthday.finish_proactive_event_decision",
                        event_store.finish_proactive_event,
                        event.event_id, lease_token, "failed",
                        error_code="DECISION_BIND_FAILED",
                    )
                    return "failed"
                try:
                    state = send_delivery_state(await self._send(gid, msg))
                except Exception:
                    logger.exception("🎂 生日祝福发送响应丢失 → 群%s", gid)
                    state = "uncertain"
                await self._run_store_io(
                    "birthday.finish_proactive_event",
                    event_store.finish_proactive_event,
                    event.event_id, lease_token, state,
                )
                if state == "confirmed":
                    logger.info("🎂 生日祝福已确认 → 群%s", gid)
                    return "confirmed"
                if state == "uncertain":
                    logger.warning("🎂 生日祝福发送未确认 → 群%s", gid)
                    return "uncertain"
            return "failed"

        msg = await _generate_message()
        if not msg:
            msg = random.choice([
                f"🎂 今天是 {name} 的生日！！糖糖记得哦～生日快乐呀！！尾巴疯狂摇晃中~ 🎉",
                f"🎉 生日快乐 {name}！！糖糖从日历里翻到的…祝你今天超级开心！🍰",
                f"🎂 哇！今天是 {name} 的生日！糖糖要第一个送上祝福～生日快乐喵~ 🎈",
            ])

        for gid in groups:
            try:
                state = send_delivery_state(await self._send(gid, msg))
                if state == "confirmed":
                    logger.info(f"🎂 生日祝福已确认 → {name}({qq_id}) 在群{gid}")
                    return "confirmed"  # 只发到第一个确认送达的群
                if state == "uncertain":
                    # 可能已投递，不能换群或在下次检查中盲重发。
                    logger.warning(
                        f"🎂 生日祝福发送未确认 → {name}({qq_id}) 在群{gid}"
                    )
                    return "uncertain"
            except Exception:
                # 外部 POST 可能已经执行，只是回调/响应在边界处丢失。
                # 不再换群重发，交给当天 uncertain 去重状态冻结。
                logger.exception(
                    f"🎂 生日祝福发送响应丢失 → {name}({qq_id}) 在群{gid}"
                )
                return "uncertain"
        return "failed"


# ═══════════════════════════════════════════════════════════
# 个人偏好 — 从 LLM 提取的 like/hate 记忆构建偏好摘要
# ═══════════════════════════════════════════════════════════
# 2026-08-16 范式转换（教训 #24）：关键词规则提取已删——
# 「该记什么偏好」是 LLM 的决策域（extract_semantic_memories 已教
# preference: 喜欢/讨厌/偏好），本模块只做查询与格式化。


class PreferenceTracker:
    """偏好查询——从 LLM 提取的 like/hate 记忆构建摘要（只读，不写）"""

    def __init__(self, storage_dir: str = ".", store=None):
        self._store = store

    def get_preferences(self, qq_id: str,
                        source_group_id: str | None = None) -> str:
        """返回该用户的偏好摘要（来自 LLM 提取的 like/hate 记忆，带真实来源）"""
        if not self._store:
            return ""
        try:
            rows = self._store.query_memories(
                str(qq_id), trusted_only=True,
                source_group_id=source_group_id,
            )
        except Exception:
            return ""
        likes = [r.get("value", "") for r in rows if r.get("key") == "like"][:3]
        hates = [r.get("value", "") for r in rows if r.get("key") == "hate"][:2]
        parts = []
        if likes:
            parts.append("喜欢: " + "、".join(likes))
        if hates:
            parts.append("讨厌: " + "、".join(hates))
        return " | ".join(parts) if parts else ""


def create_birthday_greeter(send_group_msg, store, llm_caller, get_group_ids):
    return BirthdayGreeter(
        send_group_msg=send_group_msg,
        store=store,
        llm_caller=llm_caller,
        get_group_ids=get_group_ids,
    )


def create_preference_tracker(storage_dir: str = ".", store=None):
    return PreferenceTracker(storage_dir=storage_dir, store=store)
