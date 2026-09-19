"""
小糖糖的语音引擎 🎙️
edge-tts（微软晓伊/晓晓 — 免费、零配置、够自然）

借鉴 EchoBot 的设计：
- 文本预处理：移除 CQ 码 / 贴图 / @ / emoji / markdown
- 长句分段：超过阈值自动按句子切分，降低首字延迟
- 多音色：不同情绪不同音色
- MD5 缓存：相同文本不重复合成
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from .async_io import run_bounded_blocking

logger = logging.getLogger("糖糖.Voice")

# ---- 音色配置 ----
VOICE_DEFAULT = "zh-CN-XiaoyiNeural"     # 晓伊：温柔磁性，最接近真人
VOICE_SWEET   = "zh-CN-XiaoxiaoNeural"   # 晓晓：活泼甜妹，适合撒娇/开心
VOICE_GENTLE  = "zh-CN-XiaoyiNeural"     # 温柔（同晓伊）

# 情绪 → 音色映射（丛雨三音色）
EMOTION_VOICE: dict[str, str] = {
    "开心":   VOICE_SWEET,
    "兴奋":   VOICE_SWEET,
    "得意":   VOICE_SWEET,
    "撒娇":   VOICE_SWEET,
    "害羞":   VOICE_SWEET,
    "鼓励":   VOICE_SWEET,
    "搞笑":   VOICE_SWEET,
    # 其余用默认晓伊
}

# 情绪 → 语速微调（edge-tts 用）
EMOTION_SPEED: dict[str, str] = {
    "开心":   "+5%", "兴奋":   "+8%", "得意":   "+3%",
    "鼓励":   "+3%", "搞笑":   "+6%", "惊讶":   "+5%",
    "生气":   "+8%", "愤怒":   "+10%", "厌恶": "-3%",
    "害羞":   "-5%", "撒娇":   "-5%", "温柔":   "+0%",
    "认真":   "+0%", "悄悄话": "-10%",
    "难过":   "-8%", "伤心":   "-8%",
    "无语":   "-3%", "无奈":   "-3%",
    "傲娇":   "+3%",
}

# LLM 可直接选择的韵律参数。系统只校验和执行，不从情绪替 LLM 推断。
VOICE_SPEED_MIN = 0.75
VOICE_SPEED_MAX = 1.25
VOICE_PAUSE_STYLES = ("紧凑", "自然", "舒缓")


def _apply_pause_style(text: str, pause: str) -> str:
    """把 LLM 选择的停顿风格转换为 TTS 可执行文本。"""
    if pause == "紧凑":
        return re.sub(r"[，,、；;：:]+", " ", text)
    if pause == "舒缓":
        # 「、」常连接极短并列词，按它切分会制造「才、」一类碎片；
        # 只在完整分句标点后换行，让引擎自行保留自然段间停顿。
        return re.sub(r"([，,；;。！？!?…]+)", r"\1\n", text).strip()
    return text

# 长句分段阈值（超过此长度按句子切分）
STREAMING_CHAR_THRESHOLD = 60

_PROMPT_REV = 4           # GPT-SoVITS prompt/参考音频版本——参数语义变化时
                          # 递增，让旧合成缓存自然失效（2026-08-26：米雪儿中文参考库）

# ---- 文本清洗（借鉴 EchoBot text.py）----

# CQ 码：图片、回复、@、表情、记录、文件等
_CQ_PATTERN = re.compile(r'\[CQ:[^\]]+\]')
# 贴图标签
_STICKER_PATTERN = re.compile(r'\[贴图[：:][^\]]*\]')
# @昵称（中文/英文/数字/下划线）
_AT_PATTERN = re.compile(r'@[一-鿿\w]{1,20}')
# Markdown 标记
_MD_FENCE = re.compile(r'^\s*(```|~~~)[^\n]*$', re.MULTILINE)
_MD_INLINE_CODE = re.compile(r'`([^`]+)`')
_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_MD_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+', re.MULTILINE)
_MD_QUOTE = re.compile(r'^\s*>\s?', re.MULTILINE)
_MD_LIST = re.compile(r'^\s*[-*+]\s+', re.MULTILINE)
_MD_ORDERED = re.compile(r'^\s*\d+[.)]\s+', re.MULTILINE)
_MD_MARKER = re.compile(r'[*_~]')
# 换行归一化
_NEWLINES = re.compile(r'\n{3,}')

# Emoji 码点范围（同 EchoBot）
_EMOJI_CODEPOINTS = {0x200D, 0x20E3, 0xFE0E, 0xFE0F}
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F1E6, 0x1F1FF),   # flags
    (0x1F3FB, 0x1F3FF),   # skin tone
    (0x1F300, 0x1F5FF),   # symbols & pictographs
    (0x1F600, 0x1F64F),   # emoticons
    (0x1F680, 0x1F6FF),   # transport & map
    (0x1F700, 0x1F77F),   # alchemical
    (0x1F780, 0x1F7FF),   # geometric extended
    (0x1F800, 0x1F8FF),   # arrows-c
    (0x1F900, 0x1F9FF),   # supplemental symbols
    (0x1FA70, 0x1FAFF),   # symbols extended-a
    (0x2600,  0x26FF),    # misc symbols
    (0x2700,  0x27BF),    # dingbats
)


def clean_text_for_tts(text: str, keep_tilde: bool = False, speech_friendly: bool = False) -> str:
    """TTS 前清洗文本。
    keep_tilde=True: 保留波浪号
    speech_friendly=True: 保留所有语气符号（！？！。…——等），不删猫叫拟声词（喵~呼噜）"""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")

    # 1. 移除 CQ 码（回复引用、图片、@、表情等）
    t = _CQ_PATTERN.sub("", t)
    # 2. 移除贴图标签
    t = _STICKER_PATTERN.sub("", t)
    # 3. @昵称 → 保留人名（去掉@符号）
    t = _AT_PATTERN.sub(lambda m: m.group()[1:], t)
    # 4. 语气符号 & 猫叫拟声词
    if speech_friendly:
        pass  # 语音专用模式：保留所有语气符号，不删猫叫
    else:
        if not keep_tilde:
            t = re.sub(r'~+', '', t)
            t = re.sub(r'～+', '', t)
        t = re.sub(r'(喵|呼噜|哼|嗯)[!！]+', r'\1', t)
        t = re.sub(r'[喵呼][~～噜]{1,6}', '', t)
        t = re.sub(r'^\s*(蹭蹭|贴贴|摇尾巴|耷拉耳朵|炸毛|哈气|哼)[~！!～]{0,4}\s*$', '', t, flags=re.MULTILINE)

    # 4a. 去掉括号内的动作描述：（笑）（眨眼）= 会干扰朗读，所有 TTS 都去掉
    t = re.sub(r'（[^）]{1,15}）', '', t)
    t = re.sub(r'\([^)]{1,20}\)', '', t)

    # 5. Markdown 清理
    t = _MD_FENCE.sub("", t)
    t = _MD_INLINE_CODE.sub(r"\1", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _MD_HEADING.sub("", t)
    t = _MD_QUOTE.sub("", t)
    t = _MD_LIST.sub("", t)
    t = _MD_ORDERED.sub("", t)
    # ~ 是糖糖的语尾，语音模式下不删
    if not speech_friendly and not keep_tilde:
        t = _MD_MARKER.sub("", t)
    elif speech_friendly or keep_tilde:
        t = re.sub(r'[*_]', '', t)  # 只删 * _ ，保留 ~

    # 5. Emoji → 空格
    cleaned = ""
    for ch in t:
        cp = ord(ch)
        if cp in _EMOJI_CODEPOINTS:
            cleaned += " "
        elif any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
            cleaned += " "
        else:
            cleaned += ch

    # 6. 压缩空白
    t = " ".join(cleaned.split())
    # 7. 合并过多的换行
    t = _NEWLINES.sub("\n\n", t)

    return t.strip()


# 英文 → 中文音译表（GPT-SoVITS 能读中文音译但读不了英文原文）
_ENGLISH_TRANSLIT = {
    "bug": "霸格", "BUG": "霸格", "Bug": "霸格",
    "ok": "欧剋", "OK": "欧剋", "Ok": "欧剋", "okay": "欧剋",
    "qq": "扣扣", "QQ": "扣扣",
    "ai": "人工智能", "AI": "人工智能",
    "app": "应用", "APP": "应用",
    "wifi": "无线网", "WiFi": "无线网", "Wi-fi": "无线网",
    "cpu": "处理器", "CPU": "处理器",
    "gpu": "显卡", "GPU": "显卡",
    "pc": "电脑", "PC": "电脑",
    "api": "接口", "API": "接口",
    "http": "网页协议", "HTTP": "网页协议",
    "url": "链接", "URL": "链接",
    "jpg": "图片", "png": "图片", "gif": "动图",
    "no": "不", "yes": "是", "hello": "你好", "hi": "嗨",
    "bye": "拜拜", "good": "好", "nice": "不错", "cool": "酷",
    "sorry": "对不起", "thanks": "谢谢", "thank": "谢谢",
}

# 按长度降序排序（长词优先匹配），模块加载时排一次即可
_TRANSLIT_SORTED = sorted(_ENGLISH_TRANSLIT.items(), key=lambda x: -len(x[0]))

def _transliterate_english(text: str) -> str:
    """把英文词音译成中文，GPT-SoVITS 能读。音译表里有的用音译，没有的直接删。"""
    import re as _re
    for en, zh in _TRANSLIT_SORTED:
        text = text.replace(en, zh)
    # 剩下的未知英文词直接删
    text = _re.sub(r'[a-zA-Z]+', '', text)
    return text


def split_sentences(text: str, max_chars: int = STREAMING_CHAR_THRESHOLD) -> list[str]:
    """按句子边界切分。<3字拼到相邻句，>55字硬切到逗号。"""
    text = text.strip()
    raw = re.split(r'(?<=[。！？!?~～\n])\s*', text)
    parts = [p.strip() for p in raw if p.strip()]
    if len(parts) <= 1:
        return [text]
    # Merge <3 char fragments with neighbors (prevent empty edge-tts audio)
    merged = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if len(p) < 3 and merged:
            merged[-1] = merged[-1] + " " + p
        elif len(p) < 3 and i + 1 < len(parts):
            parts[i+1] = p + " " + parts[i+1]
        else:
            merged.append(p)
        i += 1
    # Hard-split >55 char segments at commas
    result = []
    for p in merged:
        if len(p) <= 55:
            result.append(p)
        else:
            sub = re.split(r"(?<=[，,、])\s*", p)
            buf = ""
            for s in sub:
                s = s.strip()
                if not s: continue
                if buf and len(buf) + len(s) < 55:
                    buf = buf + " " + s
                else:
                    if buf: result.append(buf)
                    buf = s
            if buf: result.append(buf)
    return result if result else [text]


def mood_to_emotion(mood) -> str:
    """三维情绪模型（energy/mood/patience）→ 语音情绪标签。

    2026-08-24 批1：mood 此前从未参与语音选音色（情绪突变根因之一）。
    只在明显偏离中性时给标签——情绪是弥漫倾向，不是每句话都用力；
    不显著时返回空串，走默认 normal。"""
    try:
        e, m = mood.energy, mood.mood
    except Exception:
        return ""
    if m >= 85:
        return "开心"
    if m <= 45:
        return "难过"
    if e <= 30:
        return "悄悄话"
    return ""


class VoiceEngine:
    """语音合成引擎。支持 edge-tts (默认) 和 CosyVoice2 (克隆音色)。"""

    MODEL_PROFILES = {
        "v4": {
            "gpt": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "sovits": "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth",
        },
        "michele": {
            "gpt": "models/michele/MiXueErV3-e10.ckpt",
            "sovits": "models/michele/MiXueErV3_e100_s4900.pth",
        },
    }

    def __init__(self, voice_dir: str = "./voice_cache",
                 appid: str = "", access_token: str = "",
                 provider: str = "edge-tts", cosy_speaker: str = "tangtang"):
        self.voice_dir = Path(voice_dir)
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.cosy_speaker = cosy_speaker
        self.voice_lang = "zh"
        self.current_speaker = "murasame"  # 糖糖暂用丛雨参考音；米雪儿为空时走情绪映射
        self.model_profile = "v4"
        self._loaded_model_profile: str | None = None
        # GPT-SoVITS 单进程只有一组全局权重；切模型与合成必须在同一把锁内。
        self._model_lock = asyncio.Lock()
        self._on_gptsovits_failure = None  # 连续失败回调（service_manager.restart）
        self._cache_writes = 0  # 2026-08-24 批C：运行中节流清理计数
        self._cleanup_cache()  # 2026-08-10：启动时清理超限缓存（防磁盘无限膨胀）

    # ---- 缓存管理（2026-08-10）----

    CACHE_MAX_FILES = 500   # 缓存文件上限（约 160MB）
    CACHE_MAX_SIZE_MB = 200

    def _cleanup_cache(self):
        """启动时清理语音缓存——超过上限按 mtime 删最旧。
        语音缓存按文本 md5 命名，重复文本会命中，但长期运行仍会无限膨胀。"""
        try:
            files = [f for f in self.voice_dir.glob("*")
                     if f.is_file() and f.suffix in (".wav", ".mp3")]
            total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
            # 2026-08-10：同时按文件数和总字节数清理（大文件多时按字节兜底）
            if len(files) <= self.CACHE_MAX_FILES and total_mb <= self.CACHE_MAX_SIZE_MB:
                return
            # 按修改时间排序，删最旧的直到文件数和字节都达标（各留 90% 余量）
            files.sort(key=lambda f: f.stat().st_mtime)
            keep = max(1, int(len(files) * 0.9))
            removed = 0
            for f in files:
                if len(files) - removed <= keep:
                    break
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            if removed:
                logger.info(f"🎙️ 语音缓存清理: {removed} 个旧文件（{total_mb:.0f}MB → 已控制）")
        except Exception:
            pass

    def _note_cache_write(self):
        """缓存写入后调用——每 100 次写入触发一次清理（2026-08-24 批C）。
        此前清理只在启动时跑一次：单次运行涨破上限要等下次重启。"""
        self._cache_writes += 1
        if self._cache_writes % 100 == 0:
            self._cleanup_cache()

    # ---- 核心 TTS ----

    async def tts(self, text: str, emotion_tag: str = "", speed: float | None = None,
                  pause: str = "自然") -> str | None:
        """合成单段文本为语音文件，返回文件路径。
        GPT-SoVITS 不稳定时自动降级到 edge-tts。"""
        if self.provider == "cosyvoice":
            return await self._cosy_tts(text, emotion_tag, speed=speed, pause=pause)
        if self.provider == "gpt-sovits":
            async with self._model_lock:
                ready = await self._switch_model_locked(self.model_profile)
                result = await self._gpt_sovits_tts(
                    text, emotion_tag, lang=self.voice_lang, speed=speed, pause=pause
                ) if ready else None
            if result:
                return result
            # GPT-SoVITS 失败 → 降级 edge-tts
            logger.info(f"🎙️ GPT-SoVITS 不可用，降级到 edge-tts")
            return await self._edge_tts(text, emotion_tag, speed=speed, pause=pause)
        return await self._edge_tts(text, emotion_tag, speed=speed, pause=pause)

    # GPT-SoVITS 情绪 → 米雪儿中文参考音频。
    GPT_SOVITS_URL = "http://127.0.0.1:9880"
    # 2026-08-15：从硬编码的本机绝对路径改为相对项目根（与 service_manager.GPT_SOVITS_DIR 同规则）
    GPT_SPEAKER_DIR = Path(__file__).resolve().parent.parent / "gpt-sovits" / "speakers"

    async def switch_model(self, profile: str, *, voice_lang: str | None = None,
                           current_speaker: str | None = None) -> bool:
        """原子切换运行时权重、目标语言与参考音。"""
        if profile not in self.MODEL_PROFILES:
            logger.error(f"未知 GPT-SoVITS 模型 profile: {profile}")
            return False
        if self.provider != "gpt-sovits":
            self.model_profile = profile
            if voice_lang is not None:
                self.voice_lang = voice_lang
            if current_speaker is not None:
                self.current_speaker = current_speaker
            return True
        async with self._model_lock:
            if not await self._switch_model_locked(profile):
                return False
            if voice_lang is not None:
                self.voice_lang = voice_lang
            if current_speaker is not None:
                self.current_speaker = current_speaker
            return True

    async def _switch_model_locked(self, profile: str) -> bool:
        if profile not in self.MODEL_PROFILES:
            return False
        if self._loaded_model_profile == profile:
            self.model_profile = profile
            return True

        previous = self.model_profile
        if await self._apply_model_profile(profile):
            self.model_profile = profile
            self._loaded_model_profile = profile
            logger.info(f"🎙️ GPT-SoVITS 模型已切换: {profile}")
            return True

        # 两个权重接口分步执行，任一步失败都恢复进入切换前的完整 profile。
        if previous in self.MODEL_PROFILES and previous != profile:
            if await self._apply_model_profile(previous):
                self._loaded_model_profile = previous
            else:
                self._loaded_model_profile = None
        return False

    async def _apply_model_profile(self, profile: str) -> bool:
        weights = self.MODEL_PROFILES[profile]
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                for endpoint, path in (
                    ("set_sovits_weights", weights["sovits"]),
                    ("set_gpt_weights", weights["gpt"]),
                ):
                    resp = await client.get(
                        f"{self.GPT_SOVITS_URL}/{endpoint}",
                        params={"weights_path": path, "persist": "false"},
                    )
                    if resp.status_code != 200:
                        logger.error(f"GPT-SoVITS {endpoint} 失败: HTTP {resp.status_code}")
                        return False
            return True
        except Exception as exc:
            logger.error(f"GPT-SoVITS 模型切换失败 [{profile}]: {exc}")
            return False

    _EMOTION_SPEAKER = {
        "开心": "happy", "兴奋": "excited", "得意": "tsundere", "鼓励": "happy",
        "搞笑": "excited", "惊讶": "surprised",
        "害羞": "tsundere", "撒娇": "coquettish",
        "难过": "sad", "伤心": "sad",
        "温柔": "gentle", "认真": "gentle", "悄悄话": "gentle",
        "生气": "angry", "愤怒": "angry", "厌恶": "disgust",
        "无语": "normal", "无奈": "normal",
        "傲娇": "tsundere",
    }

    # 2026-08-26：米雪儿中文模型盲听定稿。每个 speaker 的 ref.wav 与 prompt
    # 来自同一原始切片；prompt 经过 SenseVoice/Whisper 交叉转写和人工语义校正。
    # prompt_lang/prompt_text 描述的是参考音频本身——不是目标文本！
    # 新增 speaker 目录必须同步本表（test_voice_quality_gate 有闸门钉住）。
    _SPEAKER_PROMPT = {
        "normal": ("zh", "正义是一种信念，我理想中的搜查官应该要有拯救世界的觉悟。可是我究竟还要多久，才能成为那样的人呢？"),
        "gentle": ("zh", "话说，你平时休息的时候会做些什么呢？我们之间应该有很多相似之处吧。"),
        "happy": ("zh", "喵，我的衣服是不是也挺可爱的？"),
        "excited": ("zh", "对了，最近刚推出了一款双人游戏，我很想要和你一起玩呢。"),
        "coquettish": ("zh", "嗯，我没有再偷懒哦，这是难得的休息时间了。"),
        "tsundere": ("zh", "嗯，你又来找我玩了吗？我正准备再看一集动画呢。嗯，不过先来陪你也是可以的啦。"),
        "sad": ("zh", "哼，我一定一定不会原谅你。"),
        "surprised": ("zh", "原来你这么了解我。嘿，其实我没有想到自己能收到这么独一无二的礼物，真是谢谢你了。"),
        "angry": ("zh", "可恶，我竟然会被你这种家伙干掉。"),
        "disgust": ("zh", "哇，好疼！"),
        "murasame": ("ja", "今は寝かせておいてやれ 徹夜でご主人の看病をしておったのだ"),
        "xueli": ("ja", "いや、現実に魔法があるっていうので、私はもうびっくりなんですけどね"),
        "xueli_gentle": ("ja", "すごいすごいすごい、ノアさん、サインください!"),
        "xueli_happy": ("ja", "あなたからは面白そうな匂いがぷんぷんしているので"),
        "xueli_tsundere": ("ja", "なんかすごいことが起こっているのを感じます"),
    }

    def _resolve_speaker(self, emotion_tag: str) -> str:
        """显式角色（/角色 xxx）优先；默认空 → 按情绪标签选参考音频。

        2026-08-14 修复：current_speaker 曾默认 "happy"，恒为真值导致
        _EMOTION_SPEAKER 情绪映射永不生效（情绪 TTS 隐性失效）。
        2026-08-24 批1：未知/无标签的兜底从 "happy" 改为 "normal"——此前
        voice_description 显示 normal、实际合成用 happy（日志实证，货不对板），
        且中性陈述不该用开心声线。"""
        return self.current_speaker if self.current_speaker else self._EMOTION_SPEAKER.get(emotion_tag, "normal")

    async def _gpt_sovits_tts(self, text: str, emotion_tag: str = "", lang: str = "zh",
                              speed: float | None = None, pause: str = "自然") -> str | None:
        """GPT-SoVITS 合成。自动检测中日混合文本选择最优 lang。"""
        if not text or len(text) < 2:
            return None
        clean = clean_text_for_tts(text, keep_tilde=True, speech_friendly=True)
        clean = clean.strip("。，、！？…—.,!?;: ")
        if lang in ("zh", "ja"):
            clean = _transliterate_english(clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        pause = pause if pause in VOICE_PAUSE_STYLES else "自然"
        clean = _apply_pause_style(clean, pause)
        if len(clean) < 2:
            return None

        speaker = self._resolve_speaker(emotion_tag)
        speed = 1.0 if speed is None else max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, float(speed)))

        cache_key = hashlib.md5(
            f"gptsovits|{self.model_profile}|{speaker}|{lang}|{clean}|s{speed:.2f}|p{_PROMPT_REV}".encode()
        ).hexdigest()[:12]
        output_file = self.voice_dir / f"{cache_key}.wav"

        if output_file.exists() and output_file.stat().st_size > 1000:
            return str(output_file)

        # 语言选择：显式指定优先，否则自动检测（含假名→ja，纯中文→zh）
        if lang in ("auto", "zh"):
            has_jp = bool(re.search(r'[぀-ゟ゠-ヿ]', clean))
            tts_lang = "ja" if has_jp else "zh"
        else:
            tts_lang = lang
        # prompt_lang/prompt_text 必须描述参考音频本身（见 _SPEAKER_PROMPT 注释）；
        # 表缺失时 fallback normal——闸门测试会抓住缺项，此处仅保运行时不出错
        prompt_lang, prompt_text = self._SPEAKER_PROMPT.get(speaker, self._SPEAKER_PROMPT["normal"])

        ref_wav = self.GPT_SPEAKER_DIR / speaker / "ref.wav"
        if not ref_wav.exists():
            logger.warning(f"GPT-SoVITS speaker '{speaker}' not found, using normal")
            ref_wav = self.GPT_SPEAKER_DIR / "normal" / "ref.wav"
            if not ref_wav.exists():
                return None

        last_error = ""
        got_200 = False  # 收到过 200 说明服务进程可达；不因响应体异常重启进程
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    # cut0：2026-08-24 批A 后复测——v4 + prompt 修复后 cut5 与 cut0
                    # 时长一致（33字 6.81s vs 7.05s；98字 18.56s vs 18.60s），
                    # 早前的 cut5 丢段（1.28s）确认为 prompt 错位表现而非切分 bug。
                    # cut5 无增益 → 维持 cut0。
                    resp = await client.post(
                        f"{self.GPT_SOVITS_URL}/tts",
                        json={
                            "text": clean, "text_lang": tts_lang,
                            "ref_audio_path": str(ref_wav),
                            "prompt_lang": prompt_lang,
                            "prompt_text": prompt_text,
                            "text_split_method": "cut0", "batch_size": 1,
                            "media_type": "wav", "streaming_mode": False,
                            "speed_factor": speed,
                            # 显式换行已表达舒缓分句，不再叠加固定静音。
                            "fragment_interval": 0.0,
                            "parallel_infer": True,     # 并行推理提速
                        },
                    )
                    if resp.status_code == 200:
                        got_200 = True
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        await run_bounded_blocking(
                            "voice.gpt_sovits_cache_write",
                            output_file.write_bytes,
                            resp.content,
                            logger=logger,
                            log_prefix="🎙️ 语音缓存文件写入较慢",
                        )
                        self._note_cache_write()
                        logger.info(f"🎙️ [GPT-SoVITS/{speaker}] {len(resp.content)} bytes")
                        return str(output_file)
                    err = resp.json().get("message", resp.text[:100]) if resp.text else "empty"
                    last_error = f"{resp.status_code} — {err}"
            except Exception as e:
                last_error = str(e)[:100]
            if attempt < 2:
                await asyncio.sleep(1.0)  # 等一秒重试

        logger.warning(f"GPT-SoVITS 3次尝试均失败: {last_error} (text={clean[:50]!r})")
        # 只有全程未收到 200（502/连接失败/超时）才说明服务端可能异常，
        # 交给 service_manager 重启；200 但响应体过小只走 Edge 降级。
        if not got_200 and self._on_gptsovits_failure:
            try:
                await self._on_gptsovits_failure()
                self._loaded_model_profile = None
            except Exception:
                pass
        return None

    # 情绪 → CosyVoice speed 倍率
    _EMOTION_SPEED_RATIO: dict[str, float] = {
        "开心": 1.05, "兴奋": 1.08, "得意": 1.03, "鼓励": 1.03,
        "搞笑": 1.06, "惊讶": 1.05, "生气": 1.08, "愤怒": 1.10, "厌恶": 0.97,
        "害羞": 0.95, "撒娇": 0.95, "温柔": 1.00, "认真": 1.00,
        "悄悄话": 0.90, "难过": 0.92, "伤心": 0.92, "无语": 0.97, "无奈": 0.97,
    }

    async def _cosy_tts(self, text: str, emotion_tag: str = "", speed: float | None = None,
                        pause: str = "自然") -> str | None:
        """CosyVoice2 本地合成（克隆音色），带情绪语速调整，输出 MP3"""
        if not text or len(text) < 2:
            return None

        clean = clean_text_for_tts(text, keep_tilde=True, speech_friendly=True)
        clean = _apply_pause_style(clean, pause)
        if len(clean) < 2:
            return None

        # 旧调用方未传 speed 时保留原有情绪映射；聊天主链由 LLM 显式选择。
        speed = self._EMOTION_SPEED_RATIO.get(emotion_tag, 1.0) if speed is None else speed

        cache_key = hashlib.md5(f"cosy|{self.cosy_speaker}|{clean}|s{speed:.2f}".encode()).hexdigest()[:12]
        output_file = self.voice_dir / f"{cache_key}.mp3"  # MP3 format for QQ

        if output_file.exists() and output_file.stat().st_size > 0:
            return str(output_file)

        try:
            from .cosy_voice import get_cosy_engine
            engine = get_cosy_engine()
            result = await engine.synthesize(clean, speaker_id=self.cosy_speaker, speed=speed)
            if result:
                wav_bytes, _ = result
                # Convert WAV → MP3 for QQ compatibility
                import tempfile
                tmp_wav = Path(tempfile.gettempdir()) / f"cosy_{cache_key}.wav"
                await run_bounded_blocking(
                    "voice.cosy_tmp_wav_write",
                    tmp_wav.write_bytes,
                    wav_bytes,
                    logger=logger,
                    log_prefix="🎙️ CosyVoice 临时 WAV 写入较慢",
                )
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", str(tmp_wav), "-codec:a", "libmp3lame",
                        "-b:a", "128k", "-ar", "24000", str(output_file),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0:
                        logger.info(f"🎙️ [CosyVoice/{self.cosy_speaker}] → {cache_key} (WAV→MP3)")
                    else:
                        raise RuntimeError(f"ffmpeg exit code {proc.returncode}")
                except asyncio.TimeoutError:
                    # 超时 → 杀掉进程，避免孤儿进程累积
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except Exception:
                        pass
                    logger.warning(f"ffmpeg 转 MP3 超时 (30s)，保留 WAV")
                    output_file = self.voice_dir / f"{cache_key}.wav"
                    await run_bounded_blocking(
                        "voice.cosy_fallback_wav_write",
                        output_file.write_bytes,
                        wav_bytes,
                        logger=logger,
                        log_prefix="🎙️ CosyVoice 降级 WAV 写入较慢",
                    )
                    self._note_cache_write()
                except Exception as e:
                    # 其他异常也确保进程被清理
                    if 'proc' in locals() and proc.returncode is None:
                        try:
                            proc.kill()
                            await asyncio.wait_for(proc.wait(), timeout=5)
                        except Exception:
                            pass
                    logger.warning(f"ffmpeg 转 MP3 失败: {e}，保留 WAV")
                    output_file = self.voice_dir / f"{cache_key}.wav"
                    await run_bounded_blocking(
                        "voice.cosy_fallback_wav_write",
                        output_file.write_bytes,
                        wav_bytes,
                        logger=logger,
                        log_prefix="🎙️ CosyVoice 降级 WAV 写入较慢",
                    )
                    self._note_cache_write()
                finally:
                    await run_bounded_blocking(
                        "voice.cosy_tmp_wav_cleanup",
                        tmp_wav.unlink,
                        missing_ok=True,
                        logger=logger,
                        log_prefix="🎙️ CosyVoice 临时 WAV 清理较慢",
                    )
                return str(output_file)
            return None
        except Exception as e:
            logger.warning(f"CosyVoice TTS 失败: {e}")
            return None

    async def _edge_tts(self, text: str, emotion_tag: str = "", speed: float | None = None,
                        pause: str = "自然") -> str | None:
        """edge-tts 云端合成"""
        if not text or len(text) < 2:
            return None

        clean = clean_text_for_tts(text, keep_tilde=True, speech_friendly=True)
        clean = _apply_pause_style(clean, pause)
        if len(clean) < 2:
            return None

        voice = EMOTION_VOICE.get(emotion_tag, VOICE_DEFAULT)
        if speed is None:
            rate = EMOTION_SPEED.get(emotion_tag, "+0%")
        else:
            rate = f"{round((speed - 1.0) * 100):+d}%"

        cache_key = hashlib.md5(f"{clean}|{voice}|{rate}".encode()).hexdigest()[:12]
        output_file = self.voice_dir / f"{cache_key}.mp3"

        if output_file.exists() and output_file.stat().st_size > 0:
            return str(output_file)

        try:
            import edge_tts
            communicate = edge_tts.Communicate(text=clean, voice=voice, rate=rate)
            await communicate.save(str(output_file))
            self._note_cache_write()
            logger.info(f"🎙️ [{emotion_tag or '默认'}] voice={voice} rate={rate} → {cache_key}")
            return str(output_file)
        except ImportError:
            logger.error("❌ pip install edge-tts")
            return None
        except Exception as e:
            logger.warning(f"TTS 失败: {e}")
            return None

    async def tts_streaming(self, text: str, emotion_tag: str = "", speed: float | None = None,
                            pause: str = "自然") -> list[str]:
        """合成语音。≤100 字全文合成；>100 字只合成第一段。

        注：发送端（handler._send_voice_reply）只发 audio_files[0]——
        超长时后半段本就听不到（工具描述/角色卡已约束 LLM 不超过 100 字）。
        2026-08-10 之前会切分多段全合成（白费 GPT-SoVITS 调用），已改为只合成第一段。"""
        clean = clean_text_for_tts(text, keep_tilde=True, speech_friendly=True)
        if len(clean) < 3:
            return []
        if len(clean) <= 100:
            path = await self.tts(clean, emotion_tag, speed=speed, pause=pause)
            return [path] if path else []
        # >100 字：只合成第一段（发送端只发第一段，其余白费）
        logger.info(f"🎙️ 语音文本 {len(clean)}字，超长只合成第一段")
        first_sent = split_sentences(clean, max_chars=80)[0]
        path = await self.tts(first_sent, emotion_tag, speed=speed, pause=pause)
        return [path] if path else []

    async def tts_streaming_with_profile(
            self, text: str, emotion_tag: str = "", speed: float | None = None,
            pause: str = "自然", *, model_profile: str | None = None,
            speaker: str | None = None, voice_lang: str | None = None) -> list[str]:
        """按调用方冻结的模型/参考音原子合成，不把全局角色状态泄漏给并发回合。

        scheduled media 在创建时冻结 profile，到了执行时才生成语音；GPT-SoVITS
        是单进程全局权重，因此切模、设参考音、合成必须持有同一把锁。合成完成
        后只恢复内存中的选择，已加载权重留给下一次调用按需切回，避免重复网络
        切换。旧 provider 也走同一接口，保持测试和降级路径兼容。
        """
        clean = clean_text_for_tts(text, keep_tilde=True, speech_friendly=True)
        if len(clean) < 3:
            return []
        profile = model_profile or self.model_profile
        selected_speaker = self.current_speaker if speaker is None else str(speaker)
        selected_lang = voice_lang or self.voice_lang
        first = clean
        if len(first) > 100:
            logger.info(f"🎙️ 任务语音文本 {len(first)}字，超长只合成第一段")
            first = split_sentences(first, max_chars=80)[0]

        if self.provider != "gpt-sovits":
            previous = (self.model_profile, self.current_speaker, self.voice_lang)
            self.model_profile = profile
            self.current_speaker = selected_speaker
            self.voice_lang = selected_lang
            try:
                return await self.tts_streaming(clean, emotion_tag, speed=speed, pause=pause)
            finally:
                self.model_profile, self.current_speaker, self.voice_lang = previous

        async with self._model_lock:
            previous_profile = self.model_profile
            previous_speaker = self.current_speaker
            previous_lang = self.voice_lang
            path = None
            if await self._switch_model_locked(profile):
                self.model_profile = profile
                self.current_speaker = selected_speaker
                self.voice_lang = selected_lang
                try:
                    path = await self._gpt_sovits_tts(
                        first, emotion_tag, lang=selected_lang,
                        speed=speed, pause=pause,
                    )
                finally:
                    self.model_profile = previous_profile
                    self.current_speaker = previous_speaker
                    self.voice_lang = previous_lang
        if path:
            return [path]
        # 与普通 tts 保持一致：GPT-SoVITS 响应不可用时，仍可生成可发送的
        # Edge 语音；调用方会把实际文件哈希写入 outbox，重试不会重新合成。
        fallback = await self._edge_tts(first, emotion_tag, speed=speed, pause=pause)
        return [fallback] if fallback else []

    # ---- CQ 码生成 ----

    def to_cq(self, filepath: str) -> str:
        """文件路径 → QQ 语音 CQ 码"""
        return f"[CQ:record,file=file:///{Path(filepath).resolve().as_posix()}]"

    # ---- 音色查询 ----

    def voice_description(self, tag: str) -> str:
        """情绪标签 → 音色描述"""
        if self.provider == "gpt-sovits":
            speaker = self._resolve_speaker(tag)
            role = "米雪儿" if self.model_profile == "michele" else "丛雨"
            return f"GPT-SoVITS·{role}·{speaker}"
        if self.provider == "cosyvoice":
            return f"克隆音色·{self.cosy_speaker}"
        voice = EMOTION_VOICE.get(tag, VOICE_DEFAULT)
        if voice == VOICE_SWEET:
            return "晓晓·活泼"
        return "晓伊·温柔"

    @property
    def is_available(self) -> bool:
        if self.provider == "cosyvoice":
            try:
                from .cosy_voice import get_cosy_engine
                return get_cosy_engine().available
            except Exception:
                return False
        try:
            import edge_tts
            return True
        except ImportError:
            return False


# ---- 情绪标签工具 ----

def extract_emotion_tag(text: str) -> tuple[str, str]:
    """提取情绪标签并清除所有标签。删除所有 [...] 标签（情绪和非情绪都删），取第一个有效情绪。"""
    tag = ""
    for m in re.finditer(r'\[([^\]]{1,6})\]', text):
        t = m.group(1).strip()
        if t in EMOTION_SPEED:
            tag = t
            break
    # 删除所有方括号标签，不管是不是情绪（[疑问] [俏皮] 等非标准标签全删）
    clean = re.sub(r'\s*\[[^\]]{1,10}\]\s*', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return tag, clean


def get_available_styles_for_prompt() -> str:
    """返回可用情绪标签列表，供 LLM 参考"""
    return "情绪标签：" + " ".join(f"[{t}]" for t in sorted(EMOTION_SPEED))
