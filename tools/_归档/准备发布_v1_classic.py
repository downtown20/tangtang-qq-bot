"""
准备发布包 — 清理个人数据，保留文件夹结构和必要文件
用法：python 准备发布.py
输出：../小糖糖-发布/ 目录，可直接打包发给别人
"""
import shutil, os, sys, json
from pathlib import Path

BASE = Path(__file__).parent.parent  # 项目根目录
OUT = BASE.parent / "小糖糖-发布"

# 删除旧发布目录
if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True, exist_ok=True)

print("📦 准备发布包...")

# ═══════════════════════════════════════
# 需要复制的文件
# ═══════════════════════════════════════

COPY_FILES = [
    # 核心入口
    "main.py",
    "config.yaml",
    "requirements.txt",
    "role_card.md",
    # 控制台 + 诊断
    "糖糖控制台_qt.py",
    "诊断糖糖.py",
    # 安装 & 启动脚本（统一安装/维护工具）
    "安装糖糖.bat",
    "tools/安装糖糖.py",
    "tools/同步记忆.py",
    "启动控制台.bat",
    ".stignore",
    # 文档
    "CLAUDE.md",
    "docs/用户手册/使用说明.md",
    # 工具脚本
    "tools/配置NapCat.py",
    "tools/配置SnowLuma.py",
    "tools/标注贴图情绪.py",
    "tools/快速安装ffmpeg.py",
    "tools/修复CosyVoice依赖.py",
    "tools/打包exe.py",
    "tools/准备发布.py",
    "tools/diff_sing.py",
    "tools/register_hutao_speaker.py",
    "tools/模拟消息测试.py",
    "tools/分析对话质量.py",
    "tools/dedup_memories.py",
    "tools/trim_silence.py",
    "tools/denoise.py",
    "tools/batch_render_all.py",
    "tools/convert_checkpoint.py",
    "tools/migrate_lyrics_sections.py",
    "tools/migrate_stickers.py",
    "tools/download_covers.py",
]

COPY_DIRS = [
    "agent",
    "napcat",
    "knowledge",
    "songs",
    "stickers",
    "docs",
    "tests",
    "scenarios",
    "DiffSingerMiniEngine",
]

# 需要创建的空目录（带结构 + _order.json）
ENSURE_DIRS = [
    "share_images/美图",
    "share_images/临摹完整过程",
    "voice_cache",
]

# ═══════════════════════════════════════
# 复制文件
# ═══════════════════════════════════════
for f in COPY_FILES:
    src = BASE / f
    dst = OUT / f
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, dst)
        print(f"  ✅ {f}")
    else:
        print(f"  ⚠ 跳过（不存在）: {f}")

# ═══════════════════════════════════════
# 复制目录
# ═══════════════════════════════════════
for d in COPY_DIRS:
    src = BASE / d
    dst = OUT / d
    if not src.exists():
        print(f"  ⚠ 跳过（不存在）: {d}/")
        continue
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".pytest_cache", ".git",
    ))
    print(f"  ✅ {d}/")

# ═══════════════════════════════════════
# 复制 SnowLuma（只保留最新版本，清理登录数据）
# ═══════════════════════════════════════
snow_src = BASE / "SnowLuma"
if snow_src.exists():
    snow_versions = sorted(snow_src.glob("SnowLuma-v*"))
    if snow_versions:
        latest = snow_versions[-1]  # 取最新版
        snow_dst = OUT / "SnowLuma" / latest.name
        snow_dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"  📦 复制 SnowLuma ({latest.name}) ...")
        shutil.copytree(latest, snow_dst, ignore=shutil.ignore_patterns(
            "logs", "data", "__pycache__",
        ))
        # 确保 config 目录存在但为空（不再保留你的 QQ 配置）
        cfg_dir = snow_dst / "config"
        cfg_dir.mkdir(exist_ok=True)
        # 保留 webui.json + runtime.json 模板，删除 onebot_*.json（含你的 QQ 号）
        webui_src = latest / "config" / "webui.json"
        runtime_src = latest / "config" / "runtime.json"
        if webui_src.exists():
            shutil.copy2(webui_src, cfg_dir / "webui.json")
        if runtime_src.exists():
            shutil.copy2(runtime_src, cfg_dir / "runtime.json")
        for ob_cfg in cfg_dir.glob("onebot_*.json"):
            ob_cfg.unlink()
        # 重建空 logs + data 目录
        (snow_dst / "logs").mkdir(exist_ok=True)
        (snow_dst / "data").mkdir(exist_ok=True)
        print(f"  ✅ SnowLuma/{latest.name}/（已清理登录数据）")
    else:
        print(f"  ⚠ 未找到 SnowLuma 版本目录")

# ═══════════════════════════════════════
# 排除不需要的大目录（不复制）
# ═══════════════════════════════════════
SKIP_DIRS = [
    "Retrieval-based-Voice-Conversion-WebUI",  # RVC 已废弃 (2.4GB)
    "NapCat.Shell.Windows.OneKey",             # NapCat 已废弃
    "DiffSinger",                              # 太大了 (433MB ckpt)，同学需要时自行 git clone
    ".claude",                                 # AI 工具配置
]
for d in SKIP_DIRS:
    p = OUT / d
    if p.exists():
        shutil.rmtree(p)
        print(f"  🗑 已排除 {d}/")

# ═══════════════════════════════════════
# 清理 songs/ 目录
# ═══════════════════════════════════════
songs_dir = OUT / "songs"
if songs_dir.exists():
    # 删除预渲染歌声 (997MB) — 同学自己生成
    for sub in ["audio", "covers"]:
        p = songs_dir / sub
        if p.exists():
            shutil.rmtree(p)
            p.mkdir(exist_ok=True)
            print(f"  🗑 已清空 songs/{sub}/（同学本地生成）")
    # 删除 .bak 备份文件
    for bak in songs_dir.rglob("*.bak"):
        bak.unlink()
    print(f"  🗑 已删除 songs/ 下的 .bak 文件")

# ═══════════════════════════════════════
# 创建空目录（带 _order.json）
# ═══════════════════════════════════════
for d in ENSURE_DIRS:
    p = OUT / d
    p.mkdir(parents=True, exist_ok=True)
    order = p / "_order.json"
    if not order.exists():
        order.write_text("[]", encoding="utf-8")
    print(f"  ✅ {d}/（空目录）")

# ═══════════════════════════════════════
# 清除 share_images/ 中的个人图片
# ═══════════════════════════════════════
si_dir = OUT / "share_images"
if si_dir.exists():
    for f in si_dir.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            f.unlink()
    # 删除子文件夹里的图片
    for sub in si_dir.iterdir():
        if sub.is_dir():
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                    f.unlink()
    print("  🗑 已清除 share_images/ 中的个人图片（保留目录结构）")

# ═══════════════════════════════════════
# 清除 voice_cache/ 中的个人语音
# ═══════════════════════════════════════
vc_dir = OUT / "voice_cache"
if vc_dir.exists():
    for f in vc_dir.iterdir():
        if f.is_file():
            f.unlink()
    print("  🗑 已清空 voice_cache/")

# ═══════════════════════════════════════
# 清理 config.yaml 中的敏感信息
# ═══════════════════════════════════════
import yaml

cfg_path = OUT / "config.yaml"
if cfg_path.exists():
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    if cfg is None:
        cfg = {}

    # 清除个人信息
    if "bot" in cfg:
        cfg["bot"]["qq_id"] = "你的机器人QQ号"
        cfg["bot"]["owner_qq"] = "你的QQ号"
    if "llm" in cfg:
        cfg["llm"]["api_key"] = "你的DeepSeek或Claude的API密钥"
        if "vision" in cfg["llm"]:
            cfg["llm"]["vision"]["api_key"] = "你的千问VL的API密钥（不用识图可留空）"
    if "napcat" in cfg:
        cfg["napcat"]["http_url"] = "http://127.0.0.1:3000"
        cfg["napcat"]["access_token"] = "8bzJAHPHug~kvooW"
    if "groups" in cfg:
        cfg["groups"] = {}
    if "blacklist" in cfg:
        cfg["blacklist"] = {"groups": [], "private_users": []}
    if "appearance" in cfg:
        cfg["appearance"]["background_image"] = ""
        cfg["appearance"]["font_size"] = 13
    if "image_share" in cfg:
        cfg["image_share"]["play_category"] = ""

    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8"
    )
    print("  ✅ config.yaml 已清除个人数据")

# ═══════════════════════════════════════
# 删除个人数据文件
# ═══════════════════════════════════════
PERSONAL_FILES = [
    "memory.db",
    ".greeting_state.json",
    ".mood_state.json",
    "tangtang.log",
    "startup.log",
    "all_packages.txt",
    "config.yaml.bak",
    ".env",
]
for f in PERSONAL_FILES:
    p = OUT / f
    if p.exists():
        p.unlink()
        print(f"  🗑 已删除 {f}")

# 删除 sync-conflict 日志
for log in OUT.glob("tangtang.sync-conflict-*.log"):
    log.unlink()
    print(f"  🗑 已删除 {log.name}")

# 删除 config 备份
for bak in OUT.glob("config.yaml.*"):
    if bak.name != "config.yaml":
        bak.unlink()
        print(f"  🗑 已删除 {bak.name}")

# ═══════════════════════════════════════
# ═══════════════════════════════════════
# 清理 knowledge/ — 排除个人/敏感/开发笔记
# ═══════════════════════════════════════
KNOWLEDGE_SKIP_PREFIXES = ("learned_",)  # 群聊自动学到的个人内容
KNOWLEDGE_SKIP_NAMES = {
    "PySide6性能排查经验.md",    # 个人开发调试笔记
    "RVC_MAX_YXBSQ逆向分析.md",  # 逆向工程笔记
}
knowledge_dir = OUT / "knowledge"
if knowledge_dir.exists():
    for f in list(knowledge_dir.iterdir()):
        skip = False
        if f.suffix == ".bak":
            skip = True
        elif f.name.startswith(KNOWLEDGE_SKIP_PREFIXES):
            skip = True
        elif f.name in KNOWLEDGE_SKIP_NAMES:
            skip = True
        if skip:
            f.unlink()
            print(f"  🗑 已排除 knowledge/{f.name}")
# ═══════════════════════════════════════
thumb = OUT / "stickers" / ".thumbnails"
if thumb.exists():
    shutil.rmtree(thumb)

# ═══════════════════════════════════════
# 创建 .env 模板
# ═══════════════════════════════════════
env_template = OUT / ".env.example"
env_template.write_text("""# 环境变量配置（复制为 .env 并填写）
# SnowLuma / OneBot
NAPCAT_TOKEN=8bzJAHPHug~kvooW
""", encoding="utf-8")
print("  ✅ 已创建 .env.example 模板")

# ═══════════════════════════════════════
# 全局清理
# ═══════════════════════════════════════
for pycache in OUT.rglob("__pycache__"):
    shutil.rmtree(pycache)
for pytest_cache in OUT.rglob(".pytest_cache"):
    if pytest_cache.is_dir():
        shutil.rmtree(pytest_cache)

# ═══════════════════════════════════════
# 整理文档
# ═══════════════════════════════════════
DOCS = OUT / "docs"
DOCS.mkdir(exist_ok=True)
for doc in ["CLAUDE.md"]:
    src = OUT / doc
    if src.exists():
        shutil.move(str(src), str(DOCS / doc))

# ═══════════════════════════════════════
# 创建 README
# ═══════════════════════════════════════
(OUT / "README.txt").write_text("""🍬 小糖糖 — QQ 群智能机器人
================================

📖 三步开始
  1. 双击「一键安装.bat」安装 Python 依赖
  2. 双击「启动控制台.bat」
  3. 控制台 → 设置页 → 填入机器人 QQ、你的 QQ、API Key → 保存

💡 配置也可以在控制台的「设置」页面直接填写，不用手动编辑 config.yaml
   - 🤖 机器人 → 填入机器人 QQ 号和你的 QQ 号
   - 🧠 AI模型 → 填入 DeepSeek 或 Claude 的 API Key
   - 🔌 SnowLuma → 地址保持默认即可
   - 其他分类可先不管，后续按需调整

🚀 启动步骤
  1. 仪表盘 → 「启动 SnowLuma」→ 扫码登录机器人 QQ
  2. 运行 python tools/配置SnowLuma.py（首次需配置 OneBot）
  3. 仪表盘 → 「🍬 启动小糖糖」
  4. 在群里 @机器人 或喊「糖糖」开始聊天！

📁 目录说明
  agent/             核心业务代码
  napcat/            QQ 协议适配层
  SnowLuma/          QQ 客户端（SnowLuma）
  DiffSingerMiniEngine/  歌声合成引擎
  stickers/          表情包（908张）
  share_images/      本地图库（初始为空）
  songs/             歌词语料（.txt），歌声需本地生成
  knowledge/         知识库
  tools/             工具脚本
  tests/             pytest 测试
  docs/              技术文档和说明书

💡 详细教程：docs/用户手册/使用说明.md
💡 开发者文档：docs/CLAUDE.md

⚠ 注意事项
  - 歌声 (songs/audio/) 需同学自己用 DiffSinger 生成
  - SnowLuma 首次启动需扫码登录，之后自动记住
  - 如遇问题，运行 python 诊断糖糖.py
""", encoding="utf-8")

# ═══════════════════════════════════════
# 统计与完成
# ═══════════════════════════════════════
# 计算大小
total_size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
total_mb = total_size / (1024 * 1024)

print(f"\n✅ 发布包已准备完成：{OUT}")
print(f"   体积：{total_mb:.0f} MB")
print(f"   文档在 docs/，README.txt 在根目录")
print(f"   把这个文件夹压缩发给同学即可")
print()
print("🎵 音乐包（可选，997MB）：")
print("   把整个 songs/ 目录压缩为「小糖糖-音乐包.zip」发给同学")
print("   同学解压后覆盖主包的 songs/ 即可唱歌")
