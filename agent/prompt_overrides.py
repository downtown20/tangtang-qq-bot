# -*- coding: utf-8 -*-
"""协议文本覆盖层 —— 让「规则」可以在桌面上改（2026-10-26 第 49 轮）。

## 这是什么

`agent/protocols.py` 里有 **35 个字符串常量**，但性质完全不同：

| 类别 | 例子 | 能不能让人改 |
|---|---|---|
| **协议条文**（22 条） | `THINKING_PROTOCOL`、`CORRECTION_CONTRACT`、`CONVERSATION_CRAFT` | ✅ 能——这就是「规则自由度」 |
| **代码契约**（13 条） | `SPLIT_MARKER = "[分段]"`、`UNCERTAIN_MARK = "💭~"`、`QUOTE_RESOLVED = "resolved"` | ❌ **绝对不能** |

**为什么契约不能改**：`SPLIT_MARKER` 被 `_SPLIT_PARSE_RE` 用来切消息、`UNCERTAIN_MARK`
被记忆渲染用来标「模糊记得」。把它改成 `[分条]`，解析器就再也匹配不到——
**分段静默失效、记忆标记静默丢失，而界面上显示「保存成功」**。
这类「改完看起来正常、实际坏了」的坑，本项目已经踩过太多次（教训 #13 魔术字符串锚点）。

所以这里是**白名单**：只有 `OVERRIDABLE` 里列出的 22 条会被 `prompts/<name>.md` 覆盖，
其余即使建了同名文件也当没看见。

## 生效时机

模块 import 时读一次 → **改完重启糖糖生效**。与 `config.yaml` 的语义一致；
再加一套热重载会造出第二套生效语义（教训 #38）。

## 回滚

删掉 `prompts/<name>.md` → 自动回落到 `protocols.py` 里的内置默认值。
这是最朴素的回滚，也是这个设计唯一值得吹的地方：**覆盖层永远不会毁掉默认值**。
"""
from __future__ import annotations

import re
from pathlib import Path

#: 项目根（`agent/` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 协议文本目录
PROMPT_DIR = PROJECT_ROOT / "prompts"

#: 可覆盖的协议条文：`prompts 文件名（不含 .md）` → `protocols.py 里的常量名`。
#:
#: ⚠ **只加不减要谨慎**：新增一条之前先问「改了它会不会让解析器失配」。
#: 判断标准：这个字符串被拿去**匹配/切分/渲染结构**吗？
#: 被拿去拼进 prompt 给模型读 → 可覆盖；被拿去 `re`/`in`/`==` 判断 → 不可覆盖。
OVERRIDABLE: dict[str, str] = {
    # —— 理解与思考 ——
    "comprehension": "COMPREHENSION_PROTOCOL",
    "thinking": "THINKING_PROTOCOL",
    "correction_contract": "CORRECTION_CONTRACT",
    "high_stakes": "HIGH_STAKES_GUIDANCE_CONTRACT",
    "grounding": "GROUNDING_CONTRACT",
    "grounding_no_topic": "GROUNDING_NO_TOPIC_FALLBACK",
    "memory_evidence": "MEMORY_EVIDENCE_CONTRACT",
    # —— 对话与表达 ——
    "window_reply": "WINDOW_REPLY_PROTOCOL",
    "style_anchor": "STYLE_ANCHOR",
    "reply_naturalness": "REPLY_NATURALNESS",
    "follow_up": "FOLLOW_UP_PROTOCOL",
    "conversation_craft": "CONVERSATION_CRAFT",
    "knowledge_boundary": "KNOWLEDGE_USAGE_BOUNDARY",
    # —— 记忆标记的**说明文字**（标记符号本身在契约里，不动） ——
    "uncertain_memory": "UNCERTAIN_MEMORY_CONTRACT",
    "corrected_memory": "CORRECTED_MEMORY_CONTRACT",
    # —— 画像说明 ——
    "profile_caveat": "PROFILE_CAVEAT",
    "profile_caveat_short": "PROFILE_CAVEAT_SHORT",
    "profile_caveat_line": "PROFILE_CAVEAT_LINE",
    # —— 协议条文（分段/引用/心情自报） ——
    "split_protocol": "SPLIT_PROTOCOL",
    "quote_protocol": "QUOTE_PROTOCOL",
    "mood_report": "MOOD_REPORT_PROTOCOL",
    # —— 意图理解 ——
    "intent_prompt": "INTENT_PROMPT",
}

#: 协议分组的**种子与兜底**：`(key, 标题, 一句话说明)`，顺序 = 界面从上往下的顺序。
#:
#: 为什么分组是**数据**而不是注释：注释只能给人看，界面渲染不出来；
#: 而分组和「这条管什么」恰恰是使用者**改之前最需要知道的两件事**
#: （不看说明就改协议，等于不知道自己在改什么）。
#:
#: 分组不按文件、不按常量名，按**她什么时候用得上**——使用者要找的是
#: 「我想让她别接无意义的话」，不是「哪个常量叫 WINDOW_REPLY_PROTOCOL」。
#:
#: ⚠ **真正的组定义在 `prompts/_groups.yaml`**（`read_groups()`），这张表只是
#: 「文件不存在/读坏时用哪套默认」。2026-10-26 之前组标题是**每条协议各写一份**
#: 的（`group_title` 文件头字段），同组 6 条就存 6 份副本，改哪份生效取决于
#: 遍历顺序——改第 6 条不生效，改出两个同名标题则合并成一组（见教训 #54）。
#: 组是**组级**的东西，就只能有一份定义。
PROMPT_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("understand", "怎么理解对方", "难聊、被纠正、涉及要紧事的时候，先怎么想"),
    ("reply", "怎么回", "长度、语气、接不接话——这些决定她像不像在聊天"),
    ("memory", "记错的时候怎么办", "证据排哪一层、标记是什么意思、纠正要不要真写回"),
    ("boundary", "知道但不该说", "检索到的资料怎么用、别人的隐私问到什么程度"),
    ("delivery", "发送与自报", "她能用哪些发送标记，以及标记怎么让她表现"),
    ("intent", "意图判断", "让她自己判断对方到底想要什么"),
)

#: 协议的**来源**：只有「从外部材料蒸馏出来」的那几条才有值。
#:
#: 为什么单独一张表、而不是塞进 `desc`：`desc` 回答「改它会影响什么行为」，
#: `origin` 回答「这段话从哪来」。两件事混成一句话就变成一段说明，
#: 而主人明确要求「不要过度注释」。分开之后，界面可以只在有来源时显示一个标签。
#:
#: 主人 2026-10-26 问「狗头军师的内容被蒸馏进了规则，但我好像在规则里没有看到」——
#: 它一直在（就是下面这两条），缺的只是这个标签。
PROMPT_ORIGIN: dict[str, str] = {
    "conversation_craft": "知识蒸馏",
    "knowledge_boundary": "知识蒸馏",
}

#: 每条的 `(分组 key, 一句话作用)`。
#:
#: 一句话作用只回答「改它会改变什么行为」，**不重复正文**（正文就在文件里，
#: 再抄一遍既是第二份真相又会腐烂）。主人 2026-10-26 的要求：不要过度注释。
PROMPT_META: dict[str, tuple[str, str]] = {
    "comprehension": ("understand", "被纠正或质疑时，先复述再答，禁止换个说法重发"),
    "thinking": ("understand", "需要多想的话写进隐藏段，发送前会被剥掉"),
    "high_stakes": ("understand", "健康、诊断、重大决定：先接感受再理清，不替 ta 拍板"),
    "intent_prompt": ("intent", "发来一张图时，先判断是表情包还是截图再回答"),
    "style_anchor": ("reply", "说话示范——学的是节奏和直接劲，不是逐字模仿"),
    "reply_naturalness": ("reply", "默认一两句；像打字不像写作；该展开时才展开"),
    "follow_up": ("reply", "接住前文里材料中真有的那件事，以及什么时候不该追问"),
    "window_reply": ("reply", "连续对话里要不要接这一句；沉默也是正当决定"),
    "conversation_craft": ("reply", "接的是人不是话：先接情绪再接内容，笼统的话问细节"),
    "correction_contract": ("memory", "对方纠正事实时必须真调工具写回，不许只口头说「记住了」"),
    "memory_evidence": ("memory", "证据优先级：原始记录 > 带来源的自忆 > 摘要与画像"),
    "grounding": ("memory", "提到人和往事时只用材料里真有的，不补地点时间事件"),
    "grounding_no_topic": ("memory", "没有合适的往事可接，就聊现在"),
    "uncertain_memory": ("memory", "`~` 标记的含义：只模糊记得，说出来要留余地"),
    "corrected_memory": ("memory", "`✏️` 标记的含义：以 ta 亲口纠正过的为准，不与旧说法并列"),
    "knowledge_boundary": ("boundary", "检索到的是参考不是结论；不主动索要隐私；危险信号优先"),
    "profile_caveat": ("boundary", "画像可能不准（长版，写在画像前面）"),
    "profile_caveat_short": ("boundary", "画像仅供参考（短版，空间紧张时用）"),
    "profile_caveat_line": ("boundary", "以上画像是综合生成（行内版，写在画像后面）"),
    "split_protocol": ("delivery", "`[分段]` 分条发送，每条要能单独看懂"),
    "quote_protocol": ("delivery", "`[引用]` 什么时候值得挂引用"),
    "mood_report": ("delivery", "`[心情:词]` 自报心情，可选词是固定清单"),
}

#: 单个文件的字节上限（与 `tools/asset_edit.py::MAX_TEXT_BYTES` 同口径：
#: 这些文字会整段进 system prompt，太长会挤掉真正要说的内容）
MAX_BYTES = 64 * 1024


def prompt_path(name: str) -> Path:
    """`prompts/<name>.md` 的路径。**不做白名单判断**——调用方必须先查 `OVERRIDABLE`。"""
    return PROMPT_DIR / f"{name}.md"


# ── 元数据（2026-10-26 外置到文档）─────────────────────────────────────
#
# 一条协议的「归哪一组、一句话说明、来源标注」是**文案**，主人要求
# 「和场景卡一样在文档内可改」，所以放进文件头：
#
#     ---
#     group: understand
#     desc: 被纠正或质疑时，先复述再答
#     origin: 知识蒸馏
#     ---
#     正文（真正注入的内容）
#
# **组标题不在这里**（2026-10-26 二次修正）：`group` 是结构键（指向
# `_groups.yaml` 里的一条），组的显示名住在组上——同组 6 条各写一份
# `group_title` 副本时，改哪份生效靠遍历顺序，是「改了不生效」和
# 「两个组合并」的共同根源。
#
# 代码里的 `PROMPT_META` / `PROMPT_GROUPS` / `PROMPT_ORIGIN` 降级为**默认值**：
# 文件没写就用它，所以只有正文、没有文件头的老文件行为完全不变。

_META_FENCE = "---"


def split_front_matter(text: str) -> tuple[dict, str]:
    """把 `---` 包起来的 YAML 头和正文分开。没有头就返回 ({}, 原文)。

    **解析失败一律当没有头**：一个 YAML 手误不能让她说不出话——正文照常用。
    这条比「严格校验」重要，因为它在启动路径上。
    """
    body = str(text or "")
    stripped = body.lstrip()
    if not stripped.startswith(_META_FENCE):
        return {}, body
    lines = stripped.split("\n")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _META_FENCE:
            end = i
            break
    if end is None:
        return {}, body
    try:
        import yaml
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:
        return {}, body
    if not isinstance(meta, dict):
        return {}, body
    clean = {str(k).strip(): ("" if v is None else str(v)) for k, v in meta.items()}
    return clean, "\n".join(lines[end + 1:]).lstrip("\n")


def read_meta(name: str) -> dict:
    """读一条协议的元数据（文件头）。没有/读坏了返回空 dict，调用方回落代码默认。"""
    p = prompt_path(name)
    try:
        if not p.is_file():
            return {}
        meta, _ = split_front_matter(p.read_text(encoding="utf-8-sig"))
        return meta
    except (OSError, UnicodeDecodeError):
        return {}


def read_override(name: str) -> str:
    """读一条覆盖文本。

    返回空串表示「没有覆盖 / 覆盖无效」，调用方据此回落到默认值。
    读失败、编码错、超长、全空白一律当没覆盖——**覆盖层坏了不能拖垮糖糖启动**。
    """
    if name not in OVERRIDABLE:
        return ""
    p = prompt_path(name)
    try:
        if not p.is_file():
            return ""
        if p.stat().st_size > MAX_BYTES:
            return ""
        # `utf-8-sig`：Windows 编辑器（记事本/VS Code 某些配置）会在文件头写 BOM。
        # 用 `utf-8` 读，BOM 会变成第一个字符 `\ufeff` 混进 system prompt
        # （她看到的前三个字节是「不可见字符」+ 协议正文）；而纯 BOM 文件
        # 会被 `strip()` 判成「有内容」，让一条空协议顶掉内置默认。
        # 2026-10-26 单测 `test_blank_override_falls_back[\ufeff]` 抓到的。
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    # 文件头是**文案**（界面显示用），不能跟着正文进 system prompt——
    # 否则她看到的头几行是 `---` 和 `name: ...`。剥离失败就当没有头（返回原文）。
    _, body = split_front_matter(text)
    return body if body.strip() else ""


# ── 分组（组级定义，2026-10-26 从每条的文件头收敛到独立文件）──────────
#
# 组是**组的属性**：顺序、显示名、一句话说明。它不是「每条协议的这一条」，
# 所以不能存成条目级副本——同组 N 条就有 N 份副本，改哪份生效取决于
# `dict` 遍历顺序（既不可知也不可预期）。主人 2026-10-26 报的两个症状：
#   ① 改 `group` → 条目跑到别的组（位置迁移）；改出的 key 不存在则落「未分组」，
#      几个不同 key 的条目因为显示名都叫「未分组」被界面切成同一组（合并）；
#   ② 改 `group_title` → 同组排在前面的文件先占位，后面几条改了不生效。
# 一个根因：**组的身份寄生在条目上**。组级的东西只能有一份定义。
#
# 现在的分工：
#   - `prompts/_groups.yaml`：组列表（顺序 + title + desc），**唯一真相**
#   - 条目文件头 `group: <key>`：只做引用；key 不存在 → 「未分组」+ 界面标红
#   - `PROMPT_GROUPS`：文件不存在时的种子/兜底
GROUPS_PATH = PROMPT_DIR / "_groups.yaml"


def read_groups() -> list[dict]:
    """读组定义（顺序即界面顺序）。文件不存在/读坏 → 回落 `PROMPT_GROUPS`。

    返回 `[{key, title, desc, source}]`，`source` 是 `"file"` 或 `"code"`。
    **永远不抛**：分组只是界面文案，一个 YAML 手误不能让她说不出话。
    """
    fallback = [{"key": k, "title": t, "desc": d, "source": "code"}
                for k, t, d in PROMPT_GROUPS]
    try:
        if not GROUPS_PATH.is_file():
            return fallback
        import yaml
        data = yaml.safe_load(GROUPS_PATH.read_text(encoding="utf-8-sig"))
    except Exception:  # YAML 语法错/权限/编码 —— 一律回落，启动路径不许崩
        return fallback
    if not isinstance(data, list):
        return fallback
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        out.append({
            "key": key,
            # 标题空着没有意义（界面会显示成「未分组」），用 key 兜底
            "title": str(item.get("title") or "").strip() or key,
            "desc": str(item.get("desc") or "").strip(),
            "source": "file",
        })
    return out or fallback


#: `_groups.yaml` 顶部的说明。它同时也是这个文件「能被读懂」的一半：
#: 只列 key/title 的话，下一个人不知道 key 能不能改、标题改了谁生效。
GROUPS_HEADER = """\
# 协议分组 —— 界面上「怎么理解对方 / 怎么回 / …」这些小标题就是这份文件。
#
# · 顺序 = 界面从上往下的顺序，想调整就整块上下移动；
# · `key` 是**结构**（每条协议文件头的 `group:` 指向它），改了 key 等于让指向它的
#   协议全部落到「未分组」——所以 key 别改；
# · `title` / `desc` 是**文案**，随便改。桌面端双击组标题改的也是这里的 title。
#
# 为什么单独一份文件：组是组级的东西（顺序/名字/说明），把它写进每条协议的文件头
# 就会有 N 份副本，改哪份生效取决于遍历顺序——2026-10-26 的「改 group 位置迁移」
# 「改 group_title 不生效」「两个组合并」全是这一个原因。
"""

_GROUP_KEY_RE = re.compile(r"^(\s*)-\s*key\s*:\s*(.+?)\s*$")


def _yaml_scalar(value: str) -> str:
    """把一个字符串渲染成 YAML 标量的「值」部分。

    手拼 `title: {value}` 在标题含 `#` / `:` / 以 `-` 开头时会静默变义
    （`#` 之后整段被当注释丢掉）。统一交给 `yaml.safe_dump` 决定引号。
    """
    import yaml
    dumped = yaml.safe_dump({"v": value}, allow_unicode=True,
                            default_flow_style=False, sort_keys=False)
    return dumped.split("\n", 1)[0][3:]


def render_groups_yaml(groups: list[dict] | None = None) -> str:
    """生成 `_groups.yaml` 的完整文本（文件被删/首次部署时用来自愈）。"""
    import yaml
    items = groups
    if items is None:
        items = [{"key": k, "title": t, "desc": d} for k, t, d in PROMPT_GROUPS]
    rows = [{"key": str(g.get("key") or ""),
             "title": str(g.get("title") or ""),
             "desc": str(g.get("desc") or "")}
            for g in items if str(g.get("key") or "").strip()]
    body = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    return GROUPS_HEADER + "---\n" + body


def replace_group_title(text: str, key: str, title: str) -> str:
    """在 `_groups.yaml` 的**文本**里只改 `key` 那一块的 `title`，其余字节不动。

    不做 `yaml.safe_dump` 整份重写：文件顶部的说明注释是它能被读懂的一半，
    整份 dump 会把注释和顺序感一起洗掉（`config.yaml` 上踩过两次，教训 #7）。

    返回空串 = 没找到这个 key（交给调用方报错，不要静默写入）。
    """
    lines = text.replace("\r\n", "\n").split("\n")
    start = None
    for i, line in enumerate(lines):
        m = _GROUP_KEY_RE.match(line)
        if m and m.group(2).strip("'\"") == key:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _GROUP_KEY_RE.match(lines[j]):
            end = j
            break
    field_indent = _GROUP_KEY_RE.match(lines[start]).group(1) + "  "
    rendered = f"{field_indent}title: {_yaml_scalar(title)}"
    for j in range(start + 1, end):
        if re.match(r"^\s*title\s*:", lines[j]):
            lines[j] = rendered
            return "\n".join(lines)
    # 块里没有 title 行 → 插在 key 行后面
    lines.insert(start + 1, rendered)
    return "\n".join(lines)


def list_overrides() -> list[dict]:
    """当前生效的覆盖清单（给排查脚本与界面用）。

    **按 `read_groups()` 的顺序输出**，每条带上 `group` / `group_title` / `desc`。
    排序放在这里而不是让界面自己排：界面的分组标题也来自组表，
    两处各排一次必然出现「标题顺序和条目顺序不一致」。

    `group` 只认**文件头 → `PROMPT_META` 默认**这条链，且必须是组表里的 key；
    不在表里就落「未分组」并把 `group_known` 置 False（界面标红，不静默吞掉）。
    """
    groups = read_groups()
    order = {g["key"]: i for i, g in enumerate(groups)}
    titles = {g["key"]: g["title"] for g in groups}
    group_descs = {g["key"]: g["desc"] for g in groups}
    rows: list[dict] = []
    for name, const in OVERRIDABLE.items():
        group, desc = PROMPT_META.get(name, ("", ""))
        meta = read_meta(name)
        group = str(meta.get("group") or group)
        desc = str(meta.get("desc") or desc)
        text = read_override(name)
        # `origin` 用 `in` 判断而不是真值判断：**显式空串**是「这条不要来源标签」，
        # 与「文件头没写这个字段」必须区分开，否则清空标签永远不生效。
        if "origin" in meta:
            origin = str(meta.get("origin") or "")
        else:
            origin = PROMPT_ORIGIN.get(name, "")
        known = group in titles
        rows.append({
            "name": name,
            "const": const,
            "path": f"prompts/{name}.md",
            "exists": prompt_path(name).is_file(),
            "active": bool(text),
            "chars": len(text),
            "group": group,
            "group_title": titles.get(group) or "未分组",
            "group_desc": group_descs.get(group, ""),
            # False = 文件头写的 group 不在组表里（拼错/组被删）→ 界面要标出来
            "group_known": known,
            "desc": desc,
            # 空串 = 原生协议（没有外部来源）
            "origin": origin,
            # 旧文件头残留的 `group_title` 已不再生效，界面要能说出来，
            # 否则又是一次「改了没反应」——静默失效是本项目的头号反模式
            "stale_group_title": "group_title" in meta,
        })
    rows.sort(key=lambda r: order.get(r["group"], len(order)))
    return rows


def apply_overrides(module_globals: dict) -> list[str]:
    """把 `prompts/*.md` 覆盖到模块全局（`protocols.py` 末尾调用一次）。

    返回被覆盖的常量名列表（给启动日志用——「哪几条文本被改了」应该一眼看得见）。
    """
    applied: list[str] = []
    for name, const in OVERRIDABLE.items():
        text = read_override(name)
        if not text:
            continue
        current = module_globals.get(const)
        # 只在「确实不同」时替换，避免无意义地改动模块状态
        if isinstance(current, str) and current != text:
            module_globals[const] = text
            applied.append(const)
    return applied
