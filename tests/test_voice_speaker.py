"""
测试音色解析与角色模型切换：
糖糖/丛雨使用通用 V4 + 丛雨参考音；米雪儿使用专属模型 + 中文情绪参考音。
"""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from agent.voice import EMOTION_SPEED, VoiceEngine, _PROMPT_REV
from agent.handler_commands import CommandRouter
from agent.service_manager import ServiceManager


def _engine(speaker=""):
    e = object.__new__(VoiceEngine)  # 跳过 __init__（不建缓存目录），_resolve_speaker 只需要两个属性
    e.current_speaker = speaker
    e.model_profile = "michele"
    return e


class TestResolveSpeaker:
    def test_tangtang_defaults_to_murasame_reference_and_v4(self, tmp_path):
        e = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
        assert e.current_speaker == "murasame"
        assert e.model_profile == "v4"

    def test_default_auto_uses_emotion_mapping(self):
        e = _engine()
        assert e._resolve_speaker("难过") == "sad"
        assert e._resolve_speaker("撒娇") == "coquettish"
        assert e._resolve_speaker("开心") == "happy"

    def test_unknown_emotion_falls_back_normal(self):
        # 2026-08-24 批1：无标签/未知情绪兜底从 happy 改为 normal——
        # 此前 voice_description 显示 normal、实际合成用 happy（日志实证），
        # 中性陈述也不该用开心声线。
        e = _engine()
        assert e._resolve_speaker("") == "normal"
        assert e._resolve_speaker("不存在的情绪") == "normal"

    def test_explicit_role_overrides_emotion(self):
        assert _engine(speaker="coquettish")._resolve_speaker("难过") == "coquettish"
        assert _engine(speaker="murasame")._resolve_speaker("开心") == "murasame"

    def test_selected_michele_emotions_use_independent_speakers(self):
        e = _engine()
        assert e._resolve_speaker("惊讶") == "surprised"
        assert e._resolve_speaker("生气") == "angry"
        assert e._resolve_speaker("愤怒") == "angry"
        assert e._resolve_speaker("厌恶") == "disgust"

    def test_disgust_is_available_to_send_voice_llm_enum(self):
        # send_voice 的 emotion enum 直接来自 EMOTION_SPEED.keys()。
        assert "厌恶" in EMOTION_SPEED

    def test_voice_description_reports_generic_engine_and_speaker(self):
        e = _engine()
        e.provider = "gpt-sovits"
        assert e.voice_description("厌恶") == "GPT-SoVITS·米雪儿·disgust"


class TestMichelePromptContract:
    def test_michele_refs_invalidate_pre_michele_voice_cache(self):
        assert _PROMPT_REV >= 4

    def test_selected_refs_use_chinese_prompt_text(self):
        expected = {
            "normal": "正义是一种信念",
            "gentle": "话说，你平时休息的时候",
            "happy": "喵，我的衣服是不是也挺可爱的",
            "excited": "对了，最近刚推出了一款双人游戏",
            "coquettish": "嗯，我没有再偷懒哦",
            "tsundere": "嗯，你又来找我玩了吗",
            "sad": "哼，我一定一定不会原谅你",
            "surprised": "原来你这么了解我",
            "angry": "可恶，我竟然会被你这种家伙干掉",
            "disgust": "哇，好疼",
        }
        for speaker, prompt_prefix in expected.items():
            prompt_lang, prompt_text = VoiceEngine._SPEAKER_PROMPT[speaker]
            assert prompt_lang == "zh"
            assert prompt_text.startswith(prompt_prefix)

    def test_production_config_uses_universal_v4_model(self):
        root = Path(__file__).resolve().parent.parent
        config = yaml.safe_load(
            (root / "gpt-sovits" / "GPT_SoVITS" / "configs" / "tts_infer.yaml")
            .read_text(encoding="utf-8")
        )["custom"]
        assert config["version"] == "v4"
        assert str(config["t2s_weights_path"]).replace("\\", "/").endswith(
            "GPT_SoVITS/pretrained_models/s1v3.ckpt"
        )
        assert str(config["vits_weights_path"]).replace("\\", "/").endswith(
            "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth"
        )

    def test_production_refs_match_blind_listening_winners(self):
        root = Path(__file__).resolve().parent.parent / "gpt-sovits" / "speakers"
        expected_sha256 = {
            "normal": "aa67774aa87f",
            "gentle": "d3f2a47e8260",
            "happy": "151143e97f96",
            "excited": "e627aadd0796",
            "coquettish": "a228d9b2182f",
            "tsundere": "907034a7596f",
            "sad": "a1727705bbe3",
            "surprised": "aeb4b21ee596",
            "angry": "3284a8be782c",
            "disgust": "f27ff495fcc6",
        }
        for speaker, prefix in expected_sha256.items():
            ref = root / speaker / "ref.wav"
            assert ref.is_file(), f"缺少 {speaker}/ref.wav"
            assert hashlib.sha256(ref.read_bytes()).hexdigest().startswith(prefix)


class TestRoleSpeakerIsolation:
    class FakeVoice:
        def __init__(self):
            self.voice_lang = "zh"
            self.current_speaker = "murasame"
            self.model_profile = "v4"
            self.switches = []

        async def switch_model(self, profile, *, voice_lang=None, current_speaker=None):
            self.switches.append((profile, voice_lang, current_speaker))
            self.model_profile = profile
            self.voice_lang = voice_lang
            self.current_speaker = current_speaker
            return True

    def test_murasame_role_keeps_v4_and_japanese_reference(self):
        loaded_roles = []
        sticker_roles = []
        handler = SimpleNamespace(
            voice=self.FakeVoice(),
            personality=SimpleNamespace(load_role_file=loaded_roles.append),
            _set_sticker_role=sticker_roles.append,
        )
        result = asyncio.run(CommandRouter(handler)._cmd_role("1", "丛雨"))
        assert handler.voice.switches == [("v4", "ja", "murasame")]
        assert handler.voice.current_speaker == "murasame"
        assert handler.voice.voice_lang == "ja"
        assert loaded_roles == ["role_card_murasame.md"]
        assert sticker_roles == ["murasame"]
        assert "丛雨" in result

    def test_michele_role_switches_model_card_reference_and_stickers_together(self):
        loaded_roles = []
        sticker_roles = []
        handler = SimpleNamespace(
            voice=self.FakeVoice(),
            personality=SimpleNamespace(load_role_file=loaded_roles.append),
            _set_sticker_role=sticker_roles.append,
        )

        result = asyncio.run(CommandRouter(handler)._cmd_role("1", "米雪儿"))

        assert handler.voice.switches == [("michele", "zh", "")]
        assert handler.voice.current_speaker == ""
        assert handler.voice.voice_lang == "zh"
        assert loaded_roles == ["role_card_michele.md"]
        assert sticker_roles == ["michele"]
        assert "米雪儿" in result

    def test_tangtang_role_restores_v4_and_murasame_reference(self):
        loaded_roles = []
        sticker_roles = []
        voice = self.FakeVoice()
        voice.model_profile = "michele"
        voice.current_speaker = ""
        handler = SimpleNamespace(
            voice=voice,
            personality=SimpleNamespace(load_role_file=loaded_roles.append),
            _set_sticker_role=sticker_roles.append,
        )

        result = asyncio.run(CommandRouter(handler)._cmd_role("1", "糖糖"))

        assert voice.switches == [("v4", "zh", "murasame")]
        assert voice.current_speaker == "murasame"
        assert loaded_roles == [""]
        assert sticker_roles == ["default"]
        assert "糖糖" in result

    def test_failed_model_switch_does_not_change_role_state(self):
        class FailedVoice(self.FakeVoice):
            async def switch_model(self, profile, *, voice_lang=None, current_speaker=None):
                self.switches.append((profile, voice_lang, current_speaker))
                return False

        loaded_roles = []
        sticker_roles = []
        voice = FailedVoice()
        handler = SimpleNamespace(
            voice=voice,
            personality=SimpleNamespace(load_role_file=loaded_roles.append),
            _set_sticker_role=sticker_roles.append,
        )

        result = asyncio.run(CommandRouter(handler)._cmd_role("1", "米雪儿"))

        assert "失败" in result
        assert voice.current_speaker == "murasame"
        assert loaded_roles == []
        assert sticker_roles == []

    def test_murasame_ref_keeps_its_japanese_prompt(self):
        assert VoiceEngine._SPEAKER_PROMPT["murasame"][0] == "ja"
        root = Path(__file__).resolve().parent.parent / "gpt-sovits" / "speakers"
        assert (root / "murasame" / "ref.wav").is_file()


class TestDynamicModelProfiles:
    def test_switch_model_uses_non_persistent_sovits_then_gpt_requests(self, tmp_path):
        requests = []

        class FakeResponse:
            status_code = 200

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, params=None):
                requests.append((url, params))
                return FakeResponse()

        engine = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
        with mock.patch("agent.voice.httpx.AsyncClient", return_value=FakeClient()):
            assert asyncio.run(engine.switch_model("michele")) is True

        assert [url.rsplit("/", 1)[-1] for url, _ in requests] == [
            "set_sovits_weights", "set_gpt_weights"
        ]
        assert all(params["persist"] == "false" for _, params in requests)
        assert requests[0][1]["weights_path"].endswith("MiXueErV3_e100_s4900.pth")
        assert requests[1][1]["weights_path"].endswith("MiXueErV3-e10.ckpt")
        assert engine.model_profile == "michele"

    def test_unknown_model_profile_is_rejected_without_http(self, tmp_path):
        engine = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
        with mock.patch("agent.voice.httpx.AsyncClient") as client:
            assert asyncio.run(engine.switch_model("unknown")) is False
        client.assert_not_called()


class TestMicheleAssetMigration:
    def test_active_role_assets_and_tools_use_only_michele_names(self):
        root = Path(__file__).resolve().parent.parent

        assert (root / "role_card_michele.md").is_file()
        assert not (root / "role_card_xueli.md").exists()
        default_dir = root / "stickers"
        michele_dir = root / "stickers_michele"
        assert default_dir.is_dir()
        assert michele_dir.is_dir()
        assert not (root / "stickers_xueli").exists()

        image_exts = {".jpg", ".jpeg", ".jfif", ".png", ".gif", ".webp", ".bmp"}
        default_images = {
            path.name for path in default_dir.iterdir() if path.suffix.lower() in image_exts
        }
        michele_images = {
            path.name for path in michele_dir.iterdir() if path.suffix.lower() in image_exts
        }
        default_metadata = json.loads(
            (default_dir / "metadata.json").read_text(encoding="utf-8")
        )
        michele_metadata = json.loads(
            (michele_dir / "metadata.json").read_text(encoding="utf-8")
        )

        # 旧角色的 58 张素材已迁入默认库；米雪儿使用独立、已标注的专属图库。
        assert len(default_images) == 956
        assert michele_images
        assert default_images.isdisjoint(michele_images)
        assert set(default_metadata) == default_images
        assert set(michele_metadata) == michele_images
        assert all(default_metadata.values())
        assert all(entry.get("emotions") for entry in michele_metadata.values())

        label_tool = (root / "tools" / "标注贴图情绪.py").read_text(encoding="utf-8")
        install_tool = (root / "tools" / "安装糖糖.py").read_text(encoding="utf-8")
        for source in (label_tool, install_tool):
            assert "michele" in source
            assert "xueli" not in source


class TestServiceHealthPromptContract:
    def test_health_check_uses_same_normal_prompt_as_voice_engine(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            content = b"x" * 1001

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                captured.update(kwargs["json"])
                return FakeResponse()

        with mock.patch("agent.service_manager.httpx.AsyncClient", return_value=FakeClient()):
            assert asyncio.run(ServiceManager()._check_gpt_sovits_healthy()) is True

        prompt_lang, prompt_text = VoiceEngine._SPEAKER_PROMPT["normal"]
        assert captured["prompt_lang"] == prompt_lang
        assert captured["prompt_text"] == prompt_text


def test_tts_streaming_with_profile_restores_runtime_selection(tmp_path):
    engine = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
    engine.model_profile = "v4"
    engine.current_speaker = "murasame"
    engine.voice_lang = "zh"
    engine._switch_model_locked = mock.AsyncMock(return_value=True)
    engine._gpt_sovits_tts = mock.AsyncMock(return_value=str(tmp_path / "michele.wav"))

    result = asyncio.run(engine.tts_streaming_with_profile(
        "米雪儿的定时语音", "温柔", speed=0.9, pause="舒缓",
        model_profile="michele", speaker="gentle", voice_lang="zh",
    ))

    assert result == [str(tmp_path / "michele.wav")]
    engine._switch_model_locked.assert_awaited_once_with("michele")
    engine._gpt_sovits_tts.assert_awaited_once_with(
        "米雪儿的定时语音", "温柔", lang="zh", speed=0.9, pause="舒缓",
    )
    assert (engine.model_profile, engine.current_speaker, engine.voice_lang) == (
        "v4", "murasame", "zh"
    )


def test_tts_streaming_with_profile_model_switch_failure_uses_fallback(tmp_path):
    engine = VoiceEngine(voice_dir=str(tmp_path), provider="gpt-sovits")
    engine._switch_model_locked = mock.AsyncMock(return_value=False)
    engine._edge_tts = mock.AsyncMock(return_value=str(tmp_path / "fallback.mp3"))

    result = asyncio.run(engine.tts_streaming_with_profile(
        "模型暂时不可用", model_profile="michele",
    ))

    assert result == [str(tmp_path / "fallback.mp3")]
    engine._edge_tts.assert_awaited_once()
