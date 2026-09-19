"""语音缓存文件 I/O 的事件循环边界回归测试。"""

import asyncio
import threading
import time
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from agent.voice import VoiceEngine


def test_gpt_sovits_audio_cache_write_does_not_block_event_loop(tmp_path):
    main_thread = threading.get_ident()
    writes = []

    class Response:
        status_code = 200
        text = ""
        content = b"x" * 4096

        @staticmethod
        def json():
            return {}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    def slow_write(path_obj, data):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data)

    engine = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
    engine.current_speaker = "normal"
    engine.model_profile = "v4"
    original_write = Path.write_bytes

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
            result = await engine._gpt_sovits_tts("今天天气不错")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch("httpx.AsyncClient", Client), mock.patch.object(
        Path, "write_bytes", slow_write
    ):
        ticks, result = asyncio.run(scenario())

    assert result and Path(result).is_file()
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)


def test_task_voice_preparer_hash_read_does_not_block_event_loop(tmp_path):
    from agent.handler import MessageHandler

    main_thread = threading.get_ident()
    reads = []
    audio = tmp_path / "reply.wav"
    audio.write_bytes(b"audio-bytes")
    original_read = Path.read_bytes

    def slow_read(path_obj):
        reads.append(threading.get_ident())
        time.sleep(0.05)
        return original_read(path_obj)

    class Voice:
        async def tts_streaming_with_profile(self, *_args, **_kwargs):
            return [str(audio)]

        def to_cq(self, path):
            return f"[CQ:record,file={path}]"

    handler = object.__new__(MessageHandler)
    handler.voice_enabled = True
    handler.voice = Voice()
    handler._is_voice_blocked = lambda _scope: False

    async def script(text, nickname):
        return text

    handler._text_to_voice_script = script

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
            result = await handler._task_voice_preparer(
                {"voice_text": "你好", "voice_emotion": "温柔"}, "g1"
            )
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch.object(Path, "read_bytes", slow_read):
        ticks, result = asyncio.run(scenario())

    assert result["actual"]["asset_frozen"] is True
    assert ticks > 0
    assert reads and all(thread_id != main_thread for thread_id in reads)


def test_cosyvoice_audio_cache_write_does_not_block_event_loop(tmp_path):
    """CosyVoice 的 WAV 临时文件与降级缓存写入必须离开事件循环。"""
    main_thread = threading.get_ident()
    writes = []
    original_write = Path.write_bytes

    def slow_write(path_obj, data):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data)

    class Engine:
        async def synthesize(self, *_args, **_kwargs):
            return b"wav-bytes", {"sample_rate": 24000}

    async def fail_ffmpeg(*_args, **_kwargs):
        raise RuntimeError("ffmpeg unavailable")

    engine = VoiceEngine(voice_dir=str(tmp_path), provider="cosyvoice")

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
            result = await engine._cosy_tts("你好呀")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch("agent.cosy_voice.get_cosy_engine", return_value=Engine()), mock.patch(
        "asyncio.create_subprocess_exec", fail_ffmpeg
    ), mock.patch.object(Path, "write_bytes", slow_write):
        ticks, result = asyncio.run(scenario())

    assert result and Path(result).is_file()
    assert result.endswith(".wav")
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)


def test_cosyvoice_temp_audio_cleanup_does_not_block_event_loop(tmp_path):
    """CosyVoice 临时 WAV 的失败收口也必须离开事件循环。"""
    main_thread = threading.get_ident()
    unlinks = []
    original_unlink = Path.unlink

    def slow_unlink(path_obj, *args, **kwargs):
        unlinks.append(threading.get_ident())
        time.sleep(0.05)
        return original_unlink(path_obj, *args, **kwargs)

    class Engine:
        async def synthesize(self, *_args, **_kwargs):
            return b"wav-bytes", {"sample_rate": 24000}

    async def fail_ffmpeg(*_args, **_kwargs):
        raise RuntimeError("ffmpeg unavailable")

    engine = VoiceEngine(voice_dir=str(tmp_path), provider="cosyvoice")

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
            result = await engine._cosy_tts("你好呀")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch("agent.cosy_voice.get_cosy_engine", return_value=Engine()), mock.patch(
        "asyncio.create_subprocess_exec", fail_ffmpeg
    ), mock.patch.object(Path, "unlink", slow_unlink):
        ticks, result = asyncio.run(scenario())

    assert result and Path(result).is_file()
    assert ticks > 0
    assert unlinks and all(thread_id != main_thread for thread_id in unlinks)
