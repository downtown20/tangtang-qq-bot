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

`--no-voice`：语音分卷只从 `gpt-sovits/` 取内容（第三方引擎 + 模型），
改代码 / 文档 / 安装器都不会影响它。补发小版本时加这个旗标，省掉
重复压缩 6 GB 的十分钟（分卷本身可跨版本复用，见 打包发布版本.py 的 _voice_files）。
"""
from __future__ import annotations

import os
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
#   再下一版写 v1.07.00，中号还有 93 个位子才到 2.0。
#
#   由 test_release_tag_is_three_part 钉住格式，
#   由 test_changelog_newest_version_matches_tag 钉住「改这里就得同步更新日志」。
TAG = "v1.06.00"
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

INSTALL_NOTE = """🍬 小糖糖 {tag} — 开箱即用的安装说明

本包已自带：聊天核心 · 记忆系统 · 41 首预录歌曲 · 角色专属表情包 · 图形控制台。
语音合成与识图需要额外模型，安装时按需自动下载。

━━━ 三步开始 ━━━
  1. 双击  安装糖糖.bat    —— 勾你需要功能，依赖与模型自动装好、配置自动接好线
  2. 双击  启动控制台.bat  —— 在「设置」页填：机器人的 QQ 号、你的 QQ 号、模型 API Key
  3. 控制台里启动 SnowLuma 扫码登录，再启动糖糖

━━━ 关于功能勾选 ━━━
  默认已勾上「聊天+记忆」「唱歌」「图形控制台」——够用了。
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
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{wav.stem}.mp3"
    if out.exists() and out.stat().st_size > 0 and out.stat().st_mtime >= wav.stat().st_mtime:
        return out
    tmp = out.with_name(out.name + ".part")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
             "-vn", "-c:a", "libmp3lame", "-b:a", bitrate, str(tmp)],
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
    """糖糖声线（RVC 成品）→ 128kbps mp3。

    比原声低一档是因为这些是 **40kHz 单声道**（原声那批是立体声），
    128k 单声道已经远超 SILK 能保留的信息量。
    """
    return _to_mp3(wav, MP3_CACHE_VOICE, "128k")


def _add_bundled_media(zf: zipfile.ZipFile) -> int:
    """歌曲（audio/*.wav + 歌词 + separated 补差）+ 角色贴图，路径与最终布局一致"""
    n = 0
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
        # 已经是 mp3 的（或开发机上只有 mp3 的歌）原样带
        for p in sorted(audio.glob("*.mp3")):
            if p.stem in _seen_audio:
                continue
            zf.write(p, f"songs/audio/{p.name}")
            n += 1
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
        for p in sorted(sep.glob("*_FINAL.mp3")):
            if p.stem in _seen_orig:              # 两版都在时优先无损 wav
                continue
            zf.write(p, f"songs/covers/separated/{p.name}")
            n += 1
        for p in sorted(songs.glob("*.txt")):        # 歌词
            zf.write(p, f"songs/{p.name}")
            n += 1
    for name in BUNDLED_STICKER_DIRS:
        root = BASE / name
        if not root.is_dir():
            continue
        for p, rel in _zip_walk(root):
            zf.write(p, f"{name}/{rel}")
            n += 1
    return n


def build_package() -> Path:
    out = OUT / PKG_NAME
    print(f"\n📦 {out.name}")
    extra = {"安装说明.txt": INSTALL_NOTE.format(tag=TAG)}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        n = _add_tree(zf, SNAPSHOT, extra)
        n += _add_bundled_media(zf)
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

def main() -> int:
    if not SNAPSHOT.is_dir():
        print(f"❌ 未找到快照 {SNAPSHOT}")
        print("   请先运行: python tools/准备发布.py")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    ATTACH.mkdir(parents=True, exist_ok=True)

    print("🍬 小糖糖 发布打包（一个包 + 语音分卷附件）")
    print(f"   快照: {SNAPSHOT}")
    print(f"   输出: {OUT}")

    pkg = build_package()

    skip_voice = "--no-voice" in sys.argv
    if skip_voice:
        # 复用上一版的分卷：它们只装 gpt-sovits/（第三方引擎+模型），
        # 与代码/文档改动无关。补发小版本时不必重新压 6 GB。
        parts = sorted(ATTACH.glob("tangtang-voice-*of*.zip"))
        print(f"\n{'─' * 50}")
        if parts:
            print(f"[>] --no-voice：沿用现有分卷 {len(parts)} 卷，不重新打包")
        else:
            print("[!] --no-voice 但没找到现成分卷——上一版的分卷在哪？")
    else:
        print(f"\n{'─' * 50}")
        parts = build_voice_parts()

    print(f"\n{'─' * 50}")
    print("✅ 全部产物")
    print(f"   安装包   {pkg.name}  {pkg.stat().st_size / 2**20:.0f} MB")
    for p in parts:
        print(f"   语音分卷 {p.name}  {p.stat().st_size / 2**30:.2f} GB")
    if skip_voice:
        print("\n   ⚠ 分卷未重新生成——上传时可直接复用上一版的附件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
