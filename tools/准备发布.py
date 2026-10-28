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
import sqlite3
import sys
import time
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
OUT = BASE.parent / "小糖糖-发布"

IGNORE_ALWAYS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".pytest_cache", ".git", ".hg", ".svn",
    "*.bak*", "*.log", "*.db", "*.db-shm", "*.db-wal", ".DS_Store",
)

# 桌面端源码随发布包走（2026-10-28）。
# ⚠ 曾经只拷了根目录的桌面端启动脚本，快照里根本没有 `desktop/` —— 脚本的 `cd desktop`
#   必然失败（双击只会闪一下），用户以为「桌面端坏了」。
#   这里放**源码**：够 clone 的人 `pnpm install && pnpm build` 自己构建。
#   构建产物、依赖与绿色版 exe 都不进快照：
#     · dist/ dist-electron/ 由 pnpm build 生成，跟着工具链版本变，不该固化
#     · node_modules/ 110 MB，由 pnpm-lock.yaml 锁版本
#     · release/ 是 180 MB 的绿色版，走 Release 附件（GitHub 单文件 2G 上限内）
DESKTOP_IGNORE = shutil.ignore_patterns(
    "node_modules", "dist", "dist-electron", "release", ".vite", "*.log",
)

# ═══════════════════════════════════════
# 主仓白名单（发布清单 v1 冻结）
# ═══════════════════════════════════════

COPY_DIRS = ["agent", "onebot", "tests", "scenarios", "stickers", "stickers_cg", "knowledge",
             # 2026-10-26：Qt 运维控制台从根目录移入 运维/（不再冒充启动入口）——整目录随发布。
             "运维",
             # 2026-10-28：桌面端源码（React + Electron）是 main 启动入口，
             # 详见 DESKTOP_IGNORE 的注释（构建产物与绿色版不进快照）。
             "desktop"]
# knowledge/ 整目录拷时额外剔除（2026-09-05 主人裁决 v2）：
#
# ⚠ 2026-10-23 主人定案**取代**上面那条「只发软色情 2 篇」：
#   **发蒸馏稿，不发原文。** 判据改用**前缀**，不再按文件名猜：
#     · 色色参考/<数字>-xxx/ = 蒸馏槽位 ⇒ 发
#     · 色色参考/_原文/      = 蒸馏溯源归档 ⇒ 不发（含 蒸馏说明.md）
#   前缀语义与 agent/intimate_reference.py 的 ARCHIVE_PREFIX_BLOCK 是同一条（教训 #34：
#   代码里已写明的语义不要在发布侧重新猜）。取证见 记录/T69-发布包剔除原文.md：
#   旧名单按三个文件名写死，而 T47/T2.14 改结构 + 蒸馏改名后那三个名字只存在于 _原文/ 里，
#   槽位侧新名字一个都不在名单里 ⇒ 旧规则实测会带出 11 篇，「官能技法不进公开」早已名存实亡。
# 色色参考只发软色情 2 篇（丛雨对话范例/恋爱感氛围），BDSM/官能技法与直露写作指南不进公开；
# learned_ 自动学习残留不进。
#
# ⚠ .knowledge_index.sqlite3 不拷**开发机那份**，改为在快照上现场重建（见 _build_knowledge_index）。
#
# 2026-09-19 这一天在这件事上反复了两次，两次都错，教训记全：
#
#   ① 原来随的是开发机那份索引，而它存着 knowledge/*.md 的**切块正文副本**。
#      源文件里的本机路径清掉之后，索引里还留着清理前的旧文本——实测 60 处，
#      随 v1.0/v1.1 发了出去（而当时的扫描器按后缀白名单跳过 .sqlite3，完全看不见）。
#   ② 处置时一刀切成「派生数据不进发布物」。**这一刀砍错了**：它砍掉的是用户要等的时间，
#      而主人当初指定随包正是为了省这个。
#
# 量过才知道错在哪：索引重建本身只要 0.1 秒，但 **BGE 向量重算要 6.2 秒**
# （156 chunk + 114 segment；另有 15.8 秒模型加载，那部分省不掉）。
# 取舍其实**不存在**——泄漏来自「索引是旧的」，不是来自「发布了索引」。
# 所以在清理干净的快照上重跑一遍建索引，两头都要得到。
#
# 这里仍然忽略：开发机那份是旧的，拷过来就是 ①。
KNOWLEDGE_IGNORE = shutil.ignore_patterns(
    # 下划线前缀 = 蒸馏溯源归档（色色参考/_原文/ 整目录，含蒸馏说明.md）。
    # 闸门：tests/test_release_knowledge_scope.py（含阳性对照 + 不误伤的反向闸门）
    "_*",
    "技法摘录_中文百合BDSM.md", "技法摘录_日系官能.md", "色色_写作指南.md",
    "learned_*", "_order.json",
    # 末尾带 `*`：一并挡住 WAL 模式的 -wal / -shm 边车——现场重建后也会再清一次
    # （见 _build_knowledge_index 末尾），双保险。
    ".knowledge_index.sqlite3*")


def _build_knowledge_index(target_knowledge: Path) -> None:
    """在快照的 knowledge/ 上**现场重建**索引，让用户首启省掉 6.2 秒向量计算。

    为什么是「重建」而不是「拷开发机那份」：索引里存着切块正文的副本，
    拷旧的就是把发布时点的文本连同它的历史一起发出去（2026-09-19 事故）。
    重建则保证索引内容与快照里那些**清理过的**源文件同源。

    BGE 不可用时降级为「只建文本索引、不算向量」——发布不能被模型缺失阻断，
    用户首启会自己补算（只是没有省下那 6.2 秒）。
    """
    sys.path.insert(0, str(BASE))
    try:
        from agent.knowledge import KnowledgeBase
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ knowledge/ 索引未重建（导入失败）：{e}")
        return

    engine = None
    try:
        from agent.embeddings import EmbeddingEngine
        _eng = EmbeddingEngine()
        _eng.load()
        if getattr(_eng, "_ready", False):
            engine = _eng
        else:
            print("  ⚠ 没找到 BGE 模型——索引只建文本，用户首启会自己补算向量")
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ BGE 不可用（{e}）——索引只建文本")

    t0 = time.perf_counter()
    try:
        kb = KnowledgeBase(str(target_knowledge))     # 构造即建库（含 FTS 全文索引）
        if engine is not None:
            kb.warm_embeddings(engine)                # 算向量并写回索引
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ knowledge/ 索引重建失败：{e}")
        return
    dt = time.perf_counter() - t0

    # 落回单文件：WAL 模式会留下 -wal/-shm，而快照/仓库/包里只该有那一个 .sqlite3。
    # 用 checkpoint(TRUNCATE) 把 WAL 内容并回主库再切回 DELETE 模式，比事后删文件稳。
    idx = target_knowledge / ".knowledge_index.sqlite3"
    try:
        con = sqlite3.connect(str(idx))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode = DELETE")
        con.close()
    except Exception as e:                                   # noqa: BLE001
        print(f"  ⚠ 索引 checkpoint 失败（不影响内容）：{e}")
    for side in (idx.with_name(idx.name + "-wal"), idx.with_name(idx.name + "-shm")):
        if side.exists():
            side.unlink()

    n_doc = n_chunk = 0
    try:
        con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
        n_doc = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        n_chunk = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        con.close()
    except Exception:                                        # noqa: BLE001
        pass
    print(f"  ✅ knowledge/ 索引现场重建（{dt:.1f} 秒，{n_doc} 篇 {n_chunk} 块，"
          f"{'含' if engine else '不含'}向量，{idx.stat().st_size // 1024} KB）")

COPY_FILES = [
    # 入口与核心脚本
    # 2026-10-15 T55：三个诊断脚本已从项目根移入 tools/ —— 这里去掉根副本，
    # 改由 TOOLS_KEEP_NAMES 放行（否则随包会缺这三个文件）。
    "main.py",
    "安装糖糖.bat", "start.py",
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
    # ⚠ 这里曾经还发着 `配置NapCat.py`——那是给 NapCat 程序本体写配置的脚本，
    #   而项目早已改用 SnowLuma：它指向的 `NapCat.Shell.Windows.OneKey/...` 目录
    #   在仓库里根本不存在，用户跑它只会拿到「❌ 未找到 NapCat 配置文件」。
    #   2026-09-19 主人问「不是没用 napcat 了吗」时查出来的，已移入 tools/_归档/。
    #   活的对等物是下面的 配置SnowLuma.py。
    "配置SnowLuma.py", "检查SnowLuma更新.py", "打包exe.py",
    # 连接自检：控制台的「连接自检」按钮按路径调它（_run_connection_check）。
    # 它把「连不上」翻译成「第几步还没做」。漏了它按钮就只会报一句"没找到"。
    # 2026-09-20 加。闸门同上（test_referenced_tools_are_whitelisted）。
    "检查连接.py",
    "标注贴图情绪.py", "模拟消息测试.py", "打包发布附件.py", "准备发布.py",
    "打包发布版本.py", "发布前检查.py", "渲染架构图.py",
    # 音频后处理两件套（对 songs/audio/ 的成品做降噪与切静音）。2026-10-15：
    # 原先调用它们的「一键全流程」已随翻唱制作侧一起下线，控制台里不再有调用点——
    # 保留是因为它们本身仍能对随包成品单独跑（python tools/denoise.py --song 歌名）。
    "denoise.py", "trim_silence.py",
    # 三个诊断脚本（2026-10-15 T55 从项目根移入 tools/，见 COPY_FILES 的注释）。
    # 控制台「诊断」按钮按 tools/诊断糖糖.py 调它（运维/糖糖控制台_qt.py::_DiagWorker），
    # 漏了这行按钮就只会报「没找到」—— 同 检查连接.py 那条的理由。
    "诊断糖糖.py", "全面诊断.py", "诊断对话质量.py",
}
def _sticker_note(title: str, dirname: str, role_line: str) -> str:
    """四个贴图目录共用的摆放说明。

    ⚠ 2026-09-20 重写。旧版写的是「把表情图放进本目录即可，命名用情绪词开头
    （如 开心_撒娇_任意名.jpg）」——**顺序讲反了**：命名是程序干的活，不是用户干的。
    用户只要丢图 + 点一次「重打标签」，程序会用识图模型判断情绪、自己改名、
    并把标签写进 metadata.json。让用户手写文件名不仅是多余劳动，还更差：
    手写只有一个词、没有描述文本，而糖糖取图走的是**语义检索**，标签文本越丰富越好。

    四个目录正文一致（机制完全相同），只有「什么时候用哪个目录」那段不同——
    抽成函数是为了将来改一处不用改四处。
    """
    return (
        f"🖼 {title}（已随包自带，开箱能用）\n"
        "\n"
        "【加图两步 —— 名字由程序起，不用你起】\n"
        "\n"
        "  1. 把图片丢进本目录。文件名随便：中文、乱码、微信导出的长名字都行。\n"
        "  2. 点「重打标签」让它自己认：\n"
        "        控制台 仪表盘 → 点「表情包」卡片 → 面板右上角「重打标签」\n"
        f"        命令行等价写法：python tools/标注贴图情绪.py {dirname}\n"
        "\n"
        "  程序会做三件事：识图模型看图 → 判断情绪 → 把文件重命名成\n"
        "        「情绪_情绪_四位编号.ext」   例：开心_撒娇_559e.png\n"
        "  标签和描述同时写进本目录的 metadata.json。\n"
        "\n"
        "  别手动改名：名字不是给人看的，是给检索用的，手写只会少一层信息。\n"
        "\n"
        "【它怎么被取用】\n"
        "\n"
        "  糖糖发表情时传的是一句自然语言（比如「开心地撒娇」），程序按顺序找图：\n"
        "    1. BGE 语义检索 —— 拿 metadata.json 里每张图的标签文本算相似度（主要路径）\n"
        "    2. 情绪词精确匹配 —— 标签里有这个词就直接取\n"
        "    3. 文件名关键词 —— 最后兜底\n"
        "\n"
        "  所以标注质量就是找图质量：模型给得出两三个情绪词加一句描述，\n"
        "  手写文件名只有一个词。\n"
        "\n"
        "【什么时候用哪个目录】\n"
        "\n"
        f"  {role_line}\n"
        "  糖糖发表情时不会跨目录拿图。\n"
        "\n"
        "【容易踩的几处】\n"
        "\n"
        "  · 标注需要识图后端：控制台「设置 → 识图方式」得配好\n"
        "    （本地 MiniCPM-V，或者云端千问 VL）。没配好的话按钮点得动，\n"
        "    但每一张都会报「识别失败」——图片原样留着，配好再跑一次就行。\n"
        "  · 已经标过的图会自动跳过，所以重复点「重打标签」很便宜。\n"
        "  · metadata.json 和 semantic_vectors.npz 都是程序生成的文件，别手动编辑。\n"
        "    删掉 metadata.json 可以强制全部重新识别（需要识图后端）。\n"
    )


# 骨架目录：主仓预装空目录 + 摆放说明（用户 clone 即见全貌，附件解压有指引；2026-09-05 主人提议）
SCAFFOLD_NOTES = {
    # SnowLuma 是唯一一个「必须用户手动放、且不能随包发」的组件——
    # 原先连落点目录都没给，用户得自己新建 SnowLuma/ 才知道往哪解压（2026-09-19 主人指出）。
    "SnowLuma/摆放说明.txt": "🐱 SnowLuma —— QQ 协议端（需要你手动放一次）\n\n"
    "糖糖靠它连上 QQ，但它不属于本项目：SnowLuma 的许可协议不允许随本包分发，\n"
    "也不允许由安装程序代下。所以这一步只能你自己来，一次即可。\n\n"
    "怎么放：\n"
    "  1. 打开 https://github.com/SnowLuma/SnowLuma/releases\n"
    "  2. 下载 Windows x64 版（文件名形如 SnowLuma-v1.14.9-win-x64.zip）\n"
    "  3. 解压到本目录下——解压完应该是这样的层级：\n"
    "         SnowLuma/                        ← 就是本目录\n"
    "           └─ SnowLuma-v1.14.9-win-x64/   ← 解压出来的文件夹\n"
    "                └─ SnowLuma.exe\n\n"
    "装好后：控制台「启动 SnowLuma」按钮就能用了。安装程序也会在开头检查并提示。\n\n"
    "也可以换用任意其他 OneBot 11 反向 WebSocket 实现，糖糖不绑定 SnowLuma。\n\n"
    "⚠️ 注意别和项目根目录的 onebot/ 搞混：那个是糖糖**自己**的 OneBot 适配层代码，\n"
    "   是程序的一部分；本目录放的是**第三方**的 SnowLuma 程序本体。\n",
    "gpt-sovits/摆放说明.txt": "📦 语音推理集\n\n"
    "本目录**自带**：引擎代码 + 米雪儿声线（models/michele）+ 各情绪参考音（speakers）。\n"
    "还缺 GPT-SoVITS **公开底模约 3.7 GB** —— 按 docs/用户手册/语音模型自行配置.md 自行下载放置。\n\n"
    "不配语音也能用：糖糖照常聊天，只是不发语音。\n"
    "切到「米雪儿」角色时程序会自动把权重换成 models/michele 下那两个（详见上面那份文档）。\n",
    "songs/摆放说明.txt": "🎵 预录歌曲（已随安装包自带，装好就能点歌）\n\n"
    "本目录的音频是糖糖唱好的成品——点歌时直接播放，不是实时生成，也不需要额外模型。\n\n"
    "想自己加歌：把音频命名为 歌名.wav 放进 audio/（糖糖声线）；原声放 covers/separated/歌名_FINAL.wav。\n"
    "（2026-10-15 起不再读歌词：本目录不用放 歌名.txt。）\n",
    # 四个贴图目录共用一份说明（正文抽在 _sticker_note 里，只有「什么时候用哪个」不同）
    "stickers/摆放说明.txt": _sticker_note(
        "糖糖本体（默认角色）的表情包", "stickers",
        "本目录是糖糖本体用的。米雪儿、丛雨各有自己的目录，用 /角色 切换。"),
    "stickers_cg/摆放说明.txt": _sticker_note(
        "亲密场景专用表情包", "stickers_cg",
        "本目录只在色色（亲密）模式下取用，与三个角色图库互不影响。"),
    "stickers_michele/摆放说明.txt": _sticker_note(
        "米雪儿角色专属表情包", "stickers_michele",
        "本目录只在 /角色 米雪儿 时取用。糖糖本体用 stickers/。"),
    "stickers_murasame/摆放说明.txt": _sticker_note(
        "丛雨角色专属表情包", "stickers_murasame",
        "本目录只在 /角色 丛雨 时取用。糖糖本体用 stickers/。"),
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
# 2026-10-28：图谱源已从 artifacts/图谱 移到 docs/架构图谱（原路径不存在，
# 导致快照里没有 糖糖架构图谱.html，README 的链接在发布形态下必然断）。
GRAPH_SRC = BASE / "docs" / "架构图谱"

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

# 出厂 config.yaml 的文件头注释（2026-09-20 加）。
# yaml.dump 不写注释，所以这段是生成完之后拼上去的。
# 为什么需要它：密钥字段现在是 `${VAR}` 引用（见 scrub_cfg 的注释——那串中文提示
# 曾经直接写进字段值，把整个 .env 机制架空）。值变成机器形态之后，人得在别处读到说明。
_CONFIG_HEADER = """\
# 🍬 小糖糖 配置文件（首次启动时由本文件复制为 config.yaml，之后请改 config.yaml）
#
# ⚠ 带 ${...} 的字段是**环境变量引用**，不是要你填在这里的字符串：
#      api_key: ${DEEPSEEK_KEY}   →   运行期去 .env 里找 DEEPSEEK_KEY 这一行
#   填的入口是控制台「🔑 密钥管理」页——它会写进 .env，并把这里的引用保持成 ${...}。
#   直接在这里手写一串真 Key 也能用，但那样 Key 就落进了 config.yaml（更容易被误传出去），
#   所以除非你清楚在做什么，否则请走控制台。
#
# 除此之外的字段（QQ 号、模型名、开关……）都可以直接在这里改，改完重启糖糖即可。
#
"""


def scrub_cfg(cfg: dict) -> dict:
    """真 config → 出厂模板。**密钥字段一律写成 `${VAR}` 引用，不写成中文提示串。**

    ⚠ 2026-09-20 主人实机反馈「控制台输入的 API Key 不会到 config 生效」——根因就在
      原来的三行：它们把开发机 config.yaml 里**正确的** `${DEEPSEEK_KEY}` /
       `${SNOWLUMA_TOKEN}` 主动改写成了「你的DeepSeek或Claude的API密钥」这类**字面量**。

       为什么这一定坏：
         · 运行期 `main._resolve_env_vars` 只认**整个值恰好是 `${VAR}`** 的字符串，
           字面量一个字符都不会被替换 → config.yaml 里的字面量赢，`.env` 从头到尾没人读。
         · 而控制台「密钥管理」写的是 `.env`（它只写 .env，不写 config.yaml 的密钥字段）。
           于是用户填的那一份，落在了一个没有任何人读的文件里。
         · 症状：填完保存、控制台回显也是对的，config.yaml 里却还是那串中文，
           糖糖拿它当 API Key 发出去。**唯一的例外是 provider=deepseek**——
           agent/handler.py 有一行 `if _provider == "deepseek"` 的特判兜住了它，
           所以「默认配置能用、换个 provider 就 401」这个更奇怪的现象先被人撞上。

       所以模板必须和开发机同形：`${VAR}`。脱敏早就实现了——`${VAR}` 本身就不含明文，
       它才是这个项目里唯一被设计过的凭据形态（`llm.seductive_api_key` 一直就是它）。
       人读得懂的提示挪进文件头的注释块（见 _CONFIG_HEADER）。

       QQ 号那两行（bot.qq_id / owner_qq）故意保留中文占位——它们**有控制台设置页兜底**，
       保存时会被真实值覆盖；密钥字段没有这条兜底路径，这正是两者的分界。
    """
    import copy
    c = copy.deepcopy(cfg)
    if "bot" in c:
        c["bot"]["qq_id"] = "你的机器人QQ号"
        c["bot"]["owner_qq"] = "你的QQ号"
    if "llm" in c:
        # 密钥的**变量名跟着 provider 走**——PROVIDER_KEY_ENV 是单一事实源，
        # 控制台「密钥管理」写 .env、运行期回填，用的都是它。
        #
        # 2026-09-22 修：原来这里写死 `${DEEPSEEK_KEY}`。开发机一旦把 provider
        # 切成 custom，出厂模板就会变成「provider: custom + api_key: ${DEEPSEEK_KEY}」
        # ——引用的变量和 provider 对不上。运行期 handler.py 有一行「按 provider
        # 覆盖密钥」的兜底把它捞了回来，所以症状不是"不能用"，而是模板自相矛盾、
        # 并且和本文件 794 行「换 LLM 供应商时按 provider 选对应的那一个」打架。
        # 判据那边（tests/test_config_example_contract.py 的 secret_paths）本来就是
        # provider 感知的——它一直在报这个违规，只是开发机恰好是 deepseek 时看不见。
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        from agent.secret_refs import PROVIDER_KEY_ENV
        _provider = str(c["llm"].get("provider", "deepseek") or "deepseek")
        _key_env = PROVIDER_KEY_ENV.get(_provider, PROVIDER_KEY_ENV["deepseek"])
        c["llm"]["api_key"] = "${" + _key_env + "}"
        c["llm"]["seductive_api_key"] = "${SEDUCTIVE_KEY}"
        if isinstance(c["llm"].get("vision"), dict):
            c["llm"]["vision"]["api_key"] = "${QWEN_KEY}"
            # 模型/地址不写死供应商默认——用户按接入的 API 自填（2026-09-05）
            c["llm"]["vision"].pop("model", None)
            c["llm"]["vision"].pop("base_url", None)
    # OneBot 协议端的凭据段。⚠ 键名历史上叫 `napcat`（config.yaml 里就是它），
    # 目录/模块已改名为 onebot，但**配置键刻意没跟着改**（改键会让既有安装的配置失效）。
    # 2026-09-19 安全审计发现：改目录名时这条分支被顺手改成了 "onebot"，于是永不执行、
    # 静默什么也不做——真 config 一旦把字面 token 写进去，就会原样发到公开仓库。
    # **安全控制绑死在字面量上，名字一变就无声失效。** 两个键都处理，别再绑死。
    for _key in ("napcat", "onebot"):
        if isinstance(c.get(_key), dict):
            c[_key]["access_token"] = "${SNOWLUMA_TOKEN}"
    if "voice" in c:
        # 补上听/说分离开关（真 config 未必有——2026-09-18 新增，让用户看得见这个旋钮）
        c["voice"].setdefault("asr_enabled", True)
    # 记忆自动同步总开关（2026-09-20 新增）。代码里的默认值是 True，所以**不写也能跑**；
    # 但用户看不见的开关等于不存在——他把控制台那个复选框关掉之前，
    # 根本不会知道有这么个东西。（Code Review C-6）
    # 2026-09-22：从 setdefault 改成**显式写死 auto=True**。
    # 模板是**出厂状态**，不是开发机的当前状态——和上面密钥那条同一个原则。
    # setdefault 只在键**缺失**时生效；开发机把总开关关掉（单机使用是合理的）
    # 之后它就形同虚设，出厂模板跟着变成 false，而代码里 `.get("auto", True)`
    # 和控制台的默认都是**开**——两套默认对不上，用户拿到手是关的却不知道有这开关。
    _ms = dict(c.get("memory_sync") or {})
    _ms["auto"] = True
    c["memory_sync"] = _ms
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
                      # 公开标识符（不是 QQ / 手机号）：
                      "1917036117",   # PNAS 论文 DOI 编号（knowledge/恋爱军师 的学术引用）
                      "1303431039",   # bilibili 公开视频列表的 UP 主 UID（文档引用链接）
                      # 测试夹具的假号（999900xxx 是本项目约定的「假 bot 号」）
                      "999900001", "999900002",
                      }
# 注意：token 模式拆串拼接——避免扫描器源码行被自己的 KEY_RE 命中（2026-09-05）
_TOK_VOLC = "VOLC" + "_TOKEN"
#
# ⚠ 2026-09-20：原先是写死的 `"NAPCAT_TOKEN" + "=8"`——它只认那**一个具体赋值**，
#   而 `.env.example` 的键名当天改成了 `SNOWLUMA_TOKEN`。若不动它，这条检查就又变成
#   「跑了但永远不会命中」（反模式 #30）。改成按**形状**认：`XXX_TOKEN=一串像 token 的值`。
#   值必须 ≥12 位 ASCII 字母数字/`_~.-`，所以 `SNOWLUMA_TOKEN=`（空）和
#   `NAPCAT_TOKEN=请填写你的access_token`（中文开头）都不会误报。
#   阳性对照：tests/test_release_sanitizer.py::test_sanitizer_catches_planted_token_assignment
# `.env` 里一行明文赋值的形状。2026-09-20 二次收紧，两个方向都试过：
#
#   · 只写 `NAME_TOKEN=值`（不带引号）→ `.env` 里写成 `NAME_TOKEN="值"` 时**漏报**
#     （审查提的 S10：「判据绑死形状」的又一例）。
#   · 松成「`NAME_TOKEN` + 可有可无的引号」但**不锚行**→ 立刻**误报 4 处**：
#     `_ENV_WITH_TOKEN = "..."`、`_write_env(..., X_TOKEN="...")`、
#     `KEY_TOKEN = "token_consistency"` 全被当成 env 赋值。噪音会让人把检查关掉。
#
# 所以锚死三件事，恰好是 `.env` 与 Python 源码的分界：
#   ① 行首（允许 `#`/`;` 注释前缀和 `export `）—— 关键字参数在括号里，不是行首；
#   ② 键名后**紧跟** `=`（不许空格）—— Python 里习惯写 `KEY = "值"`，`.env` 不写空格；
#   ③ 整行到值就结束 —— 值后面还有 `+ _X` 或 `)` 的都不是 env 赋值。
# 空值、`${VAR}` 占位符、中文提示语都不会命中（值字符类里没有 $、{、引号、汉字）。
# ⚠ **不要**在这里加 `(?m)` 之类的内联标志。这个片段会被拼进 `KEY_RE`，而内联标志
#   出现在表达式中间时，Python ≥3.12 直接抛 `PatternError: global flags not at the
#   start of the expression`——`准备发布.py` **导入即崩**；3.10 只是发个
#   DeprecationWarning，本机测不出来（2026-09-20 独立复核用 3.14 实测抓到的）。
#   而且这里**根本不需要**：扫描器是 `KEY_RE.search(line)` **逐行**喂进来的，
#   每行的整串就是主题串，`^`/`$` 天然按行生效。
_TOK_ASSIGN = (r"^[ \t]*(?:#|;|export[ \t]+)?[ \t]*"
               r"[A-Z][A-Z0-9_]*_TOKEN=['\"]?[A-Za-z0-9_~.\-]{12,}['\"]?[ \t]*$")
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}"
                    r"|access[_-]?token\s*[:=]\s*['\"]?[A-Za-z0-9]{10,}"
                    r"|" + _TOK_VOLC + r"|" + _TOK_ASSIGN)
# ⚠ 2026-09-19 修复：原写法 r"[Dd]:\\\\[^\\s\"'，。、]+" 有两处笔误，导致**从未匹配过任何东西**——
#   ① `\\\\` 在正则里表示「两个字面反斜杠」，而真实路径只有一个 → 永不匹配
#   ② `[^\\s...]` 的字符类把反斜杠本身也排除在外 → 就算匹配上也会在第一个分界符处截断
#   后果：这条检查从上线起一直是空转，18 处本地路径随 v1.0/v1.1 发布了出去。
#   闸门：tests/test_release_sanitizer.py
#
# ⚠ 2026-09-19 二次加固（独立安全审计发现，三处洞一起补）：
#   · 原来只认 `[Dd]:` → **C:/E:/… 完全不扫**。实测 `onebot/ws_client.py:2175` 里
#     躺着「某盘符:/Users/<操作者真名>/...」，扫描器报「零命中」，差点随包发出去。
#   · 原来盘符前的否定环视排除 `/` 和 `\` → file:// 开头的盘符 URL 这类泄漏
#     被**系统性豁免**。可它照样是泄漏（浏览器里点得开，且往往带真实用户名）。
#   · 原来只认单个反斜杠 → 双反斜杠写法（源码注释 / YAML / JSON 里极常见）不匹配。
# 现在：任意盘符 + 1~2 个分隔符 + 只排除「前面紧挨字母数字」（那是标识符不是盘符，
# 也正是 `returned:\n` 那种转义序列误报的成因）。
# ⚠ 字符类里**允许分隔符**：早先版本把 `\` `/` 也排除在外，于是匹配到第一个
#   分隔符就停——把「占位写法 + AppData/…」截成了只剩「盘符 + Users」，
#   跟「盘符 + Users + 具体用户名 + …」（真泄漏）长得一模一样，放行表根本分不开。
#   现在路径整段吃进来，靠内容判别。空格仍排除（避免把整行散文吞掉）。
WINPATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}[^\s:*?\"<>|]{2,}")

# ① 标准系统路径：任何 Windows 机器上都长这样，不构成"个人路径泄漏"。
#    例：浏览器装在 Program Files 下的机器全覆盖的标准位置，写在代码里是为了去找到它。
#    ⚠ 只放行**确实与个人无关**的系统目录——带具体用户名的用户目录不在其列。
WINPATH_SYSTEM = re.compile(
    r"^[A-Za-z]:[\\/]{1,2}("
    r"Windows|Program|Python3\d*"          # C:\Windows… C:\Program Files… C:\Python310
    r"|Users[\\/](%USERNAME%|Public|Default)"   # 占位/公共目录——具体用户名不算
    r")(?![A-Za-z0-9])",
    re.I)

# ② 明确放行的「格式示例」——在任何机器上都不存在的说明性写法。
#    ⚠ 比对的是**正则匹配到的那一段**，不是整行——`file:///D:/stickers/a.jpg`
#      匹配出来是 `D:/stickers`（到第二个分隔符就停了），所以这里写片段形式。
WINPATH_ALLOWED_FRAGMENTS = {
    "D:/stickers",   # tests/test_task_actions.py 的 CQ 码夹具（假路径，只验格式）
    "D:/voice",      # 同上
    "D:/...",        # CLAUDE.md 说明表情包原始路径的写法本身
    "Z:/不存在",      # tests/test_vision.py 的夹具：文件名就叫「不存在的路径」
    # 第三方表情包图片**自带的元数据路径**——来源是 Adobe 导出信息与素材原始位置，
    # 跟本项目的目录结构无关，在任何机器上都不指向本项目的东西。
    # 2026-09-19 安全审计时被二进制兜底扫出来，人工核对后放行。
    # （要彻底清掉得重编码图片，会损失画质；判断为不值。）
    # ⚠ 写成拼接形式：字面写出来的话，**扫描器会抓到自己的放行表**（实测踩过）。
    "D:" + "\\project\\PVZ",
    "E:" + "\\文档\\Adobe",
}

# 整体豁免的文件——它们的存在意义**就是**装载泄漏样本，不是漏网。
# 这是全仓唯一的白名单，且被 tests/test_release_sanitizer.py 钉住：清单一旦变化就红灯。
# 想加新条目？先问自己：这个文件真的非含敏感样本不可吗？还是只是懒得改文案。
SCAN_EXEMPT_FILES = {
    "tests/test_release_sanitizer.py",   # 闸门自检：LEAK 样本 + 正则判定用例表
}

# 文本扫描的后缀白名单。**曾经只有 7 个**，于是 .j2/.html/.svg/.example/.gitignore
# 这些纯文本文件全被跳过——后缀白名单等于宣布"不在名单里的类型不可能有敏感信息"，
# 而那个假设 2026-09-19 被证伪：knowledge/.knowledge_index.sqlite3（二进制）里
# 躺着 19 处本机路径，因为它既不在名单里、又是二进制。
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".txt", ".json", ".bat", ".toml",
                 ".cfg", ".ini", ".j2", ".html", ".htm", ".svg", ".example",
                 ".gitignore", ".stignore", ".csv", ".xml", ".sh", ".ps1"}
# 无后缀也要当文本看（.gitignore 这类点开头文件、Makefile 等）
TEXT_NAMES = {".gitignore", ".stignore", ".env", "LICENSE", "Makefile"}

# 二进制兜底：只搜"绝不该出现在发布物里"的几类字面量。
# 不搜 QQ 号——二进制里数字串太多，搜了等于天天误报，闸门会被当噪音忽略掉。
BYTE_PATTERNS = [
    # ⚠ 二进制用「至少两个路径分量」而不是文本那条单分量规则：
    #   文本是人写的，误报少，可以严；二进制噪声地板高得多——实测 1300 张贴图里
    #   单分量规则命中 59 个文件，几乎全是「两三个字母 + 反斜杠 + 几个随机字符」的巧合。
    #   加一个「盘符后还得再有分隔符」的要求：噪声 59 → 5，而真泄漏（旧知识库索引
    #   里那 19 处）22 处照抓不误。
    (re.compile(rb"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}[^\x00-\x1f:*?\"<>|]{2,}"
                rb"[\\/][^\x00-\x1f:*?\"<>|]{1,}"), "本机盘符路径"),
    (re.compile(rb"sk-[A-Za-z0-9]{16,}"), "疑似 API 密钥"),
]


def _path_allowed(frag: str) -> bool:
    """是「系统标准路径」或「格式示例」而不是个人路径吗（见上面两张表）"""
    return (bool(WINPATH_SYSTEM.match(frag))
            or any(frag.startswith(a) for a in WINPATH_ALLOWED_FRAGMENTS))


def _read_text_any(p: Path) -> str:
    """按 UTF-8 读；带 BOM 的 UTF-16 单独解一次。

    2026-09-19 安全审计发现：原实现只按 utf-8 errors="ignore" 读，
    **UTF-16 编码的路径/密钥整类丢失**——而 Windows 侧 .lnk / PE 资源 /
    部分导出文件就是 UTF-16。GBK 中文也会被 ignore 吃掉，只剩半截盘符。
    """
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


def _scan_text(p: Path, rel: str) -> list[str]:
    hits = []
    try:
        text = _read_text_any(p)
    except OSError:
        return hits
    for i, line in enumerate(text.splitlines(), 1):
        path_hit = any(not _path_allowed(m.group())
                       for m in WINPATH_RE.finditer(line))
        if KEY_RE.search(line) or path_hit:
            hits.append(f"{rel}:{i}: {line.strip()[:100]}")
            continue
        for m in QQ_RE.findall(line):
            if m not in QQ_KNOWN_CONSTANTS:
                hits.append(f"{rel}:{i}: {line.strip()[:100]}")
                break
    return hits


def _scan_bytes(p: Path, rel: str) -> list[str]:
    """二进制/未知类型的兜底——按原始字节搜。

    ⚠ 判别器不能只看正则：图片/压缩流里 `D:` 后跟 `\\` 纯属随机巧合。
    实测 1300 个贴图里松匹配命中 24 处，**没有一处是真的**。
    加一条判据——**匹配到的字节必须整体是合法 UTF-8 文本**——立刻分开：
    噪声 24 → 0，而故意构造的真泄漏样本（旧 knowledge 索引，19 处 UTF-8 中文路径）19 → 19。
    真路径是文本，随机字节不是。
    """
    try:
        data = p.read_bytes()
    except OSError:
        return []
    hits = []
    for pat, why in BYTE_PATTERNS:
        for m in pat.finditer(data):
            try:
                frag = m.group().decode("utf-8")
            except UnicodeDecodeError:
                continue        # 不是文本 → 压缩数据里的巧合，不是路径
            if _path_allowed(frag):
                continue
            hits.append(f"{rel}: 二进制内容命中「{why}」：{m.group()[:60]!r}")
            break               # 同一文件同一模式报一次就够
    return hits


# 扫描要跳过的目录（不是文件类型——那种"按类型跳过"正是这次事故的成因）。
# .git/ 是版本库自身，不是发布物：打包脚本的 ZIP_EXCLUDE_DIRS 也把它排除在外。
# ⚠ 但它里面躺着**历史提交的旧版本文件**，那些旧版本含清理前的本地路径。
#   本扫描器管的是"这次要发出去的字节"，管不了历史——历史要不要洗是另一个决定。
SCAN_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "temp_files"}

# 整个 vendored 第三方目录的豁免（2026-10-28 加，理由不是"图省事"）：
#
#   `gpt-sovits/` 是 GPT-SoVITS 的**第三方副本**（上游 RVC-Boss/GPT-SoVITS，
#   本地是 git clone），它自带 42 处「敏感形状」——全是上游作者自己机器上的痕迹：
#     · 盘符开头的示例占位路径（上游 README 与 webui 里的输入框默认值，
#       不是我们的盘符结构，也不指向这台机器上的任何东西）
#     · 上游 changelog 里的贡献者数字 ID
#     · 上游调试注释里的重复数字与 unix 时间戳
#   它们既不是本项目用户的隐私，也不可能"改干净"（一换版本就回来）。
#   2026-09-18 的原始决定就写过这条（见 打包发布版本.py 的 GPT_MODEL_PREFIXES 注释）：
#   「进主仓会让敏感扫描被上游自带内容永久污染」。
#
#   ⚠ 豁免的是**上游文件**，我们改过/写过的两个文件仍要扫（下面 EXCEPTIONS）——
#     否则将来谁往 tts_infer.yaml 里写本机路径，这条闸门就瞎了。
#   ⚠ 阳性对照：tests/test_release_sanitizer.py::test_vendored_engine_exemption_has_bite
SCAN_EXEMPT_PREFIXES = ("gpt-sovits/",)
SCAN_EXEMPT_PREFIX_EXCEPTIONS = {
    "gpt-sovits/GPT_SoVITS/configs/tts_infer.yaml",   # 我们加的 custom profile
    "gpt-sovits/start_api_patched.py",                # 我们写的启动包装
}
# `.env` 真值扫描的单文件上限：真值只会出现在文本里，超大文件（模型/音频）跳过
SCAN_ENV_MAX_BYTES = 8 * 1024 * 1024


def scan_env_values(root: Path, env_path: Path | None = None) -> list[str]:
    """拿 `.env` 里的**真实值**去扫快照——这是 `scan_sensitive` 抓不到的一类。

    为什么需要它：`.env` 里的密钥**没有固定形状**（`SCAN_EXEMPT_FILES` 之外的
    正则只能按形状猜），而"真值出现在发布物里"是可以精确判定的——把真值当字符串
    搜一遍就行。2026-09-20 一天里撞了两次：
      · 往 `tests/test_release_sanitizer.py`（**被整体豁免**扫不到）里塞了真 token 当样本；
      · `tests/test_connect_check.py` 拿真 token 当 `mask_token()` 的输入（输出打了码，
        但**输入**是真值）。
    两次都是 `scan_sensitive` 报"零命中"。所以这一道独立于它。

    短值（<8 位）不参与比对：`true`、`1` 这类会跟普通文本撞，制造噪音。
    """
    env_path = env_path or (BASE / ".env")
    if not env_path.exists():
        return []
    secrets = []
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            if len(val) >= 8:
                secrets.append((key.strip(), val))
    except OSError:
        return []
    if not secrets:
        return []

    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.stat().st_size > SCAN_ENV_MAX_BYTES:
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if SCAN_SKIP_DIRS & set(Path(rel).parts):
            continue
        try:
            text = p.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        for key, val in secrets:
            # 报文件名不报行号：行号要在原文里定位真值，等于把真值又抄一遍
            if val in text:
                hits.append(f"{rel}: 含有 .env 里 {key} 的真实值")
    return hits


def scan_sensitive(root: Path) -> list[str]:
    """扫描快照内敏感模式，返回命中行（空 = 干净）。

    文本按行报（好定位），其他一律按字节兜底——**不按文件类型开天窗**。
    """
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if SCAN_SKIP_DIRS & set(rel.parts):
            continue
        posix = rel.as_posix()
        if posix in SCAN_EXEMPT_FILES:
            continue
        if (posix.startswith(SCAN_EXEMPT_PREFIXES)
                and posix not in SCAN_EXEMPT_PREFIX_EXCEPTIONS):
            continue
        if p.suffix.lower() in TEXT_SUFFIXES or p.name in TEXT_NAMES:
            hits.extend(_scan_text(p, posix))
        else:
            hits.extend(_scan_bytes(p, posix))
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
            if d == "knowledge":
                ign = _merged_ignore(IGNORE_ALWAYS, KNOWLEDGE_IGNORE)
            elif d == "desktop":
                ign = _merged_ignore(IGNORE_ALWAYS, DESKTOP_IGNORE)
            else:
                ign = IGNORE_ALWAYS
            shutil.copytree(src, OUT / d, ignore=ign)
            print(f"  ✅ {d}/")

    # 1b. 知识库索引：在**清理过的**快照上现场重建（不是拷开发机那份——见函数注释）
    if (OUT / "knowledge").is_dir():
        _build_knowledge_index(OUT / "knowledge")

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

    # 4b. 语音私有资产（2026-10-28 主人拍板）：米雪儿模型与情绪参考音**没有公开下载源**，
    #     是我们自己微调的，必须随包；公开底模（3.7 GB）走「自行配置」，见
    #     docs/用户手册/语音模型自行配置.md。
    #
    #     · gpt-sovits/models/michele/ = 米雪儿的 GPT + SoVITS 权重（229 MB）
    #       agent/voice.py 的 MODEL_PROFILES["michele"] 指向它——**切到米雪儿角色时**
    #       switch_model() 调 GPT-SoVITS 的 /set_gpt_weights + /set_sovits_weights 换过去，
    #       所以这一份缺了，米雪儿就只能纯文字。
    #     · gpt-sovits/speakers/ = 各情绪的中文/日文参考音频（8 MB）——缺了 TTS 拿不到
    #       ref_audio_path，任何角色都合成不出来。
    #     ⚠ 刻意不含 GPT_SoVITS/pretrained_models（公开底模），那是第三方开源模型。
    _voice_src = BASE / "gpt-sovits"
    _VOICE_SKIP_PREFIX = (
        "GPT_SoVITS/pretrained_models/",     # 3.1 GB 公开底模——用户按文档自备
        "GPT_SoVITS/text/G2PWModel/",        # 608 MB 中文注音——同上
    )
    _VOICE_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
    # G2PWModel 的**源压缩包**（562 MB）——解压后的 G2PWModel/ 已被上面排除，
    # 这个 zip 不在那个前缀下，会白背一整份重复内容（打包发布版本.py 的
    # VOICE_EXCLUDE_NAMES 早就在防同一件事，2026-09-18）。
    _VOICE_SKIP_FILES = {"GPT_SoVITS/text/G2PWModel_1.1.zip"}
    voice_n = 0
    if _voice_src.is_dir():
        for vp in sorted(_voice_src.rglob("*")):
            if not vp.is_file():
                continue
            rel = vp.relative_to(_voice_src).as_posix()
            if rel.startswith(_VOICE_SKIP_PREFIX) or rel in _VOICE_SKIP_FILES:
                continue
            if _VOICE_SKIP_DIRS & set(vp.parts) or vp.suffix == ".pyc":
                continue
            target = OUT / "gpt-sovits" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vp, target)
            voice_n += 1
        print(f"  ✅ gpt-sovits/（引擎代码 + 米雪儿声线 + 情绪参考音，共 {voice_n} 个文件；"
              f"公开底模 3.7 GB 由用户按 docs/用户手册/语音模型自行配置.md 自备）")
    else:
        print("  ⚠ 没有 gpt-sovits/ 目录——语音功能不会随包")

    # 5. config.example.yaml（真 config 脱敏）
    import yaml
    cfg_src = BASE / "config.yaml"
    if cfg_src.exists():
        cfg = yaml.safe_load(cfg_src.read_text(encoding="utf-8")) or {}
        (OUT / "config.example.yaml").write_text(
            _CONFIG_HEADER + yaml.dump(scrub_cfg(cfg), allow_unicode=True,
                                       default_flow_style=False, sort_keys=False),
            encoding="utf-8")
        print("  ✅ config.example.yaml（已脱敏）")

    # 6. .env.example（全占位，绝无真实 token）
    # ⚠ 2026-09-20：键名必须与控制台「密钥管理」写的那份**逐字一致**——
    #   `运维/糖糖控制台_qt.py` 的 _provider_key_info 按 provider 决定写哪个变量。
    #   模板里少一个键，用户就少一条「我该填哪个变量」的线索；多一个名字
    #   （比如早先那个 NAPCAT_TOKEN），用户抄了就是白填。
    #   闸门：tests/test_config_example_contract.py 断言两边键名集合相等。
    (OUT / ".env.example").write_text(
        "# 环境变量配置（复制为 .env 并填写；也可以完全不管它——\n"
        "#  控制台「🔑 密钥管理」页会直接写这个文件）\n"
        "#\n"
        "# 这些变量名要和 config.yaml 里的 ${...} 引用对上：\n"
        "#    config.yaml                        .env 里的变量\n"
        "#    llm.api_key                        DEEPSEEK_KEY（换 provider 见下）\n"
        "#    llm.seductive_api_key              SEDUCTIVE_KEY\n"
        "#    llm.vision.api_key                 QWEN_KEY\n"
        "#    napcat.access_token                SNOWLUMA_TOKEN\n"
        "#\n"
        "# 换 LLM 供应商时按 provider 选对应的那一个（控制台选完会自动写）：\n"
        "#    deepseek→DEEPSEEK_KEY   openai→OPENAI_KEY   xai→XAI_KEY\n"
        "#    anthropic→ANTHROPIC_KEY custom→CUSTOM_LLM_KEY\n"
        "#\n"
        "# 值不要加引号，也不要在行尾写注释——本文件按 KEY=VALUE 逐行读。\n"
        "\n"
        "# 必须：糖糖的「大脑」（DeepSeek / Claude / GPT / Grok…）\n"
        "DEEPSEEK_KEY=\n"
        "# 可选：色色模式的专用模型（不填则复用上面的 key）\n"
        "SEDUCTIVE_KEY=\n"
        "# 可选：识图选「云端千问 VL」时才需要；选「本地 MiniCPM-V」不用填\n"
        "QWEN_KEY=\n"
        "# 可选：只有用了 SnowLuma / OneBot 的授权 Token 才要填——\n"
        "#   与 SnowLuma 网页面板里那一栏「授权 Token」完全一样才能连上；\n"
        "#   面板里没设 Token 的话，这里也留空。\n"
        "SNOWLUMA_TOKEN=\n"
        "# 换供应商才用得到（用哪个填哪个，其余留空即可）\n"
        "OPENAI_KEY=\n"
        "XAI_KEY=\n"
        "ANTHROPIC_KEY=\n"
        "CUSTOM_LLM_KEY=\n", encoding="utf-8")
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
        # ⚠ 排除「目录内容」而不是目录本身（`voice_cache/*` 而非 `voice_cache/`）：
        #   git 的规则是**父目录被排除时，`!` 无法再包含其中的文件**。
        #   原来三个落点目录都写成 `xxx/` + `!xxx/摆放说明.txt`，那条否定是**惰性的**——
        #   实测 voice_cache / share_images / SnowLuma 的摆放说明从来没进过仓库，
        #   只在 zip 里有。clone 的人照 README 想找落点指引，一个都找不到。
        "# 运行时产物\n__pycache__/\n*.pyc\n.pytest_cache/\nvoice_cache/*\n"
        # 知识库索引**要进仓库**（它带 BGE 向量，clone 的人首启省 6.2 秒）。
        # 2026-09-19 事故的教训不是「派生数据不能发」，而是「**旧的**派生数据不能发」——
        # 泄漏来自索引是发布前建的、源文件是之后清理的。现在每次生成快照都现场重建
        # （见 _build_knowledge_index），并由 test_shipped_index_matches_snapshot_sources_exactly
        # 逐篇比对 sha256 钉住同源。边车仍然排除。
        "# 生成物\n*.sqlite3-shm\n*.sqlite3-wal\n!knowledge/.knowledge_index.sqlite3\n"
        "share_images/*\ngenerated_images/\ntemp_files/\n*.log\n*.json\n"
        # SnowLuma 是第三方程序，不能进仓库；但落点目录的摆放说明要留
        # （没有它用户不知道该往哪解压——2026-09-19 补）
        "SnowLuma/*\n!SnowLuma/摆放说明.txt\n"
        # 贴图情绪索引不是运行时产物——原来被上面的 *.json 一刀切挡在仓库外，
        # 于是 clone 到的仓库比版本 zip 少一个文件（贴图只能退回按文件名猜情绪）。
        # 2026-09-18 核实：内容是贴图描述，无隐私字段。
        "!stickers/metadata.json\n!stickers_cg/metadata.json\n"
        "!voice_cache/摆放说明.txt\n!share_images/摆放说明.txt\n"
        "!requirements.txt\n\n"
        "# 本机工具目录\n.claude/\n.codex/\n.gitnexus/\n.stfolder/\n.stignore\n",
        encoding="utf-8")
    print("  ✅ .gitignore")

    # 8. 敏感复扫（红线自检）——两道，判据不同：
    #    ① 按**形状**认（正则）：路径/QQ/密钥样式，会命中未来才知道的泄漏
    #    ② 按**真值**认（拿 .env 里的值直接搜）：形状认不出来的密钥也躲不过
    #    2026-09-20 证明两道都要有：一天里两次真 token 进发布物，① 都报"零命中"
    #    （一次在被整体豁免的文件里，一次是打了码的值但**输入**是真值）。
    hits = scan_sensitive(OUT) + scan_env_values(OUT)
    if hits:
        print("\n⚠️  ⚠️  敏感扫描发现命中（发布前必须清零）：")
        for h in hits[:20]:
            print(f"    {h}")
        # **命中即中止**（2026-09-20 独立复核建议）。原先只打印一行提示就继续往下走，
        # 最后还以「该目录可 git init 后推送 GitHub」收尾——真正的红线只靠人读 stdout。
        # 这个项目一天里两次把真 token 带进发布物，都是靠人眼漏过去的。
        print("\n[×] 扫描命中，已中止（快照已生成但**不要推送**——先清干净再重跑）")
        raise SystemExit(1)
    print("  ✅ 敏感扫描：零命中（形状 + .env 真值，两道都过）")

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
          + "（只含音频；歌词能力已于 2026-10-15 删除）")
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
