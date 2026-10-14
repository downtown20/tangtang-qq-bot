"""记忆与聊天工具的统一访问策略。

这里只决定“谁能在什么会话里读取谁/哪个群的数据”，不决定糖糖是否调用工具。
策略保持纯函数，便于在进入 Store/NapCat 前做 fail-closed 校验。
"""

from dataclasses import dataclass
import re
from typing import Callable, Optional


PRIVATE_SCOPE_PREFIX = "_private_"

PERSON_RAW_TOOLS = frozenset({
    "get_recent_messages",
    "get_messages_by_date",
    "search_keywords",
    "count_messages",
    "get_first_met",
    "get_last_conversation",
    "search_chat_history",
})

PERSON_TRUSTED_TOOLS = frozenset({
    "search_facts",
    "search_memories",
})

RELATIONSHIP_TOOLS = frozenset({"search_relations"})

GROUP_SCOPED_TOOLS = frozenset({
    "get_message",
    "get_essence_msgs",
    "get_group_activity",
    "search_episodes",
})

MEMORY_ACCESS_TOOLS = (
    PERSON_RAW_TOOLS
    | PERSON_TRUSTED_TOOLS
    | RELATIONSHIP_TOOLS
    | GROUP_SCOPED_TOOLS
)


@dataclass(frozen=True)
class MemoryAccessDecision:
    """一次记忆读取的规范化授权结果。"""

    allowed: bool
    subject_qq: str = ""
    group_id: str = ""
    disclosure: str = "denied"
    reason: str = ""


def _deny(reason: str) -> MemoryAccessDecision:
    return MemoryAccessDecision(allowed=False, reason=reason)


def _resolve_relation_subject(
    name: str,
    resolve_subject: Optional[Callable[[str], Optional[str]]],
) -> str:
    value = str(name or "").strip().lstrip("@")
    if not value:
        return ""
    if re.fullmatch(r"\d{5,11}", value):
        return value
    if resolve_subject is None:
        return ""
    return str(resolve_subject(value) or "").strip()


def authorize_memory_access(
    tool_name: str,
    args: dict,
    *,
    scope_id: str,
    current_user: str,
    owner_qq: str,
    resolve_subject: Optional[Callable[[str], Optional[str]]] = None,
) -> MemoryAccessDecision | None:
    """授权并规范化一次记忆/聊天工具读取。

    返回 ``None`` 表示工具不属于本策略；其余结果必须由执行器遵守。
    普通用户查第三人结构化记忆只返回 recognition 级别，绝不返回原始内容。
    """

    if tool_name not in MEMORY_ACCESS_TOOLS:
        return None

    caller = str(current_user or "").strip()
    owner = str(owner_qq or "").strip()
    scope = str(scope_id or "").strip()
    is_group = bool(scope and not scope.startswith(PRIVATE_SCOPE_PREFIX))
    is_private = bool(scope.startswith(PRIVATE_SCOPE_PREFIX))

    if not caller:
        return _deny("(无法确认当前用户身份，已拒绝记忆查询)")
    if not is_group and not is_private:
        return _deny("(无法确认当前会话作用域，已拒绝记忆查询)")

    if tool_name in GROUP_SCOPED_TOOLS:
        requested_group = str(args.get("group_id") or "").strip()
        if is_group:
            if requested_group and requested_group != scope:
                return _deny("(只能查询当前群的信息，不能切换到其他群)")
            return MemoryAccessDecision(
                allowed=True,
                group_id=scope,
                disclosure="full",
            )
        if caller != owner:
            return _deny("(私聊中不能查询群聊记录；这类信息只允许主人核验)")
        return MemoryAccessDecision(
            allowed=True,
            group_id=requested_group,
            disclosure="full",
        )

    if tool_name in RELATIONSHIP_TOOLS:
        subject = _resolve_relation_subject(args.get("name", ""), resolve_subject)
        if not subject:
            if caller == owner and is_private:
                return MemoryAccessDecision(allowed=True, disclosure="full")
            return _deny("(无法唯一确认关系查询对象，已按隐私规则拒绝)")
    else:
        subject = str(
            args.get("subject_qq") or args.get("qq_id") or caller
        ).strip()

    if subject == "*":
        if caller == owner and is_private:
            return MemoryAccessDecision(
                allowed=True,
                subject_qq=subject,
                disclosure="full",
            )
        return _deny("(跨用户查询会泄露隐私，已拒绝)")

    if is_group:
        if subject != caller:
            return _deny("(群聊里只能查询你自己在当前群的信息，别人的记忆是隐私)")
        return MemoryAccessDecision(
            allowed=True,
            subject_qq=subject,
            group_id=scope,
            disclosure="full",
        )

    if caller == owner or subject == caller:
        return MemoryAccessDecision(
            allowed=True,
            subject_qq=subject,
            disclosure="full",
        )

    if tool_name in PERSON_TRUSTED_TOOLS or tool_name in RELATIONSHIP_TOOLS:
        return MemoryAccessDecision(
            allowed=True,
            subject_qq=subject,
            disclosure="recognition",
        )

    return _deny("(原始聊天记录属于本人隐私，只能查询你自己的记录)")
