"""
系统健康自检 — 让糖糖知道自己的身体状况

设计原则：
1. 自报家门——不等用户问。启动时、每小时、每天主动检查。
2. 对比"宣称"和"实际"——不只看指标数值，看功能是否真的在跑。
3. 静默失效 = 告警——任何组件应该产出但没有产出，必须被报告。
4. 糖糖是最终消费者——结果生成自然语言，注入 self_state。

每个检查项返回 {"status": "ok"|"warn"|"error", "message": str}
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timedelta

from .async_io import run_bounded_blocking
from .paths import state_path  # 状态文件落盘位置单一事实源（2026-10-15 T55-F）

logger = logging.getLogger("糖糖.HealthCheck")

class SystemHealth:
    """轻量健康自检框架。所有检查 O(1) 或 O(n) 小 n，不阻塞。"""

    def __init__(self, handler):
        self._handler = handler  # MessageHandler 引用
        self._checks: dict[str, dict] = {}  # {name: {fn, schedule, last_run}}
        self._results: dict[str, dict] = {}  # {name: {status, message, timestamp}}

    # ═══════════════════════════════════════
    # 注册
    # ═══════════════════════════════════════

    def register(self, name: str, check_fn, schedule: str = "hourly",
                 description: str = ""):
        """注册检查项。schedule: 'startup' | 'hourly' | 'daily'"""
        self._checks[name] = {
            "fn": check_fn,
            "schedule": schedule,
            "description": description,
            "last_run": None,
        }

    # ═══════════════════════════════════════
    # 运行
    # ═══════════════════════════════════════

    async def run_startup(self):
        """启动时全量检查。"""
        logger.info("🩺 启动健康检查…")
        for name, check in self._checks.items():
            if check["schedule"] in ("startup", "hourly", "daily"):
                await self._run_one(name, check)

    async def run_hourly(self):
        """每小时轻量检查。"""
        now = datetime.now()
        for name, check in self._checks.items():
            schedule = check["schedule"]
            last_run = check["last_run"]
            if (schedule == "hourly"
                    and (last_run is None
                         or (now - last_run).total_seconds() >= 3600)):
                await self._run_one(name, check)
            elif schedule == "daily" and self._should_run_daily(check, now):
                await self._run_one(name, check)

    async def run_daily(self):
        """每日全量检查（0点触发）。"""
        for name, check in self._checks.items():
            await self._run_one(name, check)

    async def _run_one(self, name: str, check: dict):
        try:
            result = await check["fn"]()
            result["timestamp"] = datetime.now().strftime("%H:%M")
            self._results[name] = result
            check["last_run"] = datetime.now()
            if result["status"] != "ok":
                logger.warning(f"🩺 [{result['status']}] {name}: {result['message']}")
        except Exception as e:
            self._results[name] = {
                "status": "error",
                "message": f"检查自身失败: {e}",
                "timestamp": datetime.now().strftime("%H:%M"),
            }
            # 之前只写内存结果、不写日志，生产观察器无法发现检查器
            # 自身崩溃；告警只保留检查名和异常类型，避免泄漏详情。
            logger.error(f"🩺 [error] {name}: {type(e).__name__}")

    def _should_run_daily(self, check: dict, now: datetime) -> bool:
        last = check["last_run"]
        if last is None:
            return True
        return (now - last).total_seconds() > 3600 * 20  # 20小时兜底

    # ═══════════════════════════════════════
    # 输出
    # ═══════════════════════════════════════

    def summary(self) -> str:
        """生成 /状态 用的自然语言摘要。"""
        if not self._results:
            return "🩺 尚未运行健康检查"

        errors = [(n, r) for n, r in self._results.items() if r["status"] == "error"]
        warns = [(n, r) for n, r in self._results.items() if r["status"] == "warn"]
        oks = [n for n, r in self._results.items() if r["status"] == "ok"]

        lines = ["🩺 系统健康："]
        if errors:
            lines.append(f"  🔴 {len(errors)} 项异常：")
            for n, r in errors:
                lines.append(f"     • {r['message']}")
        if warns:
            lines.append(f"  🟡 {len(warns)} 项需关注：")
            for n, r in warns:
                lines.append(f"     • {r['message']}")
        lines.append(f"  🟢 {len(oks)} 项正常")
        return "\n".join(lines)

    def self_state_context(self) -> str:
        """生成注入 self_state 的自然语言——糖糖的自我感知。"""
        if not self._results:
            return ""
        errors = [r["message"] for _, r in self._results.items() if r["status"] == "error"]
        warns = [r["message"] for _, r in self._results.items() if r["status"] == "warn"]
        parts = []
        if errors:
            parts.append(f"你的运行系统有{len(errors)}个严重问题需要主人修复：{'；'.join(errors[:3])}")
        if warns:
            parts.append(f"你的运行系统有{len(warns)}个小问题：{'；'.join(warns[:3])}")
        if not errors and not warns:
            parts.append("你的运行系统运行正常。")
        return " ".join(parts) if parts else ""

# ════════════════════════════════════════════════════════════
# 22 项检查函数 — 每个返回 {"status": "ok"|"warn"|"error", "message": str}
# （2026-09-20 修正：这里原写「16 项」，实际早有 21 个；加 semantic_ready 后 22。
#   照 CLAUDE.md 教训 #35——写进注释的数字也得当代码看，否则它会静默腐烂。）
# 参数: handler (MessageHandler 实例)
# ════════════════════════════════════════════════════════════

def _today():
    return datetime.now().strftime("%Y-%m-%d")

# ── 冷启动宽限期（2026-09-20）────────────────────────────────────────────
# 健康检查里有若干项判的是「**累计数据够不够**」（主人记忆数、上次清理时间、
# 今日提取次数）。这些在新装机器刚起的头十分钟里必然是空的——那不是故障，
# 是「还没有发生过」。而 `run_startup` 会把 21 项一次性全跑一遍，
# 于是第一次启动的日志页里会同时出现：
#     🩺 [error] owner_memories: 主人记忆数为 0——最严重的系统失败
#     🩺 [warn]  cleanup_ran: 上次陈旧清理: 从未——可能未执行
#     🩺 [warn]  extract_running: 今天尚未触发任何记忆提取——管道可能阻塞
# 三条都不是真的。主人 2026-09-20 说的「日志非常乱」，有很大一部分来自这里：
# **第一天全是预期行为的东西，被写成了故障的样子。**
#
# 宽限期只压「数据还没攒够」这一类，不压真正的就位检查——
# 模型没加载（semantic_ready）、网关没连上（gateway_health）照报不误。
#
# [!] 但它们"照报不误"靠的**不是本宽限期**，而是 `handler_autonomy.py` 在
#   `run_startup()` 之前那 120 秒的 `asyncio.sleep`——那段时间足够 BGE 加载完
#   （本机实测加载 3.44 秒，约 35 倍余量）。本常量只管"累计数据"类检查。
#   第一版注释写成「模型没加载在启动第一秒就有确定答案」，那是不对的：
#   真去把 120 秒拿掉，semantic_ready 会在模型尚未加载时误报 error。
_PROCESS_START = time.monotonic()
WARMUP_SECONDS = 600.0

def _in_warmup() -> bool:
    return (time.monotonic() - _PROCESS_START) < WARMUP_SECONDS

async def _store_io(handler, operation: str, func, *args, **kwargs):
    """在健康检查中统一隔离同步 Store 调用。

    MessageHandler 提供带有界并发和尾延迟观测的 ``_run_store_io``；
    轻量测试替身或独立调用者没有该入口时，退回到 ``asyncio.to_thread``。
    健康检查不能因为读取数据库而暂停入站事件循环。
    """
    runner = getattr(handler, "_run_store_io", None)
    if callable(runner):
        return await runner(operation, func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)

async def _check_gateway_health(handler) -> dict:
    """检查网关快照：不发网络请求，避免健康任务反过来占用主链路。
    依赖 NapCatClient 的三层状态和有界队列快照。
    """
    client = getattr(handler, "napcat", None)
    if client is None:
        return {"status": "warn", "message": "QQ网关未配置"}
    snapshot = getattr(client, "runtime_snapshot", {}) or {}
    if not snapshot:
        snapshot = {
            "ready": getattr(client, "ready_to_send", False),
            "ws_connected": getattr(client, "_ws_connected", False),
            "qq_online": getattr(client, "_qq_online", False),
            "event_inflight": getattr(client, "_event_inflight", 0),
            "active_scopes": len(getattr(client, "_event_workers", {}) or {}),
        }
    ready = bool(snapshot.get("ready", getattr(client, "ready_to_send", False)))
    ws = bool(snapshot.get("ws_connected", getattr(client, "_ws_connected", False)))
    qq = bool(snapshot.get("qq_online", getattr(client, "_qq_online", False)))
    inflight = int(snapshot.get("event_inflight", 0) or 0)
    scopes = int(snapshot.get("active_scopes", 0) or 0)
    outbox_store = getattr(client, "_outbox_store", None)
    outbox = (
        await _store_io(
            handler, "health.gateway.outbox",
            outbox_store.get_send_outbox_health,
        )
        if outbox_store else {}
    )
    dead = int(outbox.get("dead", 0) or 0)
    uncertain = int(outbox.get("uncertain", 0) or 0)
    confirmed_unaccounted = int(outbox.get("confirmed_unaccounted", 0) or 0)
    confirmed_conflict = int(outbox.get("confirmed_conflict", 0) or 0)
    open_count = int(outbox.get("open", 0) or 0)
    bare_sending_tasks = int(outbox.get("bare_sending_tasks", 0) or 0)
    parts = [
        f"WS={'on' if ws else 'off'}",
        f"QQ={'on' if qq else 'off'}",
        f"ready={'yes' if ready else 'no'}",
        f"inflight={inflight}",
        f"scopes={scopes}",
        f"outbox_open={open_count}",
        f"api={int(snapshot.get('api_calls', 0) or 0)}/"
        f"{int(snapshot.get('api_failures', 0) or 0)}",
    ]
    terminal_review = dead + uncertain + confirmed_unaccounted + confirmed_conflict
    needs_review = max(
        terminal_review, int(outbox.get("needs_review", 0) or 0),
    )
    if needs_review:
        parts.append(f"review={needs_review}")
    if bare_sending_tasks:
        parts.append(f"bare_tasks={bare_sending_tasks}")
    if confirmed_unaccounted or confirmed_conflict:
        parts.append(
            f"confirmed_local={confirmed_unaccounted}/{confirmed_conflict}"
        )
    status = "ok" if ready and not open_count and not needs_review else "warn"
    return {"status": status, "message": "QQ网关运行快照：" + " ".join(parts)}

async def _check_local_tts_health(handler) -> dict:
    """只读检查已配置的 GPT-SoVITS 是否仍可达，不触发推理或自动重启。"""
    config = getattr(handler, "config", {}) or {}
    voice = config.get("voice") if isinstance(config, dict) else {}
    voice = voice if isinstance(voice, dict) else {}
    provider = str(voice.get("provider") or "").strip().lower().replace("_", "-")
    if not bool(voice.get("enabled", True)) or provider not in {
            "gpt-sovits", "gptsovits"}:
        return {"status": "ok", "message": "GPT-SoVITS 未启用"}

    manager = getattr(handler, "_service_mgr", None)
    checker = getattr(manager, "_check_port", None)
    if not callable(checker):
        return {"status": "warn", "message": "GPT-SoVITS 健康检查不可用"}
    try:
        port_up = bool(await checker(9880))
    except Exception as exc:
        return {
            "status": "warn",
            "message": f"GPT-SoVITS 健康探针失败: {type(exc).__name__}",
        }

    proc = getattr(manager, "_gpt_sovits_proc", None)
    returncode = getattr(proc, "returncode", None) if proc is not None else None
    if not port_up:
        suffix = f" (exit={returncode})" if returncode is not None else ""
        return {"status": "warn", "message": f"GPT-SoVITS 端口 9880 不可达{suffix}"}
    if returncode is not None:
        return {
            "status": "warn",
            "message": f"GPT-SoVITS 端口仍在线但管理进程已退出 (exit={returncode})",
        }
    return {"status": "ok", "message": "GPT-SoVITS 端口 9880 在线"}

# ── 提取管道 ──

async def _check_extract_running(handler) -> dict:
    """过去1小时是否有提取触发"""
    m = handler.metrics
    attempts = m.get_today("extract_attempts")
    if attempts == 0:
        # 可能是刚启动、也可能是管道阻塞。只看过去1小时。
        # metrics 是当天累计，如果今天是0但刚启动→正常；如果今天是0且已过中午→有问题
        now_hour = datetime.now().hour
        if now_hour >= 12 and not _in_warmup():
            return {"status": "warn", "message": f"今天尚未触发任何记忆提取——管道可能阻塞"}
        return {"status": "ok", "message": "提取管道等待触发中（上午正常）"}
    return {"status": "ok", "message": f"今日已触发 {attempts} 次提取"}

async def _check_extract_busy(handler) -> dict:
    """主回复繁忙时提取是否成功转入持久队列。"""
    m = handler.metrics
    attempts = m.get_current("extract_attempts")
    queued = m.get_current("extract_busy_queued")
    if attempts > 0:
        ratio = queued / attempts
        return {
            "status": "ok",
            "message": f"LLM繁忙任务已持久排队 {queued}/{attempts}（{ratio:.0%}）",
        }
    return {"status": "ok", "message": "提取队列等待触发"}

# 2026-10-14 T29：提取成功率判定的**样本下限**。
# 为什么是 30 而不是 5：这些计数器是**进程内累计**（`get_current`），重启即清零 ——
# 门槛 5 意味着重启后头十几次提取就被当成「成功率」，而热身阶段的失败率不代表稳态。
MIN_EXTRACT_OUTCOMES = 30


async def _check_extract_success(handler) -> dict:
    """分别观测 JSON 协议成功率与记忆产出率。

    2026-10-14 T29：**两个门槛，缺一不可**。

    ① **尊重启动保温期**——与同文件的 `_check_extract_running` 同口径（那个检查
       从一开始就调 `_in_warmup()`，本检查漏了）。**同一个文件里两个检查对
       「刚启动」的判断不一致**，是 T35 那类「两份拷贝漂移」的同族。
    ② **样本下限 5 → 30**。

    为什么：`get_current` 读的是**进程内累计**计数器，重启即清零 ⇒ 重启后
    头十几次提取会被当成「成功率」。实测现场：启动健康检查印出
    「协议成功率 7%，JSON无效率 93%」的 WARNING，而**同一时刻**
    `extraction_jobs` 显示 10210 done / 0 failed —— **管道是健康的**。

    假警报会训练人忽略警报（本仓已因「假红」付出过代价），
    所以样本不足时宁可明确说「还在积累」，也不拿热身样本下结论。
    """
    m = handler.metrics
    outcomes = m.get_current("extract_outcomes_total")
    if _in_warmup():
        return {"status": "ok",
                "message": f"启动保温期内，提取样本积累中（{outcomes} 次）"}
    if outcomes < MIN_EXTRACT_OUTCOMES:
        return {"status": "ok",
                "message": (f"提取结果样本不足（{outcomes}/{MIN_EXTRACT_OUTCOMES} 次），"
                            f"暂不判断成功率")}
    protocol = m.get_current("extract_protocol_success") / outcomes
    produced = m.get_current("extract_with_items") / outcomes
    invalid = m.get_current("extract_invalid_json") / outcomes
    transport = m.get_current("extract_transport_error") / outcomes
    message = (
        f"协议成功率 {protocol:.0%}，产出率 {produced:.0%}，"
        f"JSON无效率 {invalid:.0%}，调用异常率 {transport:.0%}"
    )
    if protocol < 0.7:
        return {"status": "warn", "message": message}
    return {"status": "ok", "message": message}

async def _check_stale_extraction(handler) -> dict:
    """报告积压存量及相邻小时样本的到达/消化速率。"""
    try:
        snapshot = await _store_io(
            handler,
            "health.extraction_backlog",
            handler.memory.store.get_extraction_backlog_snapshot,
        )
        current = {
            "captured_at": time.time(),
            "total_user_messages": int(snapshot["total_user_messages"]),
            "backlog_messages": int(snapshot["backlog_messages"]),
            "backlog_users": int(snapshot["backlog_users"]),
            "over_30_users": int(snapshot["over_30_users"]),
            "eligible_messages": int(snapshot.get("eligible_messages", 0) or 0),
            "deferred_messages": int(snapshot.get("deferred_messages", 0) or 0),
        }
        previous = getattr(handler, "_extraction_backlog_sample", None)
        handler._extraction_backlog_sample = current

        state = "warming"
        rates = ""
        if previous:
            elapsed_hours = max(
                (current["captured_at"] - float(previous["captured_at"])) / 3600,
                1 / 3600,
            )
            arrivals = max(
                0,
                current["total_user_messages"]
                - int(previous["total_user_messages"]),
            )
            serviced = max(
                0,
                int(previous["backlog_messages"])
                + arrivals - current["backlog_messages"],
            )
            arrival_rate = arrivals / elapsed_hours
            service_rate = serviced / elapsed_hours
            if service_rate > arrival_rate:
                state = "draining"
            elif service_rate > 0:
                state = "degraded"
            else:
                state = "stopped"
            rates = f"，到达{arrival_rate:.1f}/h，消化{service_rate:.1f}/h"

        if current["eligible_messages"] == 0:
            state = "normal"
        message = (
            f"积压{current['backlog_messages']}条/{current['backlog_users']}人，"
            f"到期{current['eligible_messages']}，等待{current['deferred_messages']}，"
            f"oldest={snapshot['oldest_at'] or '-'}，state={state}{rates}"
        )
        unhealthy = bool(
            current["eligible_messages"] > 0
            and (
                current["over_30_users"] > 3
                or (previous and state in {"stopped", "degraded"})
            )
        )
        return {
            "status": "warn" if unhealthy else "ok",
            "message": message,
        }
    except Exception:
        return {"status": "warn", "message": "无法检查消息积压（数据库不可用）"}

async def _check_extraction_queue(handler) -> dict:
    """持久任务不能死亡或长时间无人消费。"""
    try:
        health = await _store_io(
            handler,
            "health.extraction_queue",
            handler.memory.store.get_extraction_queue_health,
        )
        dead = int(health.get("dead", 0) or 0)
        if dead:
            return {
                "status": "error",
                "message": f"记忆提取有 {dead} 个死信任务，需要核验后重试",
            }
        total = int(health.get("total_open", 0) or 0)
        oldest = str(health.get("oldest_open_at", "") or "")
        if oldest:
            age = (datetime.now() - datetime.strptime(
                oldest[:19], "%Y-%m-%d %H:%M:%S"
            )).total_seconds()
            if age > 3600:
                return {
                    "status": "warn",
                    "message": f"记忆提取积压 {total} 个，最老已等待 {age / 60:.0f} 分钟",
                }
        return {"status": "ok", "message": f"记忆提取开放任务 {total} 个"}
    except Exception as exc:
        return {"status": "warn", "message": f"无法检查提取队列: {exc}"}

# ── 记忆质量 ──

async def _check_reasoning_leaks(handler) -> dict:
    """是否有LLM推理泄露入库"""
    try:
        # 2026-08-10 收口：走 Store 方法
        cnt = await _store_io(
            handler,
            "health.reasoning_leaks",
            handler.memory.store.count_reasoning_leaks_today,
        )
        if cnt > 0:
            return {"status": "error", "message": f"今日有 {cnt} 条推理泄露入库——校验层未拦截"}
        return {"status": "ok", "message": "今日无推理泄露"}
    except Exception as e:
        return {"status": "warn", "message": f"检查推理泄露失败: {e}"}

async def _check_duplicate_memories(handler) -> dict:
    """是否有新增重复记忆"""
    try:
        import sqlite3 as _sqlite3
        # 2026-08-10 收口：走 Store 方法
        dup = await _store_io(
            handler,
            "health.duplicate_memories",
            handler.memory.store.count_duplicate_memories_today,
        )
        if dup > 0:
            return {"status": "warn", "message": f"今日有 {dup} 组重复记忆入库——去重可能未生效"}
        return {"status": "ok", "message": "今日无重复记忆"}
    except Exception as e:
        return {"status": "warn", "message": f"检查重复记忆失败: {e}"}

async def _check_confidence(handler) -> dict:
    """置信度分布是否正常——≥0.8应占多数"""
    m = handler.metrics
    high = m.get_today("confidence_high")
    mid = m.get_today("confidence_mid")
    low = m.get_today("confidence_low")
    total = high + mid + low
    if total > 10:
        if high / total < 0.3:
            return {"status": "warn", "message": f"高置信度(≥0.8)记忆仅 {high}/{total}——提取质量下降"}
    return {"status": "ok", "message": f"置信度分布正常 (≥0.8: {high}, 0.5-0.7: {mid})"}

async def _check_memory_truth(handler) -> dict:
    """批 5（2026-08-16 特摄事故后）：真值卫生——
    元话术污染 / 合成占比异常 / 长期未清算的 dirty 画像 / 事实簇无证据存量。
    本次事故的两类错误（低置信度种子、元话术前缀 notes）此前在健康检查里隐身。"""
    import sqlite3 as _sqlite3
    try:
        store = handler.memory.store
        def _read_truth():
            with store._connect() as conn:
                # 1) notes 元话术污染（「这是个人群像的增量更新版」曾实存）
                bad = conn.execute(
                    "SELECT COUNT(*) FROM people WHERE notes LIKE '%增量更新%' "
                    "OR notes LIKE '%根据您提供%' OR notes LIKE '%以下是%'"
                ).fetchone()[0]
                # 2) 合成行占比——合成自己写自己会失控（自激循环的存量信号）
                total = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active')='active'"
                ).fetchone()[0]
                untrusted_active = conn.execute(
                    "SELECT COUNT(*) FROM memories "
                    "WHERE COALESCE(status,'active')='active' "
                    "AND COALESCE(trust_level,'legacy_unverified') NOT IN "
                    "('verified','manual','corrected')"
                ).fetchone()[0]
                syn = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'active')='active' "
                    "AND key IN ('profile_synthesis','fact_synthesis')"
                ).fetchone()[0]
                # 3) dirty 画像超过 24h 未清算（重合成失败/未触发）
                stale_dirty = conn.execute(
                    "SELECT COUNT(*) FROM people WHERE notes_dirty=1"
                ).fetchone()[0]
                # 4) active 事实簇存量：无证据行不能再次进入摘要/合并 LLM。
                unanchored_facts = conn.execute(
                    "SELECT COUNT(*) FROM cluster_facts "
                    "WHERE COALESCE(status,'active')='active' "
                    "AND TRIM(COALESCE(evidence_ids,''))=''"
                ).fetchone()[0]
                contaminated_summaries = conn.execute(
                    "SELECT COUNT(*) FROM fact_clusters fc "
                    "WHERE TRIM(COALESCE(fc.summary,''))!='' AND EXISTS ("
                    "SELECT 1 FROM cluster_facts cf "
                    "WHERE cf.cluster_id=fc.id "
                    "AND COALESCE(cf.status,'active')='active' "
                    "AND TRIM(COALESCE(cf.evidence_ids,''))=''"
                    ")"
                ).fetchone()[0]
                # 5) 可信记忆的关系化证据必须存在且绑定同一主体/作用域。
                # 这些约束平时应为 0；这里只读观测，不在健康检查里修数据。
                evidence_missing_relation = conn.execute(
                    "SELECT COUNT(*) FROM memories m "
                    "WHERE COALESCE(m.status,'active')='active' "
                    "AND COALESCE(m.trust_level,'legacy_unverified') IN "
                    "('verified','manual','corrected') "
                    "AND TRIM(COALESCE(m.evidence_ids,''))<>'' "
                    "AND NOT EXISTS (SELECT 1 FROM memory_evidence e "
                    "WHERE e.memory_id=m.id)"
                ).fetchone()[0]
                nonself_bot_evidence = conn.execute(
                    "SELECT COUNT(DISTINCT m.id) FROM memories m "
                    "JOIN memory_evidence e ON e.memory_id=m.id "
                    "JOIN chat_log c ON c.id=e.chat_id "
                    "WHERE COALESCE(m.status,'active')='active' "
                    "AND COALESCE(m.trust_level,'legacy_unverified') IN "
                    "('verified','manual','corrected') "
                    "AND COALESCE(m.origin,'')<>'self' AND c.is_bot_reply=1"
                ).fetchone()[0]
                nonself_subject_mismatch = conn.execute(
                    "SELECT COUNT(DISTINCT m.id) FROM memories m "
                    "JOIN memory_evidence e ON e.memory_id=m.id "
                    "JOIN chat_log c ON c.id=e.chat_id "
                    "WHERE COALESCE(m.status,'active')='active' "
                    "AND COALESCE(m.trust_level,'legacy_unverified') IN "
                    "('verified','manual','corrected') "
                    "AND COALESCE(m.origin,'')<>'self' "
                    "AND TRIM(COALESCE(m.qq_id,''))<>'' "
                    "AND TRIM(COALESCE(c.qq_id,''))<>'' "
                    "AND m.qq_id<>c.qq_id"
                ).fetchone()[0]
                nonself_scope_mismatch = conn.execute(
                    "SELECT COUNT(DISTINCT m.id) FROM memories m "
                    "JOIN memory_evidence e ON e.memory_id=m.id "
                    "JOIN chat_log c ON c.id=e.chat_id "
                    "WHERE COALESCE(m.status,'active')='active' "
                    "AND COALESCE(m.trust_level,'legacy_unverified') IN "
                    "('verified','manual','corrected') "
                    "AND COALESCE(m.origin,'')<>'self' "
                    "AND TRIM(COALESCE(m.source_group_id,''))<>"
                    "TRIM(COALESCE(c.group_id,''))"
                ).fetchone()[0]
                self_binding_mismatch = conn.execute(
                    "SELECT COUNT(DISTINCT m.id) FROM memories m "
                    "JOIN memory_evidence e ON e.memory_id=m.id "
                    "JOIN chat_log c ON c.id=e.chat_id "
                    "WHERE COALESCE(m.status,'active')='active' "
                    "AND COALESCE(m.trust_level,'legacy_unverified') IN "
                    "('verified','manual','corrected') "
                    "AND COALESCE(m.origin,'')='self' AND ("
                    "TRIM(COALESCE(m.target_qq,''))<>TRIM(COALESCE(c.qq_id,'')) "
                    "OR COALESCE(c.is_bot_reply,0)<>1 "
                    "OR TRIM(COALESCE(m.source_group_id,''))<>"
                    "TRIM(COALESCE(c.group_id,'')))"
                ).fetchone()[0]
                orphan_evidence = conn.execute(
                    "SELECT COUNT(*) FROM memory_evidence e "
                    "LEFT JOIN memories m ON m.id=e.memory_id "
                    "LEFT JOIN chat_log c ON c.id=e.chat_id "
                    "WHERE m.id IS NULL OR c.id IS NULL"
                ).fetchone()[0]
            return (
                bad, total, untrusted_active, syn, stale_dirty, unanchored_facts,
                contaminated_summaries, evidence_missing_relation,
                nonself_bot_evidence, nonself_subject_mismatch,
                nonself_scope_mismatch, self_binding_mismatch, orphan_evidence,
            )

        (
            bad, total, untrusted_active, syn, stale_dirty, unanchored_facts,
            contaminated_summaries, evidence_missing_relation,
            nonself_bot_evidence, nonself_subject_mismatch,
            nonself_scope_mismatch, self_binding_mismatch, orphan_evidence,
        ) = await _store_io(
            handler, "health.memory_truth", _read_truth,
        )
        msgs = []
        if bad > 0:
            msgs.append(f"{bad} 个画像含元话术前缀")
        if total > 500 and syn / total > 0.3:
            msgs.append(f"合成行占比 {syn}/{total} 过高")
        if untrusted_active > 0:
            msgs.append(
                f"{untrusted_active} 条 active 记忆缺少可验证信任等级，已排除自动召回"
            )
        if stale_dirty > 0:
            msgs.append(f"{stale_dirty} 个 dirty 画像未清算")
        if unanchored_facts > 0:
            msgs.append(f"{unanchored_facts} 条事实簇原子事实无证据")
        if contaminated_summaries > 0:
            msgs.append(f"{contaminated_summaries} 个事实簇摘要含无证据存量")
        if evidence_missing_relation > 0:
            msgs.append(f"{evidence_missing_relation} 条可信记忆缺少关系化证据")
        if nonself_bot_evidence > 0:
            msgs.append(f"{nonself_bot_evidence} 条外部记忆绑定了糖糖回复")
        if nonself_subject_mismatch > 0:
            msgs.append(f"{nonself_subject_mismatch} 条外部记忆主体与证据不一致")
        if nonself_scope_mismatch > 0:
            msgs.append(f"{nonself_scope_mismatch} 条外部记忆作用域与证据不一致")
        if self_binding_mismatch > 0:
            msgs.append(f"{self_binding_mismatch} 条自我记忆目标或回复绑定异常")
        if orphan_evidence > 0:
            msgs.append(f"{orphan_evidence} 条证据关系指向不存在的记忆或消息")
        if msgs:
            return {"status": "warn", "message": "；".join(msgs)}
        return {
            "status": "ok",
            "message": (
                f"真值卫生正常（合成 {syn}/{total}，dirty {stale_dirty}，"
                "事实簇无证据 0，证据绑定 0）"
            ),
        }
    except Exception as e:
        return {"status": "warn", "message": f"检查真值卫生失败: {e}"}

async def _check_feedback_pipeline(handler) -> dict:
    """批 5：feedback 表是否有实际消费——只有写入没有消费是孤儿管道。
    每日报告的健康时间戳与反思 id cursor 必须分离。"""
    try:
        store = handler.memory.store
        stats = await _store_io(
            handler, "health.feedback.stats_7d", store.get_feedback_stats, days=7,
        )
        current = await _store_io(
            handler, "health.feedback.stats_1d", store.get_feedback_stats, days=1,
        )
        last_report = await _store_io(
            handler, "health.feedback.last_reported", store.kv_get,
            "feedback:last_reported",
        )
        if not last_report:
            # 2026-08-16 Codex M3：表里根本没数据时不告警（无数据可消费是正常的）
            if stats["total"] == 0:
                return {"status": "ok", "message": "feedback 表暂无数据（无消费需求）"}
            return {"status": "warn", "message": f"feedback 有 {stats['total']} 条数据但无消费记录（孤儿管道风险）"}
        coverage = current.get("verification_rate", 0)
        current_summary = (
            f"近1日 {current['all_total']} 条，可信 {current['verified_total']} 条，"
            f"可信覆盖率 {coverage:.0%}"
            if current["all_total"]
            else "近1日无新反馈"
        )
        message = (
            f"feedback 已消费（7日 {stats['all_total']} 条，"
            f"可信 {stats['verified_total']} 条；{current_summary}）"
        )
        if current["all_total"] >= 10 and coverage < 0.5:
            return {"status": "warn", "message": message}
        try:
            consumed_at = datetime.strptime(last_report, "%Y-%m-%d %H:%M")
            if datetime.now() - consumed_at > timedelta(hours=48):
                return {"status": "warn", "message": message + "；消费记录超过48小时"}
        except (TypeError, ValueError):
            return {"status": "warn", "message": message + "；消费时间格式无效"}
        return {"status": "ok", "message": message}
    except Exception as e:
        return {"status": "warn", "message": f"检查 feedback 消费失败: {e}"}

async def _check_reinforce_cooldown(handler) -> dict:
    """reinforce冷却是否生效——最高recall_count是否停止增长"""
    try:
        import sqlite3 as _sqlite3
        # 2026-08-10 收口：走 Store 方法
        max_rc = await _store_io(
            handler, "health.reinforce.max_recall", handler.memory.store.get_max_recall_count,
        )
        # 检查 kv_store 中是否有上次记录
        prev = await _store_io(
            handler, "health.reinforce.previous", handler.memory.store.kv_get,
            "health:last_max_recall_count",
        )
        if prev:
            prev_max = int(prev)
            if max_rc > prev_max + 5:
                await _store_io(
                    handler, "health.reinforce.update_previous", handler.memory.store.kv_set,
                    "health:last_max_recall_count", str(max_rc),
                )
                return {"status": "warn", "message": f"recall_count 最高值仍在增长({prev_max}→{max_rc})——冷却可能失效"}
        await _store_io(
            handler, "health.reinforce.update_previous", handler.memory.store.kv_set,
            "health:last_max_recall_count", str(max_rc),
        )
        return {"status": "ok", "message": f"recall_count 最高值稳定 ({max_rc})"}
    except Exception as e:
        return {"status": "warn", "message": f"检查reinforce冷却失败: {e}"}

# ── 用户覆盖 ──

async def _check_owner_memories(handler) -> dict:
    """主人记忆数"""
    import sqlite3 as _sqlite3
    try:
        owner = getattr(handler, 'owner_qq', '')
        if not owner:
            return {"status": "ok", "message": "未配置主人QQ"}
        # 2026-08-10 收口：走 Store 方法
        cnt = await _store_io(
            handler, "health.owner_memories", handler.memory.store.count_memories_for,
            owner,
        )
        if cnt == 0:
            # ⚠ 2026-09-20：这里原先把**主人的 QQ 号**写进了 message，而 message
            #   会经 SystemHealth.summary() 进 `/状态` 的输出——`/状态` 的权限是
            #   「主人**或群主**」（handler_commands 的 _check_group_owner）。
            #   也就是说糖糖被拉进陌生群后，群主发一条 `/状态` 就能读到主人的 QQ 号
            #   和一堆内部计数。真值只进日志（群里看不到），对外只留结论。
            # [!] 级别必须是 INFO 不能是 DEBUG：本项目 `logging.basicConfig(level=INFO)`，
            #   debug 会被**直接丢掉**——那注释里说的"真值只进日志"就变成了"哪儿都没有"
            #   （Reality Check C1 实测）。排障时"到底是哪个 QQ 号"是第一个要问的。
            #   日志文件在本机、且 `*.log` 已进 .gitignore，放这里不外泄。
            logger.info("主人记忆数为 0（owner=%s）——最严重的系统失败", owner)
            if _in_warmup():
                # 刚装好、还没聊过天 —— 0 是社会事实，不是故障
                return {"status": "ok", "message": "主人记忆待累积（刚启动，还没有足够对话）"}
            return {"status": "error", "message": "主人记忆数为 0——最严重的系统失败"}
        if cnt < 10:
            return {"status": "warn", "message": f"主人记忆仅 {cnt} 条——管道刚恢复或仍阻塞"}
        return {"status": "ok", "message": f"主人记忆 {cnt} 条"}
    except Exception as e:
        return {"status": "warn", "message": f"检查主人记忆失败: {e}"}

async def _check_zero_memory_users(handler) -> dict:
    """聊>50次、直接互动>=5次但零记忆的用户数"""
    import sqlite3 as _sqlite3
    try:
        # 2026-08-10 收口：走 Store 方法
        cnt = await _store_io(
            handler, "health.zero_memory_users", handler.memory.store.count_zero_memory_users,
            min_chats=50,
        )
        if cnt > 5:
            return {"status": "warn", "message": f"{cnt} 个直接互动用户零记忆——有人在被遗忘"}
        return {"status": "ok", "message": f"零记忆直接互动用户 {cnt} 人"}
    except Exception as e:
        return {"status": "warn", "message": f"检查零记忆用户失败: {e}"}

async def _check_profile_coverage(handler) -> dict:
    """有记忆但无画像的用户"""
    import sqlite3 as _sqlite3
    try:
        # 2026-08-10 收口：走 Store 方法
        people = await _store_io(
            handler, "health.profile_coverage", handler.memory.store.get_people_with_memories_gt,
            min_count=10, empty_notes=True, limit=10000,
        )
        cnt = len(people)
        if cnt > 10:
            return {"status": "warn", "message": f"{cnt} 个用户有>10条记忆但无画像——画像合成未触发"}
        return {"status": "ok", "message": f"缺少画像: {cnt} 人"}
    except Exception as e:
        return {"status": "warn", "message": f"检查画像覆盖失败: {e}"}

# ── 存储层 ──

async def _check_embedding_coverage(handler) -> dict:
    """embedding覆盖率"""
    import sqlite3 as _sqlite3
    try:
        # 2026-08-10 收口：走 Store 方法
        total, with_emb = await _store_io(
            handler, "health.embedding_coverage", handler.memory.store.count_embedding_coverage,
        )
        if total > 0:
            ratio = with_emb / total
            if ratio < 0.8:
                return {"status": "warn", "message": f"embedding覆盖率仅 {ratio:.0%}——去重和语义搜索降级"}
        return {"status": "ok", "message": f"embedding覆盖率 {with_emb}/{total}"}
    except Exception as e:
        return {"status": "warn", "message": f"检查embedding失败: {e}"}

async def _check_semantic_ready(handler) -> dict:
    """语义模型（BGE / 重排序）到底加载上没有（2026-09-20 加）。

    ⚠ 这条检查补的是一个**整层缺席**：原先 21 项健康检查里没有一项碰模型层
    （`embed_engine` / `reranker` 全仓零引用），而 `_check_embedding_coverage`
    在空库时 `total==0` 直接返回 ok。于是 BGE 没加载起来时——
    记忆去重、chat_index 语义搜索、贴图语义兜底、自忆匹配、
    知识库 hybrid+RRF+rerank **全部**退化成关键词/时间序——
    而 `/状态` 依然报「🟢 全部正常」。

    这正是本项目自己的话：「日志说降级不影响、实际影响全功能」。
    降级链设计得再好，也得有人告诉用户「现在走的是降级链」。
    """
    from . import installed as _installed
    try:
        embed = getattr(handler, "embed_engine", None)
        rerank = getattr(handler, "reranker", None)
        embed_ready = bool(getattr(embed, "ready", False))
        rerank_ready = bool(getattr(rerank, "ready", False))
        rerank_err = str(getattr(rerank, "_last_load_error", "") or "")

        if not embed_ready:
            note = _installed.missing_note(
                _installed.FEAT_MEMORY, "BGE 语义向量模型")
            return {"status": "error",
                    "message": ("语义向量模型没加载——记忆去重、语义检索、知识库混合检索"
                                f"全部退化成关键词匹配。{note}")}
        if not rerank_ready:
            why = f"（{rerank_err}）" if rerank_err else ""
            note = _installed.missing_note(
                _installed.FEAT_MEMORY, "重排序模型", optional=True)
            return {"status": "warn",
                    "message": f"重排序没加载{why}——召回质量下降，聊天本身不受影响。{note}"}
        return {"status": "ok", "message": "语义模型就绪（BGE + 重排序）"}
    except Exception as e:
        return {"status": "warn", "message": f"检查语义模型失败: {e}"}

async def _check_cleanup_ran(handler) -> dict:
    """陈旧清理是否在最近24小时内执行过"""
    try:
        last = await _store_io(
            handler, "health.cleanup_date", handler.memory.store.kv_get,
            "health:last_cleanup_date",
        )
        today = _today()
        if last != today:
            if not last and _in_warmup():
                # 首次运行：清理任务还没轮到第一次（它自己也要等启动流程走完）
                return {"status": "ok", "message": "陈旧清理待首次执行（刚启动）"}
            return {"status": "warn", "message": f"上次陈旧清理: {last or '从未'}——可能未执行"}
        return {"status": "ok", "message": "陈旧清理今日已执行"}
    except Exception:
        return {"status": "ok", "message": "无法检查清理状态"}

async def _check_episodes(handler) -> dict:
    """episodes表是否有数据——P2.4是否生效"""
    try:
        # 2026-08-10 收口：走 Store 方法
        cnt, today_cnt = await _store_io(
            handler, "health.episodes", handler.memory.store.count_episodes,
        )
        if cnt == 0:
            # 新系统刚接上，可能还没有episodic记忆积累
            return {"status": "ok", "message": "episodes 表为空——等待 episodic 记忆积累（新接入功能）"}
        return {"status": "ok", "message": f"episodes: {cnt} 个事件（今日+{today_cnt}）"}
    except Exception as e:
        return {"status": "warn", "message": f"检查episodes失败: {e}"}

# ── 系统运行 ──

async def _check_extraction_state_file(handler) -> dict:
    """extraction_state.json是否存在且可写"""
    import os, json
    try:
        path = getattr(handler, '_EXTRACTION_STATE_FILE',
                       str(state_path(".extraction_state.json")))
        exists = await run_bounded_blocking(
            "health.extraction_state_exists",
            os.path.exists,
            path,
            logger=logger,
            log_prefix="🩺 提取状态文件检查较慢",
        )
        if exists:
            def _read_state():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)

            await run_bounded_blocking(
                "health.extraction_state_read",
                _read_state,
                logger=logger,
                log_prefix="🩺 提取状态文件读取较慢",
            )
            return {"status": "ok", "message": "extraction_state.json 正常"}
        # 文件不存在——第一次运行，正常
        return {"status": "ok", "message": "extraction_state.json 待首次提取后创建"}
    except json.JSONDecodeError:
        return {"status": "error", "message": "extraction_state.json 损坏——重启后提取进度将丢失"}
    except Exception as e:
        return {"status": "warn", "message": f"extraction_state.json 异常: {e}"}

async def _check_autonomous_loop(handler) -> dict:
    """自治循环是否在运行"""
    last_flush = getattr(handler, '_last_metrics_flush', 0)
    now_ts = datetime.now().timestamp()
    gap = now_ts - last_flush if last_flush else 9999
    if gap > 1800:  # 30分钟
        return {"status": "warn", "message": f"自治循环可能停止——上次flush {gap/60:.0f}分钟前"}
    return {"status": "ok", "message": f"自治循环活跃（{gap/60:.0f}分钟前flush）"}

# ── 清理执行后更新标记 ──

def mark_cleanup_ran(handler):
    """在 cleanup_stale_memories 执行后调用，更新健康检查标记"""
    try:
        handler.memory.store.kv_set("health:last_cleanup_date", _today())
    except Exception:
        pass

# ── 观察器缺席告警（2026-10-14 台账 T3.1）─────────────────────────────
# 审计：「把 tools/runtime_observer.py --once 挂到现有调度；停跑 ≥2 个周期触发缺席告警」。
#
# 设计取舍：**不把 3361 行的观察器塞进事件循环**。它自己带一个阻塞的
# `time.sleep` 采样循环（--interval-seconds 默认 300，--duration-hours 默认 2），
# 进程内跑会占住事件循环。所以这里只做两件事：
#   ① 记录「上次观察器成功运行」的时间戳（落 kv，重启不丢）；
#   ② 用**健康查询方法**判断现在是否已经「缺席」≥2 个周期。
# 观察器本身仍由运维/主人手动或计划任务在进程外跑（--once 一次就够）。
#
# 为什么这不违「不要动」条款：本检查只读，不改任何状态机（审计：health_check 只读）。
OBSERVER_HEARTBEAT_KEY = 'health:observer_last_run'
OBSERVER_ABSENCE_CYCLES = 2          # 缺席 ≥2 个周期才算告警
OBSERVER_MAX_AGE_SECONDS = 3600.0    # 一个周期的定义：1 小时（观察器快照间隔 5 分钟，余量充足）

async def _check_observer_absence(handler) -> dict:
    """观察器是否在跑——停跑 ≥2 个周期报 alert。

    这条检查是「观察器挂计划任务」的另一半：**没它的话，观察器停了没人知道**，
    而观察器的作用恰恰是发现别的东西停了。

    **冷启动豁免**：从未跑过 + 进程刚起 → 不报（那不是缺席，是还没轮到）。
    但**跑过又停**必须报——哪怕在宽限期内。这两件事性质不同：
    「还没开始」是预期，「开始后停了」是故障。
    """
    raw = None
    try:
        raw = handler.memory.store.kv_get(OBSERVER_HEARTBEAT_KEY)
    except Exception as e:
        return {"status": "warn",
                "message": f"观察器心跳读取失败: {type(e).__name__}"}

    if not raw:
        if _in_warmup():
            return {"status": "ok", "message": "观察器尚未运行（启动宽限期内，属预期）"}
        return {"status": "warn",
                "message": "观察器从未运行过——生产观测处于盲区"}

    try:
        last = float(raw)
    except (TypeError, ValueError):
        return {"status": "warn", "message": "观察器心跳时间戳无法解析"}

    age = time.time() - last
    cycles = age / OBSERVER_MAX_AGE_SECONDS
    if cycles >= OBSERVER_ABSENCE_CYCLES:
        return {
            "status": "error",
            "message": (f"观察器已缺席 {cycles:.1f} 个周期（{age / 60:.0f} 分钟未运行）"
                        f"——生产观测中断，异常将不再被发现"),
        }
    return {"status": "ok",
            "message": f"观察器活跃（{age / 60:.0f} 分钟前运行）"}

# ── 管线积压健康（2026-10-14 台账 T3.1）─────────────────────────────
# 审计 T3.1 的第二个完成标准：「23 条『观察器/健康查询』中 ≥6 条获得生产调用点」。
#
# 为什么挑这 6 条：它们全是**无参或近无参的聚合计数**，
# 恰好构成一张「各条管线有没有积压」的体检表——
# 而这张表的读者，正是定时健康检查。写好了没人问，就是死代码；
# 接进来，它们立刻从死代码变成活信号。
#
# 审计明令 health_check **只读**：这里全部只调查询，不改任何状态、不触发重放。
async def _check_pipeline_backlog(handler) -> dict:
    """各条管线的积压体检（6 条健康查询的生产消费者）。

    返回 warn 而不是 error：**积压是「需要看」不是「坏了」**。
    真坏了由各管线的专属检查负责（extraction_queue / stale_extraction 等）。
    本项负责的是它们之间的**横向对照**——单看一条管线像正常，
    六条一起看才看得出「都在涨」。
    """
    store = handler.memory.store

    def _collect():
        out = {}
        out["decision_run"] = store.get_decision_run_health()
        out["inbound_event"] = store.get_inbound_event_health()
        out["proactive_event"] = store.get_proactive_event_health()
        out["receipt_mailbox"] = store.get_action_receipt_mailbox_health()
        out["quarantined"] = store.count_quarantined_chat_logs()
        out["memories_today"] = store.count_memories_today()
        out["backlog"] = store.get_persisted_unprocessed_backlog()
        return out

    try:
        data = await _store_io(handler, "health.pipeline_backlog", _collect)
    except Exception as e:
        return {"status": "warn",
                "message": f"管线积压查询失败: {type(e).__name__}: {e}"}

    backlog = data.get("backlog") or []
    backlog_total = sum(int(n) for _, n, _ in backlog) if backlog else 0
    users_with_backlog = len(backlog)

    # 未知/损坏的决策回合是**真的**要看的——它意味着状态机里有卡住的行。
    decision = data.get("decision_run") or {}
    unknown = int(decision.get("unknown", 0) or 0) + int(decision.get("corrupt", 0) or 0)
    receipt = data.get("receipt_mailbox") or {}
    receipt_open = int(receipt.get("open", 0) or 0)
    quarantined = int(data.get("quarantined", 0) or 0)

    # 2026-10-14 T30：把「读数」与「判据」分开。
    # 原实现把四类读数一视同仁地 OR 进 warn，结果**同一个 579 在启动日志里被两行
    # 检查给出相反结论**：本项说 warn，而 _check_stale_extraction 说 ok（它看
    # eligible_messages：到期为 0 即正常——实测 584 条全在等 7 天/21 天时间阈值，
    # eligible=0）。判「到期」需要 eligible/deferred 拆分，那是 stale_extraction 的
    # 职责；本项拿的是原始计数，看不到拆分，凭它告警必然与那行永远矛盾
    # （一处规则两份实现各自漂移，同 T10 发布扫描器 / T25 字符类）。
    #
    # 判据的界：**与基线无关、非 0 即为错** 的才进 parts；
    # 存量数字进 context——冻住的存量会在每次启动都报一次 warn，那不是告警是噪音。
    parts = []      # 本项能独立判定的异常
    context = []    # 只作横向对照的读数（存量而非增量）
    if backlog_total:
        context.append(
            f"未提取积压 {backlog_total} 条/{users_with_backlog} 人（到期判定归 stale_extraction）"
        )
    if unknown:
        parts.append(f"未知决策回合 {unknown}")
    if receipt_open:
        parts.append(f"回执信箱未结 {receipt_open}")
    if quarantined:
        # 实测 14065 条全部来自 2026-08-28 03:30:05 的**一次批量隔离**，近 7 天新增 0。
        context.append(f"隔离聊天记录 {quarantined}")

    today = int(data.get("memories_today", 0) or 0)
    reading = "；".join(context)
    suffix = f"（读数：{reading}）" if reading else ""
    if not parts:
        return {"status": "ok",
                "message": f"各管线无异常{suffix}（今日记忆 {today} 条）"}
    return {"status": "warn",
            "message": "；".join(parts) + suffix + f"（今日记忆 {today} 条）"}

