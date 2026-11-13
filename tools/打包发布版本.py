#!/usr/bin/env python3
"""小糖糖 发布打包（2026-09-18）

**一个包**：代码 + 文档 + 安装器 + 歌曲 + 角色贴图。
功能由用户在 `安装糖糖.bat` 里勾选，模型按需自动下载——不再按功能切多个包。

为什么不是多版本包（2026-09-18 主人指出后改）：三版本包里 1723 个条目只差
`版本.txt` 一个文件，等于用近 3 倍体积和上传量换了句「这是哪个版本」。
市场主流（AstrBot / Nekro Agent / Koishi / ComfyUI）都是「一个入口 + 安装时选」。

只有 GPT-SoVITS 语音引擎与底模太重（约 6G）不进包，走 Release 分卷，
由安装器自动下载并落位。

用法：
    python tools/准备发布.py          # 先刷新快照（白名单 + 脱敏扫描）
    python tools/打包发布版本.py       # 再打包 + 语音分卷附件
    python tools/打包发布版本.py --no-voice   # 只出主包（补发小版本时用）

产出（默认放在项目同级目录「小糖糖-发布版/」）：
    tangtang-v1.06.00.zip       约 700M（单文件，GitHub 上限 2G）
    附件/tangtang-voice-1ofN.zip  语音推理集分卷（仅勾了语音的用户下载）
    （无独立附件——桌面端绿色版已并入主包，解压后直接双击 exe）

`--no-voice`：语音分卷只从 `gpt-sovits/` 取内容（第三方引擎 + 模型），
改代码 / 文档 / 安装器都不会影响它。补发小版本时加这个旗标，省掉
重复压缩 6 GB 的十分钟（分卷本身可跨版本复用，见 打包发布版本.py 的 _voice_files）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
SNAPSHOT = BASE.parent / "小糖糖-发布"
OUT = BASE.parent / "小糖糖-发布版"
ATTACH = OUT / "附件"

# 版本号单一来源：发布前检查.py 与 README 契约测试都从这里读。
#
# [!] 编号规则（2026-09-20 主人定，当天改过一次）：**三段式 `v1.MM.PP`**。
#
#   起因：先试了 `v0.MM.PP`，结果 **v0.01.00 在 Release 列表里排到了最底下**
#   （0.x 永远小于 1.x），所以主号沿用最高的 1，中号两位递增。
#   v1.0~v1.5 那个两位式也弃用——按那种数法很快就爬到 2.x，而糖糖还在早期。
#
#   v1.06.00 是第一个三段式版本（对应原来的 v1.5 之后那一版）；
#   v1.07.00 修通首次连接；再下一版写 v1.08.00，中号还有 92 个位子才到 2.0。
#
#   由 test_release_tag_is_three_part 钉住格式，
#   由 test_changelog_newest_version_matches_tag 钉住「改这里就得同步更新日志」。
TAG = "v1.08.12"
# ⚠ 包名必须纯 ASCII（2026-09-19 实测）：GitHub Releases 会把附件名里的中文吞掉，
#   `小糖糖-v1.0.zip` 上传后变成 `-v1.0.zip`。纯 ASCII 也顺带避开浏览器/下载工具
#   在非中文 locale 下的编码问题，并与语音分卷 `tangtang-voice-*` 命名一致。
PKG_NAME = f"tangtang-{TAG}.zip"

# 打进包的排除项（快照里的 .git 是发布仓库的，不能进用户包）
ZIP_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", "temp_files",
                    "_原声mp3缓存", "_糖糖声线mp3缓存"}
# ⚠ .sqlite3 **不能一刀切**（2026-09-19 在这上面反复过一次）：
#   knowledge/.knowledge_index.sqlite3 是**要随包**的——它带 BGE 向量，
#   用户首启省 6.2 秒。当初因为它带着旧文本而把它整个排除，砍掉的是用户的等待时间。
#   正确的做法是在清理过的快照上**现场重建**（tools/准备发布.py 的 _build_knowledge_index）,
#   由 test_shipped_index_matches_snapshot_sources_exactly 保证它与源文件同源。
#   只排除边车：它们没有内容，且会让「一个 .sqlite3」变成三个文件。
ZIP_EXCLUDE_SUFFIX = (".pyc", ".log", ".part", ".sqlite3-shm", ".sqlite3-wal")

# 分卷上限——GitHub Release 单文件硬上限 2GB，留出安全余量
PART_LIMIT = int(1.8 * 1024 ** 3)

# ⚠ raw string 是**必须的**（2026-11-14 实测）：下面第 2 步写的是 Windows 路径
#   `desktop\release\...`。普通字符串里 `\r` 被解释成回车符 0x0D，写进 zip 的
#   安装说明就变成 `desktoppelease\...`——一份指向不存在路径的说明书，而
#   生成它的脚本一行报错都没有（实测 bytes: b'desktop\x0delease\\...'）。
#   另外「N 首预录歌曲」原来写死 41，2026-11-14 的包实际 0 首（见 _add_bundled_media
#   的闸门注释），所以数量改成打包时实测填入。
INSTALL_NOTE = r"""🍬 小糖糖 {tag} — 开箱即用的安装说明

本包已自带：聊天核心 · 记忆系统 · {songs} 首预录歌曲 · 角色专属表情包 · 桌面控制台。
语音合成与识图需要额外模型，安装时按需自动下载。

━━━ 三步开始 ━━━
  1. 双击  安装糖糖.bat    —— 勾你需要功能，依赖与模型自动装好、配置自动接好线
  2. 双击  desktop\release\小糖糖桌面端\小糖糖桌面端.exe
                           —— 在「设置」页填：机器人的 QQ 号、你的 QQ 号、模型 API Key
  3. 桌面端里启动 SnowLuma，在网页面板里配好连接，再启动糖糖

  （第 2 步若双击之后没反应，改用包根的 启动桌面端.bat —— 它带 --no-sandbox
    并且把工作目录钉在包根。切记：桌面端的 exe 要在**解压后的包内**原位运行，
    单独把它拷到桌面会找不到上层的 tools\ 目录，界面会报「后端桥退出」。）

━━━ 关于功能勾选 ━━━
  默认已勾上「聊天+记忆」「唱歌」——够用了（日常请用桌面端）。
  （图形控制台/PySide6 已于 2026-11-14 下线——日常请用桌面端，无需额外 GUI 框架）
  想要糖糖开口说话 → 勾「发语音+听懂语音消息」（语音模型约 6G）
  想让她看得懂图   → 勾「本地识图」（可按提示用云端，也可装 Ollama 走本地）

  勾了什么，安装程序就把对应配置开关拨到可用状态；没勾的就干净关掉。
  中途断网直接重跑 安装糖糖.bat——已下载的部分不会重复下载。
  事后想补装：重跑安装程序、勾上对应项即可。

详细说明见 README.md 与 docs/用户手册/使用说明.md
"""


def _zip_walk(root: Path):
    """遍历 root，产出 (绝对路径, 相对 root 的 posix 路径)"""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in ZIP_EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.name.endswith(ZIP_EXCLUDE_SUFFIX) or p.name.endswith(".part"):
            continue
        yield p, rel.as_posix()


def _add_tree(zf: zipfile.ZipFile, root: Path, extra: dict[str, str] | None = None) -> int:
    n = 0
    for path, rel in _zip_walk(root):
        zf.write(path, rel)
        n += 1
    for rel, content in (extra or {}).items():
        zf.writestr(rel, content)
        n += 1
    return n


# ── 歌曲与角色贴图：直接打进安装包（2026-09-18 主人裁决）──
# 唱歌播的是预录成品音频，不是 TTS 实时生成——不需要任何模型，所以装好就能点歌。
# 歌 558M + 贴图 137M 加进去每版约 930M，仍是单文件、远低于 GitHub 2G 上限；
# 拆成独立附件只会让用户多点几次、多解压几次，没有收益。
BUNDLED_STICKER_DIRS = ["stickers_michele", "stickers_murasame"]


# 转码缓存：重复打包不重转（80 首约 2 分钟，没必要每次付）。放在 OUT 下，
# 不进快照也不进 zip——zip 的输入只有 SNAPSHOT 与 _add_bundled_media 显式加的东西。
MP3_CACHE_ORIGINAL = OUT / "_原声mp3缓存"
MP3_CACHE_VOICE = OUT / "_糖糖声线mp3缓存"


def _to_mp3(wav: Path, cache_dir: Path, bitrate: str) -> Path | None:
    """把 wav 转成 mp3（带缓存）。失败返回 None——**不抛异常**。

    **为什么两种歌曲都要转**（2026-09-20 补齐，原声在 v1.2 已转、糖糖声线漏了）：
    唱歌是通过 `[CQ:record,file=file:///...]` 发出去的，也就是 **QQ 语音消息**，
    QQ 侧本来就会把它转成 SILK 这类低码率语音编码。存 40kHz PCM16 无损是白背——
    糖糖声线 40 首共 712M，占整包 67%。

    下面两条防护是复核时补的（两条都实测复现过）：
      · **ffmpeg 起不来时抛 FileNotFoundError**：docstring 说返回 None，实际会一路
        穿出 main()，在输出目录留下一个 235 MB、有贴图没有歌的残缺 zip，
        而文件名已经是最终的 `tangtang-vX.zip`
      · **直接写目标文件**：ffmpeg 被中断会留下截断的 mp3（实测残 2,097,152 字节），
        而它比源 wav 新 → **下次打包缓存命中，残file 就这么进了包**

    第三条（2026-11-14 实测定位，这次是**整批**失败）：
      · **临时名 `.mp3.part` 让 ffmpeg 猜不出封装格式** —— ffmpeg 按扩展名选 muxer，
        看到 `.part` 就 `Unable to choose an output format` 并以 -22 退出。
        于是 40/40 首糖糖声线全部转码失败，包照样生成、文件名照样是最终的
        `tangtang-vX.zip`，里面一首歌都没有。这就是 v1.08.00「0 首歌」的真正机制
        （不属于"判据只看目录存在"那种表层问题）。修法是显式 `-f mp3`。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{wav.stem}.mp3"
    if out.exists() and out.stat().st_size > 0 and out.stat().st_mtime >= wav.stat().st_mtime:
        return out
    tmp = out.with_name(out.name + ".part")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
             "-vn", "-c:a", "libmp3lame", "-b:a", bitrate,
             "-f", "mp3",                      # 临时名是 .part，必须显式指定 muxer
             str(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:                         # FileNotFoundError 也是 OSError
        print(f"      ffmpeg 起不来（{e}）——装了吗？tools/快速安装ffmpeg.py")
        tmp.unlink(missing_ok=True)
        return None
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        print(f"      ffmpeg 返回 {r.returncode}：{(r.stderr or '')[:200]}")
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, out)                         # 原子落位，断在半路不会留下残file
    return out


def _original_mp3(wav: Path) -> Path | None:
    """原声（分离出来的原唱人声）→ 192kbps mp3。"""
    return _to_mp3(wav, MP3_CACHE_ORIGINAL, "192k")


def _voice_mp3(wav: Path) -> Path | None:
    """糖糖声线（预录成品）→ 128kbps mp3。

    比原声低一档是因为这些是 **40kHz 单声道**（原声那批是立体声），
    128k 单声道已经远超 SILK 能保留的信息量。
    """
    return _to_mp3(wav, MP3_CACHE_VOICE, "128k")


def _add_bundled_media(zf: zipfile.ZipFile) -> int:
    """歌曲（audio/*.wav + 歌词 + separated 补差）+ 角色贴图，路径与最终布局一致"""
    n = 0
    n_voice = n_orig = 0          # 计数只为闸门：进包数必须等于源文件数
    songs = BASE / "songs"
    if songs.is_dir():
        audio = songs / "audio"
        # 糖糖声线 40 首：40kHz 单声道 PCM16 共 712M，占整包 67%——转 128k mp3。
        # 与原声同理（唱歌走 CQ:record，QQ 侧转 SILK），见 _to_mp3 的注释。
        _seen_audio: set[str] = set()
        for p in sorted(audio.glob("*.wav")):
            mp3 = _voice_mp3(p)
            if mp3 is None:
                print(f"   [!] 糖糖声线转码失败，跳过：{p.name}")
                continue
            _seen_audio.add(p.stem)
            zf.write(mp3, f"songs/audio/{p.stem}.mp3")
            n += 1
            n_voice += 1
        # 已经是 mp3 的（或开发机上只有 mp3 的歌）原样带
        for p in sorted(audio.glob("*.mp3")):
            if p.stem in _seen_audio:
                continue
            zf.write(p, f"songs/audio/{p.name}")
            n += 1
            n_voice += 1
        # 原声版：**41 首全部进包**，且转码成 mp3。
        # 2026-09-19 修正两处——
        #   ① 原逻辑把"已在 audio/ 里的"当重复文件跳过了。可 audio/ 是**糖糖声线**、
        #      这里是**原声**，同一首歌的两个版本不是重复。结果 41 首里 40 首没有原声，
        #      而 sing 工具还在对 LLM 承诺"对方说「原声」「原唱」时 voice 传「原声」"
        #      （agent/handler.py:5323）→ 用户点了原声只会静默降级放糖糖声线。
        #   ② 无损 WAV 存着没意义：唱歌走 `[CQ:record,...]`，QQ 侧本来就会转成 SILK
        #      这类低码率语音编码。192kbps mp3 已远超 SILK 能保留的信息量，
        #      而 1.78G → 242M 让整包留在 GitHub Releases 的 2G 单附件上限内。
        # 与原声段同口径：**wav 与 mp3 都收**。原先只 glob `*_FINAL.wav`，
        # 于是「从发布包解出来的树上重新打包」时 41 首原声会整批静默不进包
        # （发布包里 separated/ 下 41 个全是 mp3）——而 sing 工具还在对 LLM
        # 承诺「对方说原声就放原唱」。
        sep = songs / "covers" / "separated"
        _seen_orig: set[str] = set()
        for p in sorted(sep.glob("*_FINAL.wav")):
            mp3 = _original_mp3(p)
            if mp3 is None:
                print(f"   [!] 原声转码失败，跳过：{p.name}")
                continue
            # 注意用 p.stem 而不是 p.stem+"_FINAL"：p.stem 本身已经是 "歌名_FINAL"
            _seen_orig.add(p.stem)
            zf.write(mp3, f"songs/covers/separated/{p.stem}.mp3")
            n += 1
            n_orig += 1
        for p in sorted(sep.glob("*_FINAL.mp3")):
            if p.stem in _seen_orig:              # 两版都在时优先无损 wav
                continue
            zf.write(p, f"songs/covers/separated/{p.name}")
            n += 1
            n_orig += 1

        # ── 数量闸门（2026-11-14 新增）───────────────────────────────────
        # 为什么必须在这里 raise：转码失败原先只 print 一行 `[!] ... 跳过`
        # 然后 continue，于是**包能生成、文件名是最终名、里面一首歌都没有**。
        # 2026-11-14 实测的 v1.08.00 就是这种状态（songs/audio 0 首，安装说明
        # 却写着「41 首预录歌曲」）。打包器的唯一职责就是"要么完整、要么别出包"，
        # 所以这里改成硬失败——宁可不出包，也不出一个人点歌永远失败的包。
        want_voice = len({p.stem for p in audio.glob("*.wav")}
                         | {p.stem for p in audio.glob("*.mp3")})
        want_orig = len({p.stem for p in sep.glob("*_FINAL.wav")}
                        | {p.stem for p in sep.glob("*_FINAL.mp3")})
        if n_voice < want_voice:
            raise SystemExit(
                f"❌ 糖糖声线只进了 {n_voice}/{want_voice} 首——包不完整，停止打包"
                f"（先单独跑一遍转码看是哪几首失败：python -c \"...\" 或看上面 [!] 行）")
        if n_orig < want_orig:
            raise SystemExit(
                f"❌ 原声只进了 {n_orig}/{want_orig} 首——包不完整，停止打包")
        print(f"   ✅ 曲库 {n_voice} 首糖糖声线 + {n_orig} 首原声（齐全）")
    for name in BUNDLED_STICKER_DIRS:
        root = BASE / name
        if not root.is_dir():
            continue
        for p, rel in _zip_walk(root):
            zf.write(p, f"{name}/{rel}")
            n += 1
    return n


def _song_total() -> int:
    """安装说明里那句「N 首预录歌曲」——开发机有 41、包里可能 0，
    所以数量必须在**打包时实测**，不能写死（2026-11-14 的教训）。

    口径与 agent/songs.py 的发现逻辑一致：audio/ 认 wav+mp3，原声认 *_FINAL.*。
    这里不 import tools/安装糖糖.py 的 _song_count——那个模块导入期会加载
    依赖位置.py，在打包机上只会有副作用。
    """
    songs = BASE / "songs"
    stems = {p.stem for p in (songs / "audio").glob("*.wav")}
    stems |= {p.stem for p in (songs / "audio").glob("*.mp3")}
    stems |= {p.stem.removesuffix("_FINAL")
              for p in (songs / "covers" / "separated").glob("*_FINAL.*")
              if p.suffix.lower() in (".wav", ".mp3")}
    return len(stems)


def build_package() -> Path:
    out = OUT / PKG_NAME
    print(f"\n📦 {out.name}")
    extra = {"安装说明.txt": INSTALL_NOTE.format(tag=TAG, songs=_song_total())}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        n = _add_tree(zf, SNAPSHOT, extra)
        n += _add_bundled_media(zf)
        n += _add_desktop(zf)
    print(f"   ✅ {n} 个文件，{out.stat().st_size / 2**20:.0f} MB")
    return out


# ─────────────────────────────────────────
# Release 附件
# ─────────────────────────────────────────

# 语音推理集 = 引擎代码 + 权重，一并走 Release 分卷（完整版才下载）。
# 代码不进主仓的原因（2026-09-18 实测后改）：它是第三方项目 RVC-Boss/GPT-SoVITS
# 的 vendored 副本，进主仓会让敏感扫描被上游自带内容永久污染（贡献者 QQ、
# webui.py 里上游默认的本机绝对路径、代码注释里的长数字串），
# 而且安装包会白背 70M 用不上的代码。
GPT_MODEL_PREFIXES = (
    "GPT_SoVITS/pretrained_models/",
    "GPT_SoVITS/text/G2PWModel/",
    "GPT_SoVITS/text/G2PWModel_1.1.zip",   # 已解压 G2PWModel/ 的源包，重复 562M
    "models/",                              # 微调声线（糖糖声线）
    "speakers/",                            # 各情绪参考音频
    "docs/",                                # 上游多语言文档，运行时用不到
)
VOICE_INCLUDE = [
    "GPT_SoVITS/pretrained_models",
    "GPT_SoVITS/text/G2PWModel",
    "models/michele",
    "speakers",
]
VOICE_EXCLUDE_NAMES = {"G2PWModel_1.1.zip"}


# gpt-sovits/ 本身是个 git clone（上游 .git 15M + .github CI 配置）——
# 不是运行时代码，打进分卷等于把上游仓库历史一并分发（2026-09-18 实测发现）
GPT_SKIP_DIRS = {".git", ".github", "__pycache__", ".pytest_cache"}


def _voice_files():
    """先出引擎代码，再出权重——都用相对 gpt-sovits/ 的路径，解压后原地汇合"""
    src = BASE / "gpt-sovits"
    if not src.is_dir():
        print("   ⚠ 未找到 gpt-sovits/ 目录")
        return
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        if rel.startswith(GPT_MODEL_PREFIXES) or GPT_SKIP_DIRS & set(p.parts) \
                or p.name.endswith(".pyc"):
            continue
        yield p, rel
    for inc in VOICE_INCLUDE:
        root = src / inc
        if not root.exists():
            print(f"   ⚠ 缺失: gpt-sovits/{inc}")
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name in VOICE_EXCLUDE_NAMES:
                continue
            if GPT_SKIP_DIRS & set(p.parts) or p.name.endswith(".pyc"):
                continue
            yield p, p.relative_to(src).as_posix()


def build_voice_parts() -> list[Path]:
    """语音推理集分卷——每卷 <2G，安装器按名排序逐卷下载解压"""
    files = list(_voice_files())
    total = sum(p.stat().st_size for p, _ in files)
    print(f"\n🗣 语音推理集：{len(files)} 个文件，{total / 2**30:.2f} GB")
    parts: list[Path] = []
    zf = None
    cur = 0
    try:
        for path, rel in files:
            size = path.stat().st_size
            if zf is None or (cur + size > PART_LIMIT and cur > 0):
                if zf is not None:
                    zf.close()
                    print(f"   ✅ {parts[-1].name}  {parts[-1].stat().st_size / 2**30:.2f} GB")
                idx = len(parts) + 1
                # 先按总卷数占位，稍后回来改名——这里用足够位数的临时名
                out = ATTACH / f"tangtang-voice-{idx:02d}.zip"
                zf = zipfile.ZipFile(out, "w", zipfile.ZIP_STORED)
                parts.append(out)
                cur = 0
            zf.write(path, rel)
            cur += size
        if zf is not None:
            zf.close()
            print(f"   ✅ {parts[-1].name}  {parts[-1].stat().st_size / 2**30:.2f} GB")
    finally:
        if zf is not None:
            zf.close()      # close() 幂等（内部 fp=None 即返回），重复调用安全
    # 改名为 1ofN 形式（安装器按前缀抓取并排序）
    n = len(parts)
    final = []
    for i, p in enumerate(parts, 1):
        target = ATTACH / f"tangtang-voice-{i}of{n}.zip"
        p.replace(target)
        final.append(target)
    print(f"   → 共 {n} 卷：{final[0].name} … {final[-1].name}")
    return final


# ─────────────────────────────────────────

# 桌面端绿色版 → Release 附件（2026-10-28）。
#
# 为什么不塞进主包：Electron 运行时约 180 MB，主包已经约 1 GB——每次改一行前端
# 都要重传 180 MB，而附件可以跨版本复用（只有前端真变了才重出）。
# 附件名必须纯 ASCII —— GitHub Releases 会吞掉附件名里的中文（同上面 PKG_NAME 的注释）。
DESKTOP_RELEASE = BASE / "desktop" / "release" / "小糖糖桌面端"


def _add_desktop(zf: zipfile.ZipFile) -> int:
    """把已构建好的桌面端绿色版塞进主包（包内路径相对项目根）。

    2026-10-28 主人拍板：**一个包**。之前单独出 `tangtang-desktop-X.zip` 附件，
    用户要下两次、解压两次、还要记住「解压到哪」，等于把打包环节的复杂度转嫁给他。
    合并后解压一次就有 `desktop/release/小糖糖桌面端/小糖糖桌面端.exe`，双击即用。
    """
    exe = DESKTOP_RELEASE / "小糖糖桌面端.exe"
    if not exe.is_file():
        print("   ⚠ 没找到桌面端绿色版——跳过（先 cd desktop && pnpm build，"
              "再 python tools/打包桌面端.py）")
        return 0
    n = 0
    dropped = 0
    for p in sorted(DESKTOP_RELEASE.rglob("*")):
        if not p.is_file():
            continue
        # ⚠ 2026-11-14：`.lnk` **不许进包**。快捷方式里存的是**绝对路径**
        #   （实测随包那份写的是打包机的盘符绝对路径），
        #   用户解压到任何别的目录，双击它都是死链：没反应、没报错、没日志。
        #   而 README 恰好教用户"双击 lnk"。发布包里可移动的启动入口只能是
        #   相对路径解析的东西：exe 本身（Electron 自己向上找项目根，实测可用）
        #   或 启动桌面端.bat（`%~dp0`）。
        if p.suffix.lower() == ".lnk":
            dropped += 1
            continue
        zf.write(p, p.relative_to(BASE).as_posix())
        n += 1
    print(f"   ✅ 桌面端绿色版并入主包：{n} 个文件"
          + (f"（剔掉 {dropped} 个 .lnk——里面是打包机的绝对路径，换机器必成死链）"
             if dropped else ""))
    return n


def _desktop_dir() -> Path | None:
    """用户真实的桌面目录。

    [!] 不能写死 `~/Desktop`：中文 Windows 常被重定向（本机实测
    `...User Shell Folders\\Desktop` 被重定向到非默认位置），而 `Path.home()/"Desktop"`
    在那个位置并不存在——写死就会「复制成功」到一个没人看的目录里。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
            p = Path(os.path.expandvars(str(raw)))
            if p.is_dir():
                return p
    except Exception:
        pass
    for cand in (Path.home() / "Desktop", Path.home() / "桌面"):
        if cand.is_dir():
            return cand
    return None


def _copy_to_desktop(pkg: Path) -> Path | None:
    """把打好的包放一份到桌面（2026-11-14 主人要求：每更新一次就放桌面，方便自测）。

    失败（桌面不可写/找不到）只提示，不影响打包本身——桌面不是交付物。
    """
    desk = _desktop_dir()
    if desk is None:
        print("\n[!] 没找到桌面目录，跳过「放桌面」这一步（包已在本目录）")
        return None
    dst = desk / pkg.name
    try:
        shutil.copy2(pkg, dst)
    except OSError as exc:
        print(f"\n[!] 复制到桌面失败：{exc}")
        return None
    size_mb = dst.stat().st_size / 2**20
    print(f"\n📌 已放一份到桌面：{dst}  ({size_mb:.0f} MB)")
    return dst


def main() -> int:
    if not SNAPSHOT.is_dir():
        print(f"❌ 未找到快照 {SNAPSHOT}")
        print("   请先运行: python tools/准备发布.py")
        return 1
    # ⚠ 2026-11-14：准备发布.py 的敏感扫描是在**快照写完**之后才跑的，而打包是
    #   另一条命令——它原先只看快照目录在不在。当天实测：扫描报命中并 SystemExit(1)
    #   之后，手工跑本脚本，zip 照样产出、名字照样是最终名（同 v1.08.01 曲库事故的
    #   形状：闸门红了，下游照跑）。阻断标记由准备发布.py 写在快照根上。
    block = SNAPSHOT / ".release-blocked"
    if block.is_file():
        print("❌ 快照被标记为「敏感扫描未通过」，拒绝打包：")
        print("   " + str(block))
        try:
            print("   " + block.read_text(encoding="utf-8").strip().replace("\n", "\n   "))
        except OSError:
            pass
        print("   清干净后重跑 python tools/准备发布.py（通过时它会删掉这个标记）")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    ATTACH.mkdir(parents=True, exist_ok=True)

    print("🍬 小糖糖 发布打包（单包：主程序 + 桌面端 + 私有语音资产）")
    print(f"   快照: {SNAPSHOT}")
    print(f"   输出: {OUT}")

    pkg = build_package()

    # 2026-10-28：不再出语音分卷。私有声线已随包（准备发布.py 的 4b 步），
    # 公开底模 3.7G 由用户按 docs/用户手册/语音模型自行配置.md 自行放置。
    # build_voice_parts() 保留但不再调用——将来若改回「分卷托管」，直接接回这里。

    print(f"\n{'─' * 50}")
    print("✅ 全部产物")
    print(f"   安装包   {pkg.name}  {pkg.stat().st_size / 2**20:.0f} MB")
    print("   ⚠ 语音公开底模（约 3.7 GB）不随包——用户按"
          " docs/用户手册/语音模型自行配置.md 自行放置")
    _copy_to_desktop(pkg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
