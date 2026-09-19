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

COPY_DIRS = ["agent", "onebot", "tests", "scenarios", "stickers", "stickers_cg", "knowledge"]
# knowledge/ 整目录拷时额外剔除（2026-09-05 主人裁决 v2）：
# 色色参考只发软色情 2 篇（丛雨对话范例/恋爱感氛围），BDSM/官能技法与直露写作指南不进公开；
# learned_ 自动学习残留不进。
#
# ⚠ .knowledge_index.sqlite3 原先是随包的（主人指定，避免首启重建等待），2026-09-19 改为剔除：
#   它是**由 knowledge/*.md 生成的派生索引**，里面存着切块后的正文副本。清掉源文件里的本地路径后，
#   索引里仍留着清理前的旧文本——实测 5 个 chunk 带 19 处本机绝对路径，而按后缀过滤的扫描器
#   完全看不到它（.sqlite3 不在白名单，二进制直接跳过）。派生数据不该发布：
#   它本来就带 sha256 校验、启动时自动重建，首启那几十秒不值得换一次泄漏。
KNOWLEDGE_IGNORE = shutil.ignore_patterns(
    "技法摘录_中文百合BDSM.md", "技法摘录_日系官能.md", "色色_写作指南.md",
    "learned_*", "_order.json", ".knowledge_index.sqlite3")

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
    # ⚠ 这里曾经还发着 `配置NapCat.py`——那是给 NapCat 程序本体写配置的脚本，
    #   而项目早已改用 SnowLuma：它指向的 `NapCat.Shell.Windows.OneKey/...` 目录
    #   在仓库里根本不存在，用户跑它只会拿到「❌ 未找到 NapCat 配置文件」。
    #   2026-09-19 主人问「不是没用 napcat 了吗」时查出来的，已移入 tools/_归档/。
    #   活的对等物是下面的 配置SnowLuma.py。
    "配置SnowLuma.py", "检查SnowLuma更新.py", "打包exe.py",
    "标注贴图情绪.py", "模拟消息测试.py", "打包发布附件.py", "准备发布.py",
    "打包发布版本.py", "发布前检查.py", "渲染架构图.py",
    "separate_vocals.py", "separate_vocals.bat",   # 唱歌链（可选附加场景）
}
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
    # OneBot 协议端的凭据段。⚠ 键名历史上叫 `napcat`（config.yaml 里就是它），
    # 目录/模块已改名为 onebot，但**配置键刻意没跟着改**（改键会让既有安装的配置失效）。
    # 2026-09-19 安全审计发现：改目录名时这条分支被顺手改成了 "onebot"，于是永不执行、
    # 静默什么也不做——真 config 一旦把字面 token 写进去，就会原样发到公开仓库。
    # **安全控制绑死在字面量上，名字一变就无声失效。** 两个键都处理，别再绑死。
    for _key in ("napcat", "onebot"):
        if isinstance(c.get(_key), dict):
            c[_key]["access_token"] = "请修改为你自己的 access_token"
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
        # 生成物索引：2026-09-19 事故——knowledge/.knowledge_index.sqlite3 里存着
        # knowledge/*.md 的切块副本，源文件清了本机路径它还在，随包发了出去。
        "# 生成物（派生数据不该进仓库）\n*.sqlite3\n*.sqlite3-shm\n*.sqlite3-wal\n"
        "share_images/\ngenerated_images/\ntemp_files/\n*.log\n*.json\n"
        # SnowLuma 是第三方程序，不能进仓库；但落点目录的摆放说明要留
        # （没有它用户不知道该往哪解压——2026-09-19 补）
        "SnowLuma/\n!SnowLuma/摆放说明.txt\n"
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
