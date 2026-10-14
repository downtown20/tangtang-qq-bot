"""
👁 感知引擎 — 让糖糖知道群友怎么看她

闭环反馈：每次糖糖回复后，评估群友的后续反应是正面/负面/中性。
积累数据 → 周报 → 指导优化方向。

成本极低：每次评估 ~60 token，每天 < 1 分钱。
"""
from __future__ import annotations

import logging
from typing import Callable, Awaitable

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.Perception")


class PerceptionEngine:
    """感知引擎——评估群友对糖糖回复的反应"""

    def __init__(self, store, llm_call):
        """
        store: Store 实例（写 feedback 表）
        llm_call: async (system_prompt, user_message) -> str（轻量 LLM 调用）
        """
        self._store = store
        self._call_llm = llm_call

    async def _run_store_io(self, operation: str, func, *args, **kwargs):
        """把反馈写入移出事件循环，避免感知任务拖住消息处理。"""
        return await run_bounded_store_io(
            operation,
            func,
            *args,
            logger=logger,
            log_prefix="👁 感知 Store SQLite 调用较慢",
            **kwargs,
        )

    async def evaluate(self, bot_reply: str, user_reaction: str | None,
                       user_qq: str, group_id: str = "", reply_ms: int = 0,
                       direction_verified: bool = False):
        """异步评估群友反应。不阻塞主流程。"""
        # 沉默 = 中性（不调用 LLM）
        if not user_reaction or len(user_reaction.strip()) < 2:
            await self._run_store_io(
                "perception.record_feedback",
                self._store.record_feedback,
                bot_reply, user_reaction, user_qq, group_id,
                sentiment="neutral", confidence=0.6, reply_ms=reply_ms,
                direction_verified=direction_verified,
            )
            return

        # 快速路径：明确的正面/负面信号
        # 2026-08-16 范式转换（教训 #24）：_quick_check 关键词短路已删——
        # 情绪三分类由 LLM 判断（词表赋 0.95 置信会污染反馈数据）
        try:
            result = await self._llm_evaluate(bot_reply, user_reaction)
        except Exception:
            result = {"sentiment": "neutral", "confidence": 0.5}

        await self._run_store_io(
            "perception.record_feedback",
            self._store.record_feedback,
            bot_reply, user_reaction, user_qq, group_id,
            sentiment=result["sentiment"], confidence=result["confidence"],
            reply_ms=reply_ms, direction_verified=direction_verified,
        )

    async def _llm_evaluate(self, bot_reply: str, reaction: str) -> dict:
        """轻量 LLM 判断。~50 token 输入，~10 token 输出"""
        prompt = (
            f"糖糖说：{bot_reply[:120]}\n"
            f"群友回：{reaction[:120]}\n\n"
            f"群友的反应是对糖糖的正面/中性/负面？只输出一个词：positive/neutral/negative"
        )
        result = await self._call_llm(
            "判断用户对AI回复的反应。只输出 positive/neutral/negative。",
            prompt
        )
        sentiment = result.strip().lower()
        if sentiment not in ("positive", "neutral", "negative"):
            sentiment = "neutral"
        return {"sentiment": sentiment, "confidence": 0.75}

    def get_weekly_report(self) -> dict:
        """汇总最近 7 天的反馈"""
        return self._store.get_feedback_stats(days=7, verified_only=True)
