"""
测试曲库的声音版本解析（2026-08-14；2026-10-15 随歌词删除而收缩）

每首歌有两种声音：tangtang=糖糖声线（songs/audio/）/ original=原声（covers/separated/*_FINAL.{wav,mp3}）

版本令牌 2026-10-15 由 rvc 改名为 tangtang（翻唱链删除后 rvc 已名不副实）；
旧值 rvc 保留兼容——agent/songs.py 的判据只看 original，其余一律糖糖声线。

.wav 是开发机上的无损源；.mp3 是发布包里的转码形态（2026-09-19 起）——
唱歌走 CQ:record，QQ 侧本来就会转成 SILK 低码率语音编码，存无损是白背 1.5G。
两种后缀都要认，否则用户下完包会发现 40 首歌的「原声」不存在。

⚠ 2026-10-15：**歌词与分段唱歌已整体删除**。本文件原先还测 `get_section_audio`
的段落参数与「有歌词/无歌词」两种条目形态——那些用例连同被测代码一起删了。
现在曲库的输入只有音频文件，条目形态只有一种。
"""

import pytest
from pathlib import Path

from agent.songs import FULL_SECTION, SongLibrary


@pytest.fixture
def song_lib(tmp_path):
    """构建迷你曲库（只有音频，没有歌词文件）：
    songs/
      audio/勇气.wav                        ← 糖糖声线
      covers/separated/勇气_FINAL.wav       ← 原声
      covers/separated/小鸟_FINAL.wav       ← 只有原声（还没有糖糖声线版）
    """
    songs_dir = tmp_path / "songs"
    (songs_dir / "audio").mkdir(parents=True)
    (songs_dir / "covers" / "separated").mkdir(parents=True)
    (songs_dir / "audio" / "勇气.wav").write_bytes(b"TANGTANG")
    (songs_dir / "covers" / "separated" / "勇气_FINAL.wav").write_bytes(b"ORIGINAL")
    (songs_dir / "covers" / "separated" / "小鸟_FINAL.wav").write_bytes(b"ORIGINAL2")
    return SongLibrary(str(songs_dir))


class TestSongVersions:
    def test_tangtang_version_returns_audio_dir(self, song_lib):
        path = song_lib.get_song_audio("勇气", "tangtang")
        assert Path(path).name == "勇气.wav"
        assert "separated" not in path

    def test_original_version_returns_final(self, song_lib):
        path = song_lib.get_song_audio("勇气", "original")
        assert Path(path).name == "勇气_FINAL.wav"

    def test_default_version_is_tangtang(self, song_lib):
        assert Path(song_lib.get_song_audio("勇气")).name == "勇气.wav"

    def test_original_missing_returns_empty(self, song_lib):
        """没有原声的歌 → original 返回空字符串（播放层会降级到另一版）"""
        (song_lib.songs_dir / "audio" / "只有糖糖.wav").write_bytes(b"T")
        song_lib.reload()
        assert song_lib.get_song_audio("只有糖糖", "original") == ""
        assert Path(song_lib.get_song_audio("只有糖糖", "tangtang")).name == "只有糖糖.wav"

    def test_tangtang_missing_falls_back_to_original(self, song_lib):
        """只有原声的歌取「糖糖声线」时退回原声——比没有声音好"""
        assert Path(song_lib.get_song_audio("小鸟", "tangtang")).name == "小鸟_FINAL.wav"

    def test_final_only_song_discovered(self, song_lib):
        """只有 FINAL 原声的歌也必须进曲库、算有音频"""
        assert "小鸟" in song_lib.list_songs()
        assert song_lib.has_any_audio("小鸟")
        for version in ("tangtang", "original"):
            assert Path(song_lib.get_song_audio("小鸟", version)).name == "小鸟_FINAL.wav"

    def test_song_without_any_audio_is_not_listed(self, song_lib):
        """只有歌词没有音频的时代结束了——现在「在曲库里」就等于「有文件」"""
        assert song_lib.list_songs_with_audio() == sorted(song_lib.list_songs())


class TestOriginalMp3:
    """原声的 .mp3 形态（2026-09-19）。

    发布包里原声是转码后的 mp3，不是 wav。只认 .wav 的话，用户下完包会发现
    40 首歌的原声"不存在"——而 sing 工具还在对 LLM 承诺「说原声就放原唱」，
    用户点了只会静默降级放糖糖声线（_send_song_section 的降级分支）。
    """

    @pytest.fixture
    def mp3_lib(self, tmp_path):
        """模拟发布包：audio/ 有糖糖声线，separated/ 只有 mp3 原声"""
        songs_dir = tmp_path / "songs"
        (songs_dir / "audio").mkdir(parents=True)
        (songs_dir / "covers" / "separated").mkdir(parents=True)
        (songs_dir / "audio" / "勇气.wav").write_bytes(b"TANGTANG")
        (songs_dir / "covers" / "separated" / "勇气_FINAL.mp3").write_bytes(b"MP3")
        (songs_dir / "covers" / "separated" / "小鸟_FINAL.mp3").write_bytes(b"MP3")
        return SongLibrary(str(songs_dir))

    def test_mp3_original_resolves(self, mp3_lib):
        assert Path(mp3_lib.get_song_audio("勇气", "original")).name == "勇气_FINAL.mp3"

    def test_mp3_only_song_is_discovered_and_playable(self, mp3_lib):
        assert "小鸟" in mp3_lib.list_songs()
        assert mp3_lib.has_any_audio("小鸟")
        for version in ("tangtang", "original"):
            assert Path(mp3_lib.get_song_audio("小鸟", version)).name == "小鸟_FINAL.mp3"

    def test_legacy_rvc_token_still_resolves_to_tangtang_voice(self, song_lib):
        """旧令牌 rvc 必须仍然解析到糖糖声线（改名不改语义的回归闸门）。

        2026-10-15 版本令牌改名 rvc → tangtang，但历史持久化记录（turn_actions /
        出站回执）里存的还是旧值。`get_song_audio` 的判据刻意写成「只看
        original，其余一律糖糖声线」，所以旧值照旧可用。
        这条用例防的是后人把判据改成 `version == "tangtang"`——那样旧记录会静默
        降级放原声：用户点了糖糖声线却听到原唱，而且不报错。
        """
        legacy = song_lib.get_song_audio("勇气", "rvc")
        assert legacy == song_lib.get_song_audio("勇气", "tangtang")
        assert Path(legacy).name == "勇气.wav"

    def test_wav_wins_when_both_present(self, song_lib):
        """开发机上 wav 与 mp3 同时存在时取无损那份（顺序不能反）"""
        sep = song_lib.songs_dir / "covers" / "separated"
        (sep / "勇气_FINAL.mp3").write_bytes(b"MP3")
        assert Path(song_lib.get_song_audio("勇气", "original")).name == "勇气_FINAL.wav"


class TestSearchWithoutLyrics:
    """歌名匹配（歌词搜索那两步已随歌词字段删除）"""

    def test_exact_and_substring(self, song_lib):
        assert song_lib.search("勇气")["title"] == "勇气"
        assert song_lib.search("勇")["title"] == "勇气"

    def test_reverse_containment(self, song_lib):
        assert song_lib.search("那首勇气啊")["title"] == "勇气"

    def test_miss_returns_none(self, song_lib):
        assert song_lib.search("完全不存在的歌名zzz") is None

    def test_lyrics_are_no_longer_indexed(self, song_lib):
        """同目录放一个歌词 txt 也不会进入曲库——歌词能力已删除。

        这条是**删除的守卫**：如果有人把 `songs/*.txt` 的加载加回来，
        这里会立刻多出一首歌（txt 的 stem 是「歌词」），断言随之报红。
        """
        (song_lib.songs_dir / "歌词.txt").write_text("某人\n[副歌]\n一句词\n", encoding="utf-8")
        song_lib.reload()
        assert "歌词" not in song_lib.list_songs()


def test_full_section_constant_is_the_only_section_token():
    """播放层仍在用「完整」这个段落值（ActionPlan payload 的兼容键）。

    它不是「分段唱歌」的残留能力，而是**已持久化记录的兼容值**：
    payload["section"] 参与 derive_action_id，改掉它会换掉所有 sing 子动作的 id。
    """
    assert FULL_SECTION == "完整"


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
