"""
消息批处理 —— 消灭刷屏式回复

解决问题：
  1. 多人同时 @ → 合并成一段回复
  2. 同一人连续发 → 跳过中间废话，回应最终意图

机制：消息到达后不立即处理，等待短暂窗口（群聊 2s / 私聊 2s）。
窗口内新消息到达 → 加入批次，重置计时器。窗口到期 → 统一处理。

用法：
  from .message_batcher import MessageBatcher
  batcher = MessageBatcher(handler)
  await batcher.enqueue_group(msg)     # 返回 True=已入队, False=正常处理
  await batcher.enqueue_private(msg)   # 同上
"""

from __future__ import annotations

import asyncio
import logging

from .inbound_event import (
    build_platform_event_key,
    persist_inbound_message,
    serialize_segments,
)

logger = logging.getLogger("糖糖.Batcher")

BATCH_WINDOW = 2.0  # 秒


AT_DEBOUNCE = 1.5  # @消息防抖窗口（秒）——等待该时长后处理最后一条@

# 2026-08-28 任务A：批处理合并视图的标记键——handler 见该键即跳过 log_chat
# （合并文本只作 LLM 视图，绝不写成单一用户 chat_log 事实行）
BATCH_EVENT_MARKER = "_batched_events"
PERSISTED_EVENT_MARKER = "_persisted_event"

class MessageBatcher:
    """消息批处理器——每个群/私聊独立一个批次"""

    def __init__(self, handler):
        self.h = handler
        self._group_batches: dict[str, dict] = {}  # {group_id: {timer, msgs}}
        self._private_batches: dict[str, dict] = {}  # {user_id: {timer, msgs}}
        # @消息防抖——不合并内容，只防重复处理。多人同时@ → 等安静后只处理最后一条
        self._at_timers: dict[str, asyncio.Task] = {}  # {group_id: timer}

    # ═══════════════════════════════════════
    # 公开入口
    # ═══════════════════════════════════════

    async def enqueue_group(self, msg: dict) -> bool:
        """群聊消息入队。返回 True=已拦截（调用方应 return），False=正常处理"""
        if msg["message"].startswith("/"):
            return False  # 命令不批处理
        # @糖糖 或叫名字 → 立即处理，不延迟
        raw = msg.get("raw_message", "")
        if f"[CQ:at,qq={self.h.bot_qq}]" in raw:
            return False
        return self._enqueue("group", msg["group_id"], msg)

    async def enqueue_private(self, msg: dict) -> bool:
        """私聊消息入队。返回 True=已拦截，False=正常处理"""
        if msg["message"].startswith("/"):
            return False
        return self._enqueue("private", msg["user_id"], msg)

    async def debounce_at(self, group_id: str, msg: dict) -> bool:
        """@消息防抖——不合并内容，只防止同一群短时间内多次@产生多次LLM调用。
        多人同时@糖糖 → 取消前一次等待，只处理最后一条。

        Returns: True=已拦截（前一条被取消），False=正常处理（当前这条到期了该处理）
        """
        # 取消之前的防抖计时器（前一条被后一条替代）
        prev = self._at_timers.pop(group_id, None)
        if prev:
            prev.cancel()

        # 创建新计时器
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._at_timers[group_id] = future  # type: ignore

        async def _wait_then_process():
            try:
                await asyncio.sleep(AT_DEBOUNCE)
            except asyncio.CancelledError:
                return
            self._at_timers.pop(group_id, None)
            msg["_debounced"] = True
            await self.h.handle_group_message(msg)

        # 启动但不等待——调用方 return，让 NapCat 知道消息已被消费
        asyncio.create_task(_wait_then_process())

        # 总是返回 True——@消息永远走防抖路径
        return True

    async def _cancel_at_timer(self, group_id: str):
        """取消指定群的@防抖计时器"""
        timer = self._at_timers.pop(group_id, None)
        if timer:
            timer.cancel()

    # ═══════════════════════════════════════
    # 内部
    # ═══════════════════════════════════════

    def _enqueue(self, ctx: str, ctx_id: str, msg: dict) -> bool:
        batches = self._group_batches if ctx == "group" else self._private_batches
        existing = batches.get(ctx_id)
        if existing:
            existing["msgs"].append(msg)
            existing["timer"].cancel()
            existing["timer"] = asyncio.create_task(
                self._process(ctx, ctx_id, BATCH_WINDOW)
            )
            return True
        else:
            batches[ctx_id] = {
                "msgs": [msg],
                "timer": asyncio.create_task(
                    self._process(ctx, ctx_id, BATCH_WINDOW)
                ),
            }
            return True

    async def _process(self, ctx: str, ctx_id: str, delay: float):
        """计时器到期——处理批次"""
        await asyncio.sleep(delay)
        batches = self._group_batches if ctx == "group" else self._private_batches
        batch = batches.pop(ctx_id, None)
        if not batch:
            return
        msgs = batch["msgs"]
        if len(msgs) == 1:
            # 单条消息：加 _batched 标记后重入，防止再次被批处理拦截
            msgs[0]["_batched"] = True
            if ctx == "group":
                await self.h.handle_group_message(msgs[0])
            else:
                await self.h.handle_private_message(msgs[0])
        elif ctx == "group":
            await self._reply_batched_group(ctx_id, msgs)
        else:
            await self._reply_batched_private(ctx_id, msgs)

    # ═══════════════════════════════════════
    # 合并回复
    # ═══════════════════════════════════════

    async def _reply_batched_group(self, group_id: str, msgs: list[dict]):
        """多人消息合并 → 构造合并消息，走正常 handler 流程。

        2026-08-28 任务A：先逐条幂等落真实事件（每人各自一行 chat_log，带真实
        user/group/raw/segments/message_id/time），再构造只供 LLM 使用的合并视图；
        合并视图带 BATCH_EVENT_MARKER——handler 见标记即跳过 log_chat，
        绝不让多人消息出现在单一用户的 chat_log 事实行（历史事故，见审查 C1）。
        """
        msgs = self._persist_batch_events("group", msgs)
        if not msgs:
            logger.info("♻️ 批处理事件均为持久化重放，跳过重复回合")
            return
        first = msgs[0]
        if len(msgs) == 1:
            single = dict(first)
            single["_batched"] = True
            single[PERSISTED_EVENT_MARKER] = True
            await self.h.handle_group_message(single)
            return
        lines = [
            f"【同时有 {len(msgs)} 个人找你，请在一段回复里自然地回应所有人。"
            f"不要分开发送 N 段独立回复——像真实的群聊一样，自然地 @ 对应的人】\n"
        ]
        for m in msgs:
            lines.append(f"{m['nickname']} 说：{m['message']}\n")
        combined_text = "".join(lines)

        # 构造一个合并消息，走 handler 正常流程（不绕过 LLM 基础设施）
        combined_msg = {
            "group_id": group_id,
            "user_id": first["user_id"],
            "nickname": first["nickname"],
            "message": combined_text,
            "raw_message": combined_text,
            "message_id": 0,  # 视图不冒充真实事件 id——真实行已逐条落库
            "time": first.get("time", 0),
            # P0-C：保留真实 source message_ids——set_reminder 等回合动作的
            # 幂等 source 用（同一批合并视图重试幂等，不同消息可新建）
            "_source_message_ids": [
                int(m["message_id"]) for m in msgs if m.get("message_id")
            ],
            # P0-1b：合并视图仍需绑定每条真实入站事实；message_id 只够动作
            # 兼容路径使用，ChatContext 要消费稳定 event_key。
            "_source_event_keys": [
                event_key for event_key in (
                    build_platform_event_key("group", m) for m in msgs
                ) if event_key
            ],
            "_batched": True,
            BATCH_EVENT_MARKER: True,
        }
        await self.h.handle_group_message(combined_msg)

    async def _reply_batched_private(self, user_id: str, msgs: list[dict]):
        """同一人连续消息 → 构造合并上下文，走正常 handler 流程。

        2026-08-28 任务A：与群路径相同——先逐条落真实事件，合并文本只作 LLM 视图。
        """
        msgs = self._persist_batch_events("private", msgs)
        if not msgs:
            logger.info("♻️ 私聊批处理事件均为持久化重放，跳过重复回合")
            return
        nick = msgs[0]["nickname"]
        if len(msgs) == 1:
            single = dict(msgs[0])
            single["_batched"] = True
            single[PERSISTED_EVENT_MARKER] = True
            await self.h.handle_private_message(single)
            return
        lines = [
            "【对方连续发了多条消息，这是完整的对话链。请只做一次回复，"
            "回应 ta 的最终意图——不要逐条回复中间的每条消息】\n"
        ]
        for m in msgs:
            lines.append(f"  「{m['message']}」\n")
        lines.append(f"\n请自然回复 {nick}（1-3 句话）：")
        combined_text = "".join(lines)

        combined_msg = {
            "user_id": user_id,
            "nickname": nick,
            "message": combined_text,
            "raw_message": combined_text,
            "message_id": 0,  # 视图不冒充真实事件 id——真实行已逐条落库
            "time": msgs[0].get("time", 0),
            # P0-C：保留真实 source message_ids（同群/私批处理）
            "_source_message_ids": [
                int(m["message_id"]) for m in msgs if m.get("message_id")
            ],
            "_source_event_keys": [
                event_key for event_key in (
                    build_platform_event_key("private", m) for m in msgs
                ) if event_key
            ],
            "_batched": True,
            BATCH_EVENT_MARKER: True,
        }
        await self.h.handle_private_message(combined_msg)

    # ═══════════════════════════════════════
    # 原始事件持久化（2026-08-28 任务A）
    # ═══════════════════════════════════════

    def _persist_batch_events(self, ctx: str, msgs: list[dict]) -> list[dict]:
        """逐条幂等持久化真实事件：每人的消息各自一行 chat_log，带真实
        user/group/raw_message/segments/message_id/time（精确时间保留，
        第一句/顺序查询不破坏）。

        幂等：event_key = scope:platform_message_id，NapCat 重投/重启重放
        被唯一索引拦截（insert_chat 返回 None），不重复记账。
        message_id 缺失(=0)时不设幂等键——不强去重，每行都是真实事件。
        持久化失败只告警不阻断——LLM 视图仍照常回复，事件由兜底恢复。
        """
        accepted: list[dict] = []
        for m in msgs:
            try:
                result = persist_inbound_message(
                    self.h.memory, ctx, m, m.get("message", ""),
                )
                if result.duplicate:
                    logger.info("♻️ 忽略已持久化的批处理事件")
                    continue
                accepted.append(m)
            except Exception as e:
                logger.warning(f"批处理事件落库失败（不阻断回复）: {e}")
                accepted.append(m)
        return accepted


def _segments_to_json(m: dict) -> str:
    """旧内部名兼容；真实实现统一由 inbound_event 契约提供。"""
    return serialize_segments(m)
