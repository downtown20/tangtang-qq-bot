"""
ASR 模型定位修复（2026-08-15）：
旧代码硬编码 model.onnx，但 2025-09-09 int8 版实际文件是 model.int8.onnx——
目录存在却永远「找不到模型」：每次启动重复下载 233MB，加载识别器时 FileNotFoundError。
_find_model_file 按优先级匹配：model.onnx → model.int8.onnx → 任意 .onnx。

SILK v3 解码（2026-08-24）：
QQ 语音 get_record 返回的是 SILK v3（魔数 b'\\x02#!SILK_V3'，文件名带 .amr 后缀），
标准 ffmpeg 无 silk 解码器转码必失败——_convert_to_wav 魔数检测走 pilk 解码。
"""

import asyncio
import math
import struct
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

from agent.asr import _convert_to_wav, _find_model_file


class TestFindModelFile:
    def test_prefers_model_onnx(self, tmp_path):
        (tmp_path / "model.onnx").write_bytes(b"x")
        (tmp_path / "model.int8.onnx").write_bytes(b"y")
        assert _find_model_file(tmp_path).name == "model.onnx"

    def test_int8_fallback(self, tmp_path):
        (tmp_path / "model.int8.onnx").write_bytes(b"y")
        assert _find_model_file(tmp_path).name == "model.int8.onnx"

    def test_any_onnx_fallback(self, tmp_path):
        (tmp_path / "weird-name.onnx").write_bytes(b"z")
        assert _find_model_file(tmp_path).name == "weird-name.onnx"

    def test_missing_dir_returns_none(self, tmp_path):
        assert _find_model_file(tmp_path / "nope") is None

    def test_empty_dir_returns_none(self, tmp_path):
        assert _find_model_file(tmp_path) is None


class TestEnsureModelLocal:
    """模型已在本地时，_ensure_model 不触发下载、直接定位 onnx（旧代码此路径永远失败）"""

    def test_existing_int8_model_ready(self, tmp_path, monkeypatch):
        import agent.asr as asr_mod

        model_dir = tmp_path / asr_mod._SENSE_VOICE_MODEL
        model_dir.mkdir(parents=True)
        (model_dir / "model.int8.onnx").write_bytes(b"y")
        monkeypatch.setattr(asr_mod, "_MODEL_DIR", tmp_path)

        rec = asr_mod.VoiceRecognizer()
        import asyncio

        ok = asyncio.run(rec._ensure_model())
        assert ok is True
        assert rec._model_dir == model_dir
        assert rec._model_file.name == "model.int8.onnx"

    def test_recognizer_load_does_not_block_event_loop(self, monkeypatch):
        """首次 sherpa-onnx 模型加载必须在线程池执行。"""
        import agent.asr as asr_mod

        rec = asr_mod.VoiceRecognizer()
        rec._available = True
        loaded_threads = []

        async def ready_model():
            return True

        def slow_load():
            loaded_threads.append(threading.get_ident())
            time.sleep(0.05)
            rec._recognizer = object()

        monkeypatch.setattr(rec, "_ensure_model", ready_model)
        monkeypatch.setattr(rec, "_load_recognizer", slow_load)

        async def scenario():
            ticks = 0
            stopped = False

            async def ticker():
                nonlocal ticks
                while not stopped:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                ok = await rec.init()
            finally:
                stopped = True
                await task
            return ok, ticks

        ok, ticks = asyncio.run(scenario())
        assert ok is True
        assert ticks > 0
        assert loaded_threads and loaded_threads[0] != threading.get_ident()


class TestConvertToWavSilk:
    """2026-08-24：QQ 语音是 SILK v3——魔数检测走 pilk 解码（ffmpeg 无 silk 解码器）。
    三条真实失败语音实测头字节 b'\\x02#!SILK_V3'，pilk 解码后 SenseVoice 转写成功。"""

    @staticmethod
    def _silk_bytes() -> bytes:
        return b"\x02#!SILK_V3\x1f\x00" + b"\x00" * 100

    def test_silk_magic_routes_to_pilk_decode(self, tmp_path):
        silk = tmp_path / "voice.amr"  # 文件名 .amr，内容是 silk（真实情况）
        silk.write_bytes(self._silk_bytes())
        calls = []

        def _fake_to_wav(src, dst, rate):
            calls.append((src, dst, rate))
            Path(dst).write_bytes(b"fake-wav")

        fake_pilk = types.SimpleNamespace(silk_to_wav=_fake_to_wav)
        with mock.patch.dict(sys.modules, {"pilk": fake_pilk}):
            out = asyncio.run(_convert_to_wav(str(silk)))
        assert out is not None
        assert Path(out).exists()
        assert Path(out).suffix == ".wav"
        assert calls and calls[0][0] == str(silk) and calls[0][2] == 16000

    def test_silk_without_pilk_degrades_to_none(self, tmp_path):
        silk = tmp_path / "voice.amr"
        silk.write_bytes(self._silk_bytes())
        # sys.modules 里放 None → import pilk 抛 ImportError（标准技巧）
        with mock.patch.dict(sys.modules, {"pilk": None}):
            out = asyncio.run(_convert_to_wav(str(silk)))
        assert out is None  # 静默降级，不抛异常

    def test_non_silk_skips_pilk(self, tmp_path):
        # 非 silk 内容不触发 pilk 分支（走 ffmpeg）——pilk 模块不存在也不炸
        p = tmp_path / "other.bin"
        p.write_bytes(b"\x00" * 64)
        with mock.patch.dict(sys.modules, {"pilk": None}):
            with mock.patch("agent.asr.shutil.which", return_value=None):
                out = asyncio.run(_convert_to_wav(str(p)))
        assert out is None  # ffmpeg 未安装路径的既有降级行为

    def test_header_read_does_not_block_event_loop(self, tmp_path):
        """输入音频头读取不能在异步转码函数中同步占住事件循环。"""
        import agent.asr as asr_mod

        audio = tmp_path / "other.amr"
        audio.write_bytes(b"\x00" * 64)
        original_open = open

        class SlowFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self._wrapped.__exit__(*args)

            def read(self, *args, **kwargs):
                import time

                time.sleep(0.05)
                return self._wrapped.read(*args, **kwargs)

        def slow_open(*args, **kwargs):
            return SlowFile(original_open(*args, **kwargs))

        async def scenario():
            ticks = 0
            stopped = False

            async def ticker():
                nonlocal ticks
                while not stopped:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                out = await _convert_to_wav(str(audio))
            finally:
                stopped = True
                await task
            return ticks, out

        with mock.patch.object(asr_mod, "open", slow_open), mock.patch(
            "agent.asr.shutil.which", return_value=None
        ):
            ticks, out = asyncio.run(scenario())

        assert out is None
        assert ticks > 0

    def test_ready_wav_inspection_does_not_block_event_loop(self, tmp_path, monkeypatch):
        """已是 16kHz WAV 的快速路径也不能同步读取文件。"""
        import agent.asr as asr_mod

        audio = tmp_path / "ready.wav"
        audio.write_bytes(b"fake-wav")

        def slow_read(_wav_path):
            import time

            time.sleep(0.05)
            return 16000, []

        monkeypatch.setattr(asr_mod, "_read_wav", slow_read)

        async def scenario():
            ticks = 0
            stopped = False

            async def ticker():
                nonlocal ticks
                while not stopped:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                out = await _convert_to_wav(str(audio))
            finally:
                stopped = True
                await task
            return ticks, out

        ticks, out = asyncio.run(scenario())

        assert out == str(audio)
        assert ticks > 0

    def test_wav_read_does_not_block_transcription_event_loop(self, monkeypatch):
        """转写阶段读取 WAV 样本不能同步阻塞前台事件循环。"""
        import agent.asr as asr_mod

        class Stream:
            result = types.SimpleNamespace(text="你好")

            def accept_waveform(self, *_args):
                pass

        class Recognizer:
            def create_stream(self):
                return Stream()

            def decode_stream(self, _stream):
                pass

        rec = asr_mod.VoiceRecognizer()
        rec._available = True
        rec._recognizer = Recognizer()

        async def fake_convert(_audio_path):
            return "same.wav"

        def slow_read(_wav_path):
            import time

            time.sleep(0.05)
            return 16000, [0.0, 0.1]

        monkeypatch.setattr(asr_mod, "_convert_to_wav", fake_convert)
        monkeypatch.setattr(asr_mod, "_read_wav", slow_read)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", types.SimpleNamespace())

        async def scenario():
            ticks = 0
            stopped = False

            async def ticker():
                nonlocal ticks
                while not stopped:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                text = await rec.transcribe("same.wav")
            finally:
                stopped = True
                await task
            return ticks, text

        ticks, text = asyncio.run(scenario())
        assert text == "你好"
        assert ticks > 0

    def test_transcription_temp_wav_cleanup_does_not_block_event_loop(
        self, tmp_path, monkeypatch
    ):
        """转写阶段的临时 WAV 清理不能同步阻塞事件循环。"""
        import agent.asr as asr_mod

        class Stream:
            result = types.SimpleNamespace(text="你好")

            def accept_waveform(self, *_args):
                pass

        class Recognizer:
            def create_stream(self):
                return Stream()

            def decode_stream(self, _stream):
                pass

        rec = asr_mod.VoiceRecognizer()
        rec._available = True
        rec._recognizer = Recognizer()
        converted = tmp_path / "converted.wav"
        converted.write_bytes(b"wav")

        async def fake_convert(_audio_path):
            return str(converted)

        def fake_read(_wav_path):
            return 16000, [0.0, 0.1]

        unlinks = []
        original_unlink = Path.unlink

        def slow_unlink(path_obj, *args, **kwargs):
            unlinks.append(threading.get_ident())
            time.sleep(0.05)
            return original_unlink(path_obj, *args, **kwargs)

        monkeypatch.setattr(asr_mod, "_convert_to_wav", fake_convert)
        monkeypatch.setattr(asr_mod, "_read_wav", fake_read)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", types.SimpleNamespace())

        async def scenario():
            ticks = 0
            stopped = False

            async def ticker():
                nonlocal ticks
                while not stopped:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            try:
                text = await rec.transcribe("source.amr")
            finally:
                stopped = True
                await task
            return ticks, text

        with mock.patch.object(Path, "unlink", slow_unlink):
            ticks, text = asyncio.run(scenario())

        assert text == "你好"
        assert ticks > 0
        assert unlinks and all(thread_id != threading.get_ident() for thread_id in unlinks)

    def test_real_pilk_roundtrip(self, tmp_path):
        # 真实 pilk：wav→encode→silk→_convert_to_wav→16k wav（自包含端到端）
        pytest.importorskip("pilk")
        import wave

        # 0.5s 440Hz 正弦波 24kHz PCM（QQ silk 原生采样率）
        sr, dur = 24000, 0.5
        n = int(sr * dur)
        pcm = tmp_path / "t.pcm"
        pcm.write_bytes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440.0 * i / sr)))
            for i in range(n)
        ))

        import pilk
        silk = tmp_path / "t.silk"
        pilk.encode(str(pcm), str(silk), pcm_rate=sr, tencent=True)

        out = asyncio.run(_convert_to_wav(str(silk)))
        assert out is not None
        with wave.open(out, "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getnchannels() == 1
            assert 0.3 < wf.getnframes() / 16000 < 0.7  # 时长基本不变
