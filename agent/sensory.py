"""
👁 感官能力 — 让糖糖感知时间和天气

已注册技能:
- get_time: 获取当前时间、日期、星期、时段
- get_weather: 查询城市实时天气（wttr.in 免费API，无需Key）

用法：在 handler.py 中调用 ``from .sensory import _register; _register()`` 即可。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

logger = logging.getLogger("糖糖.Sensory")

# ═══════════════════════════════════════════════════════════════════
# 技能注册入口（模块级 _register 函数，由 handler.py 调用）
# ═══════════════════════════════════════════════════════════════════


def _register():
    """注册所有感官技能（延迟导入避免循环引用）"""
    from .skills import register_skill

    @register_skill(
        "get_time",
        "返回当前精确时间、日期和星期。你（LLM）不知道现在几点——你的训练数据可能已过时。这个工具给你真实的墙上时间。",
        {},
    )
    async def _get_time_skill() -> str:
        return get_current_time()

    @register_skill(
        "get_weather",
        "返回指定城市当前的实时天气数据（温度/湿度/风速/天气描述）。你（LLM）不知道真实世界的天气——说错了可能让人下雨天没带伞、冷天穿少了感冒。",
        {"city": "城市名称，如'北京'、'上海'、'长沙'、'郴州'"},
    )
    async def _get_weather_skill(city: str = "") -> str:
        if not city or not city.strip():
            return "请告诉我想查哪个城市的天气喵~ 比如'北京'、'上海'"
        return await query_weather(city.strip())


# ═══════════════════════════════════════════════════════════════════
# 时间查询
# ═══════════════════════════════════════════════════════════════════

def get_current_time() -> str:
    """返回当前时间的友好描述，包括日期、星期、时段"""
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekdays[now.weekday()]

    hour = now.hour
    if 5 <= hour < 8:
        period = "清晨"
    elif 8 <= hour < 12:
        period = "上午"
    elif 12 <= hour < 14:
        period = "中午"
    elif 14 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 22:
        period = "晚上"
    else:
        period = "深夜"

    return (
        f"现在是{now.year}年{now.month}月{now.day}日 {weekday} "
        f"{period}{hour}点{now.minute:02d}分"
    )


# ═══════════════════════════════════════════════════════════════════
# 天气查询（wttr.in — 免费、无需 API Key）
# ═══════════════════════════════════════════════════════════════════

async def query_weather(city: str) -> str:
    """通过 wttr.in 查询城市天气，返回格式化的自然语言描述"""
    import httpx

    # 清理 ANSI 转义码
    def _clean(text: str) -> str:
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        # wttr.in 有时返回带样式标签的 HTML
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()

    async def _fetch(url: str) -> str | None:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "TangTangBot/1.0"},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                text = _clean(resp.text)
                if text and len(text) > 3 and "sorry" not in text.lower():
                    return text
            return None

    try:
        # 方案 A：wttr.in 紧凑格式（含 emoji + 温湿度风）
        url_a = f"https://wttr.in/{city}?format=%l:+%c+%t+(体感%f)+💧%h+💨%w&lang=zh"
        result = await _fetch(url_a)
        if result:
            logger.info(f"🌤 天气查询: {city} → {result[:100]}")
            return f"【{city}天气】{result}"

        # 方案 B：更简短的格式
        url_b = f"https://wttr.in/{city}?format=3&lang=zh"
        result = await _fetch(url_b)
        if result:
            logger.info(f"🌤 天气查询(v2): {city} → {result[:100]}")
            return result

        # 方案 C：尝试英文名重试
        # 如果中文名不成功，可能在 URL encode 上有问题
        from urllib.parse import quote

        url_c = f"https://wttr.in/{quote(city)}?format=3&lang=zh"
        result = await _fetch(url_c)
        if result:
            logger.info(f"🌤 天气查询(v3): {city} → {result[:100]}")
            return result

        return f"唔…没查到「{city}」的天气喵~ 要不试试用拼音或者英文名？"

    except Exception as e:
        logger.error(f"天气查询失败 [{city}]: {e}")
        return f"天气查询暂时失败了喵…（{str(e)[:60]}）"
