"""
🧮 计算器 — 让糖糖能精确计算

已注册技能:
- calculate: 精确数学计算，避免 LLM 算错

LLM 做加减乘除经常出错（尤其是大数）。这个技能让 Python 来做数学，
结果 100% 准确。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("糖糖.Calc")


def _register():
    """注册计算器技能"""
    from .skills import register_skill

    @register_skill(
        "calculate",
        "精确计算数学表达式——返回100%正确的数值结果。你（LLM）做加减乘除经常出错，尤其是大数和复杂表达式。"
        "这个工具让 Python 来做数学，结果绝对准确。支持加减乘除、乘方、括号、百分比等。",
        {"expression": "数学表达式，如'19382*4729'、'(100+200)/3'、'2**10'、'15%*200'"},
    )
    async def _calculate_skill(expression: str = "") -> str:
        return calculate(expression)


def calculate(expression: str) -> str:
    """计算数学表达式，返回格式化结果"""
    import math

    if not expression or not expression.strip():
        return "请给我一个算式喵~ 比如'19382×4729'"

    expr = expression.strip()

    # 预处理：中文/口语符号 → 标准 Python 表达式
    replacements = {
        "×": "*", "x": "*", "X": "*",
        "÷": "/", "：": "/",
        "（": "(", "）": ")",
        "^": "**",
        "百分之": "/100",
        "的": "*",  # "200的15%" → "200*15%"
    }
    for old, new in replacements.items():
        expr = expr.replace(old, new)

    # 处理口语化的百分比："200的15%" → 不做额外处理（LLM 应该已转成标准表达式）
    # 但如果表达式里还有 % 符号，处理它
    if "%" in expr:
        # "15%" → "15/100", "200*15%" → "200*15/100"
        expr = expr.replace("%", "/100")

    # 安全检查：只允许数字、运算符、括号、空格、小数点、数学函数
    allowed = set("0123456789+-*/().^ eEπpjmathsilogqrtn")
    # 更实用的做法：黑名单危险函数
    dangerous = ["__", "import", "exec", "eval", "open", "file", "system", "subprocess", "os.", "sys."]
    expr_lower = expr.lower()
    for d in dangerous:
        if d in expr_lower:
            return f"唔…这个算式里有危险的东西喵（{d}），糖糖不敢算"

    try:
        # 注入 math 模块函数
        safe_globals = {
            "__builtins__": {},
            "math": math,
            "pi": math.pi,
            "e": math.e,
            "sqrt": math.sqrt,
            "sin": math.sin,
            "cos": math.cos,
            "tan": math.tan,
            "log": math.log,
            "log10": math.log10,
            "log2": math.log2,
            "abs": abs,
            "round": round,
            "pow": pow,
            "int": int,
            "float": float,
        }
        result = eval(expr, safe_globals, {})

        # 格式化结果
        if isinstance(result, float):
            # 避免浮点噪声：0.1+0.2 → 0.30000000000000004
            if abs(result) < 1e-10:
                result = 0.0
            # 如果是整数则显示整数
            if result == int(result) and abs(result) < 1e15:
                result = int(result)
            else:
                result = round(result, 10)

        logger.info(f"🧮 计算: {expression} → {result}")
        return f"计算结果：{expression} = {result}"

    except ZeroDivisionError:
        return "除数不能是零喵！这在数学上是不允许的"
    except (SyntaxError, NameError, TypeError) as e:
        return f"这个算式我不太会算喵…能换个说法吗？（{e}）"
    except Exception as e:
        logger.error(f"计算失败 [{expression}]: {e}")
        return f"计算出错了…{str(e)[:60]}"
