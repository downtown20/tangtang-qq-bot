"""
📊 每日播报 — 糖糖每天主动发送群活跃日报

asyncio 定时器 + JSON 持久化模式（GreetingScheduler 已退役 2026-08-15）。
每天在配置时间点发送：日期天气 + 群活跃统计 + 趣味小语。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from napcat.ws_client import is_send_confirmed, send_delivery_state
from .async_io import run_bounded_blocking, run_bounded_store_io

logger = logging.getLogger("糖糖.DailyReport")

STATE_FILE = ".daily_report_state.json"


class DailyReportScheduler:
    """每日播报调度器（带持久化 + 补发容错）"""

    def __init__(
        self,
        config: dict,
        llm_caller: Callable[..., Awaitable[str]],      # async (system, user) -> str
        send_group_msg: Callable[..., Awaitable[bool]],  # async (group_id, text) -> bool
        get_group_ids: Callable[[], list[str]],          # () -> list[str]
        get_stats: Callable[[], dict],                   # () -> {people, memory, chat}
        get_weather: Callable[..., Awaitable[str]],      # async (city) -> str
        get_blacklist: Callable[[], set[str]] | None = None,
        enrich: Callable[[str, str], str] | None = None,
    ):
        self._cfg = config
        self._llm = llm_caller
        self._send = send_group_msg
        self._get_groups = get_group_ids
        self._get_stats = get_stats
        self._get_weather = get_weather
        self._get_blacklist = get_blacklist
        self._enrich = enrich

        self._enabled = config.get("enabled", True)
        self._time = config.get("time", "9:00")
        self._target_groups = config.get("groups", [])
        self._include_weather = config.get("include_weather", True)
        self._include_stats = config.get("include_stats", True)
        self._catchup_minutes = config.get("catchup_minutes", 30)

        # 运行时状态
        self._sent_today: list[str] = []  # 今天已发送的群
        self._uncertain_today: list[str] = []  # 已接受但未确认；当天不自动重发
        self._sending_today: list[str] = []  # 发送中崩溃；重启转 uncertain
        self._last_date: str = ""
        self._task: asyncio.Task | None = None
        self._claim_lock = asyncio.Lock()
        self._state_corrupt = False

    # ════════════════════════════════════════════════════════════
    # 公开接口
    # ════════════════════════════════════════════════════════════

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_time(self, time_str: str):
        """设置播报时间，格式 HH:MM"""
        import re
        if re.match(r'^\d{1,2}:\d{2}$', time_str):
            self._time = time_str
            logger.info(f"📊 每日播报时间已更新: {time_str}")

    def get_schedule(self) -> str:
        return f"📊 每日播报 {self._time}（{'已启用' if self._enabled else '已关闭'}）"

    def start(self):
        """启动定时检查循环"""
        if not self._enabled:
            logger.info("📊 每日播报未启用")
            return
        if self._task and not self._task.done():
            return

        self._load_state()
        if self._state_corrupt:
            logger.error("📊 每日播报状态损坏，已停用自动发送以避免重复播报")
            return
        asyncio.create_task(self._catch_up())
        self._task = asyncio.create_task(self._loop())
        logger.info(f"📊 每日播报已启动: {self._time}")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._save_state()

    # ════════════════════════════════════════════════════════════
    # 定时循环
    # ════════════════════════════════════════════════════════════

    async def _loop(self):
        """每 30 秒检查一次是否到点"""
        while True:
            try:
                await asyncio.sleep(30)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("每日播报检查出错")
                await asyncio.sleep(30)

    async def _tick(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        await self._reset_daily_async(today)
        current = now.strftime("%H:%M")

        if current == self._time:
            await self._broadcast()

    # ════════════════════════════════════════════════════════════
    # 补发容错
    # ════════════════════════════════════════════════════════════

    async def _catch_up(self):
        """启动时检查：如果当前时间在播报窗口之后不超过 catchup_minutes，补发"""
        now = datetime.now()
        try:
            h, m = map(int, self._time.split(":"))
            scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        except (ValueError, AttributeError):
            return

        if scheduled <= now < scheduled + timedelta(minutes=self._catchup_minutes):
            if self._sent_today:
                logger.debug("📊 今天已播报过，跳过补发")
                return
            logger.info("📊 补发每日播报（进程可能之前挂了）")
            await self._broadcast()

    # ════════════════════════════════════════════════════════════
    # 播报发送
    # ════════════════════════════════════════════════════════════

    async def _broadcast(self):
        """发送播报到所有目标群"""
        if self._state_corrupt:
            logger.error("📊 状态文件待人工修复，跳过每日播报")
            return
        groups = self._get_target_groups()
        terminal = (
            set(self._sent_today) | set(self._uncertain_today)
            | set(self._sending_today)
        )
        pending = [g for g in groups if g not in terminal]

        if not pending:
            return

        logger.info(f"📊 触发每日播报 → {len(pending)}个群")

        # 生成一次播报内容，所有群共用（加随机小变化）
        report = await self._generate_report()

        for group_id in pending:
            try:
                if report:
                    group_report = report
                    # 2026-08-16 发送链路审计：LLM 播报发送前过清洗+贴图解析
                    if self._enrich:
                        group_report = self._enrich(group_report, group_id)
                        if not group_report:
                            continue
                    # 生成播报期间可能有另一轮 _broadcast 同时进入；在锁内
                    # 二次检查并先落盘 sending，持久化失败则绝不执行 POST。
                    async with self._claim_lock:
                        terminal = (
                            set(self._sent_today) | set(self._uncertain_today)
                            | set(self._sending_today)
                        )
                        if group_id in terminal:
                            continue
                        self._sending_today.append(group_id)
                        if not await self._save_state_async():
                            self._sending_today.remove(group_id)
                            logger.error(
                                "📊 无法持久化发送占位，跳过群%s播报", group_id,
                            )
                            continue
                    result = await self._send(group_id, group_report)
                    # 只有明确送达才落盘去重；accepted/uncertain 状态保留在
                    # pending，下次调度可再次尝试而不会伪造「已播报」。
                    state = send_delivery_state(result)
                    async with self._claim_lock:
                        if is_send_confirmed(result):
                            if group_id in self._sending_today:
                                self._sending_today.remove(group_id)
                            if group_id not in self._sent_today:
                                self._sent_today.append(group_id)
                            persisted = await self._save_state_async()
                            if not persisted:
                                self._sent_today.remove(group_id)
                                self._sending_today.append(group_id)
                            else:
                                logger.info(f"📊 每日播报已发送 → 群{group_id}")
                        else:
                            if group_id in self._sending_today:
                                self._sending_today.remove(group_id)
                            if state == "uncertain":
                                if group_id not in self._uncertain_today:
                                    self._uncertain_today.append(group_id)
                            persisted = await self._save_state_async()
                            if not persisted:
                                if group_id in self._uncertain_today:
                                    self._uncertain_today.remove(group_id)
                                self._sending_today.append(group_id)
                    if state != "confirmed":
                        logger.warning(
                            f"📊 每日播报发送未确认 ({state}) → 群{group_id}"
                        )
                await asyncio.sleep(random.uniform(2, 5))
            except Exception:
                # 回调异常时请求是否已到网关不可知；保留 sending，
                # 下次启动 _load_state 会转成 uncertain。
                logger.exception(f"发送每日播报到群{group_id}失败")

    async def _generate_report(self) -> str:
        """调 LLM 生成自然播报文本"""
        now = datetime.now()
        date_str = now.strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]

        # 天气
        weather_text = ""
        if self._include_weather:
            try:
                weather_text = await self._get_weather("郴州")  # 糖糖老家
            except Exception:
                weather_text = "（天气暂时查不到喵~）"

        # 群统计
        stats_text = ""
        if self._include_stats:
            try:
                stats = await run_bounded_store_io(
                    "daily_report.get_stats",
                    self._get_stats,
                    logger=logger,
                    log_prefix="📊 每日播报 Store SQLite 调用较慢",
                )
                stats_text = (
                    f"糖糖认识了{stats.get('people_count', '?')}位群友，"
                    f"记住了{stats.get('memory_count', '?')}件事，"
                    f"群里聊了{stats.get('chat_count', '?')}条消息"
                    + (f"。{stats['feedback_vibe']}" if stats.get('feedback_vibe') else "")
                )
            except Exception:
                stats_text = ""

        system = (
            "你是一只有活力的猫娘——小糖糖。现在你要在群里发今日播报。\n"
            "播报要包含：日期、天气、群数据（如果提供了的话），"
            "最后加一句元气满满的鼓励或有趣的点评。\n"
            "语气要活泼可爱但不要太长（3-5句话），像早上跟朋友打招呼一样自然。\n"
            "每次播报的开头和结尾都换一种方式——每天用同一个模板，群友会觉得是机器人在定时播报，而不是有人在跟他们道早安。"
        )
        user = (
            f"今天是{date_str} {weekday}。\n"
            + (f"天气：{weather_text}\n" if weather_text else "")
            + (f"群数据：{stats_text}\n" if stats_text else "")
            + "\n请用猫娘的语气发一条元气满满的今日播报~"
        )

        try:
            reply = await self._llm(system, user)
            reply = reply.strip().strip('"').strip("'")
            if len(reply) > 300:
                reply = reply[:300]
            if not reply or len(reply) < 10:
                return self._fallback(date_str, weekday, weather_text)
            return reply
        except Exception:
            logger.exception("生成播报失败")
            return self._fallback(date_str, weekday, weather_text)

    def _fallback(self, date_str: str, weekday: str, weather: str) -> str:
        """兜底播报模板"""
        templates = [
            f"☀️ 早上好喵~ 今天是{date_str} {weekday}！\n{weather}\n新的一天也要元气满满哦～",
            f"🍬 叮咚！糖糖的每日播报来啦~\n{date_str} {weekday}\n{weather}\n大家今天有什么计划呀？",
            f"✨ 早安！{date_str} {weekday}\n{weather}\n糖糖今天也会努力给大家带来快乐喵~",
        ]
        return random.choice(templates)

    # ════════════════════════════════════════════════════════════
    # 持久化
    # ════════════════════════════════════════════════════════════

    def _save_state(self) -> bool:
        if self._state_corrupt:
            logger.error("📊 状态文件损坏，拒绝覆盖原文件")
            return False
        state = {
            "date": self._last_date or datetime.now().strftime("%Y-%m-%d"),
            "sent": self._sent_today,
            "uncertain": self._uncertain_today,
            "sending": self._sending_today,
        }
        tmp_file = f"{STATE_FILE}.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_file, STATE_FILE)
            return True
        except OSError:
            logger.exception("📊 保存每日播报状态失败")
            try:
                if os.path.isfile(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass
            return False

    async def _save_state_async(self) -> bool:
        """把播报状态文件写入移出事件循环；同步入口保留给 stop/兼容调用方。"""
        return bool(await run_bounded_blocking(
            "daily_report.save_state",
            self._save_state,
            logger=logger,
            log_prefix="📊 每日播报状态文件写入较慢",
        ))

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
            for key in ("sent", "uncertain", "sending"):
                value = state.get(key, [])
                if not isinstance(value, list):
                    raise ValueError(f"{key} must be a list")
            saved_date = state.get("date", "")
            if not isinstance(saved_date, str):
                raise ValueError("date must be a string")
            today = datetime.now().strftime("%Y-%m-%d")
            if saved_date == today:
                self._sent_today = state.get("sent", [])
                self._uncertain_today = state.get("uncertain", [])
                self._sending_today = state.get("sending", [])
                if self._sending_today:
                    self._uncertain_today = list(dict.fromkeys(
                        self._uncertain_today + self._sending_today
                    ))
                    self._sending_today = []
                self._last_date = today
            else:
                logger.debug(f"📊 状态日期({saved_date})≠今天({today})，丢弃")
            self._state_corrupt = False
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            self._state_corrupt = True
            logger.exception("📊 每日播报状态损坏，保留原文件并禁止自动发送")

    def _get_target_groups(self) -> list[str]:
        if self._target_groups:
            groups = self._target_groups
        else:
            groups = self._get_groups()
        blacklist = self._get_blacklist() if self._get_blacklist else set()
        return [g for g in groups if g not in blacklist]

    async def _reset_daily_async(self, today: str):
        if self._state_corrupt:
            return
        if not self._last_date:
            self._last_date = today
        if today != self._last_date:
            self._sent_today = []
            self._uncertain_today = []
            self._sending_today = []
            self._last_date = today
            await self._save_state_async()
            logger.debug(f"📊 日期翻篇 {today}，重置播报记录")

    def _reset_daily(self, today: str):
        """同步兼容入口；事件循环路径使用 ``_reset_daily_async``。"""
        if self._state_corrupt:
            return
        if not self._last_date:
            self._last_date = today
        if today != self._last_date:
            self._sent_today = []
            self._uncertain_today = []
            self._sending_today = []
            self._last_date = today
            self._save_state()
            logger.debug(f"📊 日期翻篇 {today}，重置播报记录")


# ═══════════════════════════════════════════════════════════════════
# 便利工厂函数
# ═══════════════════════════════════════════════════════════════════

def create_daily_report_scheduler(
    config: dict,
    llm_caller,
    send_group_msg,
    get_group_ids,
    get_stats,
    get_weather,
    get_blacklist=None,
    enrich=None,
) -> DailyReportScheduler:
    report_cfg = config.get("daily_report", {})
    return DailyReportScheduler(
        config=report_cfg,
        llm_caller=llm_caller,
        send_group_msg=send_group_msg,
        get_group_ids=get_group_ids,
        get_stats=get_stats,
        get_weather=get_weather,
        get_blacklist=get_blacklist,
        enrich=enrich,
    )
