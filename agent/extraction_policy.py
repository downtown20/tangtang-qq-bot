"""记忆提取积压的单一准入策略。

数量阈值保证活跃对话及时提取；时间老化保证千人群中的低频用户不会
永久饿死。这里只做纯判断，不建任务、不调用 LLM。
"""
from __future__ import annotations

from datetime import datetime


IMMEDIATE_MESSAGES = 10
STALE_MESSAGES = 3
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
ONE_OFF_AFTER_SECONDS = 21 * 24 * 60 * 60


def extraction_backlog_ready(row: dict, now: datetime) -> bool:
    """返回一个用户的未处理消息是否已经达到持久队列准入条件。"""
    message_count = int(row.get("messages", 0) or 0)
    if message_count >= IMMEDIATE_MESSAGES:
        return True
    try:
        oldest = datetime.fromisoformat(str(row.get("oldest_at") or ""))
    except (TypeError, ValueError):
        return False
    if oldest.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=oldest.tzinfo)
    elif oldest.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    age_seconds = max(0.0, (now - oldest).total_seconds())
    if message_count >= STALE_MESSAGES:
        return age_seconds >= STALE_AFTER_SECONDS
    return message_count > 0 and age_seconds >= ONE_OFF_AFTER_SECONDS


def summarize_extraction_backlog(users: list[dict], now: datetime) -> dict:
    """汇总到期与等待中的债务；结果不含用户标识。"""
    eligible: list[dict] = []
    deferred: list[dict] = []
    for row in users:
        target = eligible if extraction_backlog_ready(row, now) else deferred
        target.append(row)

    def _messages(rows: list[dict]) -> int:
        return sum(int(row.get("messages", 0) or 0) for row in rows)

    def _oldest(rows: list[dict]) -> str:
        values = [str(row.get("oldest_at") or "") for row in rows]
        return min((value for value in values if value), default="")

    return {
        "eligible_users": len(eligible),
        "eligible_messages": _messages(eligible),
        "eligible_oldest_at": _oldest(eligible),
        "deferred_users": len(deferred),
        "deferred_messages": _messages(deferred),
        "deferred_oldest_at": _oldest(deferred),
    }
