"""
📐 单位换算 — 让糖糖能换算温度/距离/重量/货币

已注册技能:
- convert: 单位换算（温度、距离、重量、体积、面积、货币）

货币汇率走 frankfurter.app（欧洲央行数据，免费无需Key）。
其他换算用硬编码公式，本地毫秒级完成。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("糖糖.Convert")


def _register():
    """注册换算技能"""
    from .skills import register_skill

    @register_skill(
        "convert",
        "在多套单位系统间精确换算（温度、距离、重量、体积、面积、货币）。你（LLM）做单位换算经常出错——100华氏度到底是多少摄氏度？让工具算，100%准确。",
        {
            "value": "要换算的数值，如'100'",
            "from_unit": "原单位，如'公里'或'km'、'摄氏度'或'celsius'、'人民币'或'CNY'",
            "to_unit": "目标单位，如'英里'或'miles'、'华氏度'或'fahrenheit'、'美元'或'USD'",
        },
    )
    async def _convert_skill(value: str = "0", from_unit: str = "", to_unit: str = "") -> str:
        try:
            val = float(value)
        except (ValueError, TypeError):
            return f"「{value}」好像不是有效的数字喵~"
        return await convert(val, from_unit.strip(), to_unit.strip())


# ═══════════════════════════════════════════════════════════════════
# 内置换算表（公式用标准国际单位作为中介）
# ═══════════════════════════════════════════════════════════════════

# 温度需要特殊处理（不是线性转换）
TEMPERATURE = {
    "摄氏度": "celsius", "°c": "celsius", "c": "celsius", "celsius": "celsius",
    "华氏度": "fahrenheit", "°f": "fahrenheit", "f": "fahrenheit", "fahrenheit": "fahrenheit",
    "开尔文": "kelvin", "k": "kelvin", "开": "kelvin", "kelvin": "kelvin",
}

# 长度 → 米
LENGTH_TO_M = {
    "公里": 1000, "km": 1000, "千米": 1000,
    "米": 1, "m": 1,
    "英里": 1609.344, "mile": 1609.344, "miles": 1609.344, "mi": 1609.344,
    "英尺": 0.3048, "ft": 0.3048, "feet": 0.3048, "foot": 0.3048,
    "英寸": 0.0254, "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "厘米": 0.01, "cm": 0.01,
    "毫米": 0.001, "mm": 0.001,
    "码": 0.9144, "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "里": 500, "华里": 500,
    "尺": 1 / 3, "市尺": 1 / 3,
    "寸": 1 / 30, "市寸": 1 / 30,
}

# 重量 → 克
WEIGHT_TO_G = {
    "千克": 1000, "kg": 1000, "公斤": 1000,
    "克": 1, "g": 1,
    "磅": 453.592, "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
    "盎司": 28.3495, "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "斤": 500, "市斤": 500,
    "两": 50,
    "毫克": 0.001, "mg": 0.001,
    "吨": 1_000_000, "t": 1_000_000,
}

# 体积 → 升
VOLUME_TO_L = {
    "升": 1, "l": 1, "公升": 1,
    "毫升": 0.001, "ml": 0.001,
    "加仑": 3.78541, "gal": 3.78541, "gallon": 3.78541, "gallons": 3.78541,
    "品脱": 0.473176, "pt": 0.473176, "pint": 0.473176, "pints": 0.473176,
    "夸脱": 0.946353, "qt": 0.946353, "quart": 0.946353, "quarts": 0.946353,
    "立方米": 1000, "m³": 1000, "m3": 1000,
}

# 面积 → 平方米
AREA_TO_M2 = {
    "平方米": 1, "m²": 1, "m2": 1, "平米": 1,
    "平方英尺": 0.092903, "ft²": 0.092903, "ft2": 0.092903,
    "平方公里": 1_000_000, "km²": 1_000_000, "km2": 1_000_000,
    "公顷": 10000, "ha": 10000,
    "亩": 666.667, "市亩": 666.667,
    "英亩": 4046.86, "acre": 4046.86, "acres": 4046.86,
}

# 速度 → m/s
SPEED_TO_MS = {
    "km/h": 1 / 3.6, "公里/小时": 1 / 3.6, "kph": 1 / 3.6, "kmh": 1 / 3.6,
    "m/s": 1, "米/秒": 1,
    "mph": 0.44704, "英里/小时": 0.44704,
    "节": 0.514444, "kn": 0.514444, "knot": 0.514444, "knots": 0.514444,
    "马赫": 343, "mach": 343,
}


async def convert(value: float, from_unit: str, to_unit: str) -> str:
    """主换算入口：识别单位类型 → 执行换算"""
    if not from_unit or not to_unit:
        return "请告诉我从什么单位换到什么单位喵~ 比如'100公里转英里'"

    if from_unit == to_unit:
        return f"{value} {from_unit} = {value} {to_unit}（单位相同啦~）"

    # 归一化单位名
    fu = _norm(from_unit)
    tu = _norm(to_unit)

    # 温度
    if fu in TEMPERATURE and tu in TEMPERATURE:
        return _convert_temp(value, fu, tu)

    # 长度
    if fu in LENGTH_TO_M and tu in LENGTH_TO_M:
        return _convert_linear(value, fu, tu, LENGTH_TO_M)

    # 重量
    if fu in WEIGHT_TO_G and tu in WEIGHT_TO_G:
        return _convert_linear(value, fu, tu, WEIGHT_TO_G)

    # 体积
    if fu in VOLUME_TO_L and tu in VOLUME_TO_L:
        return _convert_linear(value, fu, tu, VOLUME_TO_L)

    # 面积
    if fu in AREA_TO_M2 and tu in AREA_TO_M2:
        return _convert_linear(value, fu, tu, AREA_TO_M2)

    # 速度
    if fu in SPEED_TO_MS and tu in SPEED_TO_MS:
        return _convert_linear(value, fu, tu, SPEED_TO_MS)

    # 货币（走 API）
    if _is_currency(fu) and _is_currency(tu):
        return await _convert_currency(value, fu, tu)

    # 兜底
    return (
        f"抱歉喵…糖糖暂时不支持「{from_unit}」到「{to_unit}」的换算。\n"
        f"目前支持：温度/距离/重量/体积/面积/速度/货币"
    )


def _norm(u: str) -> str:
    """归一化：去空格、全角转半角、转小写"""
    u = u.strip().lower()
    # 全角字母 → 半角
    trans = str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    )
    return u.translate(trans)


# ═══════════════════════════════════════════════════════════════════
# 温度换算（非线性）
# ═══════════════════════════════════════════════════════════════════

def _convert_temp(v: float, fr: str, to: str) -> str:
    fc = TEMPERATURE[fr]
    tc = TEMPERATURE[to]
    fl = _temp_label(fr, fc)
    tl = _temp_label(to, tc)

    # 先转成摄氏度
    if fc == "celsius":
        c = v
    elif fc == "fahrenheit":
        c = (v - 32) * 5 / 9
    else:  # kelvin
        c = v - 273.15

    # 再转目标
    if tc == "celsius":
        r = c
    elif tc == "fahrenheit":
        r = c * 9 / 5 + 32
    else:  # kelvin
        r = c + 273.15

    return f"{v} {fl} = {_fmt(r)} {tl}"


def _temp_label(unit: str, canon: str) -> str:
    """显示用的温度单位名"""
    if "摄氏" in unit or unit in ("c", "°c"):
        return "°C"
    if "华氏" in unit or unit in ("f", "°f"):
        return "°F"
    return "K"


# ═══════════════════════════════════════════════════════════════════
# 线性换算（长度/重量/体积/面积/速度）
# ═══════════════════════════════════════════════════════════════════

def _convert_linear(v: float, fr: str, to: str, table: dict) -> str:
    base = v * table[fr]  # → 基准单位（米/克/升/平方米/m/s）
    result = base / table[to]
    return f"{v} {fr} = {_fmt(result)} {to}"


# ═══════════════════════════════════════════════════════════════════
# 货币换算（frankfurter.app — 欧洲央行汇率，免费无需Key）
# ═══════════════════════════════════════════════════════════════════

CURRENCY_MAP = {
    "人民币": "CNY", "rmb": "CNY", "元": "CNY", "块": "CNY", "cny": "CNY",
    "美元": "USD", "美金": "USD", "usd": "USD", "$": "USD",
    "日元": "JPY", "jpy": "JPY", "円": "JPY",
    "欧元": "EUR", "eur": "EUR",
    "港币": "HKD", "hkd": "HKD",
    "韩元": "KRW", "krw": "KRW",
    "英镑": "GBP", "gbp": "GBP", "￡": "GBP",
    "澳元": "AUD", "aud": "AUD",
    "加元": "CAD", "cad": "CAD",
    "新台币": "TWD", "twd": "TWD", "台币": "TWD",
    "新加坡元": "SGD", "sgd": "SGD", "新币": "SGD",
    "卢布": "RUB", "rub": "RUB",
    "印度卢比": "INR", "inr": "INR",
    "泰铢": "THB", "thb": "THB",
    "瑞士法郎": "CHF", "chf": "CHF",
}


def _is_currency(u: str) -> bool:
    return u in CURRENCY_MAP or u.upper() in CURRENCY_MAP.values()


def _currency_code(u: str) -> str:
    """返回 ISO 4217 货币代码"""
    return CURRENCY_MAP.get(u, u.upper())


async def _convert_currency(v: float, fr: str, to: str) -> str:
    code_fr = _currency_code(fr)
    code_to = _currency_code(to)

    if code_fr == code_to:
        return f"{v} {code_fr} = {v} {code_to}（同币种~）"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            url = f"https://api.frankfurter.app/latest?amount={v}&from={code_fr}&to={code_to}"
            resp = await client.get(url, headers={"User-Agent": "TangTangBot/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                rates = data.get("rates", {})
                if code_to in rates:
                    r = rates[code_to]
                    logger.info(f"💱 货币换算: {v} {code_fr} → {_fmt(r)} {code_to}")
                    return (
                        f"{v} {code_fr} = {_fmt(r)} {code_to}\n"
                        f"（汇率来源：欧洲央行，更新时间 {data.get('date', '?')}）"
                    )
            return f"货币换算失败…API返回了意外的结果（{resp.status_code}），稍后再试喵~"
    except Exception as e:
        logger.error(f"货币换算失败 [{code_fr}→{code_to}]: {e}")
        return f"货币换算暂时失败了喵…（{str(e)[:60]}）"


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _fmt(n: float) -> str:
    """格式化数字：去掉多余的尾零"""
    if abs(n) < 0.01 and n != 0:
        return f"{n:.6g}"
    if abs(n) >= 1e6 or abs(n) < 0.001:
        return f"{n:.6g}"
    if n == int(n):
        return str(int(n))
    # 显示最多4位小数，去掉尾零
    s = f"{n:.4f}"
    s = s.rstrip("0").rstrip(".")
    return s
