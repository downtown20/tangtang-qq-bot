"""
小糖糖的曲库系统 🎤 (v2 — 分段唱歌)

支持：
  - 歌词段落标记 [主歌1] [副歌] [主歌2] [尾声] 等
  - 按段落查询和播放
  - LLM 自动选段（副歌/主歌/指定段落）
  - 向后兼容无标记的旧歌词文件
"""

import random
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("糖糖.Songs")

# 段落选择优先级（LLM 不指定时用）
DEFAULT_SECTION_ORDER = ["副歌", "主歌1", "主歌", "段落1", "完整"]


class SongLibrary:
    """曲库管理器 (v2 — 分段支持)"""

    def __init__(self, songs_dir: str = "./songs"):
        self.songs_dir = Path(songs_dir)
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        self.songs: dict[str, dict] = {}
        self._load()

    # ── 加载 ──

    def _load(self):
        """加载 songs/ 下所有 .txt 文件 + 发现无歌词但有音频的翻唱。"""
        self.songs = {}
        has_lyrics: set[str] = set()

        # 1. 加载有歌词的歌曲（.txt）
        for f in self.songs_dir.glob("*.txt"):
            if f.name.endswith(".bak"):
                continue
            try:
                content = f.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                song = self._parse_song(f.stem, content)
                if song:
                    self.songs[f.stem] = song
                    has_lyrics.add(f.stem)
            except Exception as e:
                logger.warning(f"加载歌曲失败 {f.name}: {e}", exc_info=True)

        # 2. 发现有音频但无歌词的翻唱
        #    音频来源两处：songs/audio/（RVC 糖糖声线成品）+ covers/separated/*_FINAL.*（原声人声成品——
        #    分离完还没拷贝到 audio/ 的歌也必须进曲库，否则"有几首歌"永远数不全）
        #    原声同时接受 .wav 与 .mp3：开发机上放无损 wav，发布包里是转码后的 mp3
        #    （见 打包发布版本.py 的 _original_mp3——唱歌走 CQ:record，QQ 侧本来就会转成
        #     SILK 低码率语音编码，存无损是白背体积；1.78G → 242M）
        audio_dir = self.songs_dir / "audio"
        final_dir = self.songs_dir / "covers" / "separated"
        no_lyrics_count = 0
        candidates: list[tuple[Path, str]] = []
        if audio_dir.exists():
            # 两种后缀都认：开发机上存无损 wav，发布包里是 ffmpeg 转码后的 mp3。
            # 理由同原声（见下方 original_audio）——唱歌走 CQ:record，QQ 侧本来就会
            # 转成 SILK 低码率语音编码，40kHz PCM16 无损是白背 712M。
            # 两版同时在时优先 wav（开发机场景）。
            _by_audio: dict[str, Path] = {}
            for w in sorted(audio_dir.glob("*.wav")) + sorted(audio_dir.glob("*.mp3")):
                prev = _by_audio.get(w.stem)
                if prev is None or (prev.suffix.lower() == ".mp3" and w.suffix.lower() == ".wav"):
                    _by_audio[w.stem] = w
            candidates.extend((w, n) for n, w in sorted(_by_audio.items()))
        if final_dir.exists():
            by_name: dict[str, Path] = {}
            for w in sorted(final_dir.glob("*_FINAL.*")):
                if w.suffix.lower() not in (".wav", ".mp3"):
                    continue
                name = w.stem.removesuffix("_FINAL")
                prev = by_name.get(name)
                # 两个版本同时在（开发机）时优先无损 wav
                if prev is None or (prev.suffix.lower() == ".mp3" and w.suffix.lower() == ".wav"):
                    by_name[name] = w
            candidates.extend((w, n) for n, w in sorted(by_name.items()))
        for wav, name in candidates:
            if name not in self.songs:
                # 无歌词：创建占位条目，只有完整音频
                self.songs[name] = {
                    "title": name,
                    "artist": "",
                    "lyrics": "",
                    "sections": {
                        "完整": {
                            "name": "完整",
                            "lines": [],
                            "text": "",
                            "line_count": 0,
                            "audio": str(wav),
                            "preview": "（无歌词，有翻唱音频）",
                        }
                    },
                    "section_names": ["完整"],
                    "file": "",
                    "no_lyrics": True,
                }
                no_lyrics_count += 1

        has = len(has_lyrics)
        total = len(self.songs)
        if total:
            sectioned = sum(1 for s in self.songs.values()
                          if not s.get("no_lyrics") and len(s.get("sections", {})) > 1)
            no_lrc = total - has
            parts = [f"{total}首 ({has}有歌词"]
            if no_lrc:
                parts.append(f"{no_lrc}纯翻唱")
            parts.append(f"{sectioned}首分段)")
            logger.info(f"🎤 曲库: {'，'.join(parts)}")
        else:
            logger.info("🎤 曲库为空")

    def _parse_song(self, title: str, content: str) -> dict | None:
        """解析歌词文件：歌手 + 段落。返回 song dict。"""
        lines = content.split("\n")

        # 歌手名（第一行）
        artist = ""
        lyrics_start = 0
        first = lines[0].strip() if lines else ""
        if first and not any(ch in first for ch in "，。！？的了我在是不有会和"):
            artist = first
            lyrics_start = 1

        # 解析段落
        sections: dict[str, dict] = {}
        current_name = "完整"
        current_lines: list[str] = []

        for line in lines[lyrics_start:]:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]") and len(stripped) < 20:
                # 段落标记
                if current_lines:
                    sections[current_name] = self._make_section(
                        title, current_name, current_lines
                    )
                current_name = stripped[1:-1]
                current_lines = []
            elif stripped:
                current_lines.append(stripped)

        # 最后一段
        if current_lines:
            sections[current_name] = self._make_section(
                title, current_name, current_lines
            )
        elif current_name not in sections:
            # 段落标记后没有歌词行（不太可能但兜底）
            sections[current_name] = self._make_section(title, current_name, [])

        if not sections:
            return None

        # 汇总歌词全文（给 LLM 参考）
        full_lyrics = "\n".join(
            "\n".join(sec["lines"]) for sec in sections.values()
        )

        return {
            "title": title,
            "artist": artist,
            "lyrics": full_lyrics,
            "sections": sections,
            "section_names": list(sections.keys()),
            "file": str(Path(self.songs_dir) / f"{title}.txt"),
        }

    def _make_section(self, title: str, name: str, lines: list[str]) -> dict:
        """为段落构建数据（含音频路径）。"""
        text = "\n".join(lines)
        # 段落音频路径: songs/audio/歌名/段落名.wav
        audio_dir = self.songs_dir / "audio" / title
        audio_path = audio_dir / f"{name}.wav"
        if not audio_path.exists():
            audio_path = audio_dir / f"{name}.mp3"

        # 向后兼容：旧格式 songs/audio/歌名.{wav,mp3}（只有一段时）
        if not audio_path.exists() and len(lines) > 0:
            for _suf in (".wav", ".mp3"):
                _legacy = self.songs_dir / "audio" / f"{title}{_suf}"
                if _legacy.exists():
                    audio_path = _legacy
                    break

        # 原声兜底：covers/separated/歌名_FINAL.{wav,mp3}（分离好的原唱人声，未拷贝到 audio/ 的歌）
        if not audio_path.exists():
            final = self.original_audio(title)
            if final:
                audio_path = Path(final)

        return {
            "name": name,
            "lines": lines,
            "text": text,
            "line_count": len(lines),
            "audio": str(audio_path) if audio_path.exists() else "",
            "preview": text[:50].replace("\n", " ") + ("…" if len(text) > 50 else ""),
        }

    def reload(self):
        """热重载曲库（修改歌词或加新歌后）。"""
        self._load()

    # ── 查询 ──

    def has_songs(self) -> bool:
        return len(self.songs) > 0

    def list_songs(self) -> list[str]:
        return list(self.songs.keys())

    def get_song(self, name: str) -> dict | None:
        return self.songs.get(name)

    def search(self, query: str) -> dict | None:
        """按歌名/歌词搜索。精确→包含→歌词→歌手→反向→最长子串匹配。"""
        if not query or not self.songs:
            return None
        # 1. 精确匹配
        if query in self.songs:
            return self.songs[query]
        # 2. 查询是歌名的子串（"心做" in "心做し"）
        for title, info in self.songs.items():
            if query in title:
                return info
        # 3. 查询在歌词中
        for title, info in self.songs.items():
            if info.get("lyrics") and query in info["lyrics"]:
                return info
        # 4. 查询匹配歌手
        for title, info in self.songs.items():
            if info.get("artist") and query in info["artist"]:
                return info
        # 5. 反向：歌名是查询的子串（"那个心做し" 包含 "心做し"）
        for title, info in self.songs.items():
            if title in query:
                return info
        # 6. 拆词匹配：查询中任意长度>=2的词匹配歌名
        for title, info in self.songs.items():
            for word in query.split():
                if len(word) >= 2 and word in title:
                    return info
        # 7. 滑动窗口，找最长匹配（解决 "いきものがかり" vs "いきのこり" 冲突）
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

    def random_song(self) -> dict | None:
        if not self.songs:
            return None
        return random.choice(list(self.songs.values()))

    def search_by_emotion(self, text: str) -> dict | None:
        """按情绪推荐歌曲。"""
        if not self.songs:
            return None
        emotion_map = {
            "开心": ["开心", "快乐", "幸福", "甜蜜", "甜", "笑", "阳光", "晴天"],
            "难过": ["难过", "伤心", "眼泪", "哭", "孤独", "寂寞", "遗憾", "后来"],
            "励志": ["勇敢", "坚强", "梦想", "追", "光", "翅膀", "飞", "星"],
            "爱情": ["爱", "喜欢", "想你", "心跳", "遇见", "爱情", "告白"],
            "思念": ["思念", "想念", "远方", "好久不见", "回忆", "记得"],
            "离别": ["再见", "离别", "离开", "后会无期", "送别"],
            "温馨": ["温柔", "暖暖", "晚安", "微风", "静静", "慢慢"],
        }
        matched = None
        for emotion, keywords in emotion_map.items():
            if any(kw in text for kw in keywords):
                matched = emotion
                break
        if matched:
            emotion_kw = emotion_map[matched]
            candidates = []
            for title, info in self.songs.items():
                searchable = title + info["lyrics"][:200]
                if any(kw in searchable for kw in emotion_kw):
                    candidates.append(info)
            if candidates:
                return random.choice(candidates)
        return self.random_song()

    # ── 段落查询 ──

    def get_section(self, title: str, section_name: str) -> dict | None:
        """获取指定歌的指定段落。"""
        song = self.songs.get(title)
        if not song:
            return None
        return song["sections"].get(section_name)

    def get_default_section(self, title: str) -> dict | None:
        """获取默认段落（副歌 > 主歌1 > 主歌 > 第一段）。"""
        song = self.songs.get(title)
        if not song:
            return None
        for name in DEFAULT_SECTION_ORDER:
            if name in song["sections"]:
                return song["sections"][name]
        # 兜底：返回第一个段落
        if song["sections"]:
            return list(song["sections"].values())[0]
        return None

    def find_section(self, title: str, section_hint: str) -> dict | None:
        """
        模糊查找段落。
        section_hint 可以是:
          - exact: "副歌", "主歌1"
          - fuzzy: "高潮部分" → "副歌", "开头" → "主歌1", "结尾" → "尾声"
        """
        song = self.songs.get(title)
        if not song:
            return None
        sections = song["sections"]

        # 精确匹配
        if section_hint in sections:
            return sections[section_hint]

        # 模糊词映射
        fuzzy_map = {
            "高潮": "副歌", "副歌": "副歌", "高潮部分": "副歌",
            "开头": "主歌1", "前面": "主歌1", "第一段": "主歌1",
            "结尾": "尾声", "最后": "尾声", "最后一段": "尾声",
            "中间": "主歌2",
        }
        mapped = fuzzy_map.get(section_hint)
        if mapped and mapped in sections:
            return sections[mapped]

        # 包含匹配
        for name in sections:
            if section_hint in name:
                return sections[name]

        return None

    def original_audio(self, title: str) -> str:
        """原声（原唱干净人声）路径，没有则空串。

        接 .wav 与 .mp3 两种：开发机上存的是分离出来的无损 wav；发布包里是
        ffmpeg 转码后的 mp3（同目录、同名、只换后缀），因为唱歌走 CQ:record、
        QQ 侧本来就会把它转成 SILK 低码率语音编码——存无损是白背 1.5G。
        """
        d = self.songs_dir / "covers" / "separated"
        for suf in (".wav", ".mp3"):
            p = d / f"{title}_FINAL{suf}"
            if p.exists():
                return str(p)
        return ""

    def get_section_audio(self, title: str, section_name: str, version: str = "rvc") -> str:
        """获取段落音频文件的路径。没有则返回空字符串。

        version:
          "rvc"（默认）= 糖糖声线——songs/audio/ 下的 RVC 转换成品
          "original" = 原声——covers/separated/{title}_FINAL.{wav,mp3}
        """
        if version == "original":
            return self.original_audio(title)

        # rvc：段落音频 → 旧单文件 wav → 旧单文件 mp3 → 原声 FINAL 兜底
        sec = self.get_section(title, section_name)
        if sec and sec.get("audio"):
            return sec["audio"]
        legacy = self.songs_dir / "audio" / f"{title}.wav"
        if legacy.exists():
            return str(legacy)
        legacy_mp3 = self.songs_dir / "audio" / f"{title}.mp3"
        if legacy_mp3.exists():
            return str(legacy_mp3)
        return self.original_audio(title)

    def has_any_audio(self, title: str) -> bool:
        """判断某首歌是否有任何音频（RVC 糖糖声线、原声或旧格式）。"""
        song = self.songs.get(title)
        if not song:
            # 曲库里没有但可能 audio/（RVC）或 covers/separated（原声）下有文件
            _ad = self.songs_dir / "audio"
            return any((_ad / f"{title}{s}").exists() for s in (".wav", ".mp3")) or \
                   bool(self.original_audio(title))
        for sec in song["sections"].values():
            if sec.get("audio"):
                return True
        # 兜底要与上面 `not song` 那条、以及 get_section_audio 的旧格式兜底**同口径**：
        # 都认 wav 与 mp3。原来这里只查 .wav——「段落标记下没有歌词」+ 只有旧式 mp3
        # 时，has_any_audio=False 而 get_section_audio 能返回真实路径，
        # 那首歌就不进 list_songs_with_audio()，LLM 数出来的歌名比实际少。
        _ad = self.songs_dir / "audio"
        return any((_ad / f"{title}{s}").exists() for s in (".wav", ".mp3")) or \
               bool(self.original_audio(title))

    def list_songs_with_audio(self) -> list[str]:
        """返回有音频（真能播放）的歌名列表——不截断，LLM 数歌名就得到真实数量。"""
        return [name for name in self.songs if self.has_any_audio(name)]

    def songs_with_status(self) -> list[dict]:
        """
        返回所有歌曲的状态列表（给控制台用）。
        status: "ready" | "partial" | "pending" | "no_lyrics"
        """
        result = []
        for title, song in self.songs.items():
            total = len(song["sections"])
            with_audio = sum(1 for s in song["sections"].values() if s.get("audio"))
            has_score = (self.songs_dir / "scores" / f"{title}.ds").exists()
            # 纯翻唱（无歌词文件）或有歌词文件但无歌词内容
            has_lyrics = not song.get("no_lyrics") and any(
                sec.get("text", "").strip() for sec in song["sections"].values()
            )

            if with_audio == total and has_lyrics:
                status = "ready"
            elif with_audio > 0 and not has_lyrics:
                status = "no_lyrics"
            elif with_audio > 0:
                status = "partial"
            elif not has_lyrics:
                status = "no_lyrics"
            else:
                # 有歌词就可渲染（无需 .ds 乐谱）
                status = "pending"

            result.append({
                "title": title,
                "artist": song["artist"],
                "status": status,
                "total_sections": total,
                "with_audio": with_audio,
                "has_score": has_score,
                "sections": {
                    name: {
                        "lines": sec["line_count"],
                        "audio": bool(sec.get("audio")),
                        "preview": sec["preview"],
                    }
                    for name, sec in song["sections"].items()
                },
            })
        return result

    # ── LLM 提示词 ──

    def build_sing_prompt(
        self,
        song: dict,
        section: str | None = None,
        voice_available: bool = False,
    ) -> str:
        """
        构建唱歌 LLM 提示词（全曲播放模式）。

        展示完整歌词，LLM 自然唱歌，末尾加 [SING] 触发全曲音频。
        """
        artist_line = f"（原唱：{song['artist']}）" if song.get("artist") else ""

        return (
            f"## 🎤 唱歌时间！\n\n"
            f"群友点了一首歌，你很喜欢这首歌，自然地唱出来。\n\n"
            f"**歌名**：{song['title']}{artist_line}\n\n"
            f"**歌词**：\n{song['lyrics']}\n\n"
            f"**唱歌要求**：\n"
            f"- 像在 KTV 一样自然地唱歌，可以唱一段或整首\n"
            f"- 在歌词间加入你的小情绪（'这段好甜' '每次唱到这句都会鼻酸'）\n"
            f"- 但不要打断歌词太多，主要把歌唱出来\n"
            f"- 唱完后自然地问'好听吗？'或聊聊对这首歌的感受\n"
            f"- **在回复末尾必须加上 [SING] 标记**，这是播放歌曲音频的唯一方式，不加就不会放歌！"
        )
