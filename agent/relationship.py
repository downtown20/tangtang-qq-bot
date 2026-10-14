"""
关系档案系统 — 维护「我和这个人的关系是怎样的」

与 people.notes（她是谁）互补：这里存的是「我们之间」的事。
每 100 条私聊触发一次 LLM 合成，生成稳定档案注入每轮上下文。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.Relationship")


class RelationshipManager:
    """关系档案管理器"""

    def __init__(self, store, llm_call):
        self._store = store
        self._llm = llm_call  # async (system_prompt, user_message) -> str
        self._update_threshold = 80  # 每 N 条新消息触发一次合成
        self._syncing: set = set()  # 2026-08-16 Codex I4：同用户单飞——并发私聊只合成一次

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """关系档案异步路径的同步 Store 边界。"""
        return await run_bounded_store_io(
            operation, func, *args, logger=logger,
            log_prefix="🤝 关系 Store SQLite 调用较慢", **kwargs,
        )

    async def maybe_update(self, qq_id: str) -> bool:
        """检查是否需要更新关系档案——每 N 条新消息触发一次"""
        # 2026-08-16 批 1a：失败退避——合成失败时成功游标不推进，
        # 但也不能每条消息都重试一次（LLM 空耗 + 日志风暴）
        fail_until = await self._run_store_io(
            "relationship.kv_get_failure_backoff", self._store.kv_get,
            f"rel_syn_fail_until:{qq_id}",
        ) or ""
        if fail_until and fail_until > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
            return False

        person = await self._run_store_io(
            "relationship.get_person", self._store.get_or_create_person, qq_id,
        )
        total = person.get("total_chats", 0) if person else 0
        last_updated = person.get("relationship_updated", 0) if person else 0

        if total - last_updated >= self._update_threshold:
            # 2026-08-16 Codex I4：单飞——await 前没有锁时，多条私聊并发越过
            # 阈值检查 → 排队重复合成并互相覆盖
            if qq_id in self._syncing:
                return False
            self._syncing.add(qq_id)
            try:
                return await self._synthesize(qq_id)
            finally:
                self._syncing.discard(qq_id)
        return False

    def _mark_failure(self, qq_id: str) -> None:
        """合成失败退避 6 小时（持久化——重启后仍有效）"""
        nxt = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        self._store.kv_set(f"rel_syn_fail_until:{qq_id}", nxt)

    async def _mark_failure_async(self, qq_id: str) -> None:
        """在关系合成失败路径异步写入退避时间。"""
        await self._run_store_io(
            "relationship.mark_failure", self._mark_failure, qq_id,
        )

    async def _synthesize(self, qq_id: str) -> bool:
        """LLM 合成关系档案——从 chat_log 提取真实数据，不编造"""
        # 获取关键数据
        earliest = await self._run_store_io(
            "relationship.get_earliest_chats", self._store.get_earliest_chats,
            qq_id, limit=10,
        )
        person = await self._run_store_io(
            "relationship.get_person", self._store.get_or_create_person,
            qq_id, "",
        )
        nickname = person.get("nickname", qq_id) if person else qq_id
        intimacy = person.get("intimacy", 0) if person else 0

        if not earliest:
            await self._mark_failure_async(qq_id)
            return False

        # 构建早期对话样本
        early_text = "\n".join(
            f"{'糖糖' if m['is_bot'] else nickname}: {m['message'][:100]}"
            for m in earliest
        )

        try:
            summary = await self._llm(
                "你是一个档案整理助手。从对话记录中提取关于糖糖和这个人的关系信息。"
                "只提取明确的事实，不要推测或编造。不确定就写「未知」。",
                f"糖糖和 {nickname} 最早的一些对话：\n\n{early_text}\n\n"
                f"当前关系：亲密度 {intimacy}/100\n\n"
                "提取以下信息（用简洁的条目，不要编造，不确定就写「未知」）：\n"
                "1. 他们怎么认识的？第一句有意义的话是什么？\n"
                "2. 对方喜欢糖糖怎么称呼ta？\n"
                "3. 对方对糖糖有什么特别的偏好或不满？\n"
                "4. 他们之间有什么特别的梗或回忆？\n\n"
                "直接输出条目，不要前缀。"
            )
        except Exception as e:
            # 2026-08-16 Codex I4：LLM 异常也必须退避——此前只有空输出才退避，
            # 网络/API 故障时每条私聊都重试（日志风暴 + LLM 空耗）
            logger.warning(f"关系档案 LLM 调用失败 {qq_id}: {e}")
            await self._mark_failure_async(qq_id)
            return False

        if summary and len(summary) > 10:
            # 2026-08-16 批 1a：全「未知」结果不覆盖旧档案——prompt 允许
            # 「不确定就写未知」，但全部未知 = 没提取到任何东西，
            # 原样写入只会抹掉已有档案（DB 实证存在整段「未知」元评论）
            items = re.findall(r"^\s*\d+[\.、]\s*(.+)$", summary, re.M)
            if items and all("未知" in it for it in items):
                await self._mark_failure_async(qq_id)
                logger.info(f"⏭ 关系档案全「未知」跳过覆盖: {nickname}({qq_id})")
                return False
            await self._run_store_io(
                "relationship.set_summary", self._store.set_relationship_summary,
                qq_id, summary.strip(),
            )
            await self._run_store_io(
                "relationship.update_timestamp",
                self._store._update_relationship_timestamp, qq_id,
            )
            logger.info(f"📝 关系档案更新: {nickname}({qq_id})")
            return True
        await self._mark_failure_async(qq_id)
        return False

    def get_summary(self, qq_id: str) -> str:
        """获取关系档案——注入上下文用"""
        return self._store.get_relationship_summary(qq_id) or ""
