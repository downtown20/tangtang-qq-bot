"""
对话状态追踪器 —— 糖糖知道什么时候该说话、什么时候该闭嘴

三种状态：
  🟢 对话中  —— 被@/叫名字后激活，60 秒内自然回复
  🟡 旁观    —— 默认状态，只听不说
  🔴 退让    —— 检测到别人在聊天时不硬插

核心原则：
  - 状态切换用规则（谁@谁、窗口时间）——快、确定
  - 该不该回用 LLM 判断——不硬编码，具体情况具体分析
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

from agent.async_io import run_bounded_blocking, run_bounded_store_io

logger = logging.getLogger("糖糖.Conversation")

ENGAGED_WINDOW = 120      # 对话窗口秒数——允许自然的对话停顿
GRACE_PERIOD = 10           # 容差秒数
FADE_PERIOD = 300           # 窗口过期后 5 分钟内仍给部分加分（渐变退出）
MAX_CONSECUTIVE = 5        # 连续回复上限
MAX_LLM_SILENCES = 2       # LLM 连续两次选择沉默后，窗口进入渐变期
MAX_ACTIVE_WINDOWS = 5000  # 软上限；只回收已完成 FADE 的窗口，不驱逐活跃会话


class ConversationTracker:
    """追踪群聊中的对话参与状态"""

    def __init__(self, handler):
        self.h = handler
        # 2026-08-16 跨群窗口键修复：此前按 user_id 单键——同一人在 B 群互动会
        # 顶掉 A 群的窗口（现场：主人在 88888888 发了条 @ → 顶掉 77777777
        # 的窗口 → 30 秒前的追问被当插话 92/93 跳过）。改为 (group_id, user_id)。
        self._engaged: dict[tuple, dict] = {}
        self._auto_initiated: dict[str, float] = {}  # {group_id: 自治消息发送时间}
        self._durable_store = None
        self._durable_cursor = 0
        self._durable_invalid_events = 0
        self._durable_private_dirty = False
        # 事件循环内清理过期窗口时只摘除状态，摘要计算排队到有界线程池。
        self._pending_window_summaries = deque()
        self._pending_summary_task = None
        # 记录本回合已完成 durable 读穿的任务，后续同步只读方法复用该快照。
        self._durable_sync_task = None

    @staticmethod
    def _key(group_id: str, user_id: str) -> tuple:
        return (str(group_id), str(user_id))

    # ═══════════════════════════════════════
    # 公开接口
    # ═══════════════════════════════════════

    def bind_durable_store(self, store) -> None:
        """绑定 ADR-003 窗口真值并从同一快照重建缓存。"""
        self._durable_store = store
        if not hasattr(self, "_private_windows"):
            self._private_windows = {}
        since = (
            datetime.now() - timedelta(
                seconds=ENGAGED_WINDOW + GRACE_PERIOD + FADE_PERIOD,
            )
        ).strftime("%Y-%m-%d %H:%M:%S")
        try:
            snapshot = store.get_conversation_window_rebuild_snapshot(
                since, private_per_user=5,
            )
            for event in snapshot.get("events", []):
                event_id = self._coerce_durable_event_id(event)
                if event_id is None:
                    continue
                self._apply_durable_window_event(event)
                self._durable_cursor = max(self._durable_cursor, event_id)
            self._durable_cursor = max(0, int(snapshot.get("cursor", 0)))
            if self._durable_private_dirty:
                self._save_state()
                self._durable_private_dirty = False
        except Exception as exc:
            logger.warning(
                "durable 窗口启动恢复失败，保留现有缓存稍后读穿: %s",
                type(exc).__name__,
            )

    def _sync_durable_events(self) -> None:
        """按 high-water 增量读穿；失败时不移动 cursor、不破坏现有窗口。"""
        store = self._durable_store
        if store is None:
            return
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if current_task is not None and current_task is self._durable_sync_task:
            return
        try:
            while True:
                events = store.list_conversation_window_events_after(
                    self._durable_cursor, limit=200,
                )
                if not events:
                    break
                for event in events:
                    event_id = self._coerce_durable_event_id(event)
                    if event_id is None:
                        # reader 合同之外的坏行不能让整个增量批次卡死；
                        # 以一个保守的 synthetic high-water 占位跳过它。
                        self._durable_cursor += 1
                        continue
                    self._apply_durable_window_event(event)
                    self._durable_cursor = max(self._durable_cursor, event_id)
                if len(events) < 200:
                    break
        except Exception as exc:
            logger.warning(
                "durable 窗口增量读取失败，保留 cursor 下次重试: %s",
                type(exc).__name__,
            )
        finally:
            if self._durable_private_dirty:
                self._save_state()
                self._durable_private_dirty = False

    async def _sync_durable_events_async(self) -> None:
        """异步读穿 durable 窗口事件；Store I/O 不占用消息事件循环。"""
        store = self._durable_store
        if store is None:
            return
        try:
            while True:
                events = await run_bounded_store_io(
                    "conversation_tracker.list_window_events",
                    store.list_conversation_window_events_after,
                    self._durable_cursor,
                    limit=200,
                    logger=logger,
                    log_prefix="durable 窗口增量读取较慢",
                )
                if not events:
                    break
                for event in events:
                    event_id = self._coerce_durable_event_id(event)
                    if event_id is None:
                        self._durable_cursor += 1
                        continue
                    self._apply_durable_window_event(event)
                    self._durable_cursor = max(self._durable_cursor, event_id)
                if len(events) < 200:
                    break
        except Exception as exc:
            logger.warning(
                "durable 窗口异步增量读取失败，保留 cursor 下次重试: %s",
                type(exc).__name__,
            )
        finally:
            if self._durable_private_dirty:
                self._save_state()
                self._durable_private_dirty = False

    def _coerce_durable_event_id(self, event: dict) -> int | None:
        try:
            raw_id = event["id"]
            if isinstance(raw_id, bool):
                raise ValueError("boolean durable event id")
            event_id = int(raw_id)
            if event_id <= 0:
                raise ValueError("non-positive durable event id")
            return event_id
        except (KeyError, TypeError, ValueError, OverflowError):
            self._durable_invalid_events += 1
            logger.error(
                "隔离无效 durable 窗口事件 id，cursor 跳过: event_id=%s",
                event.get("id", "?") if isinstance(event, dict) else "?",
            )
            return None
    def _apply_durable_window_event(self, event: dict) -> bool:
        """应用一个已确认事件；不用即时 API，时间只取 occurred_at。"""
        try:
            event_id = int(event["id"])
            action_id = str(event["domain_action_id"] or "").strip()
            channel = str(event["channel"] or "")
            actor_kind = str(event["actor_kind"] or "")
            user_id = str(event["conversation_user_id"] or "").strip()
            group_id = str(event.get("group_id") or "")
            scope_id = str(event["scope_id"] or "")
            reply_text = str(event.get("reply_text") or "")[:200]
            occurred_at = datetime.strptime(
                str(event["occurred_at"]), "%Y-%m-%d %H:%M:%S",
            ).timestamp()
            if occurred_at > time.time() + 60:
                raise ValueError("future durable window event")
            if event_id <= 0 or not action_id or actor_kind != "bot" or not user_id:
                raise ValueError("invalid durable window identity")
            if channel == "group":
                if not group_id or scope_id != group_id:
                    raise ValueError("group durable window scope mismatch")
                expires_at = occurred_at + ENGAGED_WINDOW
                if time.time() > expires_at + FADE_PERIOD:
                    return True
                key = self._key(group_id, user_id)
                window = self._engaged.get(key)
                if not window:
                    window = {
                        "expires_at": expires_at,
                        "count": 0,
                        "llm_silence_count": 0,
                        "group_id": group_id,
                        "my_replies": [],
                        "their_msgs": [],
                        "topic": "",
                        "durable_event_ids": [],
                    }
                    self._engaged[key] = window
                durable_ids = window.setdefault("durable_event_ids", [])
                if event_id in durable_ids:
                    return True
                if event_id not in durable_ids:
                    durable_ids.append(event_id)
                    window["durable_event_ids"] = durable_ids[-256:]
                    window["count"] = int(window.get("count", 0)) + 1
                    window["expires_at"] = max(
                        float(window.get("expires_at", 0)), expires_at,
                    )
                    window["llm_silence_count"] = 0
                    if reply_text:
                        window.setdefault("my_replies", []).append(reply_text)
                        window["my_replies"] = window["my_replies"][-3:]
                return True

            if channel == "private":
                if group_id or scope_id != f"_private_{user_id}":
                    raise ValueError("private durable window scope mismatch")
                window = self._private_windows.get(user_id)
                if not window:
                    window = {
                        "my_replies": [], "their_msgs": [], "summary": "",
                        "msg_since_summary": 0, "last_active": 0,
                        "durable_event_ids": [],
                    }
                    self._private_windows[user_id] = window
                durable_ids = window.setdefault("durable_event_ids", [])
                if event_id in durable_ids:
                    return True
                if event_id not in durable_ids:
                    durable_ids.append(event_id)
                    window["durable_event_ids"] = durable_ids[-100:]
                    if reply_text:
                        window.setdefault("my_replies", []).append(reply_text)
                        window["my_replies"] = window["my_replies"][-5:]
                    window["msg_since_summary"] = int(
                        window.get("msg_since_summary", 0)
                    ) + 1
                    window["last_active"] = max(
                        float(window.get("last_active", 0)), occurred_at,
                    )
                    self._durable_private_dirty = True
                return True
            raise ValueError("unsupported durable window channel")
        except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
            # 损坏/越界事件隔离后继续推进 high-water cursor，避免一条坏记录
            # 永久卡住其后的全部窗口事件；计数可供运行时健康检查取证。
            self._durable_invalid_events += 1
            logger.error(
                "隔离损坏的 durable 窗口事件，cursor 跳过: event_id=%s error=%s",
                event.get("id", "?"), type(exc).__name__,
            )
            return True

    def on_reply_sent(self, user_id: str, group_id: str, reply_text: str = ""):
        """糖糖回复了某人——进入/续期对话中，记录回复文本"""
        now = time.time()
        key = self._key(group_id, user_id)
        existing = self._engaged.get(key)
        if existing:
            existing["expires_at"] = now + ENGAGED_WINDOW
            existing["count"] += 1
            existing["llm_silence_count"] = 0
        else:
            self._engaged[key] = {
                "expires_at": now + ENGAGED_WINDOW,
                "count": 1,
                "llm_silence_count": 0,
                "group_id": group_id,
                "my_replies": [],
                "their_msgs": [],
                "topic": "",
            }
        # 记录回复文本（最多保留 3 条）
        if reply_text:
            w = self._engaged[key]
            w.setdefault("my_replies", []).append(reply_text[:200])
            if len(w["my_replies"]) > 3:
                w["my_replies"] = w["my_replies"][-3:]
            # 系统只保存双方原文；是否接下一句话由 LLM 在该回合自主判断。
        self._cleanup()
        if len(self._engaged) > MAX_ACTIVE_WINDOWS:
            logger.warning(
                f"🪟 活跃窗口超过软上限 {MAX_ACTIVE_WINDOWS}，保留未完成窗口，"
                "等待其自然完成后回收"
            )

    def on_llm_silence(self, user_id: str, group_id: str) -> bool:
        """记录窗口内由 LLM 做出的沉默决定。

        连续两次沉默说明当前互动正在自然结束；系统只据此把窗口推进渐变期，
        不替 LLM 判断单条消息是否值得回复。返回本次是否发生状态转换。
        """
        w = self._engaged.get(self._key(group_id, user_id))
        if not w:
            return False
        w["llm_silence_count"] = int(w.get("llm_silence_count", 0)) + 1
        if w["llm_silence_count"] < MAX_LLM_SILENCES:
            return False
        # get_window_bonus 以 expires_at 的超时年龄区分全窗口(50)和渐变(65)。
        # 推到 GRACE_PERIOD 之外即可，不删除窗口，后续仍能按渐变规则自然重启。
        w["expires_at"] = min(w["expires_at"], time.time() - GRACE_PERIOD - 0.001)
        logger.info(
            f"🪟 连续沉默 {w['llm_silence_count']} 次，窗口进入渐变期 | "
            f"group={group_id} user={user_id}"
        )
        return True

    def on_llm_reply_decision(self, user_id: str, group_id: str):
        """LLM 选择回复会中断“连续沉默”，发送结果不影响决策统计。"""
        w = self._engaged.get(self._key(group_id, user_id))
        if w:
            w["llm_silence_count"] = 0

    def record_user_msg(self, user_id: str, group_id: str, text: str):
        """记录窗口内对方的消息（含容差期）"""
        if not self.is_engaged(user_id, group_id):
            return
        w = self._engaged.get(self._key(group_id, user_id))
        if w:
            w.setdefault("their_msgs", []).append(text[:200])
            if len(w["their_msgs"]) > 3:
                w["their_msgs"] = w["their_msgs"][-3:]

    def get_window_context(self, user_id: str, group_id: str) -> str:
        """获取窗口对话上下文——注入 LLM 以保持连贯。
        2026-08-16 范式转换：话题标签与语调指令已删（词频/关键词替 LLM 决策）——
        只给窗口原文与计数信号，聊什么、什么语气由 LLM 自己读原文判断。"""
        self._sync_durable_events()
        w = self._engaged.get(self._key(group_id, user_id))
        if not w:
            return ""
        parts = []
        if w.get("their_msgs"):
            parts.append("【对话窗口——对方最近说了】")
            for msg in w["their_msgs"][-3:]:
                parts.append(f"  「{msg}」")
        if w.get("my_replies"):
            parts.append("【你刚才回复了 ta】")
            for reply in w["my_replies"][-2:]:
                parts.append(f"  「{reply}」")
        # 情绪动量（互动频率——客观计数信号）
        mood = self._get_mood_guidance(group_id, user_id)
        if mood:
            parts.append(mood)
        return "\n".join(parts) if parts else ""

    def get_intimacy_bonus(self, user_id: str, group_id: str) -> float:
        """窗口互动频率→亲密额外加成。窗口内聊天密度高，关系应该涨更快。"""
        w = self._engaged.get(self._key(group_id, user_id))
        if not w:
            return 0.0
        count = w.get("count", 0)
        if count >= 8:
            return 0.03  # 深度对话
        elif count >= 4:
            return 0.02  # 中等对话
        elif count >= 2:
            return 0.01  # 简短对话
        return 0.005      # 一句话

    # ═══════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════

    def _get_mood_guidance(self, group_id: str, user_id: str) -> str:
        """情绪动量——基于窗口内的互动频率调整语调提示"""
        w = self._engaged.get(self._key(group_id, user_id))
        if not w:
            return ""
        count = w.get("count", 0)
        if count >= 5:
            return "对话很活跃——保持轻松自然的节奏，不必刻意。"
        elif count >= 2:
            return "对话在慢慢展开——不着急，自然地接话就好。"
        return ""

    def force_engage(self, user_id: str, group_id: str):
        """强制进入对话中——被@或叫名字时"""
        now = time.time()
        key = self._key(group_id, user_id)
        existing = self._engaged.get(key)
        if isinstance(existing, dict) and now <= float(
                existing.get("expires_at", 0) or 0
        ) + GRACE_PERIOD:
            # 重复 @ 是续期/重新唤醒，不是新建会话；保留窗口上下文，
            # 只清掉连续沉默计数，让本次显式呼叫重新获得 LLM 决策机会。
            existing["expires_at"] = now + ENGAGED_WINDOW
            existing["llm_silence_count"] = 0
            self._cleanup()
            return

        self._engaged[key] = {
            "expires_at": now + ENGAGED_WINDOW,
            "count": 0,
            "llm_silence_count": 0,
            "group_id": group_id,
            "my_replies": [],
            "their_msgs": [],
            "topic": "",
        }
        self._cleanup()
        if len(self._engaged) > MAX_ACTIVE_WINDOWS:
            logger.warning(
                f"🪟 活跃窗口超过软上限 {MAX_ACTIVE_WINDOWS}，保留未完成窗口，"
                "等待其自然完成后回收"
            )

    def is_engaged(self, user_id: str, group_id: str) -> bool:
        """检查是否处于对话中（含容差期）"""
        self._sync_durable_events()
        w = self._engaged.get(self._key(group_id, user_id))
        if not w:
            return False
        if time.time() > w["expires_at"] + GRACE_PERIOD:
            return False
        return True

    def has_active_window(self, group_id: str) -> bool:
        """2026-08-16 Codex I2：群内是否存在任何活跃窗口（自治抑制用）。
        替代外部遍历 _engaged——键已是 (group,user) 元组，外部拿 key 当
        user_id 传 is_engaged 会永远 False（自治在活跃对话中插话的事故）。"""
        self._sync_durable_events()
        now = time.time()
        return any(
            key[0] == str(group_id) and now <= w["expires_at"] + GRACE_PERIOD
            for key, w in self._engaged.items()
        )

    def get_window_state(self, user_id: str, group_id: str) -> dict:
        """2026-08-16 Codex I2：窗口状态公共读取——替代外部 _engaged.get
        （旧访问点用 user_id 单键，元组键下永远取空）。"""
        self._sync_durable_events()
        return self._engaged.get(self._key(group_id, user_id), {})

    def get_durable_health(self) -> dict:
        """返回 durable 窗口同步健康快照；不暴露消息正文或用户标识。"""
        self._sync_durable_events()
        return {
            "bound": self._durable_store is not None,
            "cursor": int(self._durable_cursor),
            "invalid_events": int(self._durable_invalid_events),
            "engaged_windows": len(self._engaged),
            "private_windows": len(getattr(self, "_private_windows", {})),
        }

    def get_window_bonus(self, user_id: str, group_id: str) -> tuple[bool, int]:
        """返回 (是否在窗口, 门槛分数)。全窗口=50, 渐变=65, 无=80"""
        self._sync_durable_events()
        key = self._key(group_id, user_id)
        w = self._engaged.get(key)
        if not w:
            return False, 80
        age = time.time() - w["expires_at"]
        if age <= GRACE_PERIOD:
            return True, 50
        elif age <= FADE_PERIOD:
            return True, 65
        else:
            # 窗口真正关闭——提取跨窗口记忆
            self._extract_window_summary(user_id, group_id)
            del self._engaged[key]
            return False, 80

    async def get_window_bonus_async(self, user_id: str, group_id: str) -> tuple[bool, int]:
        """异步获取窗口门槛；过期摘要的 BGE 编码移出事件循环。"""
        await self._sync_durable_events_async()
        self._durable_sync_task = asyncio.current_task()
        key = self._key(group_id, user_id)
        w = self._engaged.get(key)
        if not w:
            return False, 80
        age = time.time() - w["expires_at"]
        if age <= GRACE_PERIOD:
            return True, 50
        if age <= FADE_PERIOD:
            return True, 65

        # 先在事件循环中摘除快照，避免后台摘要线程与新窗口状态交叉写入。
        window = self._engaged.pop(key, None)
        if window:
            summary = await run_bounded_blocking(
                "conversation_tracker.extract_window_summary",
                self._summarize_window,
                window,
                logger=logger,
                log_prefix="窗口过期摘要提取较慢",
            )
            self._commit_window_summary(user_id, group_id, summary)
        return False, 80

    def _queue_window_summary(self, user_id: str, group_id: str, window: dict):
        """将过期窗口快照排队，并在有事件循环时启动后台摘要任务。"""
        self._pending_window_summaries.append((user_id, group_id, window))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._pending_summary_task
        if task is None or task.done():
            self._pending_summary_task = loop.create_task(
                self._drain_window_summaries()
            )

    async def _drain_window_summaries(self):
        """后台逐个收口过期摘要，避免清理任务挤占当前消息回合。"""
        while self._pending_window_summaries:
            user_id, group_id, window = self._pending_window_summaries[0]
            summary = await run_bounded_blocking(
                "conversation_tracker.cleanup_summary",
                self._summarize_window,
                window,
                logger=logger,
                log_prefix="窗口清理摘要提取较慢",
            )
            self._pending_window_summaries.popleft()
            self._commit_window_summary(user_id, group_id, summary)
            await asyncio.sleep(0)

    async def flush_pending_summaries(self):
        """优雅退出前等待已排队的窗口摘要，避免尾部关系状态丢失。"""
        task = self._pending_summary_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            await asyncio.shield(task)
        elif self._pending_window_summaries:
            await self._drain_window_summaries()

    def _summarize_window(self, w: dict) -> list[str]:
        """在线程池中执行窗口消息的 BGE 摘要计算，不修改追踪器状态。"""
        if not w or not hasattr(self.h, 'embed_engine') or not self.h.embed_engine:
            return []
        if not self.h.embed_engine.ready:
            return []
        # 收集双方消息（各 ≤10 条，共 ≤20 条）
        all_msgs = []
        for msg in w.get("their_msgs", [])[-10:]:
            if len(msg.strip()) >= 2 and msg.strip() not in ("嗯", "好", "哦", "知道了", "哈哈哈", "呵呵"):
                all_msgs.append(msg)
        for msg in w.get("my_replies", [])[-10:]:
            if len(msg.strip()) >= 2:
                all_msgs.append(msg)
        if len(all_msgs) < 2:
            return []
        # BGE 编码 → 取 top-3 最接近中心向量的句子
        try:
            import numpy as np
            encoded = [
                (msg, self.h.embed_engine.encode(msg[:200]))
                for msg in all_msgs
            ]
            encoded = [(msg, vector) for msg, vector in encoded if vector is not None]
            vectors = [vector for _, vector in encoded]
            if len(encoded) < 2:
                return []
            center = np.mean(vectors, axis=0)
            similarities = [
                (float(np.dot(vector, center)), msg)
                for msg, vector in encoded
            ]
            similarities.sort(key=lambda x: -x[0])
            return [s[1][:200] for s in similarities[:3]]
        except Exception:
            return []

    def _commit_window_summary(self, user_id: str, group_id: str, summary: list[str]):
        """在事件循环中提交已计算的窗口摘要。"""
        if not summary or not hasattr(self.h, 'self_state') or not self.h.self_state:
            return
        rel = self.h.self_state.relationships.get(user_id)
        if rel:
            rel.last_conversation = {
                "summary": summary,
                "timestamp": time.strftime("%Y-%m-%d %H:%M"),
                "group_id": group_id,
            }

    def _extract_window_summary(self, user_id: str, group_id: str, window=None):
        """窗口关闭时收集消息供 BGE 提取摘要。"""
        w = window if window is not None else self._engaged.get(
            self._key(group_id, user_id)
        )
        summary = self._summarize_window(w)
        self._commit_window_summary(user_id, group_id, summary)

    def should_yield(self, user_id: str, group_id: str) -> bool:
        """是否应该退让——这个人在跟别人说话，不是在跟糖糖说话"""
        # 不在对话中就不存在退让
        if not self.is_engaged(user_id, group_id):
            return False
        # 连续回复超过上限→退让
        key = self._key(group_id, user_id)
        w = self._engaged.get(key)
        if w and w["count"] >= MAX_CONSECUTIVE:
            logger.info(f"🤐 对话退让 [{user_id}]: 已连续回复 {w['count']} 条")
            del self._engaged[key]
            return True
        return False

    # ═══════════════════════════════════════
    # 私聊窗口——永久对话上下文
    # ═══════════════════════════════════════

    STATE_FILE = ".conversation_state.json"

    def init_private_windows(self):
        """从持久化恢复私聊窗口"""
        self._private_windows: dict[str, dict] = {}
        self._load_state()

    def on_private_reply(self, user_id: str, reply_text: str, user_msg: str = ""):
        """私聊回复——记录上下文，永不超时"""
        w = self._private_windows.get(user_id)
        if not w:
            w = {
                "my_replies": [],
                "their_msgs": [],
                "summary": "",
                "msg_since_summary": 0,
            }
            self._private_windows[user_id] = w
        w["my_replies"].append(reply_text[:200])
        if len(w["my_replies"]) > 5:
            w["my_replies"] = w["my_replies"][-5:]
        if user_msg:
            w["their_msgs"].append(user_msg[:200])
            if len(w["their_msgs"]) > 10:
                w["their_msgs"] = w["their_msgs"][-10:]
        w["msg_since_summary"] += 1
        w["last_active"] = time.time()  # 追踪最后活跃时间
        # 2026-08-16 范式转换：关键词情绪共振（_update_private_mood）已删——
        # 对方情绪由 mood_tracker 模型与 LLM 判断，系统不按词表定语气指令
        self._save_state()

    def get_private_context(self, user_id: str) -> str:
        """私聊上下文——注入 LLM 保持长对话连贯。
        2026-08-16 范式转换：情绪提示（关键词 mood_tone）已删——
        只给对话原文，情绪与语气由 LLM 自己读。"""
        self._sync_durable_events()
        w = self._private_windows.get(user_id)
        if not w:
            return ""
        parts = []
        if w.get("summary"):
            parts.append(f"【你们之前的对话摘要】{w['summary']}")
        if w.get("their_msgs"):
            parts.append("【ta最近对你说过的话】")
            for msg in w["their_msgs"][-3:]:
                parts.append(f"  「{msg}」")
        if w.get("my_replies"):
            parts.append("【你最近对 ta 说过的话】")
            for r in w["my_replies"][-3:]:
                parts.append(f"  「{r}」")
        return "\n".join(parts) if parts else ""

    def is_private_expired(self, user_id: str) -> bool:
        """私聊窗口是否已过期（24 小时无消息）。

        E1（2026-08-28）：last_active 缺失（旧状态/未迁移）按**安全过期**
        处理——返回 True 不假装活跃；get_private_context 仍可用（上下文
        链路不死，只影响「活跃」判定）。"""
        self._sync_durable_events()
        w = self._private_windows.get(user_id)
        if not w:
            return True
        last_active = w.get("last_active", 0)
        if not last_active:
            return True  # 缺字段 → 安全过期（历史反模式：缺失即永不超时）
        return (time.time() - last_active) > 86400  # 24小时

    # ── 内部 ──


    # ── 持久化 ──

    def _save_state(self):
        """保存私聊窗口状态"""
        import json
        try:
            data = {}
            for uid, w in self._private_windows.items():
                data[uid] = {
                    "summary": w.get("summary", ""),
                    "mood_tone": w.get("mood_tone", "neutral"),
                    "msg_since_summary": w.get("msg_since_summary", 0),
                    # E1：last_active 持久化——重启后 is_private_expired 才能
                    # 正确判定（此前只写不存，恢复后缺失 → 永不超时）
                    "last_active": w.get("last_active", 0),
                    "their_msgs": w.get("their_msgs", [])[-10:],
                    "my_replies": w.get("my_replies", [])[-5:],
                    "durable_event_ids": w.get("durable_event_ids", [])[-100:],
                }
            from pathlib import Path
            Path(self.STATE_FILE).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_state(self):
        """加载私聊窗口状态"""
        import json
        try:
            from pathlib import Path
            path = Path(self.STATE_FILE)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                for uid, w in data.items():
                    self._private_windows[uid] = {
                        "summary": w.get("summary", ""),
                        "mood_tone": w.get("mood_tone", "neutral"),
                        "msg_since_summary": w.get("msg_since_summary", 0),
                        # E1：恢复 last_active；旧状态缺字段 → 0 → is_private_expired
                        # 按安全过期处理（不假装活跃，上下文仍可用）
                        "last_active": w.get("last_active", 0),
                        "their_msgs": w.get("their_msgs", [])[-10:],
                        "my_replies": w.get("my_replies", [])[-5:],
                        "durable_event_ids": w.get("durable_event_ids", [])[-100:],
                    }
        except Exception:
            self._private_windows = {}

    def _cleanup(self):
        """清理过期窗口"""
        now = time.time()
        expired = [
            key for key, w in self._engaged.items()
            if now > w["expires_at"] + FADE_PERIOD
        ]
        for key in expired:
            # 与 get_window_bonus 保持同一 finalization 契约：先摘除状态，
            # 再提取跨窗口摘要；事件循环内的 BGE 工作必须排队到后台。
            window = self._engaged.pop(key, None)
            if not window:
                continue
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._extract_window_summary(*key[::-1], window=window)
            else:
                self._queue_window_summary(*key[::-1], window)
