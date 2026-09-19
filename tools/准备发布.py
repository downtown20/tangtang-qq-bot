#!/usr/bin/env python3
"""小糖糖 发布构建 v2（2026-09-05）— 主仓白名单快照 + 附件清单

按 docs/发布/发布清单_v1_20260905.md（已冻结）生成 GitHub 主仓快照：
  白名单制——没列即不进。第三方引擎/个人数据/多版本残留一律不复制。
  主仓 = 聊天核心代码 + 贴图 + 精选文档 + 配置模板（clone 即跑，无模型自动降级纯文本模式）。
附件（歌包/语音推理集/角色贴图）不打包，仅输出清单与打包命令——体积大（713M/6.5G/138M），
分发方式（GitHub Release 附件/网盘）待主人定后可按清单打包。

v1（同学离线整包）已归档 tools/_归档/准备发布_v1_classic.py。

用法：python tools/准备发布.py
输出：项目上级目录 小糖糖-发布/（快照，可 git init 后 push GitHub）
"""
import re
import shutil
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
OUT = BASE.parent / "小糖糖-发布"

IGNORE_ALWAYS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", ".git", ".hg", ".svn",
    "*.bak*", "*.log", "*.db", "*.db-shm", "*.db-wal", ".DS_Store",
)

# ═══════════════════════════════════════
# 主仓白名单（发布清单 v1 冻结）
# ═══════════════════════════════════════

COPY_DIRS = ["agent", "napcat", "tests", "scenarios", "stickers", "stickers_cg", "knowledge"]
# knowledge/ 整目录拷时额外剔除（2026-09-05 主人裁决 v2）：
# 色色参考只发软色情 2 篇（丛雨对话范例/恋爱感氛围），BDSM/官能技法与直露写作指南不进公开；
# learned_ 自动学习残留不进；索引 .knowledge_index.sqlite3 随包（主人指定，避免首启重建等待）
KNOWLEDGE_IGNORE = shutil.ignore_patterns(
    "技法摘录_中文百合BDSM.md", "技法摘录_日系官能.md", "色色_写作指南.md",
    "learned_*", "_order.json")

COPY_FILES = [
    # 入口与核心脚本
    "main.py", "糖糖控制台_qt.py", "诊断糖糖.py", "全面诊断.py", "诊断对话质量.py",
    "启动控制台.bat", "安装糖糖.bat", "start.py",  # start.py: ASCII 启动桥（bat 纯 ASCII 不能写中文文件名）
    "requirements.txt",
    # 人格角色卡（主人拍板公开）
    "role_card.md", "role_card_michele.md", "role_card_murasame.md",
    # 开发者文档
    "CLAUDE.md",
]

# tools/ 白名单（2026-09-05 泄漏事故后收窄：默认全剔，只保留通用部署/运维/对用户有意义的工具。
# 记忆运维/评测/真实环境测试脚本含真实 QQ 或依赖内部环境——不进发布，dev 保留。）
TOOLS_KEEP_NAMES = {
    "安装糖糖.py", "体检.py", "快速安装ffmpeg.py", "同步记忆.py",
    "配置SnowLuma.py", "配置NapCat.py", "检查SnowLuma更新.py", "打包exe.py",
    "标注贴图情绪.py", "模拟消息测试.py", "打包发布附件.py", "准备发布.py",
    "打包发布版本.py", "发布前检查.py", "渲染架构图.py",
    "separate_vocals.py", "separate_vocals.bat",   # 唱歌链（可选附加场景）
}
# 骨架目录：主仓预装空目录 + 摆放说明（用户 clone 即见全貌，附件解压有指引；2026-09-05 主人提议）
SCAFFOLD_NOTES = {
    "gpt-sovits/摆放说明.txt": "📦 语音推理集（自动获取，无需手动解压）\n\n"
    "本目录已自带引擎代码；模型（底模 + 糖糖声线 + 情绪参考音频，约 6G）由安装程序自动下载。\n\n"
    "获取方式：双击项目根目录的「安装糖糖.bat」→ 功能选择里勾上「发语音+听懂语音消息」\n"
    "下载中断可直接重跑，已下好的不会重复下载。\n\n"
    "装好后重启控制台——糖糖能发语音、各情绪参考音频齐全。\n"
    "不装语音也不影响聊天——糖糖自动以文字回复。\n",
    "songs/摆放说明.txt": "🎵 预录歌曲（已随安装包自带，装好就能点歌）\n\n"
    "本目录的音频是糖糖唱好的成品——点歌时直接播放，不是实时生成，也不需要额外模型。\n\n"
    "想自己加歌：把音频命名为 歌名.wav 放 audio/，同名歌名.txt 放本目录写歌词（分段落参考现有 txt）。\n",
    "stickers_michele/摆放说明.txt": "🖼 米雪儿角色专属表情包（已随包自带）\n\n"
    "角色表情是绑定的、不能跨用：运行 /角色 米雪儿 后糖糖只用本目录的表情。\n"
    "本目录为空时才需要补图——把表情图放进本目录即可，命名用情绪词开头（如 开心_撒娇_任意名.jpg）。\n"
    "糖糖本体（/角色 糖糖）用的是 stickers/ 默认表情包——已自带 956 张。\n",
    "stickers_murasame/摆放说明.txt": "🖼 丛雨角色专属表情包（已随包自带）\n\n"
    "角色表情是绑定的、不能跨用：运行 /角色 丛雨 后糖糖只用本目录的表情。\n"
    "本目录为空时才需要补图——把表情图放进本目录即可，命名用情绪词开头（如 开心_撒娇_任意名.jpg）。\n"
    "糖糖本体用 stickers/ 默认表情包（已自带 956 张）。\n",
    "share_images/摆放说明.txt": "🖼 图片分享图库\n\n"
    "糖糖定时/主动发图的图片来源：把图片直接放入本目录（支持子文件夹分类，如 share_images/美图/）。\n"
    "控制台 → 图片分享 可管理。初始为空——糖糖只有你放进来的图。\n",
    "voice_cache/摆放说明.txt": "🗣 语音缓存（自动生成，无需手动管理）\n\n"
    "糖糖合成的语音文件缓存目录——运行时自动创建与管理，一般不需要理会。\n",
}
DOCS_SUBDIRS = ["用户手册", "decisions", "发布"]  # docs/ 精选子目录
# 内部协作文档（2026-09-18 主人裁决）——文件本身就是「协作分工 / 发布流程」记录，
# 不进公开仓库。判据是**文件性质**，不是内容里顺带提到工具名：ROADMAP 等
# 技术文档里的零散提及保留，技术价值大于措辞噪音。
DOCS_EXCLUDE_NAMES = {
    "GitHub发布计划_20260905.md", "发布清单_v1_20260905.md",
    "发布规划书_20260905.md", "模块整理方案_20260905.md",
    "已删备份清单_20260905.txt", "已删RVC模型清单_20260905.txt",
    "Claude_Code_协作任务包_2026-08-28.md",
    "Claude_Code_协作任务包_20260828_P1-3c.md",
    # 这三个会被第 10 步搬到别处（README→根、LICENSE.txt→根、模块地图.md→docs/），
    # docs/发布/ 里再留一份就是重复——读者会在两个地方看到同一篇，
    # 改的时候还容易只改一处（2026-09-19）
    "README.md", "LICENSE.txt", "模块地图.md",
}
DEV_PLAN_TOP = True                              # 开发规划/ 顶层 .md（不含归档/）
GRAPH_SRC = BASE / "artifacts" / "图谱"

# ═══════════════════════════════════════
# 敏感字段映射（config.yaml → 占位）
# ═══════════════════════════════════════
# 发布初始外观（2026-09-06 三段式模型默认）：深色主题 + 岩石灰配色（黑白灰开箱）。
# ⚠ 与 agent/theme_palettes.py 的 DEFAULT_* 对齐（改一处必须同步另一处）。
APPEARANCE_DEFAULT = {
    "theme_mode": "dark",        # dark / light / auto
    "color_scheme": "graphite",  # SCHEMES 键或 "custom"
    "custom_color": "#737373",
    "font_size": 13,
}


def scrub_cfg(cfg: dict) -> dict:
    import copy
    c = copy.deepcopy(cfg)
    if "bot" in c:
        c["bot"]["qq_id"] = "你的机器人QQ号"
        c["bot"]["owner_qq"] = "你的QQ号"
    if "llm" in c:
        c["llm"]["api_key"] = "你的DeepSeek或Claude的API密钥"
        if isinstance(c["llm"].get("vision"), dict):
            c["llm"]["vision"]["api_key"] = "你的视觉API密钥（不用识图可留空）"
            # 模型/地址不写死供应商默认——用户按接入的 API 自填（2026-09-05）
            c["llm"]["vision"].pop("model", None)
            c["llm"]["vision"].pop("base_url", None)
    if "napcat" in c:
        c["napcat"]["access_token"] = "请修改为你自己的 access_token"
    if "voice" in c:
        # 补上听/说分离开关（真 config 未必有——2026-09-18 新增，让用户看得见这个旋钮）
        c["voice"].setdefault("asr_enabled", True)
    if "groups" in c:
        c["groups"] = {}
    if "blacklist" in c:
        c["blacklist"] = {"groups": [], "private_users": []}
    if "appearance" in c:
        # 初始外观整节替换为 A 套默认（主人壁纸主题/自定义色不进发布）
        c["appearance"] = dict(APPEARANCE_DEFAULT)
    # 通用兜底：任何 9-11 位纯数字字符串（QQ/群号，含 dict 键如 scenario_targets）→ 占位
    _QQ_PLACEHOLDER = "10001"
    def _clean_key(k):
        return _QQ_PLACEHOLDER if isinstance(k, str) and k.isdigit() and 9 <= len(k) <= 11 else k
    def _clean(v):
        if isinstance(v, dict):
            return {_clean_key(k): _clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clean(x) for x in v]
        if isinstance(v, str) and v.isdigit() and 9 <= len(v) <= 11:
            return _QQ_PLACEHOLDER
        return v
    return _clean(c)


# ═══════════════════════════════════════
# 敏感扫描（发布红线自检）
# ═══════════════════════════════════════
QQ_RE = re.compile(r"\b[1-9]\d{8,10}\b")        # 9-11 位数字（QQ/手机号候选，排除日期）
QQ_KNOWN_CONSTANTS = {"2147483647", "2147483648",      # int32 边界（store.py SQL 常量）
                      "3221225477",                    # 0xC0000005 崩溃码（GPT-SoVITS 事故记录）
                      "1787875200", "1787875201",      # unix 时间戳（tests 事件 time 字段，
                      "1787961600", "1787940000",      #   2023-2026 年段，非 QQ/手机号）
                      "1700000000",
                      }
# 注意：token 模式拆串拼接——避免扫描器源码行被自己的 KEY_RE 命中（2026-09-05）
_TOK_VOLC = "VOLC" + "_TOKEN"
_TOK8 = "NAPCAT_TOKEN" + "=8"
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}"
                    r"|access[_-]?token\s*[:=]\s*['\"]?[A-Za-z0-9]{10,}"
                    r"|" + _TOK_VOLC + r"|" + _TOK8)
WINPATH_RE = re.compile(r"[Dd]:\\\\[^\\s\"'，。、]+")   # 只盯个人数据盘 D:\（C:\Python310 等通用路径不算）


def scan_sensitive(root: Path) -> list[str]:
    """扫描快照内敏感模式，返回命中行（空 = 干净）。"""
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".md", ".yaml", ".yml", ".txt", ".json", ".bat"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if KEY_RE.search(line) or WINPATH_RE.search(line):
                hits.append(f"{p.relative_to(root)}:{i}: {line.strip()[:100]}")
                continue
            for m in QQ_RE.findall(line):
                if m not in QQ_KNOWN_CONSTANTS:
                    hits.append(f"{p.relative_to(root)}:{i}: {line.strip()[:100]}")
                    break
    return hits


# ═══════════════════════════════════════
# 主流程
# ═══════════════════════════════════════
def _clear_tree(root: Path, attempts: int = 3) -> None:
    """清空目录内容但保留目录本身（不删根——根可能被进程当 cwd 占用，2026-09-05）。"""
    import time
    for _ in range(attempts):
        stuck = False
        for child in list(root.iterdir()):
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                stuck = True
        if not stuck:
            return
        time.sleep(2)
    raise OSError(f"多次重试后仍有文件被占用: {root}")


def main() -> None:
    print("📦 小糖糖 发布构建 v2（白名单快照）")
    OUT.mkdir(parents=True, exist_ok=True)
    # 保留 .git 历史（2026-09-05 事故：直接 rmtree 会把已 push 的 .git 删成残缺）
    _git_tmp = None
    if (OUT / ".git").exists():
        _git_tmp = OUT.parent / (OUT.name + ".git.tmp")
        (OUT / ".git").rename(_git_tmp)
    _clear_tree(OUT)
    if _git_tmp:
        _git_tmp.rename(OUT / ".git")
        print("  ♻ 已保留快照 .git（git 历史无损，内容差异下次 status 可见）")

    # 1. 整目录复制（knowledge 带额外剔除规则，与通用规则合并）
    def _merged_ignore(*fns):
        return lambda dir_, names: {n for fn in fns for n in fn(dir_, names)}
    for d in COPY_DIRS:
        src = BASE / d
        if src.is_dir():
            ign = _merged_ignore(IGNORE_ALWAYS, KNOWLEDGE_IGNORE) if d == "knowledge" else IGNORE_ALWAYS
            shutil.copytree(src, OUT / d, ignore=ign)
            print(f"  ✅ {d}/")

    # 2. 根文件
    for f in COPY_FILES:
        s = BASE / f
        if s.is_file():
            shutil.copy2(s, OUT / f)
            print(f"  ✅ {f}")
        else:
            print(f"  ⚠ 跳过（不存在）: {f}")

    # 3. tools/ 白名单（默认全剔——内部测试/记忆运维脚本含真实 QQ，绝不进发布）
    (OUT / "tools").mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in sorted(TOOLS_KEEP_NAMES):
        s = BASE / "tools" / name
        if s.is_file():
            shutil.copy2(s, OUT / "tools" / s.name)
            copied += 1
    print(f"  ✅ tools/（白名单 {copied} 个：部署/运维/通用工具）")

    # 4. docs/ 精选
    def _docs_ignore(dir_, names):
        return set(IGNORE_ALWAYS(dir_, names)) | (set(names) & DOCS_EXCLUDE_NAMES)

    skipped_docs = []
    for sub in DOCS_SUBDIRS:
        src = BASE / "docs" / sub
        if src.is_dir():
            shutil.copytree(src, OUT / "docs" / sub, ignore=_docs_ignore)
            skipped_docs += [n for n in DOCS_EXCLUDE_NAMES if (src / n).is_file()]
    # 开发规划顶层技术报告（不含 归档/）
    plan_src, plan_dst = BASE / "docs" / "开发规划", OUT / "docs" / "开发规划"
    plan_dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for s in sorted(plan_src.glob("*.md")):
        if s.name in DOCS_EXCLUDE_NAMES:
            skipped_docs.append(s.name)
            continue
        shutil.copy2(s, plan_dst / s.name)
        n += 1
    if skipped_docs:
        print(f"  ⏭ docs 排除内部协作文档 {len(skipped_docs)} 篇：{'、'.join(sorted(skipped_docs))}")
    # 架构图谱 artifact → docs/架构图谱/（只取 html + 生成器；full_graph.json 36.8M 数据源不进发布）
    if GRAPH_SRC.is_dir():
        graph_dst = OUT / "docs" / "架构图谱"
        graph_dst.mkdir(parents=True, exist_ok=True)
        for graph_file in ("糖糖架构图谱.html", "build_graph.py"):
            s = GRAPH_SRC / graph_file
            if s.is_file():
                shutil.copy2(s, graph_dst / s.name)
    print(f"  ✅ docs/（用户手册 + decisions + 发布 + 开发规划顶层 {n} 篇 + 架构图谱 html）")

    # 5. config.example.yaml（真 config 脱敏）
    import yaml
    cfg_src = BASE / "config.yaml"
    if cfg_src.exists():
        cfg = yaml.safe_load(cfg_src.read_text(encoding="utf-8")) or {}
        (OUT / "config.example.yaml").write_text(
            yaml.dump(scrub_cfg(cfg), allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")
        print("  ✅ config.example.yaml（已脱敏）")

    # 6. .env.example（全占位，绝无真实 token）
    (OUT / ".env.example").write_text(
        "# 环境变量配置（复制为 .env 并填写）\n"
        "# SnowLuma / OneBot —— 与 config.yaml 的 napcat.access_token 一致\n"
        "NAPCAT_TOKEN=请填写你的access_token\n", encoding="utf-8")
    print("  ✅ .env.example（占位）")

    # 7. 骨架目录 + 摆放说明（主人 2026-09-05：框架提前给用户，附件解压有指引）
    for rel, note in SCAFFOLD_NOTES.items():
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(note, encoding="utf-8")
    print(f"  ✅ 骨架目录 ×{len(SCAFFOLD_NOTES)}（gpt-sovits/songs/角色贴图/share_images/voice_cache + 摆放说明）")

    # 8. .gitignore
    (OUT / ".gitignore").write_text(
        "# 密钥与隐私——绝不提交\n"
        ".env\nconfig.yaml\n*.db\n*.db-shm\n*.db-wal\n"
        "memory_sync/\nQQ数据备份-*/\n\n"
        "# 运行时产物\n__pycache__/\n*.pyc\n.pytest_cache/\nvoice_cache/\n"
        "share_images/\ngenerated_images/\ntemp_files/\n*.log\n*.json\n"
        # 贴图情绪索引不是运行时产物——原来被上面的 *.json 一刀切挡在仓库外，
        # 于是 clone 到的仓库比版本 zip 少一个文件（贴图只能退回按文件名猜情绪）。
        # 2026-09-18 核实：内容是贴图描述，无隐私字段。
        "!stickers/metadata.json\n!stickers_cg/metadata.json\n"
        "!voice_cache/摆放说明.txt\n!share_images/摆放说明.txt\n"
        "!requirements.txt\n\n"
        "# 本机工具目录\n.claude/\n.codex/\n.gitnexus/\n.stfolder/\n.stignore\n",
        encoding="utf-8")
    print("  ✅ .gitignore")

    # 8. 敏感复扫（红线自检）
    hits = scan_sensitive(OUT)
    if hits:
        print("\n⚠️  ⚠️  敏感扫描发现命中（发布前必须清零）：")
        for h in hits[:20]:
            print(f"    {h}")
    else:
        print("  ✅ 敏感扫描：零命中")

    # 9. 附件清单（只报告，不打包）
    audio_dir = BASE / "songs" / "audio"
    sep_dir = BASE / "songs" / "covers" / "separated"
    n_audio = len(list(audio_dir.glob("*.wav"))) if audio_dir.is_dir() else 0
    audio_stems = {p.stem for p in audio_dir.glob("*.wav")} if audio_dir.is_dir() else set()
    extra_final = []
    if sep_dir.is_dir():
        for p in sorted(sep_dir.glob("*_FINAL.wav")):
            if p.stem.removesuffix("_FINAL") not in audio_stems:
                extra_final.append(p.name)
    print(f"\n🎵 附件 A 歌包：songs/audio {n_audio} 首"
          + (f" + separated 补差 {len(extra_final)} 首 {extra_final}" if extra_final else "")
          + " + songs/*.txt 歌词")
    gpt = BASE / "gpt-sovits"
    if gpt.is_dir():
        size_gb = sum(f.stat().st_size for f in gpt.rglob("*") if f.is_file()) / 2**30
        print(f"🗣 附件 B 语音推理集：gpt-sovits/（{size_gb:.1f}G）")
    for name, d in (("角色贴图A", "stickers_michele"), ("角色贴图B", "stickers_murasame")):
        p = BASE / d
        if p.is_dir():
            mb = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 2**20
            print(f"🖼 附件 {name}：{d}/ {mb:.0f}M")

    # 10. 发布文档补位（R2/主人产物——源存 docs/发布/，缺失时提示）
    for src_name, dst_rel in (("README.md", "README.md"),
                              ("LICENSE.txt", "LICENSE"),
                              ("模块地图.md", "docs/模块地图.md")):
        src = BASE / "docs" / "发布" / src_name
        if src.is_file():
            dst = OUT / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            print(f"  ⚠ 缺 docs/发布/{src_name}——发布文档未补位（如已有定稿请放此处）")

    # 11. 汇总
    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"\n✅ 主仓快照完成：{OUT}（{total / 2**20:.0f} MB）")
    print("   ⚠ 该目录可 git init 后推送 GitHub")


if __name__ == "__main__":
    main()
