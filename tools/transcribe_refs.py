"""
一次性工具：转写所有 GPT-SoVITS speaker 参考音频 → prompt_text 表
（2026-08-24 批A：官方定性 prompt_text 与 ref 不匹配 = 短句早停根因。
  SenseVoice 对角色声线转写为乱码（实测），whisper-small 准确——用 whisper。
  模型在 asr_models/whisper-small，hf-mirror 手动下载。）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SPEAKERS_DIR = Path(__file__).resolve().parent.parent / "gpt-sovits" / "speakers"
WHISPER_DIR = Path(__file__).resolve().parent.parent / "asr_models" / "whisper-small"


async def main():
    from faster_whisper import WhisperModel

    model = WhisperModel(str(WHISPER_DIR), device="cpu", compute_type="int8")
    for d in sorted(SPEAKERS_DIR.iterdir()):
        if not d.is_dir():
            continue
        ref = d / "ref.wav"
        if not ref.exists():
            print(f"{d.name}: (无 ref.wav)")
            continue
        segs, info = model.transcribe(str(ref), language=None, beam_size=5)
        lang = info.language
        text = "".join(s.text for s in segs).strip()
        print(f"{d.name} [{lang}] {text!r}")


if __name__ == "__main__":
    asyncio.run(main())
