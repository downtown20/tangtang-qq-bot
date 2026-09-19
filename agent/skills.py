"""
🛠 技能注册系统 — 让 LLM 能调用工具函数

使用方式：
1. 定义 async handler 函数
2. 用 @register_skill 装饰器注册
3. 自动转为 OpenAI Tool Calling 定义（function calling），LLM 自主调用

注意：已不再使用旧的 JSON 嵌入方式 {"skill":"name","args":{...}}。
当前全部走原生 OpenAI function calling，由 handler.py 的 _call_llm_with_skills 统一调度。
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any

logger = logging.getLogger("糖糖.Skills")


@dataclass
class Skill:
    name: str
    description: str                     # 给 LLM 看的，描述何时调用
    parameters: dict[str, str]           # {参数名: 说明}
    handler: Callable[..., Awaitable[str]]
    method_type: str = "agent"           # tool | agent | behavior — 决定返回值处理方式


# 全局技能注册表
_registry: dict[str, Skill] = {}


def register_skill(name: str, description: str, parameters: dict[str, str] = None,
                   method_type: str = "agent"):
    """装饰器：注册一个技能。
    method_type: "tool"(纯计算，LLM继续处理) | "agent"(返回值需LLM消化，默认) | "behavior"(副作用操作，不触发LLM再思考)"""
    def wrapper(fn: Callable[..., Awaitable[str]]):
        _registry[name] = Skill(
            name=name,
            description=description,
            parameters=parameters or {},
            handler=fn,
            method_type=method_type,
        )
        logger.info(f"🛠 技能已注册: {name}")
        return fn
    return wrapper


def list_skills() -> list[Skill]:
    return list(_registry.values())


def get_method_type(name: str) -> str:
    """查询一个工具的方法类型。未注册的工具默认返回 "agent"。"""
    skill = _registry.get(name)
    return skill.method_type if skill else "agent"


def build_tool_definitions() -> list[dict]:
    """将已注册技能转换为 OpenAI 原生 Tool Calling 格式。
    替代旧的 JSON 文本嵌入方式——LLM 通过原生 function calling 调用技能。"""
    tools = []
    for skill in _registry.values():
        properties = {}
        for param_name, param_desc in skill.parameters.items():
            properties[param_name] = {
                "type": "string",
                "description": param_desc,
            }
        tool = {
            "type": "function",
            "function": {
                "name": skill.name,
                "description": skill.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(skill.parameters.keys()),
                },
            },
        }
        tools.append(tool)
    return tools





async def execute_skill(skill_name: str, args: dict) -> str | None:
    """执行技能并返回结果文本"""
    skill = _registry.get(skill_name)
    if not skill:
        return None
    try:
        logger.info(f"🔧 执行技能: {skill_name}({args})")
        result = await skill.handler(**args)
        return str(result) if result else ""
    except Exception as e:
        logger.error(f"技能 {skill_name} 执行失败: {e}")
        return f"[技能执行失败: {e}]"

