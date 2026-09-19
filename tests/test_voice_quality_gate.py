"""
语音生成与情绪决策测试

覆盖 GPT-SoVITS 响应处理、LLM 韵律参数和情绪决策。
语音质量门已退役：HTTP 成功且响应体达到基本文件大小即可发送，
不再由 VAD、时长或能量检测替 LLM/语音引擎拒绝结果。
"""
import asyncio
import io
import math
import struct
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from agent.voice import VoiceEngine, mood_to_emotion


# ---- WAV 构造 ----

def _make_wav(dur_s: float, sr: int = 32000, amp: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(dur_s * sr)
        frames = bytearray()
        for i in range(n):
            v = int(amp * math.sin(2 * math.pi * 220.0 * i / sr))
            frames += struct.pack('<h', v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class TestMoodToEmotion:
    def test_high_mood_happy(self):
        assert mood_to_emotion(SimpleNamespace(energy=80, mood=90)) == "开心"

    def test_low_mood_sad(self):
        assert mood_to_emotion(SimpleNamespace(energy=80, mood=30)) == "难过"

    def test_tired_whisper(self):
        assert mood_to_emotion(SimpleNamespace(energy=10, mood=70)) == "悄悄话"

    def test_neutral_returns_empty(self):
        assert mood_to_emotion(SimpleNamespace(energy=70, mood=70)) == ""

    def test_bad_object_safe(self):
        assert mood_to_emotion(None) == ""


class TestGptSovitsResponseHandling:
    """GPT-SoVITS 成功响应直接落盘；只对传输/响应完整性做基本校验。"""

    @staticmethod
    def _engine(tmp_path):
        e = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
        # 本组专测米雪儿中文情绪参考库；生产默认糖糖则固定为丛雨参考音。
        e.current_speaker = ""
        e.model_profile = "michele"
        e._on_gptsovits_failure = None
        return e

    @staticmethod
    def _mock_httpx(content):
        class _FakeResp:
            status_code = 200
            text = ""

            def __init__(self, c):
                self.content = c

            def json(self):
                return {}

        class _FakeClient:
            def __init__(self, resp, counter):
                self._resp = resp
                self._counter = counter

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                self._counter.post_calls += 1
                self._counter.last_json = k.get("json")
                return self._resp

        class _FakeCls:
            def __init__(self, resp):
                self._resp = resp
                self.post_calls = 0  # 每次重试会新建 client，计数放在类级别

            def __call__(self, *a, **k):
                return _FakeClient(self._resp, self)

        fake = _FakeCls(_FakeResp(content))
        return fake

    def test_short_wav_is_saved_without_quality_rejection(self, tmp_path):
        e = self._engine(tmp_path)
        short = _make_wav(0.3, amp=8000)
        fake = self._mock_httpx(short)
        with mock.patch("httpx.AsyncClient", fake):
            result = asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert result is not None
        assert Path(result).read_bytes() == short
        assert fake.post_calls == 1

    def test_silent_wav_is_saved_without_quality_rejection(self, tmp_path):
        e = self._engine(tmp_path)
        silent = _make_wav(3.0, amp=0)
        fake = self._mock_httpx(silent)
        with mock.patch("httpx.AsyncClient", fake):
            result = asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert result is not None
        assert Path(result).read_bytes() == silent
        assert fake.post_calls == 1

    def test_good_audio_saved(self, tmp_path):
        e = self._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            result = asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert result is not None
        assert list(tmp_path.glob("*.wav"))

    def test_server_error_triggers_restart(self, tmp_path):
        # 全程 502（未收到过 200）= 服务端可能异常 → 触发重启回调
        e = self._engine(tmp_path)
        calls = []

        async def _cb():
            calls.append(1)

        e._on_gptsovits_failure = _cb
        fake = self._mock_httpx(b"")
        fake._resp.status_code = 502
        fake._resp.text = '{"message":"bad gateway"}'
        with mock.patch("httpx.AsyncClient", fake):
            result = asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert result is None
        assert calls == [1]
        assert fake.post_calls == 3

    def test_request_uses_cut0_split(self, tmp_path):
        # 2026-08-24 批2 校准回退后钉住 cut0；批A 复测确认：v4+prompt 修复后
        # cut5 与 cut0 时长一致（丢段是 prompt 错位表现），cut5 无增益维持 cut0。
        e = self._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert fake.last_json["text_split_method"] == "cut0"
        assert fake.last_json["fragment_interval"] == 0.0

    def test_request_forwards_llm_speed_factor(self, tmp_path):
        e = self._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            asyncio.run(e._gpt_sovits_tts("主人，慢一点说嘛～", speed=0.82))
        assert fake.last_json["speed_factor"] == 0.82

    def test_request_preserves_comma_for_natural_prosody(self, tmp_path):
        e = self._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            asyncio.run(e._gpt_sovits_tts("主人，慢一点说嘛～", speed=1.0, pause="自然"))
        assert "，" in fake.last_json["text"]
        assert fake.last_json["fragment_interval"] == 0.0

    def test_spacious_pause_avoids_short_enumeration_fragments(self, tmp_path):
        e = self._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            asyncio.run(e._gpt_sovits_tts(
                "哼～哥～哥～……才、才不是特别想叫你呢！", speed=0.85, pause="舒缓"
            ))
        assert "……\n" in fake.last_json["text"]
        assert "才、\n" not in fake.last_json["text"]
        assert fake.last_json["fragment_interval"] == 0.0

    def test_cached_wav_is_reused_without_quality_revalidation(self, tmp_path):
        e = self._engine(tmp_path)
        # 质量门退役后，缓存命中不再加载 VAD/解码 WAV。
        good = _make_wav(4.0, amp=8000)
        from agent.voice import _PROMPT_REV, _transliterate_english
        clean = _transliterate_english("今天天气真不错呀哈哈")
        clean = " ".join(clean.split())
        import hashlib
        cache_key = hashlib.md5(
            f"gptsovits|michele|normal|zh|{clean}|s1.00|p{_PROMPT_REV}".encode()
        ).hexdigest()[:12]
        bad_file = tmp_path / f"{cache_key}.wav"
        bad_file.write_bytes(_make_wav(3.0, amp=0))
        fake = self._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            result = asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        assert result == str(bad_file)
        assert bad_file.read_bytes() != good
        assert fake.post_calls == 0


class TestQualityGateRemoval:
    def test_voice_module_contains_no_legacy_quality_gate(self):
        import agent.voice as voice_module

        src = Path(voice_module.__file__).read_text(encoding="utf-8")
        assert "_check_audio_quality" not in src
        assert "_speech_duration_sec" not in src
        assert "sherpa_onnx" not in src


class TestCacheCleanupThrottle:
    """2026-08-24 批C：运行中节流清理——每 100 次写入触发一次。
    此前清理只在启动时跑，单次运行涨破上限要等重启。"""

    def test_cleanup_triggered_every_100_writes(self, tmp_path):
        e = TestGptSovitsResponseHandling._engine(tmp_path)
        with mock.patch.object(e, "_cleanup_cache") as m:
            for _ in range(99):
                e._note_cache_write()
            m.assert_not_called()
            e._note_cache_write()  # 第 100 次
            m.assert_called_once()


class TestSpeakerPromptTable:
    """2026-08-24 批A：prompt_text/prompt_lang 按 speaker 查表——
    官方定性 prompt_text 与 ref 不匹配 = 短句早停根因，ref 全为日语。"""

    def test_request_uses_speaker_prompt_table(self, tmp_path):
        e = TestGptSovitsResponseHandling._engine(tmp_path)
        good = _make_wav(4.0, amp=8000)
        fake = TestGptSovitsResponseHandling._mock_httpx(good)
        with mock.patch("httpx.AsyncClient", fake):
            asyncio.run(e._gpt_sovits_tts("今天天气真不错呀哈哈"))
        # 默认 normal speaker：prompt_lang 必须跟随米雪儿中文参考音频（zh），
        # prompt_text 必须是 ref 的真实转写——不是「你好」，也不是目标语言
        assert fake.last_json["prompt_lang"] == "zh"
        assert fake.last_json["prompt_text"].startswith("正义是一种信念")
        assert fake.last_json["text_lang"] == "zh"  # 目标文本语言不受影响

    def test_prompt_table_covers_all_speaker_refs(self):
        # 闸门：新增 speaker 目录漏配表项直接红灯（与发送链路契约闸门同理）
        speakers_dir = Path(__file__).resolve().parent.parent / "gpt-sovits" / "speakers"
        if not speakers_dir.exists():
            pytest.skip("speakers 目录不存在")
        missing = [
            d.name for d in sorted(speakers_dir.iterdir())
            if d.is_dir() and (d / "ref.wav").exists() and d.name not in VoiceEngine._SPEAKER_PROMPT
        ]
        assert missing == [], f"speaker 有 ref.wav 但 _SPEAKER_PROMPT 缺项: {missing}"


class TestSendVoiceToolEmotion:
    """2026-08-24 深夜：send_voice 的 emotion 必填参数写入 turn_actions——
    发送路径注入为文本标签（「不写用默认语气」曾让 LLM 跳过情绪决策，
    全部语音兜底 normal 音色局促统一，主人实测反馈）。"""

    def _handler(self):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.voice_enabled = True
        return h

    def test_voice_prosody_stored_in_turn_actions(self):
        h = self._handler()
        ta = {}
        out = asyncio.run(h._execute_tool(
            "send_voice",
            {"emotion": "开心", "speed": 0.82, "pause": "舒缓"},
            turn_actions=ta,
        ))
        assert ta["voice"] is True
        assert ta["voice_emotion"] == "开心"
        assert ta["voice_speed"] == 0.82
        assert ta["voice_pause"] == "舒缓"
        assert "语音" in out

    def test_missing_emotion_no_crash(self):
        # LLM 漏传参数（中继/兼容层差异）不炸——退化为既有链（文本标签→mood→normal）
        h = self._handler()
        ta = {}
        out = asyncio.run(h._execute_tool("send_voice", {}, turn_actions=ta))
        assert ta["voice"] is True
        assert "voice_emotion" not in ta
        assert ta["voice_speed"] == 1.0
        assert ta["voice_pause"] == "自然"

    def test_invalid_prosody_falls_back_to_safe_defaults(self):
        h = self._handler()
        ta = {}
        asyncio.run(h._execute_tool(
            "send_voice", {"emotion": "开心", "speed": "飞快", "pause": "随便"},
            turn_actions=ta,
        ))
        assert ta["voice_speed"] == 1.0
        assert ta["voice_pause"] == "自然"

    def test_voice_disabled_blocks(self):
        h = self._handler()
        h.voice_enabled = False
        ta = {}
        out = asyncio.run(h._execute_tool("send_voice", {"emotion": "开心"}, turn_actions=ta))
        assert "不可用" in out
        assert ta == {}


class TestVoiceEmotionSmoothing:
    """相邻语音情绪平滑：窗口内兴奋系↔低落系跳变 → 温柔过渡。"""

    def _handler(self):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h._voice_emotion_history = {}
        return h

    def test_jump_pair_bridged_with_gentle(self):
        h = self._handler()
        assert h._smooth_voice_emotion("k", "开心") == "开心"
        assert h._smooth_voice_emotion("k", "难过") == "温柔"  # UP→DOWN 跳变

    def test_jump_pair_down_to_up_bridged(self):
        h = self._handler()
        assert h._smooth_voice_emotion("k", "伤心") == "伤心"
        assert h._smooth_voice_emotion("k", "兴奋") == "温柔"  # DOWN→UP 跳变

    def test_same_direction_no_bridge(self):
        h = self._handler()
        assert h._smooth_voice_emotion("k", "开心") == "开心"
        assert h._smooth_voice_emotion("k", "兴奋") == "兴奋"  # 同为兴奋系
        assert h._smooth_voice_emotion("k", "温柔") == "温柔"  # 中性过渡不拦

    def test_window_expired_no_bridge(self):
        h = self._handler()
        assert h._smooth_voice_emotion("k", "开心") == "开心"
        h._voice_emotion_history["k"] = ("难过", time.time() - 200)  # 超出3分钟窗口
        assert h._smooth_voice_emotion("k", "开心") == "开心"

    def test_empty_tag_passthrough(self):
        h = self._handler()
        assert h._smooth_voice_emotion("k", "") == ""

    def test_per_target_isolation(self):
        h = self._handler()
        assert h._smooth_voice_emotion("group:1", "开心") == "开心"
        # 另一个会话对象不受影响
        assert h._smooth_voice_emotion("private:2", "难过") == "难过"
