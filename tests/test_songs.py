"""
测试曲库声音版本解析（2026-08-14）
每首歌有两种声音：rvc=糖糖声线（songs/audio/）/ original=原声（covers/separated/*_FINAL.{wav,mp3}）

.wav 是开发机上的无损源；.mp3 是发布包里的转码形态（2026-09-19 起）——
唱歌走 CQ:record，QQ 侧本来就会转成 SILK 低码率语音编码，存无损是白背 1.5G。
两种后缀都要认，否则用户下完包会发现 40 首歌的「原声」不存在。
"""

import pytest
from pathlib import Path
from agent.songs import SongLibrary


@pytest.fixture
def song_lib(tmp_path):
    """构建迷你曲库：
    songs/
      audio/勇气.wav                        ← RVC 糖糖声线
      covers/separated/勇气_FINAL.wav       ← 原声
      covers/separated/小鸟_FINAL.wav       ← 只有原声（无歌词无 RVC，如 SAKURA 场景）
      勇气.txt（有歌词+分段）
      晴天.txt（只有歌词，两种音频都没有）
    """
    songs_dir = tmp_path / "songs"
    (songs_dir / "audio").mkdir(parents=True)
    (songs_dir / "covers" / "separated").mkdir(parents=True)
    (songs_dir / "audio" / "勇气.wav").write_bytes(b"RVC")
    (songs_dir / "covers" / "separated" / "勇气_FINAL.wav").write_bytes(b"ORIGINAL")
    (songs_dir / "covers" / "separated" / "小鸟_FINAL.wav").write_bytes(b"ORIGINAL2")
    (songs_dir / "勇气.txt").write_text(
        "梁静茹\n[副歌]\n我们都需要勇气\n来面对流言蜚语\n", encoding="utf-8"
    )
    (songs_dir / "晴天.txt").write_text(
        "周杰伦\n[主歌1]\n故事的小黄花\n从出生那年就飘着\n", encoding="utf-8"
    )
    return SongLibrary(str(songs_dir))


class TestSongVersions:
    def test_rvc_version_returns_audio_dir(self, song_lib):
        path = song_lib.get_section_audio("勇气", "副歌", "rvc")
        assert Path(path).name == "勇气.wav"
        assert "separated" not in path

    def test_original_version_returns_final(self, song_lib):
        path = song_lib.get_section_audio("勇气", "副歌", "original")
        assert Path(path).name == "勇气_FINAL.wav"

    def test_default_version_is_rvc(self, song_lib):
        path = song_lib.get_section_audio("勇气", "副歌")
        assert Path(path).name == "勇气.wav"

    def test_original_missing_returns_empty(self, song_lib):
        """没有原声的歌 → original 返回空字符串（播放层会降级到另一版）"""
        assert song_lib.get_section_audio("晴天", "主歌1", "original") == ""

    def test_final_only_song_discovered(self, song_lib):
        """只有 FINAL 原声的歌（如 SAKURA）也必须进曲库、算有音频"""
        assert "小鸟" in song_lib.list_songs()
        assert song_lib.has_any_audio("小鸟")
        # 两种 version 都解析到 FINAL（只有这一版）
        for version in ("rvc", "original"):
            path = song_lib.get_section_audio("小鸟", "完整", version)
            assert Path(path).name == "小鸟_FINAL.wav"


class TestOriginalMp3:
    """原声的 .mp3 形态（2026-09-19）。

    发布包里原声是转码后的 mp3，不是 wav。只认 .wav 的话，用户下完包会发现
    40 首歌的原声"不存在"——而 sing 工具还在对 LLM 承诺「说原声就放原唱」，
    用户点了只会静默降级放糖糖声线（agent/handler.py:10239 的降级分支）。
    """

    @pytest.fixture
    def mp3_lib(self, tmp_path):
        """模拟发布包：audio/ 有 RVC 糖糖声线，separated/ 只有 mp3 原声"""
        songs_dir = tmp_path / "songs"
        (songs_dir / "audio").mkdir(parents=True)
        (songs_dir / "covers" / "separated").mkdir(parents=True)
        (songs_dir / "audio" / "勇气.wav").write_bytes(b"RVC")
        (songs_dir / "covers" / "separated" / "勇气_FINAL.mp3").write_bytes(b"MP3")
        (songs_dir / "covers" / "separated" / "小鸟_FINAL.mp3").write_bytes(b"MP3")
        (songs_dir / "勇气.txt").write_text(
            "梁静茹\n[副歌]\n我们都需要勇气\n", encoding="utf-8")
        return SongLibrary(str(songs_dir))

    def test_mp3_original_resolves(self, mp3_lib):
        path = mp3_lib.get_section_audio("勇气", "副歌", "original")
        assert Path(path).name == "勇气_FINAL.mp3"

    def test_mp3_only_song_is_discovered_and_playable(self, mp3_lib):
        assert "小鸟" in mp3_lib.list_songs()
        assert mp3_lib.has_any_audio("小鸟")
        for version in ("rvc", "original"):
            path = mp3_lib.get_section_audio("小鸟", "完整", version)
            assert Path(path).name == "小鸟_FINAL.mp3"

    def test_wav_wins_when_both_present(self, song_lib):
        """开发机上 wav 与 mp3 同时存在时取无损那份（顺序不能反）"""
        sep = song_lib.songs_dir / "covers" / "separated"
        (sep / "勇气_FINAL.mp3").write_bytes(b"MP3")
        path = song_lib.get_section_audio("勇气", "副歌", "original")
        assert Path(path).name == "勇气_FINAL.wav"


# ═══════════════════════════════════════════════════════
# 唱歌与 TTS 开关解耦契约（2026-09-18）
# ═══════════════════════════════════════════════════════

def test_no_singing_path_is_gated_by_voice_enabled():
    """唱歌播预录成品音频，不是 TTS 合成——任何唱歌调用点都不得被 voice_enabled 挡住。

    发布版「文字版」「识图版」不发语音（voice.enabled=false），但三个版本都能点歌。
    2026-09-18 实测：唱歌共三条路径（群聊/私聊 × LLM 工具点歌 / [SING] 文本标记），
    其中标记路径原先与 TTS 共用 voice_enabled —— 一关发声就唱不了歌。

    ⚠ 不要求每个调用点都查 _is_voice_blocked：LLM 工具点歌是对方明确点的，
    不该被「之前说过别发语音」抵消；只有标记路径（她主动唱）才需要尊重那个偏好。

    AST 定位「函数体里调用了 _send_singing_actions 的 if」——比字符串匹配稳，
    调用点挪位置、改缩进都不会误判。
    """
    import ast

    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body_src = "\n".join(ast.get_source_segment(src, s) or "" for s in node.body)
        if "_send_singing_actions" not in body_src:
            continue
        checked += 1
        test_src = ast.get_source_segment(src, node.test) or ""
        assert "voice_enabled" not in test_src, \
            f"唱歌调用点被 voice_enabled 挡住了（关掉 TTS 就唱不了歌）：{test_src}"
    assert checked >= 3, f"预期至少 3 个唱歌调用点（群/私聊 × 工具/标记路径），实际 {checked}"

    # 真正发歌的函数本身也必须与 TTS 开关无关——这是最内层保证
    sender = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_send_song_section")
    assert "voice_enabled" not in (ast.get_source_segment(src, sender) or ""), \
        "_send_song_section 不得读 voice_enabled——它只负责把预制音频发出去"
