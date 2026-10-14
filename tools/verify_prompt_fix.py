"""
2026-08-24 批A 端到端验证：prompt_text/prompt_lang 修好后短句早停是否消失
（修复前短句 ≤26 字稳定早停 0.44s；修复后应输出与字数匹配的时长）
"""
import asyncio
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.voice import VoiceEngine

VOICE_DIR = Path(__file__).resolve().parent.parent / "voice_cache"

SHORT = [
    "好的呢",
    "我知道啦",
    "那今天就先这样吧我们明天再聊呀",
    "其实我刚刚也在想这个问题感觉你说得挺有道理的",
]
LONG = "今天外面下雨了记得带伞路上小心一点到家了跟我说一声我一直在等你回来"


async def main():
    e = VoiceEngine(voice_dir=str(VOICE_DIR), provider="gpt-sovits")
    e._on_gptsovits_failure = None
    for text in SHORT + [LONG]:
        path = await e._gpt_sovits_tts(text)
        if not path:
            print(f"{len(text):2d}字 {text!r}: FAIL（3次尝试不过检→降级）")
            continue
        with wave.open(path, "rb") as w:
            dur = w.getnframes() / w.getframerate()
        print(f"{len(text):2d}字 {text!r}: {dur:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
