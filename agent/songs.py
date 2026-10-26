"""
小糖糖的曲库系统 🎤

只做一件事：**播放录好的成品音频**。不做任何实时合成。
两处音频来源：
  - 糖糖声线：`songs/audio/歌名.{wav,mp3}`
  - 原声：    `songs/covers/separated/歌名_FINAL.{wav,mp3}`
只有 *_FINAL 的翻唱（还没有糖糖声线版）也必须进曲库——否则「有几首歌」永远数不全。

.wav 是开发机上的无损源，.mp3 是发布包里的转码形态：唱歌走 CQ:record，
QQ 侧本来就会把它转成 SILK 低码率语音编码，存无损是白背体积。两种后缀都要认。

⚠ 2026-10-15：**歌词与「分段唱歌」能力已整体删除**（主人确认：22 个歌词 txt
是他删的，不需要了，相关代码一并删掉）。随之删掉的还有：

  - `songs/*.txt` 歌词文件的加载与解析（`_parse_song`）
  - `[主歌1]`/`[副歌]` 段落标记 → 段落数据（`_make_section` / `sections`）
  - 段落查询 `get_section` / `get_default_section` / `find_section` 与
    `DEFAULT_SECTION_ORDER` 选段优先级
  - `lyrics` / `artist` / `section_names` / `no_lyrics` 字段
  - `search_by_emotion`（它拿情绪词去匹配**歌词正文**，字段没了它就没有信号了）
  - `get_section_audio(title, section, version)` → `get_song_audio(title, version)`

**为什么整块删而不是留个空壳**：段落与歌词是同一份数据的两面——段落标记本来就写在
歌词文件里。歌词没了，段落就只剩「完整」一个值，`DEFAULT_SECTION_ORDER` 的每个候选
都匹配不到，选段逻辑恒等于「放整首」。留着它只会让后人以为「分段唱歌」还在。

**不要因为「以后可能恢复歌词」而把它加回来**：真要恢复，应该连段落语义一起重新设计
（什么算一段、谁决定放哪段、LLM 怎么表达），而不是把这段死逻辑从 git 里捞回来。
"""

import logging
from pathlib import Path

logger = logging.getLogger("糖糖.Songs")

#: 曲库里唯一的「段落」概念——播放层（ActionPlan child 的 payload）仍在用，
#: 保留它是为了不改已持久化记录的 action_id（改 payload = 换 id = 幂等保护失效）。
FULL_SECTION = "完整"


class SongLibrary:
    """曲库管理器——只认音频文件。"""

    def __init__(self, songs_dir: str = "./songs"):
        self.songs_dir = Path(songs_dir)
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        self.songs: dict[str, dict] = {}
        self._load()

    # ── 加载 ──

    def _load(self):
        """扫描两处音频来源，建成 {歌名: {"title": 歌名}}。

        songs 的键就是歌名；音频路径不做缓存（`get_song_audio` 现查现返回），
        这样 `/歌单 重载` 之后新放的音频立刻可见，不会留下过期路径。
        """
        self.songs = {}
        names: set[str] = set()

        audio_dir = self.songs_dir / "audio"
        if audio_dir.exists():
            for f in list(audio_dir.glob("*.wav")) + list(audio_dir.glob("*.mp3")):
                names.add(f.stem)

        final_dir = self.songs_dir / "covers" / "separated"
        if final_dir.exists():
            for f in final_dir.glob("*_FINAL.*"):
                if f.suffix.lower() not in (".wav", ".mp3"):
                    continue
                names.add(f.stem.removesuffix("_FINAL"))

        for name in sorted(names):
            self.songs[name] = {"title": name}

        total = len(self.songs)
        if total:
            with_voice = sum(1 for n in self.songs if self.tangtang_audio(n))
            logger.info(
                f"🎤 曲库: {total}首（{with_voice}首糖糖声线，"
                f"{total - with_voice}首只有原声）"
            )
        else:
            logger.info("🎤 曲库为空")

    def reload(self):
        """热重载曲库（放进新音频后）。"""
        self._load()

    # ── 音频解析 ──

    def tangtang_audio(self, title: str) -> str:
        """糖糖声线成品路径（songs/audio/），没有则空串。"""
        for suf in (".wav", ".mp3"):
            p = self.songs_dir / "audio" / f"{title}{suf}"
            if p.exists():
                return str(p)
        return ""

    def original_audio(self, title: str) -> str:
        """原声（原唱干净人声）路径，没有则空串。

        接 .wav 与 .mp3 两种：开发机上存的是分离出来的无损 wav；发布包里是
        ffmpeg 转码后的 mp3（同目录、同名、只换后缀）——存无损是白背 1.5G。
        """
        d = self.songs_dir / "covers" / "separated"
        for suf in (".wav", ".mp3"):
            p = d / f"{title}_FINAL{suf}"
            if p.exists():
                return str(p)
        return ""

    def get_song_audio(self, title: str, version: str = "tangtang") -> str:
        """取可播放的音频路径；没有则空串（调用方负责降级/报错）。

        version:
          "original" = 原声——covers/separated/{title}_FINAL.{wav,mp3}
          其余任何值 = 糖糖声线——songs/audio/{title}.{wav,mp3}，缺了退回原声

        ⚠ 判据**只认 "original"**，不要改成 `version == "tangtang"`：
        历史持久化记录（turn_actions / 出站回执）里存着旧令牌 "rvc"，
        改成正面判据会让旧记录静默降级放原声——用户点了糖糖声线却听到原唱，
        而且不报错。
        """
        if version == "original":
            return self.original_audio(title)
        return self.tangtang_audio(title) or self.original_audio(title)

    def has_any_audio(self, title: str) -> bool:
        """某首歌是否有任何可播放音频（糖糖声线或原声）。"""
        return bool(self.tangtang_audio(title) or self.original_audio(title))

    def list_songs_with_audio(self) -> list[str]:
        """有音频（真能播放）的歌名列表——不截断，LLM 数歌名就得到真实数量。"""
        return [name for name in self.songs if self.has_any_audio(name)]

    # ── 查询 ──

    def has_songs(self) -> bool:
        return len(self.songs) > 0

    def list_songs(self) -> list[str]:
        return list(self.songs.keys())

    def search(self, query: str) -> dict | None:
        """按歌名匹配。精确 → 包含 → 反向 → 拆词 → 最长子串。

        （原来还有「查询在歌词中」「查询匹配歌手」两步——随歌词字段一起删掉了。）
        """
        if not query or not self.songs:
            return None
        # 1. 精确匹配
        if query in self.songs:
            return self.songs[query]
        # 2. 查询是歌名的子串（"心做" in "心做し"）
        for title, info in self.songs.items():
            if query in title:
                return info
        # 3. 反向：歌名是查询的子串（"那个心做し" 包含 "心做し"）
        for title, info in self.songs.items():
            if title in query:
                return info
        # 4. 拆词匹配：查询中任意长度>=2的词匹配歌名
        for title, info in self.songs.items():
            for word in query.split():
                if len(word) >= 2 and word in title:
                    return info
        # 5. 滑动窗口，找最长匹配（解决 "いきものがかり" vs "いきのこり" 冲突）
        best = None
        best_len = 0
        for i in range(len(query)):
            for j in range(i + 2, len(query) + 1):
                fragment = query[i:j]
                for title, info in self.songs.items():
                    if fragment in title and len(fragment) > best_len:
                        best = info
                        best_len = len(fragment)
        return best

    # ── LLM 提示词 ──

    def build_sing_prompt(self, song: dict, voice_available: bool = False) -> str:
        """构建唱歌回合的 LLM 引导段（不含歌词——歌词能力已删除）。

        `voice_available` 保留在签名里：调用方（handler）仍按语音开关决定是否
        提「唱完可以发语音」，删掉会让那条分支失去依据。
        """
        return (
            f"## 🎤 唱歌时间！\n\n"
            f"群友点了《{song['title']}》，你很喜欢这首歌，自然地接住这个话题。\n\n"
            f"**要求**：\n"
            f"- 说一两句你对这首歌的真实感受（哪句词戳你、什么时候听过），别念歌词\n"
            f"- 聊到差不多时，**在回复末尾加上 [SING] 标记**——这是播放这首歌音频的"
            f"唯一方式，不加就不会放歌！\n"
            f"- 放完之后自然地问一句'好听吗？'或者接着聊"
        )
