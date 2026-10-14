"""
🌐 翻译技能 — 让糖糖能在多语言之间切换

MyMemory 免费 API + 字符集自动检测源语言。无需 Key。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("糖糖.Translate")

LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "pt": "葡萄牙语",
    "ru": "俄语", "it": "意大利语", "ar": "阿拉伯语", "th": "泰语",
    "vi": "越南语", "id": "印尼语",
}

NAME_TO_CODE = {v: k for k, v in LANG_NAMES.items()}
NAME_TO_CODE.update({
    "中文": "zh", "英文": "en", "日文": "ja", "韩文": "ko",
    "法文": "fr", "德文": "de", "西班牙文": "es", "俄文": "ru",
    "繁体": "zh-TW", "简体": "zh-CN", "日语": "ja", "韩语": "ko",
})


def _register():
    from .skills import register_skill

    @register_skill(
        "translate",
        "在多种语言之间翻译文本，支持中英日韩法德西俄等16种语言互译。你（LLM）自己猜的翻译可能有语法错误或用词不当——这个工具给出准确的外语对应表达。",
        {
            "text": "要翻译的文本",
            "to_lang": "目标语言，如'英语'、'日语'、'en'、'ja'。不填默认翻译成中文",
        },
    )
    async def _translate_skill(text: str = "", to_lang: str = "") -> str:
        if not text or not text.strip():
            return "请给我要翻译的文字喵~"
        return await translate(text.strip(), to_lang.strip() if to_lang else "")


async def translate(text: str, to_lang: str = "") -> str:
    """翻译文本"""
    import httpx

    to_code = _resolve_lang(to_lang) if to_lang else "zh"
    if not to_code:
        return f"不认识「{to_lang}」这个语言…试试 英语、日语、韩语？"

    # 根据字符集猜源语言
    from_code = _detect_lang(text)
    # 拉丁字母语言（英法西德等）字符集相同，不提前拦截，交给 API 判断
    if from_code == to_code and from_code in ("zh", "ja", "ko", "ru", "ar", "th"):
        return f"「{text}」已经是{_lang_name(to_code)}了呀~"

    to_name = _lang_name(to_code)

    # CJK 语言之间互译（中日韩）直接用 LLM，MyMemory 质量太差
    cjk = {"zh", "ja", "ko"}
    if from_code in cjk and to_code in cjk:
        return _llm_fallback(text, from_code, to_code)

    # 方案 A：MyMemory API
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            url = (
                f"https://api.mymemory.translated.net/get"
                f"?q={text}&langpair={from_code}|{to_code}"
            )
            resp = await client.get(url, headers={"User-Agent": "TangTangBot/1.0"})
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("responseData", {}).get("translatedText", "")
                # 过滤 API 错误信息（如 PLEASE SELECT TWO DISTINCT LANGUAGES）
                if result and result.strip() and \
                   "PLEASE SELECT" not in result.upper() and \
                   "INVALID" not in result.upper() and \
                   result.strip().lower() != text.lower():
                    logger.info(f"🌐 翻译: {text[:30]} → {to_name}: {result[:60]}")
                    return f"「{text}」\n{to_name}：{result}"
    except Exception as e:
        logger.debug(f"MyMemory 翻译失败: {e}")

    # 方案 B：LLM 兜底
    return _llm_fallback(text, from_code, to_code)


def _llm_fallback(text: str, from_code: str, to_code: str) -> str:
    """LLM 翻译提示——MyMemory 不可用或质量不行时用"""
    from_name = _lang_name(from_code)
    to_name = _lang_name(to_code)
    return (
        f"请将以下{from_name}文本翻译成{to_name}。只输出翻译结果，不要加解释或引号：\n\n{text}"
    )


def _detect_lang(text: str) -> str:
    """根据字符集猜测语言代码"""
    # 中文（含汉字）
    if re.search(r'[一-鿿]', text):
        return "zh"
    # 日文假名
    if re.search(r'[぀-ゟ゠-ヿ]', text):
        return "ja"
    # 韩文
    if re.search(r'[가-힯]', text):
        return "ko"
    # 俄文
    if re.search(r'[Ѐ-ӿ]', text):
        return "ru"
    # 阿拉伯文
    if re.search(r'[؀-ۿ]', text):
        return "ar"
    # 泰文
    if re.search(r'[฀-๿]', text):
        return "th"
    # 默认英文
    return "en"


def _resolve_lang(name: str) -> str:
    """语言名 → ISO 代码"""
    name = name.strip().lower()
    if name in LANG_NAMES:
        return name
    if name in NAME_TO_CODE:
        return NAME_TO_CODE[name]
    en_map = {
        "chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
        "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
        "italian": "it", "portuguese": "pt", "arabic": "ar", "thai": "th",
        "vietnamese": "vi", "indonesian": "id",
    }
    if name in en_map:
        return en_map[name]
    return ""


def _lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)
