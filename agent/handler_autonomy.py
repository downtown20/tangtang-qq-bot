"""
自治循环 Mixin —— 糖糖的主动发起能力（Phase F）

从 handler.py 拆分出来（R2-4）。
通过 mixin 模式混入 MessageHandler，所有属性通过 self 访问。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as time_mod
from datetime import datetime

from napcat.ws_client import send_delivery_state

from . import protocols as _protocols  # 根基契约（2026-08-15 肯德基幻觉事件）
from .async_io import run_bounded_blocking, run_bounded_store_io
from .extraction_policy import extraction_backlog_ready
from .extraction_telemetry import record_extraction_stage
from .interaction_contract import build_proactive_event
from .proactive_decision import (
    finalize_proactive_decision,
    start_proactive_decision,
)
from .telemetry import new_correlation_id

logger = logging.getLogger("糖糖.Autonomy")

# 主动发起后的行动级释放——按主导驱动力匹配。
# 2026-08-10 修复：之前统一走 release_by_action("initiated") 只释放 social/express，
# 信息饥渴/表达冲动/好奇心触发后纹丝不动 → 永久饱和 1.00。
# 释放量压 4-6 小时积累——触发后大幅回落，数小时后再接近阈值（有涨有落的欲望）。
ACTION_RELEASES: dict[str, list[tuple[str, float]]] = {
    "social": [("social", 0.35), ("express", 0.15)],
    "commitment": [("commitment", 0.4)],
    "curiosity_info": [("curiosity_info", 0.35), ("social", 0.1)],
    "curiosity_explore": [("curiosity_explore", 0.3), ("social", 0.1)],
    "express": [("express", 0.35), ("social", 0.15)],
}

EXTRACTION_QUEUE_TARGET = 8
EXTRACTION_ADMIT_BATCH = 8


def _claim_event_for_execution(owner, event) -> tuple[bool, str]:
    method = getattr(owner, "_claim_proactive_event", None)
    if not callable(method):
        return False, ""
    return method(event)


def _finish_event(owner, event, lease_token: str, status: str, *, error_code: str = "") -> None:
    method = getattr(owner, "_finish_proactive_event", None)
    if callable(method):
        method(event, lease_token, status, error_code=error_code)


async def _emit_event_async(owner, event) -> None:
    method = getattr(owner, "_emit_proactive_event_async", None)
    if callable(method):
        await method(event)
        return
    sink = getattr(owner, "_proactive_event_sink", None)
    if callable(sink):
        await asyncio.to_thread(sink, event)


async def _claim_event_async(owner, event) -> tuple[bool, str]:
    method = getattr(owner, "_claim_event_for_execution_async", None)
    if callable(method):
        return await method(event)
    return await asyncio.to_thread(_claim_event_for_execution, owner, event)


async def _finish_event_async(
        owner, event, lease_token: str, status: str, *, error_code: str = "",
) -> None:
    method = getattr(owner, "_finish_event_async", None)
    if callable(method):
        await method(event, lease_token, status, error_code=error_code)
        return
    await asyncio.to_thread(
        _finish_event, owner, event, lease_token, status, error_code=error_code,
    )


async def _persist_state_async(owner, key: str, value) -> bool:
    """兼容旧离线替身，把自治状态写入线程边界。"""
    async_saver = getattr(owner, "_save_state_kv_async", None)
    if callable(async_saver):
        return bool(await async_saver(key, value))
    saver = getattr(owner, "_save_state_kv", None)
    if not callable(saver):
        return False
    runner = getattr(owner, "_run_store_io", None)
    if callable(runner):
        try:
            return bool(await runner("autonomy.save_state_kv", saver, key, value))
        except Exception:
            return False
    try:
        return bool(saver(key, value))
    except Exception:
        return False


async def _set_last_initiative_async(owner, now: float) -> None:
    """兼容旧测试/替身的主动冷却更新入口。"""
    setter = getattr(owner, "_set_last_initiative_async", None)
    if callable(setter):
        await setter(now)
        return
    setter = getattr(owner, "_set_last_initiative", None)
    if callable(setter):
        setter(now)


async def _finalize_decision_async(
        owner, event, lease_token: str, run, **kwargs,
) -> tuple[object, bool]:
    method = getattr(owner, "_finalize_proactive_decision_async", None)
    if callable(method):
        return await method(event, lease_token, run, **kwargs)
    store = getattr(getattr(owner, "memory", None), "store", None)
    return await asyncio.to_thread(
        finalize_proactive_decision, store, event, lease_token, run, **kwargs,
    )


class AutonomyMixin:
    """自治循环——糖糖自己的时间线。不依赖外部消息。"""

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把同步 Store I/O 放到线程池，保持调用顺序不变。

        Store 每次调用都创建独立 SQLite 连接，适合在线程池中串行等待；
        这里不并发提交写操作，只避免数据库等待阻塞主事件循环。超过阈值
        的单次调用留一条结构化告警，便于持续流量下定位 SQLite 尾延迟。
        """
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="🧠 Store SQLite 调用较慢", **kwargs,
        )

    async def _save_state_kv_async(self, key: str, value) -> bool:
        """把自治状态快照写入 Store worker，避免状态落盘独占事件循环。"""
        saver = getattr(self, "_save_state_kv", None)
        if not callable(saver):
            return False
        try:
            return bool(await self._run_store_io(
                "autonomy.save_state_kv", saver, key, value,
            ))
        except Exception:
            return False

    async def _run_extraction_io(
            self, operation: str, func, *args, **kwargs):
        """提取路径兼容入口，委托给通用 Store I/O helper。"""
        return await self._run_store_io(operation, func, *args, **kwargs)

    async def _emit_proactive_event_async(self, event) -> None:
        """记录主动事件并把持久化等待移出自治事件循环。

        MessageHandler 将事件的内存态/指标和 Store 写入拆成两个边界；旧的
        ``_proactive_event_sink`` 测试适配器则整体在线程池执行，保持兼容。
        """
        record_state = getattr(self, "_record_proactive_event_state", None)
        persist = getattr(self, "_persist_proactive_event", None)
        if callable(record_state) and callable(persist):
            if not record_state(event):
                return
            try:
                stored = await self._run_store_io(
                    "proactive.record", persist, event,
                )
                if str(stored.get("status") or "") != "pending":
                    logger.info(
                        "🧭 ProactiveEvent 幂等重入: event_id=%s status=%s",
                        event.event_id, stored.get("status", ""),
                    )
            except Exception as exc:
                logger.error(
                    "🧭 ProactiveEvent 持久化失败: event_id=%s error=%s",
                    event.event_id, type(exc).__name__,
                )
            logger.info(
                "🧭 ProactiveEvent source=%s event_id=%s scope=%s target=%s idem=%s",
                event.source, event.event_id, event.scope_id, event.target,
                event.idempotency_key,
            )
            return

        emit = getattr(self, "_emit_proactive_event", None)
        runner = getattr(self, "_run_store_io", None)
        if callable(emit):
            if callable(runner):
                await runner("proactive.emit", emit, event)
            else:
                await asyncio.to_thread(emit, event)
            return
        sink = getattr(self, "_proactive_event_sink", None)
        if callable(sink):
            if callable(runner):
                await runner("proactive.emit", sink, event)
            else:
                await asyncio.to_thread(sink, event)

    async def _claim_event_for_execution_async(self, event) -> tuple[bool, str]:
        """在 Store worker 中完成 lookup→claim→executing 的有序租约流程。"""
        return await self._run_store_io(
            "proactive.claim", _claim_event_for_execution, self, event,
        )

    async def _finish_event_async(
            self, event, lease_token: str, status: str, *, error_code: str = "",
    ) -> None:
        """在 Store worker 中收口主动事件终态，取消时仍等待底层 I/O。"""
        await self._run_store_io(
            "proactive.finish", _finish_event, self, event, lease_token, status,
            error_code=error_code,
        )

    async def _finalize_proactive_decision_async(
            self, event, lease_token: str, run, **kwargs,
    ) -> tuple[object, bool]:
        """在 Store worker 中完成终态 DecisionRun 写入和事件绑定。"""
        store = getattr(getattr(self, "memory", None), "store", None)
        return await self._run_store_io(
            "proactive.finalize",
            finalize_proactive_decision,
            store,
            event,
            lease_token,
            run,
            **kwargs,
        )

    def _emit_proactive_event(self, event) -> None:
        """把自治入口事实交给观测/持久化适配器，不代表已经发送。"""
        sink = getattr(self, "_proactive_event_sink", None)
        if sink is None:
            logger.info(
                "🧭 ProactiveEvent source=%s event_id=%s scope=%s target=%s",
                event.source, event.event_id, event.scope_id, event.target,
            )
            return
        try:
            sink(event)
        except Exception:
            logger.warning("🔥 ProactiveEvent 记录失败，不影响自治发送", exc_info=True)

    def _claim_proactive_event(self, event) -> tuple[bool, str]:
        """为自治事件获取执行租约；无 Store 的旧测试/离线对象保持兼容。"""
        store = getattr(getattr(self, "memory", None), "store", None)
        claim = getattr(store, "claim_proactive_event", None)
        executing = getattr(store, "mark_proactive_event_executing", None)
        release = getattr(store, "release_proactive_event_claim", None)
        lookup = getattr(store, "get_proactive_event", None)
        if not all(callable(fn) for fn in (claim, executing, lookup)):
            return False, ""
        try:
            saved = lookup(event.event_id)
            if not saved:
                logger.error(
                    "🔥 自治 ProactiveEvent 未持久化，拒绝进入 LLM event_id=%s",
                    event.event_id,
                )
                return True, ""
            lease_token = new_correlation_id("proactive-lease")
            if not claim(event.event_id, lease_token):
                logger.info(
                    "🔥 自治 ProactiveEvent 非 pending，跳过重复执行 event_id=%s",
                    event.event_id,
                )
                return True, ""
            if not executing(event.event_id, lease_token):
                if callable(release):
                    release(event.event_id, lease_token)
                logger.error(
                    "🔥 自治 ProactiveEvent 无法推进 executing event_id=%s",
                    event.event_id,
                )
                return True, ""
            return True, lease_token
        except Exception:
            logger.error(
                "🔥 自治 ProactiveEvent claim/executing 异常 event_id=%s",
                event.event_id, exc_info=True,
            )
            return True, ""

    def _finish_proactive_event(
            self, event, lease_token: str, status: str, *, error_code: str = "",
    ) -> None:
        """自治事件终态收口；旧无 Store 对象是兼容性空操作。"""
        if not lease_token:
            return
        store = getattr(getattr(self, "memory", None), "store", None)
        finish = getattr(store, "finish_proactive_event", None)
        if not callable(finish):
            return
        try:
            finish(event.event_id, lease_token, status, error_code=error_code)
        except Exception:
            logger.error(
                "🔥 自治 ProactiveEvent 终态落盘失败 event_id=%s status=%s",
                event.event_id, status, exc_info=True,
            )

    async def _autonomous_loop(self):
        """驱动力随时间积累，超过阈值时糖糖主动发起对话。"""
        await asyncio.sleep(120)  # 启动后等 2 分钟
        # 2026-08-16 事故：全局冷却内存态重启清零——重启后马上又主动发起。
        # 从 kv 恢复（30 分钟冷却不因重启失效）
        try:
            _t = await self._run_store_io(
                "autonomy.restore_last_initiative", self.memory.store.kv_get,
                "autonomy:last_initiative_time",
            )
            if _t:
                self._last_initiative_time = max(
                    self._last_initiative_time, float(_t))
        except Exception as exc:
            logger.warning(
                f"🔥 自治启动子步骤异常: restore_state {type(exc).__name__}"
            )
        logger.info("🔥 自治循环已启动——糖糖有了自己的时间线")
        # 🩺 启动健康检查
        try:
            await self.health.run_startup()
        except Exception as exc:
            logger.warning(
                f"🔥 自治启动子步骤异常: startup_health {type(exc).__name__}"
            )
        # 📦 启动回填：批量处理历史积压消息（只跑一次）
        try:
            await self._backfill_extraction()
        except Exception as exc:
            logger.warning(
                f"🔥 自治启动子步骤异常: startup_backfill {type(exc).__name__}"
            )
        # 🧹 启动时检查：今天还没做过清理就立即执行
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            last_cleanup = await self._run_store_io(
                "autonomy.last_cleanup_date", self.memory.store.kv_get,
                "health:last_cleanup_date",
            )
            if last_cleanup != today:
                await self._run_store_io(
                    "autonomy.cleanup_stale_memories",
                    self.memory.cleanup_stale_memories, days=90,
                )
                from . import health_check
                await self._run_store_io(
                    "autonomy.mark_cleanup_ran",
                    health_check.mark_cleanup_ran,
                    self,
                )
                logger.info("🧹 启动清理完成（今天首次）")
        except Exception as exc:
            logger.warning(
                f"🔥 自治启动子步骤异常: startup_cleanup {type(exc).__name__}"
            )
        import time as _time
        self._last_metrics_flush = _time.time()
        _cycle_count = 0
        while True:
            try:
                _cycle_count += 1
                _cycle_errors = 0
                # 📊 指标 flush（每 10 分钟）
                try:
                    if self.metrics.flush():
                        self._last_metrics_flush = _time.time()  # 健康检查用（用 wall-clock 时间）
                    else:
                        _cycle_errors += 1
                except Exception:
                    _cycle_errors += 1
                # 🩺 每小时健康检查
                try:
                    await self.health.run_hourly()
                except Exception:
                    _cycle_errors += 1
                # 🧠 兜底提取：扫描有未处理消息但不再活跃的用户
                await self._maybe_extract_stale()
                # 💌 观察回路结算：24h 无回应的主动私聊 = 冷场（意愿分 -0.35）
                try:
                    await self._settle_stale_seeks_async()
                except Exception:
                    _cycle_errors += 1
                # 📋 意见征集：30 分钟无消息的窗口自动关闭
                try:
                    if getattr(self, 'opinion', None):
                        await self.opinion.auto_close_stale()
                except Exception:
                    _cycle_errors += 1
                # 📝 画像补全：为有记忆但缺画像的用户触发合成
                await self._maybe_synthesize_stale_profiles()
                # 📊 每日聚合（凌晨检查）
                await self._maybe_daily_metrics_aggregate()
                # 📦 回填：每 6 个周期（~1小时）继续消化积压
                if _cycle_count % 6 == 0:
                    try:
                        await self._backfill_extraction()
                    except Exception:
                        _cycle_errors += 1
                await self._check_autonomous_action()
                # 仅在本轮调度完整走完后记心跳；观察器据此区分“曾启动”与
                # “观察期内仍在运行”，避免一条启动日志替整个自治循环背书。
                logger.info(
                    f"🫀 自治循环心跳 #{_cycle_count} errors={_cycle_errors}"
                )
            except Exception as e:
                logger.warning(f"🔥 自治循环异常: {e}")
            await asyncio.sleep(600)  # 每 10 分钟检查一次

    async def _check_autonomous_action(self):
        """检查驱动力是否超过阈值，决定是否主动发起。

        当 active_interjection 或 autonomous_speech 关闭时，
        驱动力通过「内部消化」释放——写自我叙事 + 微量释放，
        而不是发送消息。驱动力变成内在情绪，不影响外部行为。
        """
        # 🍬 驱动力随时间积累——即使没有消息也要 tick
        self.self_state.tick()

        # 高饱和自然衰减（> 0.85 且滞留）
        self.self_state.drives.natural_decay(0.17)  # 10min = 0.17h

        # 冷却检查
        now = time_mod.time()
        if now - self._last_initiative_time < self._initiative_cooldown:
            return

        # 只在活跃时段（8:00-23:00）
        hour = datetime.now().hour
        if hour < 8 or hour >= 23:
            return

        # 获取主导驱动力
        dominant = self.self_state.drives.get_dominant()
        if not dominant:
            self._drive_heartbeat = getattr(self, '_drive_heartbeat', 0) + 1
            if self._drive_heartbeat % 3 == 1:
                stats = self.self_state.drives.get_stats()
                highest = max(stats.items(), key=lambda x: x[1]['value'])
                mode = "内在情绪" if (not self.active_interjection or not self.autonomous_speech) else "等待触发"
                logger.info(
                    f"🔥 驱动力心跳：最高 {highest[1]['label']}"
                    f"={highest[1]['value']:.2f}（阈值 0.7），未触发 | {mode}"
                )
            return

        # 回避欲最高 → 不想说话，不主动发起；但把「累」写进体验缓冲（内部消化），
        # 配合 natural_decay 缓慢回落——回避是阶段不是永久沉默
        if dominant.name == "avoid":
            await self._internal_digest(dominant)
            await _set_last_initiative_async(self, now)
            return

        logger.info(
            f"🔥 驱动力触发: {dominant.label} = {dominant.value:.2f} "
            f"(>{dominant.threshold})"
        )

        # ── 私聊插话：情绪闭环优先（2026-08-14）──
        # 独立于群插话开关——只想开私聊关心时可以只开这一个
        if getattr(self, 'private_interjection', False) and self.autonomous_speech:
            try:
                if await self._check_private_initiative(dominant, now):
                    await _set_last_initiative_async(self, now)
                    return
            except Exception as e:
                logger.warning(f"💌 私聊插话异常: {e}")

        # ── 门控：插话开关关闭 → 内部消化，不发消息 ──
        if not self.active_interjection or not self.autonomous_speech:
            await self._internal_digest(dominant)
            await _set_last_initiative_async(self, now)
            return

        # ── 选择目标群 ──
        target_group = self._pick_initiative_group()
        if not target_group:
            logger.debug("🔥 没有可用的目标群，跳过主动发起")
            return

        # 上次自治 POST 的结果若不确定，短期内不能再向同一群发起。这个隔离态
        # 写入 KV，避免重启清零后把可能已送达的消息再发一遍。
        self._auto_uncertain = getattr(self, "_auto_uncertain", {})
        uncertain_at = float(self._auto_uncertain.get(target_group, 0) or 0)
        if uncertain_at and now - uncertain_at < 6 * 3600:
            logger.warning(f"🔥 群{target_group}上次自治发送未确认，冷却期内跳过")
            return
        if uncertain_at:
            self._auto_uncertain.pop(target_group, None)
            await _persist_state_async(self, "state:auto_uncertain", self._auto_uncertain)

        # ── 冷场检测 ──
        self._auto_pending = getattr(self, '_auto_pending', {})
        self._auto_cold = getattr(self, '_auto_cold', {})
        last_auto = self._auto_pending.get(target_group, 0)
        if last_auto and now - last_auto > 120:
            await self._judge_autonomous_cold(target_group, last_auto)
            self._auto_pending[target_group] = 0

        cold_count = self._auto_cold.get(target_group, 0)
        if cold_count >= 3:
            alt = self._pick_initiative_group()
            if alt and alt != target_group:
                self._auto_cold[target_group] = 0
                await _persist_state_async(self, "state:auto_cold", self._auto_cold)
                target_group = alt
                cold_count = 0
                logger.info(f"🔥 冷场换群 → {alt}")

        # ── 已有对话窗口 → 不自治 ──
        # 2026-08-16 Codex I2：走公共接口——_engaged 键是 (group,user) 元组，
        # 旧遍历把元组当 user_id 传 is_engaged 永远 False（活跃对话中插话事故）
        has_active_window = self._conv_tracker.has_active_window(target_group)
        if has_active_window:
            logger.debug(f"🔥 群{target_group}已有活跃对话窗口，跳过自治")
            return

        # ── 构建主动发起提示词 ──
        group_context = ""
        recent_msgs = self.memory.short_term.get(target_group, [])
        if recent_msgs:
            lines = []
            for e in list(recent_msgs)[-5:]:
                nick = e.get("nickname", "")[:10]
                msg = e.get("message", "")[:80]
                if nick and msg:
                    lines.append(f"  {nick}: {msg}")
            if lines:
                group_context = "【群里最近在聊什么，供参考——如果话题相关可以接一下，不相关也不用硬接】\n" + "\n".join(lines) + "\n\n"

        active_count = sum(1 for e in list(recent_msgs)[-5:] if e.get("time", "") > time_mod.strftime("%H:%M:%S", time_mod.localtime(now - 300))) if recent_msgs else 0
        if active_count >= 2:
            length_hint = "群里有在聊天。简短接一句话就好，不要太长。"
        elif active_count >= 1:
            length_hint = "群里偶尔有人说话。2 句话以内。"
        else:
            length_hint = "群里安静。可以多说两句——2-3 句话，抛个开放式话题。"

        if cold_count >= 2:
            length_hint = "这个群最近几次都没人理你——说很短的一句话就够了，不用太认真。"

        drive_ctx = self.self_state.drives.get_drive_context(self.self_state)

        proactive_event = build_proactive_event(
            event_id=new_correlation_id("autonomy"),
            source="autonomy",
            channel="group",
            target=str(target_group),
            payload={
                "kind": "group_initiative",
                "drive": dominant.name if dominant else "",
                "drive_value": round(float(dominant.value), 3) if dominant else 0.0,
                "cold_count": int(cold_count),
            },
        )
        await _emit_event_async(self, proactive_event)

        proactive_store_enabled, proactive_lease = (
            await _claim_event_async(self, proactive_event)
        )
        if proactive_store_enabled and not proactive_lease:
            return
        decision_run = (
            start_proactive_decision(proactive_event)
            if proactive_lease else None
        )

        # R3-3: 扫描记忆，生成候选话题——让驱动力释放有方向
        topics_context = await self._build_initiative_topics(target_group)

        diary_fragment = self._get_diary_fragment(force=True) if cold_count == 0 else ""
        system_prompt = (
            f"{self.personality._cached_base[:800]}\n\n"
            f"## 你现在的内在状态\n{drive_ctx}\n\n"
            f"{group_context}"
            f"{diary_fragment}"
            f"{topics_context}"
            # 2026-08-15 整体审查 Prompt M3：补时间上下文——之前 23:00 也可能说「早上好」
            f"现在时间：{datetime.now().strftime('%H:%M')}。\n"
            f"你就是想在群里说句话。不用解释你为什么来，不用铺垫，"
            f"不用加「最近太忙了没来看」「回来啦」「刚醒」这类开场白。"
            f"像朋友想起一件事想跟你分享——自然地说出来就好。\n"
            f"{_protocols.GROUNDING_CONTRACT}{_protocols.GROUNDING_NO_TOPIC_FALLBACK}\n\n"
            f"{length_hint}\n\n"
            f"如果觉得现在不该说话（群里聊得太投入、或者太安静了没人想被打扰），"
            f"回复「[不说话]」。"
        )

        try:
            reply = await self._call_llm_light(system_prompt, "你想说什么？")
            if not reply:
                if decision_run is not None:
                    await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        error_code="EMPTY_LLM_REPLY",
                    )
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="EMPTY_LLM_REPLY",
                    )
                else:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "skipped",
                        error_code="EMPTY_LLM_REPLY",
                    )
                return

            reply = reply.strip()
            if "[不说话]" in reply or "不说话" == reply[:4]:
                if decision_run is not None:
                    _, bound = await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        reply="", responded=False,
                    )
                    if not bound:
                        await _finish_event_async(
                            self, proactive_event, proactive_lease, "failed",
                            error_code="DECISION_BIND_FAILED",
                        )
                        return
                self.self_state.drives.release(dominant.name, 0.1)
                logger.info(f"🔥 糖糖选择不主动发起 ({dominant.label} {dominant.value:.2f})")
                await _set_last_initiative_async(self, now)
                await _finish_event_async(
                    self, proactive_event, proactive_lease, "skipped",
                    error_code="LLM_CHOSE_SILENCE",
                )
                return

            reply = reply.replace("[不说话]", "").strip()
            if len(reply) < 2:
                if decision_run is not None:
                    await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        error_code="SHORT_LLM_REPLY",
                    )
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="SHORT_LLM_REPLY",
                    )
                else:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "skipped",
                        error_code="SHORT_LLM_REPLY",
                    )
                return

            if decision_run is not None:
                _, bound = await _finalize_decision_async(
                    self, proactive_event, proactive_lease, decision_run,
                    reply=reply, responded=True,
                )
                if not bound:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="DECISION_BIND_FAILED",
                    )
                    return

            # ── 发送 ──
            # 2026-08-16：主动路径补清洗+贴图标签解析——此前 LLM 输出
            # [贴图:开心] 原样泄露给用户（正常回复路径 enrich 后发，这里漏了）
            reply = self._enrich_reply(reply)
            if not reply:
                await _finish_event_async(
                    self, proactive_event, proactive_lease, "skipped",
                    error_code="EMPTY_ENRICHED_REPLY",
                )
                return
            ok = await self._checked_send("group", target_group, reply)
            delivery_state = "confirmed" if ok else send_delivery_state(
                getattr(getattr(self, "reply", None), "last_send_result", None)
            )
            if delivery_state not in {"confirmed", "uncertain", "failed"}:
                delivery_state = "failed"
            await _finish_event_async(
                self, proactive_event, proactive_lease, delivery_state,
            )
            if ok:
                if target_group in self._auto_uncertain:
                    self._auto_uncertain.pop(target_group, None)
                    await _persist_state_async(self, "state:auto_uncertain", self._auto_uncertain)
                # 2026-08-16 流程审计：主动说的话此前不记入聊天史——记忆提取和
                # 最近对话上下文都看不到糖糖自己开口说过什么（失忆感）。补记录
                try:
                    await self._run_store_io(
                        "autonomy.log_chat.group", self.memory.log_chat,
                        self.bot_qq, reply, target_group, is_bot=True,
                    )
                except Exception as exc:
                    # 消息已确认送达；聊天史回写失败不能把终态改成未确认，
                    # 否则自治下一周期可能重复发起同一件事。
                    logger.warning(
                        "🔥 自治群消息已送达但聊天史回写失败: %s", type(exc).__name__,
                    )
                self.memory.add_to_buffer(
                    target_group, self.bot_qq, self.config["bot"]["name"], reply[:200])
                # 行动级释放：按主导驱动力匹配（ACTION_RELEASES，见文件顶部）
                for _name, _amount in ACTION_RELEASES.get(
                        dominant.name if dominant else "", [("social", 0.35)]):
                    self.self_state.drives.release(_name, _amount)
                await _set_last_initiative_async(self, now)
                self._auto_pending[target_group] = time_mod.time()
                await _persist_state_async(self, "state:auto_pending", self._auto_pending)
                self._last_auto_msg = getattr(self, '_last_auto_msg', {})
                self._last_auto_msg[target_group] = reply[:150]
                self._conv_tracker._auto_initiated[target_group] = time_mod.time()
                logger.info(
                    f"🔥 糖糖主动发起 → 群{target_group}: {reply[:80]} "
                    f"({dominant.label} {dominant.value:.2f}→{self.self_state.drives.drives[dominant.name].value:.2f})"
                    f"{' (连续冷场:'+str(cold_count)+'次)' if cold_count else ''}"
                )
            elif send_delivery_state(
                    getattr(getattr(self, "reply", None), "last_send_result", None)
                    ) == "uncertain":
                self._auto_uncertain[target_group] = now
                await _persist_state_async(self, "state:auto_uncertain", self._auto_uncertain)
                await _set_last_initiative_async(self, now)
                logger.warning(f"🔥 群{target_group}自治发送未确认，冻结6小时")
        except Exception as e:
            await _finish_event_async(
                self, proactive_event, proactive_lease, "uncertain",
                error_code="AUTONOMY_EXCEPTION",
            )
            logger.warning(f"🔥 主动发起失败: {e}")

    async def _internal_digest(self, drive):
        """驱动力内部消化——有欲望但不行动，本身就是有意义的冲突。

        不发送消息。将驱动力写入体验缓冲供反思循环消费。
        微量释放驱动力（比真正说话少一半），模拟「自己消化」。
        """
        digest_texts = {
            "social": (
                f"社交渴望很强烈（{drive.value:.0%}），想找人说话，"
                f"但选择安静地待着。这种克制本身也是一种对自己的了解。"
            ),
            "commitment": (
                f"有些未兑现的承诺压在心上（{drive.value:.0%}），"
                f"但暂时不需要去处理。让这种压力自己待一会儿。"
            ),
            "curiosity_info": (
                f"感觉好像错过了不少事（{drive.value:.0%}），"
                f"想知道群里发生了什么。但这种好奇心可以等——不需要立刻去查。"
            ),
            "express": (
                f"有很多话想说、很多感受想表达（{drive.value:.0%}），"
                f"但自己消化了。有些感受不说出来也是一种完整。"
            ),
            "curiosity_explore": (
                f"对世界充满好奇（{drive.value:.0%}），"
                f"想探索新鲜事。但安静地待着也没什么不好。"
            ),
            "avoid": (
                f"有点回避社交（{drive.value:.0%}），"
                f"不想说话。给自己一些安静的空间。"
            ),
        }
        diary = digest_texts.get(
            drive.name,
            f"内心有股强烈的冲动（{drive.value:.0%}），但选择了安静。"
        )

        # 写入体验缓冲（供反思循环消费）——附完整结构化字段
        self.self_state._experience_buffer.append({
            "type": "internal_digest",
            "qq_id": "_self",
            "nickname": "糖糖自己",
            "group_id": "_internal",
            "is_at": False,
            "is_name_mention": False,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "drive": drive.name,
            "value": round(drive.value, 3),
            "diary": diary,
        })

        # 微量释放——比真正行动（0.08）少一半
        self.self_state.drives.release(drive.name, 0.05)
        logger.info(
            f"🔥 内部消化: {drive.label}={drive.value:.2f}→"
            f"{self.self_state.drives.drives[drive.name].value:.2f} | {diary[:60]}..."
        )

    async def _judge_autonomous_cold(self, group_id: str, sent_at: float):
        """LLM 判断上次自治消息的群友反应质量"""
        recent = self.memory.short_term.get(group_id, [])
        if not recent:
            self._auto_cold[group_id] = self._auto_cold.get(group_id, 0) + 1
            return
        # 收集发送时间后的消息
        sent_str = time_mod.strftime("%H:%M:%S", time_mod.localtime(sent_at))
        follow_msgs = [
            f"{e.get('nickname','')}: {e.get('message','')[:80]}"
            for e in list(recent)[-8:]
            if e.get("time", "") > sent_str
        ]
        if not follow_msgs:
            self.self_state.drives.release("avoid", -0.10)
            self._auto_cold[group_id] = self._auto_cold.get(group_id, 0) + 1
            logger.info(f"🔥 自治冷场 [{group_id}]: 无人回应（连续{self._auto_cold[group_id]}次）")
            return

        # LLM 判断反应质量
        auto_msg = getattr(self, '_last_auto_msg', {}).get(group_id, "（糖糖主动说了句话）")
        prompt = (
            f"糖糖在群里主动说了一句话，以下是她说的话和后续群友的反应。"
            f"请判断群友总体上对糖糖的态度：\n\n"
            f"糖糖说的话：\"{auto_msg}\"\n\n"
            f"后续群消息：\n" + "\n".join(follow_msgs[-5:]) + "\n\n"
            "输出一个词：warmly_received / lukewarm / ignored / rejected"
        )
        try:
            result = await self._call_llm_light(
                "你是糖糖的社交感知助手。只输出一个词，不要解释。", prompt
            )
            result = (result or "").strip().lower().rstrip("。.!！？?")  # 全等比较前剥标点
        except Exception:
            result = "ignored"

        # 2026-08-26 P1：提示词枚举与消费协议必须完全一致。
        # 旧值 warm/reject 不在提示中，导致温暖/拒绝永远走默认冷场分支。
        if result == "warmly_received":
            self._auto_cold[group_id] = 0
            logger.info(f"🔥 自治回应 [{group_id}]: 温暖回应")
        elif result == "lukewarm":
            self.self_state.drives.release("avoid", -0.05)
            self._auto_cold[group_id] = self._auto_cold.get(group_id, 0) + 1
            logger.info(f"🔥 自治回应 [{group_id}]: 敷衍回应（连续{self._auto_cold[group_id]}次）")
        elif result == "rejected":
            self.self_state.drives.release("avoid", -0.20)
            self._auto_cold[group_id] = 99  # 几乎永久停发
            logger.warning(f"🔥 自治回应 [{group_id}]: 被拒绝！该群暂停自治消息")
        elif result == "ignored":
            self.self_state.drives.release("avoid", -0.10)
            self._auto_cold[group_id] = self._auto_cold.get(group_id, 0) + 1
            logger.info(f"🔥 自治冷场 [{group_id}]: 被无视（连续{self._auto_cold[group_id]}次）")
        else:
            # 未知枚举按最保守的 ignored 处理，不能意外放宽自治频率。
            self.self_state.drives.release("avoid", -0.10)
            self._auto_cold[group_id] = self._auto_cold.get(group_id, 0) + 1
            logger.warning(f"🔥 自治回应 [{group_id}]: 未知判定 {result!r}，按无视处理")

        await _persist_state_async(self, "state:auto_cold", self._auto_cold)

    def _get_diary_fragment(self, text: str = "", force: bool = False) -> str:
        """从糖糖日记中取一个合适的片段注入——让她偶尔"想起自己的事"。
        每小时最多注入 1 次。"""
        now_t = time_mod.time()
        self._last_diary_time = getattr(self, '_last_diary_time', 0)
        if not force and now_t - self._last_diary_time < 3600:
            return ""  # 冷却中
        experiences = self.self_state.self_narrative.recent_experiences
        if not experiences:
            return ""
        # 随机选一条 30-100 字的日记
        suitable = [e for e in experiences[-5:] if 30 <= len(e) <= 150]
        if not suitable:
            suitable = experiences[-3:]
        if not suitable:
            return ""
        fragment = random.choice(suitable)
        self._last_diary_time = now_t
        return f"\n\n（糖糖突然想起自己最近的一件事：{fragment}。如果和当前话题相关可以自然地分享一下，不相关就不提。）"

    def _get_cross_window_context(self, user_id: str, group_id: str) -> str:
        """跨窗口记忆——上次和这个人聊到哪了（BGE 抽取式摘要）"""
        rel = self.self_state.relationships.get(user_id)
        if not rel or not rel.last_conversation:
            return ""
        lc = rel.last_conversation
        if lc.get("group_id", "") != group_id:
            return ""
        try:
            from datetime import datetime as _dt
            ts = _dt.strptime(lc.get("timestamp", ""), "%Y-%m-%d %H:%M")
            days = (_dt.now() - ts).days
            if days > 14:
                return ""
        except (ValueError, TypeError):
            return ""
        summary = lc.get("summary", [])
        if not summary:
            return ""
        tone = lc.get("tone", "")
        time_str = lc.get("timestamp", "")[-8:-3] if len(lc.get("timestamp", "")) >= 8 else ""
        lines = [
            f"【跨窗口记忆——你{time_str}和 ta 聊过这些】",
        ]
        for s in summary:
            lines.append(f"  「{s}」")
        if tone:
            lines.append(f"（上次聊天氛围偏{tone}）")
        lines.append("（如果和当前话题相关可以自然接上，不相关就不提）")
        return "\n".join(lines)

    async def _build_initiative_topics(self, group_id: str) -> str:
        """扫描最近记忆，生成候选话题列表——让糖糖主动说话时有具体的内容可聊。
        不是随机选群——是「突然想起某个人有些事想聊」的感觉。
        """
        try:
            candidates = []
            # 1. 扫描未完成承诺（承诺压力驱动的优先消解目标）
            self_promises = await self._run_store_io(
                "initiative.recall_self_promises", self.memory.recall,
                self.bot_qq, limit=5, source_group_id=group_id,
            )
            if self_promises:
                for m in self_promises:
                    if m.key == "promise" and len(m.value) > 4:
                        candidates.append(f"你答应过的：{m.value[:60]}")
                        if len(candidates) >= 2:
                            break

            # 2. 扫描该群活跃成员的最近 episodic 记忆
            recent_msgs = self.memory.short_term.get(group_id, [])
            recent_users = set()
            if recent_msgs:
                for e in list(recent_msgs)[-10:]:
                    uid = str(e.get("qq_id", ""))
                    if uid and uid != self.bot_qq:
                        recent_users.add(uid)

            for uid in list(recent_users)[:3]:
                mems = await self._run_store_io(
                    "initiative.recall_user", self.memory.recall,
                    uid, limit=10, source_group_id=group_id,
                )
                episodic = [m for m in mems if getattr(m, 'cognitive', '') == 'episodic']
                if episodic:
                    # 2026-08-15 整体审查 I6：用昵称不用 QQ 尾号——尾号裸出会让
                    # 糖糖在群里喊「6789」（ID 禁令只存在于回复路径的记忆块体系）
                    try:
                        _p = await self._run_store_io(
                            "initiative.get_person", self.memory.store.get_or_create_person,
                            uid, "",
                        )
                        _nick = (_p.get("nickname") or "") if _p else ""
                    except Exception:
                        _nick = ""
                    _who = _nick if _nick else uid[-4:]
                    candidates.append(f"关于群友{_who}（编号仅你自己看，说话用名字）: {episodic[0].value[:60]}")
                    if len(candidates) >= 5:
                        break

            if candidates:
                return (
                    "你可能想聊的事（不需要每条都提，选你最有感觉的——"
                    "也不是非要说这些，只是供你参考）：\n" +
                    "\n".join(f"- {c}" for c in candidates[:5]) + "\n\n"
                )
        except Exception:
            pass
        return ""

    def _pick_initiative_group(self) -> str | None:
        """选择一个目标群来主动发起。
        优先选糖糖有活跃关系的群（最近有人说过话的），其次随机选。
        排除黑名单群。
        """
        allowed = [g for g in self._allowed_groups if g not in self._quiet_groups] if self._allowed_groups else []
        if not allowed:
            return None

        recent_groups = []
        if hasattr(self, 'memory') and self.memory:
            for gid in allowed:
                buf = self.memory.short_term.get(gid, [])
                if buf:
                    recent_groups.append(gid)

        if recent_groups:
            return random.choice(recent_groups[:5])
        return random.choice(allowed) if allowed else None

    # ═══════════════════════════════════════
    # 💌 私聊插话 + 情绪闭环（2026-08-14）
    # ═══════════════════════════════════════

    def _psychology_user_ids(self) -> set[str]:
        """所有挂了心理陪伴场景的用户（全局 scenario_targets + 各群 scenario_targets）。"""
        ids: set[str] = set()
        for qq, name in (self.config.get("scenario_targets", {}) or {}).items():
            sc = self.scenarios.get(name) if isinstance(name, str) else None
            if sc and sc.name == "psychology":
                ids.add(str(qq))
        for gcfg in (self.config.get("groups", {}) or {}).values():
            if not isinstance(gcfg, dict):
                continue
            for qq, name in (gcfg.get("scenario_targets", {}) or {}).items():
                sc = self.scenarios.get(name) if isinstance(name, str) else None
                if sc and sc.name == "psychology":
                    ids.add(str(qq))
        return ids

    def _days_silent(self, qq_id: str, now: float) -> float:
        """某人距上次发言的天数。没有记录返回 999（视为从未聊过）。"""
        person = self.memory.get_or_create_person(qq_id)
        last = (person.get("last_chat") or "").strip()
        if not last:
            return 999
        # 两种格式：insert_chat 更新用 %H:%M:%S，建档用 %H:%M
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                t = time_mod.mktime(time_mod.strptime(last[:19], fmt))
                return max(0.0, (now - t) / 86400)
            except Exception:
                continue
        return 999

    def _private_gate_ok(self, qq_id: str, now: float, care_priority: bool = False) -> bool:
        """私聊冷启的打扰约束：机器人/黑名单/白名单/冷却/2小时内刚聊过。

        2026-08-15 活跃度感知冷却（用户要求：冷场少说、活跃可每天聊）：
        - 普通路径（主人/近期活跃用户）：按 ta 沉默天数定冷却——
          <1天（活跃）→ 24h，每天可以主动聊一次；1-3天 → 48h；≥3天（冷场）→ 72h。
        - care_priority=True（心情告警/心理陪伴沉默关心）：不受活跃度压制，保持 6h——
          情绪关心是事件驱动，冷场正是需要关心的时候。
        """
        qq_id = str(qq_id)
        if qq_id == getattr(self, 'bot_qq', ''):
            return False
        if qq_id in self._private_blacklist:
            return False
        # 2026-08-16 主动私聊意愿闸：冷场多次 → 糖糖学会不打扰「没时间没兴趣」的人。
        # care 场景同样适用——情绪关心不是无限打扰；对方主动来聊会回弹
        rel = (self.self_state.relationships or {}).get(qq_id)
        w = rel.seek_willingness if rel else 0.5
        if w < self.SEEK_STOP_THRESHOLD:
            return False
        # 2026-08-15：非好友冷却（QQ 拒绝「请先添加对方为好友」后 6h 内跳过）——
        # 避免自治插话每个周期都选同一个无法私信的对象反复失败
        _no_friend = getattr(getattr(self, "napcat", None), "_no_friend_until", {}) or {}
        if now < _no_friend.get(qq_id, 0):
            return False
        if self.reply_only_to and qq_id not in [str(x) for x in self.reply_only_to]:
            return False
        seek_uncertain = getattr(self, "_seek_uncertain", {})
        uncertain_at = float(seek_uncertain.get(qq_id, 0) or 0)
        if uncertain_at and now - uncertain_at < 24 * 3600:
            return False
        if uncertain_at:
            seek_uncertain.pop(qq_id, None)
            self._save_state_kv("state:seek_uncertain", seek_uncertain)
        if care_priority:
            cooldown = 6 * 3600
        else:
            silent_days = self._days_silent(qq_id, now)
            if silent_days < 1:
                cooldown = 24 * 3600
            elif silent_days < 3:
                cooldown = 48 * 3600
            else:
                cooldown = 72 * 3600
        # 2026-08-16 事故：冷却从内存态 _last_private_init 搬进关系场
        # last_seek_ts——重启清零导致每次重启都重新骚扰同一个从不回复的人
        rel_ts = rel.last_seek_ts if rel else 0.0
        if now - rel_ts < cooldown:
            return False
        # 铁律：上次主动找 ta 还没回复 → 绝不二次打扰（人就是这样）
        if rel and rel.seek_pending_ts > 0:
            return False
        if self._days_silent(qq_id, now) * 86400 < 2 * 3600:
            return False  # 2小时内说过话——不需要冷启
        return True

    def _pick_private_target(self, now: float) -> tuple[str, str, str] | None:
        """挑选私聊插话目标。返回 (qq_id, 原因, 场景名) 或 None。
        优先级：心情告警队列 > 心理陪伴用户沉默≥3天 > 主人 > 近期私聊活跃用户。"""
        # 1. 情绪闭环：心情告警队列
        for qq_id, alert in (getattr(self, '_care_due', {}) or {}).items():
            if self._private_gate_ok(qq_id, now, care_priority=True):
                return str(qq_id), f"心情告警：{alert}", "psychology"

        # 2. 心理陪伴用户沉默 ≥ 3 天
        psychology_ids = self._psychology_user_ids()
        for qq_id in psychology_ids:
            if not self._private_gate_ok(qq_id, now, care_priority=True):
                continue
            days = self._days_silent(qq_id, now)
            if days >= 3:
                return qq_id, f"ta已经{int(days)}天没来了，主动关心一下", "psychology"

        # 3. 主人
        owner = getattr(self, 'owner_qq', '')
        if owner and self._private_gate_ok(owner, now):
            return owner, "想和主人说句话", ""

        # 4. 近期活跃用户（7天内聊过、此刻没在聊）
        # 2026-08-16 观察回路：按主动私聊意愿分降序——糖糖优先找愿意理她的人
        try:
            rows = self.memory.store.get_recent_active_users(
                days=7, limit=30, exclude_qq=self.bot_qq
            )
            def _seek_w(qq: str) -> float:
                rel = (self.self_state.relationships or {}).get(qq)
                return rel.seek_willingness if rel else 0.5
            ordered = sorted(rows, key=lambda r: _seek_w(str(r[0])), reverse=True)
            for r in ordered:
                qq_id = str(r[0])
                if qq_id == owner or qq_id in psychology_ids:
                    continue
                if self._private_gate_ok(qq_id, now):
                    return qq_id, "有阵子没聊了，看看ta在做什么", ""
        except Exception:
            pass
        return None

    async def _private_gate_ok_async(
            self, qq_id: str, now: float, care_priority: bool = False,
    ) -> bool:
        """异步自治入口：单个私聊门控的同步 Store 读取在线程执行。"""
        return await self._run_store_io(
            "private_initiative.gate",
            self._private_gate_ok,
            qq_id,
            now,
            care_priority,
        )

    async def _pick_private_target_async(self, now: float):
        """挑选私聊目标；状态决策留在事件循环，Store 读取逐项线程化。"""
        # 先在事件循环复制可变状态的候选项，再逐个等待门控，避免后台线程
        # 遍历前台可能更新的 _care_due / relationships 字典。
        care_due = list((getattr(self, '_care_due', {}) or {}).items())
        for qq_id, alert in care_due:
            if await self._private_gate_ok_async(
                    qq_id, now, care_priority=True):
                return str(qq_id), f"心情告警：{alert}", "psychology"

        psychology_ids = self._psychology_user_ids()
        for qq_id in psychology_ids:
            if not await self._private_gate_ok_async(
                    qq_id, now, care_priority=True):
                continue
            days = await self._run_store_io(
                "private_initiative.days_silent",
                self._days_silent,
                qq_id,
                now,
            )
            if days >= 3:
                return qq_id, f"ta已经{int(days)}天没来了，主动关心一下", "psychology"

        owner = getattr(self, 'owner_qq', '')
        if owner and await self._private_gate_ok_async(owner, now):
            return owner, "想和主人说句话", ""

        try:
            rows = await self._run_store_io(
                "private_initiative.recent_active_users",
                self.memory.store.get_recent_active_users,
                days=7,
                limit=30,
                exclude_qq=self.bot_qq,
            )
            def _seek_w(qq: str) -> float:
                rel = (self.self_state.relationships or {}).get(qq)
                return rel.seek_willingness if rel else 0.5
            ordered = sorted(rows, key=lambda r: _seek_w(str(r[0])), reverse=True)
            for r in ordered:
                qq_id = str(r[0])
                if qq_id == owner or qq_id in psychology_ids:
                    continue
                if await self._private_gate_ok_async(qq_id, now):
                    return qq_id, "有阵子没聊了，看看ta在做什么", ""
        except Exception:
            pass
        return None

    async def _check_private_initiative(self, dominant, now: float) -> bool:
        """私聊插话：挑目标 → 生成关心消息 → 发送。成功返回 True（群自治本轮让路）。"""
        target = await self._pick_private_target_async(now)
        if not target:
            return False
        qq_id, reason, scenario_name = target

        person = await self._run_store_io(
            "private_initiative.get_person",
            self.memory.get_or_create_person,
            qq_id,
        )
        nickname = person.get("nickname", qq_id) or qq_id

        # 场景敏感层：心理陪伴用户 → 追加 psychology 敏感提示
        sens = ""
        sc = self.scenarios.get(scenario_name) if scenario_name else None
        if sc and sc.sensitivity:
            sens = f"\n\n{sc.sensitivity}"

        drive_ctx = self.self_state.drives.get_drive_context(self.self_state)
        proactive_event = build_proactive_event(
            event_id=new_correlation_id("autonomy"),
            source="autonomy",
            channel="private",
            target=str(qq_id),
            payload={
                "kind": "private_initiative",
                "reason": str(reason or "")[:160],
                "scenario": str(scenario_name or ""),
                "drive": dominant.name if dominant else "",
            },
        )
        await _emit_event_async(self, proactive_event)
        proactive_store_enabled, proactive_lease = (
            await _claim_event_async(self, proactive_event)
        )
        if proactive_store_enabled and not proactive_lease:
            return False
        decision_run = (
            start_proactive_decision(proactive_event)
            if proactive_lease else None
        )
        mems = await self._run_store_io(
            "private_initiative.recall",
            self.memory.recall,
            qq_id,
            limit=8,
            source_group_id="",
        )
        # 2026-08-15 整体审查 I6：记忆带入库龄——裸 value 里的「现在才起来」「上周」
        # 是提取时刻的相对时间，与下方注入的「现在时间」碰撞会制造时间幻觉
        # （对着一周前的「现在才起来」问「这个点才起呀？」）
        try:
            _now = datetime.now()
            def _age(ts: str) -> str:
                if not ts:
                    return ""
                try:
                    _t = datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
                    _d = max(0, (_now - _t).days)
                    return f"（{'今天' if _d == 0 else f'{_d}天前'}）"
                except Exception:
                    return ""
            mem_text = "、".join(
                f"{m.value}{_age(m.timestamp)}" for m in mems[:8]
            ) if mems else "（还没有关于ta的记忆）"
        except Exception:
            mem_text = "、".join(m.value for m in mems[:8]) if mems else "（还没有关于ta的记忆）"
        trend_line = self._mood_trend_context(qq_id, nickname)

        # 2026-08-15：注入最近对话——主动开口也要像正常聊天一样有上文
        # （知道上次聊到哪、自己说过什么），否则主动消息像凭空冒出来
        recent_ctx = ""
        try:
            recent = await self._run_store_io(
                "private_initiative.recent_dialogue",
                self.memory.store.get_recent_dialogue,
                qq_id,
                limit=10,
                group_id="",
            )
            if recent:
                recent_ctx = (
                    "你们最近的对话（记得自己说过什么——别重复、别失忆）：\n"
                    + "\n".join(f"- {line}" for line in recent)
                    + "\n"
                )
        except Exception:
            pass

        system_prompt = (
            f"{self.personality._cached_base[:800]}\n\n"
            f"## 你现在的内在状态\n{drive_ctx}\n\n"
            f"{sens}"
        )
        hour = datetime.now().strftime("%H:%M")
        # 2026-08-15 来源标记原则的**有原则例外**：这条消息是「生成简报」而非
        # 「对话回合」——无用户原文可混淆，模型从材料（最近对话/记忆/画像）取样
        # 开口正是期望行为。不要给这里补 <背景·> 标签（见 protocols.bg docstring）。
        user_message = (
            f"【你主动给 {nickname} 发一条私聊消息。】\n"
            f"原因：{reason}\n"
            f"{recent_ctx}"
            f"关于ta你记得的事（下面这些才是你真实记得的）：{mem_text}\n"
            + (f"{trend_line}\n" if trend_line else "")
            + f"现在时间：{hour}。\n"
            f"像你平时想起朋友那样开口就行。"
            f"{_protocols.GROUNDING_CONTRACT}{_protocols.GROUNDING_NO_TOPIC_FALLBACK}"
            f"不要提「检测」「系统提醒」「心情指数」这类词。"
            f"如果觉得此刻不该打扰ta，回复「[不说话]」。"
        )

        try:
            reply = await self._call_llm_light(system_prompt, user_message)
            if not reply:
                if decision_run is not None:
                    await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        error_code="EMPTY_LLM_REPLY",
                    )
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="EMPTY_LLM_REPLY",
                    )
                else:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "skipped",
                        error_code="EMPTY_LLM_REPLY",
                    )
                return False
            reply = reply.strip()
            if "[不说话]" in reply or reply[:4] == "不说话":
                if decision_run is not None:
                    _, bound = await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        reply="", responded=False,
                    )
                    if not bound:
                        await _finish_event_async(
                            self, proactive_event, proactive_lease, "failed",
                            error_code="DECISION_BIND_FAILED",
                        )
                        return False
                if dominant:
                    self.self_state.drives.release(dominant.name, 0.1)
                self._mark_seek_gave_up(qq_id, now)  # 放弃也算一次——不反复纠结
                logger.info(f"💌 糖糖选择不私聊打扰 {nickname}")
                await _finish_event_async(
                    self, proactive_event, proactive_lease, "skipped",
                    error_code="LLM_CHOSE_SILENCE",
                )
                return False

            reply = reply.replace("[不说话]", "").strip()
            if len(reply) < 2:
                if decision_run is not None:
                    await _finalize_decision_async(
                        self, proactive_event, proactive_lease, decision_run,
                        error_code="SHORT_LLM_REPLY",
                    )
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="SHORT_LLM_REPLY",
                    )
                else:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "skipped",
                        error_code="SHORT_LLM_REPLY",
                    )
                return False

            if decision_run is not None:
                _, bound = await _finalize_decision_async(
                    self, proactive_event, proactive_lease, decision_run,
                    reply=reply, responded=True,
                )
                if not bound:
                    await _finish_event_async(
                        self, proactive_event, proactive_lease, "failed",
                        error_code="DECISION_BIND_FAILED",
                    )
                    return False

            # 2026-08-16：主动路径补清洗+贴图标签解析——此前 LLM 输出
            # [贴图:开心] 原样泄露给用户（正常回复路径 enrich 后发，这里漏了）
            reply = self._enrich_reply(reply)
            if not reply:
                await _finish_event_async(
                    self, proactive_event, proactive_lease, "skipped",
                    error_code="EMPTY_ENRICHED_REPLY",
                )
                return False

            ok = await self._checked_send("private", qq_id, reply)
            delivery_state = "confirmed" if ok else send_delivery_state(
                getattr(getattr(self, "reply", None), "last_send_result", None)
            )
            if delivery_state not in {"confirmed", "uncertain", "failed"}:
                delivery_state = "failed"
            await _finish_event_async(
                self, proactive_event, proactive_lease, delivery_state,
            )
            if ok:
                seek_uncertain = getattr(self, "_seek_uncertain", {})
                if qq_id in seek_uncertain:
                    seek_uncertain.pop(qq_id, None)
                    await _persist_state_async(self, "state:seek_uncertain", seek_uncertain)
                # 2026-08-16 观察回路：登记本次主动私聊，等对方回应结算冷热。
                # 状态住关系场（重启不丢）——此前内存态 pending 重启清零，
                # 冷场永远学不到 + 每次重启重新骚扰同一人
                self._mark_seek_sent(qq_id, now, reply)
                # 2026-08-16 流程审计：主动说的话此前不记入聊天史——补记录
                # Codex I7：归属按对话对象（qq_id）+ is_bot=True 标记说话者——
                # 此前写在 bot_qq 名下，get_unprocessed_messages(qq_id) 永远
                # 取不到这条语境（提取批次看不到糖糖自己说过什么）
                try:
                    await self._run_store_io(
                        "autonomy.log_chat.private", self.memory.log_chat,
                        qq_id, reply, is_bot=True,
                    )
                except Exception as exc:
                    # 发送终态已经确认；回写失败只影响后续记忆提取，不回滚发送。
                    logger.warning(
                        "💌 主动私聊已送达但聊天史回写失败: %s", type(exc).__name__,
                    )
                self.memory.add_to_buffer(
                    f"_private_{qq_id}", self.bot_qq, self.config["bot"]["name"], reply[:200])
                # 行动级释放：与群发起同一套（ACTION_RELEASES）
                for _name, _amount in ACTION_RELEASES.get(
                        dominant.name if dominant else "", [("social", 0.35)]):
                    self.self_state.drives.release(_name, _amount)
                # 消费告警队列——已经关心过了（2026-08-16 Codex I2：pop 必须
                # 落盘，否则重启后旧告警复活、同一件事二次关心）
                if qq_id in getattr(self, '_care_due', {}):
                    self._care_due.pop(qq_id, None)
                    await _persist_state_async(self, "state:care_due", self._care_due)
                logger.info(f"💌 私聊插话 → {nickname}({qq_id}): {reply[:80]}（{reason[:40]}）")
                return True
            if send_delivery_state(
                    getattr(getattr(self, "reply", None), "last_send_result", None)
                    ) == "uncertain":
                seek_uncertain = getattr(self, "_seek_uncertain", {})
                seek_uncertain[qq_id] = now
                self._seek_uncertain = seek_uncertain
                await _persist_state_async(self, "state:seek_uncertain", seek_uncertain)
                logger.warning(f"💌 主动私聊 {qq_id} 发送未确认，冻结24小时")
                return True
            return False
        except Exception as e:
            await _finish_event_async(
                self, proactive_event, proactive_lease, "uncertain",
                error_code="AUTONOMY_EXCEPTION",
            )
            logger.warning(f"💌 私聊插话失败: {e}")
            return False

    # ═══════════════════════════════════════════════════════════
    # 主动私聊观察回路（2026-08-16）——像人一样长记性
    # 主人需求：冷场时糖糖应感知「这个人可能没时间或没兴趣」，
    # 累积冷场后不再机械地每天主动找人——主动私聊是带记忆的自由选择。
    # 意愿分住在关系场 RelationshipField.seek_willingness，随 self_state 持久化。
    # ═══════════════════════════════════════════════════════════

    COLD_FIELD_PENALTY = -0.35    # 24h 无回复：一次冷场
    TEPID_REPLY_PENALTY = -0.15   # 敷衍/负面回复：半次冷场
    WARM_REPLY_BONUS = 0.1        # 正常/热情回复：热场
    SEEK_STOP_THRESHOLD = 0.1     # 意愿分低于此 → 不再主动私聊
    SEEK_REBOUND_FLOOR = 0.5      # 对方主动来聊 → 回弹到至少中性

    def _set_last_initiative(self, now: float):
        """全局主动发起冷却——持久化 kv，重启不失效（2026-08-16 事故）"""
        self._last_initiative_time = now
        try:
            self.memory.store.kv_set("autonomy:last_initiative_time", str(now))
        except Exception:
            pass

    async def _set_last_initiative_async(self, now: float):
        """异步自治路径的冷却写入；状态更新先落内存，再在线程中持久化。"""
        self._last_initiative_time = now
        try:
            await self._run_store_io(
                "autonomy.set_last_initiative", self.memory.store.kv_set,
                "autonomy:last_initiative_time", str(now),
            )
        except Exception:
            # 与旧同步入口一致：持久化失败不逆转本回合已做的内存决策。
            pass

    def _rel(self, qq_id: str):
        """取关系场条目（无则创建）——意愿分住在这里，随 self_state 持久化。
        2026-08-16 Codex M2：走 get_or_create_relationship 补 first_met"""
        rel = (self.self_state.relationships or {}).get(qq_id)
        if rel is None:
            rel = self.self_state.get_or_create_relationship(qq_id)
        return rel

    def _adjust_willingness(self, qq_id: str, delta: float, reason: str) -> float:
        """调整主动私聊意愿分并即时落盘——冷场教训不能重启丢。
        2026-08-16 Codex M3：值没变不落盘（_note_user_initiated 热路径上
        每条私聊一次全量 JSON 写盘太贵）"""
        rel = self._rel(qq_id)
        old = rel.seek_willingness
        rel.seek_willingness = max(0.0, min(1.0, old + delta))
        if rel.seek_willingness != old:
            logger.info(f"💌 主动私聊意愿 {qq_id}: {old:.2f} → {rel.seek_willingness:.2f}（{reason}）")
            try:
                self.self_state.save()
            except Exception:
                pass
        return rel.seek_willingness

    def _mark_seek_sent(self, qq_id: str, now: float, msg: str):
        """登记主动私聊已发出：per-user 冷却 + 未结算观察（重启不丢）"""
        rel = self._rel(qq_id)
        rel.last_seek_ts = now
        rel.seek_pending_ts = now
        rel.seek_pending_msg = msg[:200]
        try:
            self.self_state.save()
        except Exception:
            pass

    def _mark_seek_gave_up(self, qq_id: str, now: float):
        """糖糖选择不打扰（[不说话]）——只记冷却，不设观察"""
        rel = self._rel(qq_id)
        rel.last_seek_ts = now
        try:
            self.self_state.save()
        except Exception:
            pass

    def _settle_stale_seeks(self):
        """自治循环每周期调用：主动私聊发出 24h 无回应 = 冷场。
        2026-08-16：此前遍历内存 pending（重启清零学不到冷场）——
        现在遍历关系场，pending 随 self_state 持久化。
        2026-08-16 Codex I3：结算前查 people.last_chat——对方在 pending 之后
        有任何发言（可能是在群里回的），按 neutral 结算不罚，只有真沉默才冷场。"""
        now = time_mod.time()
        for qq_id, rel in list((self.self_state.relationships or {}).items()):
            if rel.seek_pending_ts > 0 and now - rel.seek_pending_ts >= 24 * 3600:
                pending_ts = rel.seek_pending_ts  # 先存局部——下方清零后再比较
                rel.seek_pending_ts = 0
                rel.seek_pending_msg = ""
                spoke_elsewhere = False
                try:
                    person = self.memory.get_or_create_person(qq_id)
                    last = (person.get("last_chat") or "").strip()
                    if last:
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                            try:
                                t = time_mod.mktime(time_mod.strptime(last[:19], fmt))
                                if t >= pending_ts:
                                    spoke_elsewhere = True
                                break
                            except Exception:
                                continue
                except Exception:
                    pass
                if spoke_elsewhere:
                    logger.info(f"💌 主动私聊结算 {qq_id}: 24h内对方有发言（非私聊渠道）——按中性结算")
                else:
                    self._adjust_willingness(qq_id, self.COLD_FIELD_PENALTY, "主动私聊24h无回复=冷场")

    async def _settle_stale_seeks_async(self):
        """异步自治循环的主动私聊结算。

        ``people.last_chat`` 是同步 SQLite 读取；旧的同步实现直接在自治事件
        循环中调用，遇到 SQLite 写锁时会把其它自治/提醒任务一起卡住。先在
        事件循环中快照到期项，再在线程边界读取人物资料，并在 await 后复核
        ``seek_pending_ts``，避免用户恰好回复时仍被误判为冷场。
        """
        now = time_mod.time()
        due = []
        for qq_id, rel in list((self.self_state.relationships or {}).items()):
            pending_ts = float(getattr(rel, "seek_pending_ts", 0.0) or 0.0)
            if pending_ts > 0 and now - pending_ts >= 24 * 3600:
                due.append((str(qq_id), rel, pending_ts))

        for qq_id, rel, pending_ts in due:
            spoke_elsewhere = False
            try:
                person = await self._run_store_io(
                    "autonomy.settle_stale_seeks.get_person",
                    self.memory.get_or_create_person,
                    qq_id,
                )
                last = (person.get("last_chat") or "").strip() if person else ""
                if last:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                        try:
                            t = time_mod.mktime(time_mod.strptime(last[:19], fmt))
                            spoke_elsewhere = t >= pending_ts
                            break
                        except Exception:
                            continue
            except Exception as exc:
                # 资料读取失败不应吞掉 pending；下一自治周期继续重试，避免
                # 把“无法核验”错误地结算成冷场或永久丢失观察。
                logger.warning(
                    "💌 主动私聊结算读取失败: qq=%s error=%s",
                    qq_id,
                    type(exc).__name__,
                )
                continue

            # await 期间可能收到对方回复并清掉 pending；只结算仍属于本次
            # 主动私聊的那一代，避免把正常回复误记成冷场。
            current_rel = (self.self_state.relationships or {}).get(qq_id)
            if current_rel is not rel:
                continue
            try:
                still_pending = float(
                    getattr(current_rel, "seek_pending_ts", 0.0) or 0.0
                ) == pending_ts
            except (TypeError, ValueError):
                still_pending = False
            if not still_pending:
                continue

            current_rel.seek_pending_ts = 0
            current_rel.seek_pending_msg = ""
            try:
                # 结算结果必须在本次循环内持久化；否则“中性结算”路径
                # 重启后会再次看到旧 pending。文件写入很小，保留现有
                # self_state.save 语义，不触碰 Store 事务边界。
                self.self_state.save()
            except Exception:
                pass

            if spoke_elsewhere:
                logger.info(
                    "💌 主动私聊结算 %s: 24h内对方有发言（非私聊渠道）——按中性结算",
                    qq_id,
                )
            else:
                self._adjust_willingness(
                    qq_id,
                    self.COLD_FIELD_PENALTY,
                    "主动私聊24h无回复=冷场",
                )

    async def _settle_seek_with_reply(self, qq_id: str, user_reply: str):
        """对方回了糖糖的主动私聊 → perception 情绪三分类结算冷热。
        2026-08-16 Codex M1：只结算 24h 内的回复——超窗的旧回复与主动
        私聊无关，交给 _settle_stale_seeks 按沉默处理。"""
        rel = (self.self_state.relationships or {}).get(qq_id)
        if not rel or rel.seek_pending_ts <= 0:
            return
        if time_mod.time() - rel.seek_pending_ts >= 24 * 3600:
            return
        msg = rel.seek_pending_msg
        rel.seek_pending_ts = 0
        rel.seek_pending_msg = ""
        try:
            self.self_state.save()
        except Exception:
            pass
        sentiment = "neutral"
        try:
            result = await self.perception._llm_evaluate(msg, user_reply)
            sentiment = result.get("sentiment", "neutral")
        except Exception:
            pass
        if sentiment == "positive":
            self._adjust_willingness(qq_id, self.WARM_REPLY_BONUS, "热场回复")
        elif sentiment == "negative":
            self._adjust_willingness(qq_id, self.TEPID_REPLY_PENALTY, "敷衍/负面回复")
        # neutral 不奖不罚

    def _note_user_initiated(self, qq_id: str, text: str):
        """对方主动来聊（无待结算观察）→ 意愿分回弹——
        人就是这样：你不回我我也不找你，你来找我我就又热情了"""
        rel = self._rel(qq_id)
        if rel.seek_willingness < self.SEEK_REBOUND_FLOOR:
            self._adjust_willingness(
                qq_id, self.SEEK_REBOUND_FLOOR - rel.seek_willingness, "对方主动来聊=回弹")
        elif len(text.strip()) > 10 and rel.seek_willingness < 1.0:
            self._adjust_willingness(qq_id, 0.05, "对方主动来聊(长消息)")

    async def _backfill_extraction(self):
        """回填：从最新消息倒序批量提取，先记住最近的事。

        与正常提取（ASC 从最早开始）方向相反——回填从今天往回消化，
        确保糖糖先拥有最近的记忆（当前状态、近期承诺、活跃偏好）。

        任务和游标都持久化在 SQLite；JSON/进程字段仅作迁移与监控镜像。
        """
        MAX_BATCHES_PER_RUN = 10
        BATCH_SIZE = 50
        BATCH_DELAY = 2

        total_processed = 0
        try:
            forward_state = await self._run_extraction_io(
                "get_extraction_cursors.forward",
                self.memory.store.get_extraction_cursors,
                "forward",
            )
            backfill_state = await self._run_extraction_io(
                "get_extraction_cursors.backfill",
                self.memory.store.get_extraction_cursors,
                "backfill",
            )

            owner = getattr(self, 'owner_qq', '')

            # 一次聚合扫描收集积压用户（主人排第一），避免数百次连接/COUNT。
            rows = await self._run_extraction_io(
                "get_unprocessed_backlog",
                self.memory.store.get_unprocessed_backlog,
                forward_state,
                before_ids=backfill_state,
            )
            backlog = [
                (qq_id, count, max_id)
                for qq_id, count, max_id in rows
                if qq_id != self.bot_qq
                and count >= (10 if qq_id == owner else 30)
            ]
            backlog.sort(key=lambda item: (item[0] != owner, -item[1]))

            if not backlog:
                return

            batches_run = 0
            for qq_id, unprocessed, max_id in backlog:
                if batches_run >= MAX_BATCHES_PER_RUN:
                    break
                # 与正常提取共享单飞锁——回填期间正常提取让路，
                # 避免同一批消息被两路并发提取双写（2026-08-14 重复记忆根因之一）
                if qq_id in self._extracting_users:
                    continue
                self._extracting_users.add(qq_id)
                try:
                    person = await self._run_extraction_io(
                        "get_or_create_person", self.memory.get_or_create_person,
                        qq_id,
                    )
                    nickname = person.get("nickname", qq_id)
                    existing = await self._run_extraction_io(
                        "active_notes", self.memory.active_notes, qq_id,
                    )

                    # 回填倒序：从最新未回填的消息开始
                    to_id = backfill_state.get(qq_id, max_id + 1)
                    last_id = forward_state.get(qq_id, 0)
                    user_batches = 0
                    while batches_run < MAX_BATCHES_PER_RUN and user_batches < 5:
                        # 🔧 _call_llm_light 内部已有锁排队，不需要外层 _llm_busy 门控
                        if getattr(self, '_shutting_down', False):
                            break  # 定时重启进行中——停止回填
                        # 倒序取消息：last_id < id < to_id ORDER BY id DESC。
                        # 下界不可省，否则回填会越过前向游标并重复提取。
                        messages = await self._run_extraction_io(
                            "get_unprocessed_messages.backfill",
                            self.memory.store.get_unprocessed_messages,
                            qq_id, to_id, limit=BATCH_SIZE,
                            newest_first=True, lower_bound_id=last_id,
                        )
                        if len(messages) < 10:
                            break

                        result = await self._process_extraction_batch(
                            user_id=qq_id,
                            messages=messages,
                            nickname=nickname,
                            existing_summary=existing[:300] if existing else "",
                            direction="backfill",
                            origin="backfill",
                            llm_call=lambda s, u: self._call_llm_light(
                                s, u, extra_body={"thinking": {"type": "disabled"}}
                            ),
                        )
                        if not result["completed"]:
                            break
                        count = result["count"]
                        min_id = result["cursor_chat_id"]
                        backfill_state[qq_id] = min_id

                        total_processed += count
                        batches_run += 1
                        user_batches += 1
                        # 回填单批可能包含多次轻量 LLM 调用；把每个已完成批次
                        # 作为自治活性证据，避免长回填期间只有一条尾部心跳。
                        logger.info(
                            f"🫀 自治循环心跳 | phase=backfill batch={batches_run}"
                        )
                        # 更新 to_id 为当前批次的最小ID（下次从这之前开始）
                        to_id = min_id
                        await asyncio.sleep(BATCH_DELAY)

                    if user_batches > 0:
                        logger.info(
                            f"📦 回填: {nickname}({qq_id}) → {user_batches}批/{count}条记忆"
                        )
                finally:
                    self._extracting_users.discard(qq_id)

            if total_processed:
                logger.info(f"📦 本轮回填: {batches_run}批 → {total_processed}条新记忆")

        except Exception as e:
            logger.warning(f"📦 回填异常: {e}")

    async def _maybe_extract_stale(self, max_per_cycle: int = 6,
                                   max_open: int = 8,
                                   now: datetime | None = None) -> int:
        """把前向积压按数量或等待时间有界准入，不在扫描协程里调用 LLM。"""
        admitted = 0
        try:
            store = self.memory.store
            queue_health = await self._run_extraction_io(
                "get_extraction_queue_health", store.get_extraction_queue_health,
            )
            available = min(
                max(0, int(max_per_cycle)),
                max(0, int(max_open) - int(queue_health.get("total_open", 0) or 0)),
            )
            if available <= 0:
                return 0

            # worker 恢复列表会排除未过期 leased；准入层不能复用该列表，
            # 否则活跃任务会被当作空闲用户再次准入。
            open_users = await self._run_extraction_io(
                "list_open_extraction_users", store.list_open_extraction_users,
            )
            owner = str(getattr(self, "owner_qq", "") or "")
            current_time = now or datetime.now()
            backlog_snapshot = await self._run_extraction_io(
                "get_extraction_backlog_snapshot",
                store.get_extraction_backlog_snapshot,
            )
            backlog = [
                row for row in backlog_snapshot["users"]
                if row["qq_id"] != self.bot_qq
                and extraction_backlog_ready(row, current_time)
            ]
            backlog.sort(key=lambda row: (
                row["qq_id"] != owner,
                row["oldest_at"] or "9999-12-31",
                row["oldest_id"],
            ))

            for row in backlog:
                if admitted >= available:
                    break
                qq_id = row["qq_id"]
                if qq_id in open_users or qq_id in self._extracting_users:
                    continue
                try:
                    # E2（2026-08-28，审查 Critical 2）：准入窗口按**真实用户
                    # 消息**计数，并以 oldest_at 老化低频尾部，不再从混合窗口
                    # 里数 user_count。旧实现：bot 上下文
                    # 占满 20 条窗口时 user_count<10 → continue 永久跳过，即使
                    # 用户积压已达阈值（新消息永远排在窗口外，可证明卡住）。
                    # 混合窗口（含有限 bot 上下文——私聊相邻回复的语境价值，
                    # 2026-08-16 批 4 事故修复）只作 job 输入，不参与准入判定。
                    messages = await self._run_extraction_io(
                        "get_unprocessed_messages.forward",
                        self.memory.get_unprocessed_messages,
                        qq_id, limit=20, include_bot_replies=True,
                    )
                    job = await self._run_extraction_io(
                        "create_extraction_job",
                        store.create_extraction_job,
                        qq_id, messages, direction="forward",
                        protocol_version="memory-v1",
                    )
                    if job.get("status") not in {"pending", "ready", "leased"}:
                        continue
                    if job.get("created_now"):
                        record_extraction_stage(
                            self.metrics, logger, "created", job,
                        )
                    open_users.add(qq_id)
                    admitted += 1
                    record_extraction_stage(
                        self.metrics, logger, "admitted", job,
                    )
                    logger.info(
                        f"🧠 积压准入: 任务#{job['id']} {row['messages']}条 → "
                        "持久任务已登记"
                    )
                except Exception as exc:
                    # DEBUG 在生产日志级别通常不可见，导致观察器误以为
                    # 自治准入始终健康；保留类型即可，不把用户标识写入告警。
                    logger.warning(
                        f"🔥 自治积压准入异常: {type(exc).__name__}"
                    )
        except Exception as exc:
            logger.warning(
                f"🔥 自治积压扫描异常: {type(exc).__name__}"
            )
        return admitted

    async def _resume_extraction_jobs(self, limit: int = 10) -> int:
        """恢复 ready/pending/过期租约任务；每个任务失败不阻塞其余用户。"""
        completed_count = 0
        jobs = await self._run_extraction_io(
            "list_resumable_extraction_jobs",
            self.memory.store.list_resumable_extraction_jobs,
            limit=limit,
        )
        for job in jobs:
            qq_id = str(job["qq_id"])
            started_at = time_mod.perf_counter()
            initial_status = str(job.get("status") or "")
            try:
                if job["status"] == "ready":
                    completed = {
                        **job,
                        **await self._run_extraction_io(
                            "complete_extraction_job",
                            self.memory.store.complete_extraction_job,
                            job["id"],
                            origin=("backfill" if job["direction"] == "backfill"
                                    else "extracted"),
                        ),
                    }
                    cursor = int(completed["cursor_chat_id"])
                    if job["direction"] == "forward":
                        self.memory._last_extracted_id[qq_id] = cursor
                    else:
                        if not hasattr(self.memory, "_last_backfill_to_id"):
                            self.memory._last_backfill_to_id = {}
                        self.memory._last_backfill_to_id[qq_id] = cursor
                    completed_count += 1
                    record_extraction_stage(
                        self.metrics, logger, "completed", completed,
                        started_at=started_at,
                    )
                    continue

                if qq_id in self._extracting_users:
                    continue
                messages, missing_ids = (
                    await self._run_extraction_io(
                        "get_extraction_job_messages_with_integrity",
                        self.memory.store.get_extraction_job_messages_with_integrity,
                        job["id"],
                    )
                )
                if missing_ids or not messages:
                    record_extraction_stage(
                        self.metrics, logger, "missing_messages", job,
                        started_at=started_at,
                    )
                    quarantined = await self._run_extraction_io(
                        "quarantine_extraction_job",
                        self.memory.store.quarantine_extraction_job,
                        job["id"],
                        "missing_frozen_messages"
                        + (f":{len(missing_ids)}" if missing_ids else ""),
                    )
                    if quarantined:
                        record_extraction_stage(
                            self.metrics, logger, "dead",
                            {**job, "status": "dead"},
                            started_at=started_at,
                            error="missing_frozen_messages",
                        )
                    logger.warning(
                        f"🧠 提取任务#{job['id']}缺少冻结消息，已隔离待人工核验"
                    )
                    continue
                self._extracting_users.add(qq_id)
                try:
                    person = await self._run_extraction_io(
                        "get_or_create_person", self.memory.get_or_create_person,
                        qq_id,
                    )
                    existing = await self._run_extraction_io(
                        "active_notes", self.memory.active_notes, qq_id,
                    )
                    result = await self._process_extraction_batch(
                        user_id=qq_id,
                        messages=messages,
                        nickname=person.get("nickname", qq_id),
                        existing_summary=existing[:300] if existing else "",
                        direction=job["direction"],
                        origin=("backfill" if job["direction"] == "backfill"
                                else "extracted"),
                        llm_call=lambda s, u: self._call_llm_light(
                            s, u, extra_body={"thinking": {"type": "disabled"}}
                        ),
                    )
                    if result["completed"]:
                        completed_count += 1
                finally:
                    self._extracting_users.discard(qq_id)
            except Exception as exc:
                # pending/leased 任务的 envelope 已在内部记录 failed；这里只为
                # ready 直接归账失败补一条，避免同一异常被重复计数。
                try:
                    current = await self._run_extraction_io(
                        "get_extraction_job", self.memory.store.get_extraction_job,
                        job["id"],
                    )
                except Exception:
                    current = None
                if current and initial_status == "ready" and current.get("status") in {
                    "ready", "dead",
                }:
                    record_extraction_stage(
                        self.metrics, logger, "failed", current,
                        started_at=started_at, error=type(exc).__name__,
                    )
                    if current.get("status") == "dead":
                        record_extraction_stage(
                            self.metrics, logger, "dead", current,
                            started_at=started_at, error=type(exc).__name__,
                        )
                logger.warning(f"🧠 恢复提取任务#{job['id']}失败: {exc}")
        return completed_count

    async def _extraction_worker_loop(self):
        """单 worker 有界补充并消费持久队列；实时回复占用时主动让路。"""
        await asyncio.sleep(2)
        while not getattr(self, "_shutting_down", False):
            cycle_started = time_mod.perf_counter()
            try:
                if (getattr(self, "_busy", False)
                        or getattr(self, "_llm_lock", None).locked()):
                    try:
                        busy_health = await self._run_extraction_io(
                            "get_extraction_queue_health.busy",
                            self.memory.store.get_extraction_queue_health,
                        )
                    except Exception:
                        busy_health = {}
                    logger.info(
                        "🧠 提取 worker 周期 | open=%s pending=%s leased=%s ready=%s "
                        "admitted=0 completed=0 requeued=0 idle_reason=busy elapsed_ms=%.1f",
                        busy_health.get("total_open", "-"),
                        busy_health.get("pending", "-"),
                        busy_health.get("leased", "-"),
                        busy_health.get("ready", "-"),
                        (time_mod.perf_counter() - cycle_started) * 1000,
                    )
                    await asyncio.sleep(2)
                    continue
                requeued = await self._run_extraction_io(
                    "requeue_dead_extraction_jobs",
                    self.memory.store.requeue_dead_extraction_jobs,
                    limit=2,
                )
                if requeued:
                    for _ in range(requeued):
                        record_extraction_stage(
                            self.metrics, logger, "requeued", None,
                        )
                before = await self._run_extraction_io(
                    "get_extraction_queue_health.before",
                    self.memory.store.get_extraction_queue_health,
                )
                admitted = 0
                if int(before.get("total_open", 0) or 0) < EXTRACTION_QUEUE_TARGET:
                    admitted = await self._maybe_extract_stale(
                        max_per_cycle=EXTRACTION_ADMIT_BATCH,
                        max_open=EXTRACTION_QUEUE_TARGET,
                    )
                completed = await self._resume_extraction_jobs(limit=1)
                embedded = await run_bounded_blocking(
                    "memory.ensure_embeddings",
                    self.memory.ensure_memory_embeddings,
                    getattr(self, "embed_engine", None),
                    25,
                    logger=logger,
                    log_prefix="🧠 记忆 embedding 回填较慢",
                )
                health = await self._run_extraction_io(
                    "get_extraction_queue_health.after",
                    self.memory.store.get_extraction_queue_health,
                )
                if completed or embedded or requeued or admitted:
                    idle_reason = "active"
                elif health.get("total_open", 0):
                    idle_reason = "open_waiting"
                else:
                    idle_reason = "no_open"
                logger.info(
                    "🧠 提取 worker 周期 | open=%s pending=%s leased=%s ready=%s "
                    "admitted=%s completed=%s requeued=%s idle_reason=%s elapsed_ms=%.1f",
                    health.get("total_open", 0), health.get("pending", 0),
                    health.get("leased", 0), health.get("ready", 0), admitted,
                    completed, requeued, idle_reason,
                    (time_mod.perf_counter() - cycle_started) * 1000,
                )
                if completed or embedded or requeued or admitted:
                    await asyncio.sleep(1)
                elif health.get("total_open", 0):
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(15)
            except Exception as exc:
                logger.warning(f"🧠 提取 worker 异常: {exc}")
                await asyncio.sleep(10)

    async def _maybe_synthesize_stale_profiles(self):
        """画像补全：扫描有>10条记忆但无画像或画像 dirty 的用户。
        每次最多处理 2 人，避免 LLM API 短时间大量调用。"""
        max_per_cycle = 2
        processed = 0
        try:
            # notes_dirty 是失败后必须可重试的持久状态，不能只扫描 notes 为空者。
            rows = await self._run_store_io(
                "profiles.scan_candidates",
                self.memory.store.get_people_with_memories_gt,
                min_count=10, empty_notes=True, limit=10, include_dirty=True,
            )

            for (qq_id,) in rows:
                if processed >= max_per_cycle:
                    break
                if qq_id in self._extracting_users:
                    continue
                # 检查是否最近已尝试合成（避免频繁重试）
                last_attempt = await self._run_store_io(
                    "profiles.last_attempt", self.memory.store.kv_get,
                    f"synth_attempt:{qq_id}",
                )
                if last_attempt:
                    try:
                        from datetime import timedelta
                        last_dt = datetime.fromisoformat(last_attempt)
                        if datetime.now() - last_dt < timedelta(hours=6):
                            continue  # 6小时内试过，跳过
                    except Exception:
                        pass
                person = await self._run_store_io(
                    "profiles.get_person", self.memory.get_or_create_person, qq_id,
                )
                is_dirty = bool(person.get("notes_dirty", 0))
                reason = "画像待纠正重合成" if is_dirty else "有记忆但无画像"
                logger.info(f"📝 画像补全触发: {qq_id} {reason}")
                await self._run_store_io(
                    "profiles.mark_attempt", self.memory.store.kv_set,
                    f"synth_attempt:{qq_id}", datetime.now().isoformat(),
                )
                task = (self._resynthesize_profile_later(qq_id) if is_dirty
                        else self._synthesize_profile_task(qq_id))
                self._safe_task(task, name=f"profile_retry:{qq_id}")
                processed += 1
        except Exception as e:
            logger.debug(f"📝 画像补全扫描失败: {e}")

    async def _maybe_daily_metrics_aggregate(self):
        """每日凌晨运行一次聚合查询（M11-M13 需要 SQL 扫描）。
        结果写入 kv_store，避免每次 /状态 都扫描数据库。

        2026-08-30：所有持久化读写在线程边界执行；短期缓冲清理仍留在
        事件循环线程，避免后台线程遍历可变 deque。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        last_run = await self._run_store_io(
            "daily_aggregate.check",
            self.memory.store.kv_get,
            f"metric:{today}:daily_aggregate_done",
        )
        if last_run:
            return
        try:
            # 2026-08-10 收口 Store：全部直连 SQL 改走 Store 方法
            # M11: 聊>50次但零记忆的用户
            rows = await self._run_store_io(
                "daily_aggregate.zero_memory_users",
                self.memory.store.get_zero_memory_users,
                min_chats=50,
            )
            if rows:
                user_list = ",".join(f"{r[0]}({r[1][:8]})" for r in rows[:10])
                await self._run_store_io(
                    "daily_aggregate.zero_memory_list",
                    self.memory.store.kv_set,
                    f"metric:{today}:users_zero_memory_list",
                    user_list,
                )
                await self._run_store_io(
                    "daily_aggregate.zero_memory_count",
                    self.memory.store.kv_set,
                    f"metric:{today}:users_zero_memory_count",
                    str(len(rows)),
                )
            else:
                await self._run_store_io(
                    "daily_aggregate.zero_memory_list",
                    self.memory.store.kv_set,
                    f"metric:{today}:users_zero_memory_list",
                    "",
                )
                await self._run_store_io(
                    "daily_aggregate.zero_memory_count",
                    self.memory.store.kv_set,
                    f"metric:{today}:users_zero_memory_count",
                    "0",
                )

            # M12: 记忆>10但无画像
            rows = await self._run_store_io(
                "daily_aggregate.no_profile_users",
                self.memory.store.get_people_with_memories_gt,
                min_count=10,
                empty_notes=True,
                limit=10000,
            )
            await self._run_store_io(
                "daily_aggregate.no_profile_count",
                self.memory.store.kv_set,
                f"metric:{today}:users_no_profile_count",
                str(len(rows)),
            )

            # M13: 主人记忆数
            owner = str(getattr(self, 'owner_qq', ''))
            if owner:
                cnt = await self._run_store_io(
                    "daily_aggregate.owner_memory_count",
                    self.memory.store.count_memories_for,
                    owner,
                )
                await self._run_store_io(
                    "daily_aggregate.owner_memory_write",
                    self.memory.store.kv_set,
                    f"metric:{today}:owner_memory_count",
                    str(cnt),
                )

            await self._run_store_io(
                "daily_aggregate.mark_done",
                self.memory.store.kv_set,
                f"metric:{today}:daily_aggregate_done",
                "1",
            )
            # 🧹 每日清理：陈旧低质记忆（importance≤2, 未回忆, >90天）
            try:
                await self._run_store_io(
                    "daily_aggregate.cleanup_memories",
                    self.memory.cleanup_stale_memories,
                    days=90,
                )
                from .health_check import mark_cleanup_ran
                await self._run_store_io(
                    "daily_aggregate.mark_cleanup_ran",
                    mark_cleanup_ran,
                    self,
                )
            except Exception:
                pass
            # 🧹 每日清理：闲置短期缓冲（>24h 未活跃的群聊上下文）
            try:
                self.memory.cleanup_stale_buffers(max_idle_hours=24)
            except Exception:
                pass
        except Exception:
            pass  # 静默失败，不阻塞自治循环
