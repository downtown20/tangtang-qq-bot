"""背景块预算的「缺口告知」与定序原则（2026-10-16 T59/T57）。

三条契约：
1. 没有淘汰也没有裁剪时，输出**逐字节**不变（不许凭空多出一个块）；
2. 真有淘汰/裁剪时，告知必须**恰好**列出那些块，且只提**真实存在**的取回工具；
3. 定序原则：没有工具能取回的块（关系档案）必须排在能取回的块（窗口/记忆/…）前面。"""
from __future__ import annotations

from agent import protocols

import re as _re

_ALLOWED_TOOLS: set[str] = set()
for _tools in protocols._BG_RECOVERY_TOOLS.values():
    # 映射值可能是复合串（"search_facts / search_memories"）——按单词抽，
    # 否则断言会把合法工具误判成「没登记」。
    _ALLOWED_TOOLS.update(_re.findall(r"search_[a-z_]+", _tools))


def _labels(blocks):
    return [label for label, _text in blocks]


def _notice(blocks) -> str:
    found = [text for label, text in blocks if label == "缺口"]
    return found[0] if found else ""


def test_no_gap_notice_when_nothing_is_dropped():
    """非饱和回合逐字节不变——这是「不占 token」承诺的闸门。"""
    bgs = [("协议", "协议内容" * 3), ("记忆", "一条记忆"), ("窗口", "最近的对话")]
    out = protocols.fit_backgrounds(bgs, budget_tokens=1200)
    assert _labels(out) == ["协议", "记忆", "窗口"]
    assert _notice(out) == ""


def test_gap_notice_lists_exactly_the_missing_blocks():
    bgs = [
        ("协议", "协议内容" * 3),
        ("窗口", "窗" * 900),
        ("记忆", "记" * 200),
        ("日记", "日" * 900),
    ]
    out = protocols.fit_backgrounds(bgs, budget_tokens=400)
    text = _notice(out)
    assert text, "有淘汰却没有告知——又回到静默丢块"
    kept = set(_labels(out))
    missing = [label for label in ("窗口", "记忆", "日记") if label not in kept]
    assert missing, "构造没生效：这一轮应当有块被丢"
    for label in missing:
        assert label in text, f"{label} 被丢了却没写进告知：{text}"


def test_gap_notice_marks_partially_kept_blocks():
    """被裁的块是「只放了一部分」，不是「完全没进来」——两种事实不能混。"""
    bgs = [("窗口", "窗" * 2000)]
    out = protocols.fit_backgrounds(bgs, budget_tokens=600)
    text = _notice(out)
    assert "窗口" in text and "只放了一部分" in text, text


def test_gap_notice_only_names_real_tools():
    """不许在告知里发明工具——工具清单是代码事实，不是文案。
    （注意：中文属于 \\w，所以不能用 split/\\w+ 取工具名——必须用只认 ASCII 的 search_[a-z_]+。）"""
    import re
    bgs = [
        ("协议", "协议内容" * 3),
        ("窗口", "窗" * 900),
        ("记忆", "记" * 200),
        ("日记", "日" * 900),
        ("知识库", "知" * 900),
    ]
    text = _notice(protocols.fit_backgrounds(bgs, budget_tokens=400))
    named = set(re.findall(r"search_[a-z_]+", text))
    assert named, "告知里一个取回工具都没提"
    assert named <= _ALLOWED_TOOLS, f"告知里出现了没登记的工具：{named - _ALLOWED_TOOLS}"
    # 正向对照：知识库也在这一轮被丢了，但它没有工具——不许被写成可取回。
    tail = text.split("要的话")[-1] if "要的话" in text else text
    assert "知识库" not in tail, tail


def test_gap_notice_is_placed_before_other_blocks():
    """协议块要紧贴用户原文是既有不变量，告知插在最前才不会把它挤开。"""
    bgs = [("协议", "协议内容" * 3), ("窗口", "窗" * 2000)]
    out = protocols.fit_backgrounds(bgs, budget_tokens=600)
    if _notice(out):
        assert _labels(out)[0] == "缺口"


def test_relationship_archive_outranks_tool_recoverable_blocks():
    """定序原则：丢了能自己取回的，必须先让位给取不回的（关系档案没有工具）。

    2026-10-23 T64：名单来源改为**直接从 _BG_RECOVERY_TOOLS 取**——原来这里手抄了
    一份，而手抄的那份里还有「补读」（那个块早就不产生了）。手抄清单必然腐烂
    （CLAUDE.md §三 那条「技能与工具清单不在这里手抄」的同一条纪律）。
    """
    recoverable = [label for label in protocols._BG_RECOVERY_TOOLS
                   if label in protocols.BACKGROUND_PRIORITY]
    assert recoverable, "取回工具表里一个与优先级表对得上的标签都没有——表被改坏了"
    for label in recoverable:
        assert (protocols.BACKGROUND_PRIORITY["关系档案"]
                < protocols.BACKGROUND_PRIORITY[label]), label


def test_relationship_archive_survives_a_saturated_round():
    """复刻饱和回合：修前（关系档案 P9）它最后一个进池、预算已耗尽 → 被整块丢掉；
    修后（P4）它先进池。这条是本次改动的回归闸门。

    2026-10-25：`estimate_tokens` 换成实测系数（中文 1.8→0.72）后，写死的
    `budget_tokens=1200` 在新尺子下能装下全部四块，前提（饱和）失效。
    **判据没变，是尺子变了**——改成按 `_bg_est` 现场算「刚好装下前三块、
    差一块量装不下第四块」，这样以后再换尺子也不用改这条。
    """
    bgs = [
        ("协议", "协" * 120),
        ("窗口", "窗" * 360),
        ("记忆", "记" * 200),
        ("关系档案", "档" * 250),
    ]
    # 只够「协议（保护块）+ 关系档案」+ 一点零头——后面的 flex 块必然进不来。
    # 用 _bg_est 现场算而不是写死数字，换尺子时这条不用再改一次。
    need = sum(protocols._bg_est(c) for _l, c in bgs if _l in ("协议", "关系档案"))
    budget = need + 90
    out = protocols.fit_backgrounds(bgs, budget_tokens=budget)
    labels = _labels(out)
    assert "关系档案" in labels, f"取不回的档案又被丢了：{labels}"
    assert "记忆" not in labels, "这个构造里应当恰有块被挤掉（否则测试没在测淘汰）"
    assert "记忆" in _notice(out)


def test_archive_only_round_has_no_notice():
    """正向对照：材料放得下时不许产生告知（否则它会变成每轮常驻噪声）。"""
    out = protocols.fit_backgrounds([("关系档案", "档" * 100), ("窗口", "窗" * 50)],
                                    budget_tokens=1200)
    assert _labels(out) == ["关系档案", "窗口"]

# ═══════════════════════════════════════════════════════════════════
# 登记-消费漂移闸门（2026-10-23 T64）
# ═══════════════════════════════════════════════════════════════════
#
# 病根：BACKGROUND_PRIORITY / _BG_RECOVERY_TOOLS 是**手写的登记表**，而块是
# handler 里 append 出来的**消费点**。两者没人比对，于是表里攒下了三个永远
# 不会出现的标签（画像 / 补读 / 知识库，清理见 protocols.py 那条注释）。
#
# 这类漂移在本仓有前科：BACKGROUND_PRIORITY 的「画像」与「补读」都曾被
# 单独记录成「登记-消费漂移」，记了两次、每次都说「留给专门的漂移任务」——
# 然后继续漂着。**手写清单必然腐烂**（CLAUDE.md 三节明写「技能与工具清单
# 不在这里手抄」），所以这里把它机器化：表 与 生产点 双向对账。
#
# 为什么用 AST 而不是 grep：标签可能出现在注释或字符串里（本文件自己的
# docstring 就写着「补读」「知识库」），文本匹配会被自己的说明文字糊过去
# （T2.2 的自指陷阱）。

_GATE_PRODUCER_EXCEPTIONS = {
    # 由 fit_backgrounds 内部插入，不经 handler 的 backgrounds.append
    "缺口",
}


def _background_labels_from_source() -> dict[str, list[str]]:
    """扫 agent/*.py，收集 backgrounds.append(("标签", ...)) 形态的标签。"""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    out: dict[str, list[str]] = {}
    for path in sorted((root / "agent").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("append", "insert")):
                continue
            # 只认接收者名字里带 background 的容器——knowledge.py 里也有
            # hits.append(("semantic", ...)) 之类的元组，它们不是背景块。
            recv = node.func.value
            if not (isinstance(recv, ast.Name) and "background" in recv.id.lower()):
                continue
            if not node.args:
                continue
            first_arg = node.args[-1] if node.func.attr == "insert" else node.args[0]
            if not (isinstance(first_arg, ast.Tuple) and first_arg.elts):
                continue
            head = first_arg.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                out.setdefault(head.value, []).append(
                    f"{path.name}:{node.lineno}")
    return out


def test_every_priority_label_has_a_producer():
    """表里登记的每个标签，都必须真的有人产出它（否则就是漂移）。"""
    producers = set(_background_labels_from_source()) | _GATE_PRODUCER_EXCEPTIONS
    registered = set(protocols.BACKGROUND_PRIORITY)
    orphans = sorted(registered - producers)
    assert not orphans, (
        "BACKGROUND_PRIORITY 里登记了**永远不会产生**的块："
        + "、".join(orphans)
        + "\n处置：删掉该键（它已经不是活概念），或补上生产点。"
        + "\n**不要**为了变绿往 _GATE_PRODUCER_EXCEPTIONS 里加——那个白名单"
        + "只给 fit_backgrounds 内部插入的块（目前只有「缺口」）用。"
        + "\n历史：画像 / 补读 / 知识库 就是这样漂了几轮才被清掉（T64）。")


def test_every_produced_label_has_a_priority():
    """反向：真的产出的块，必须在表里登记过优先级（否则会被按默认 9 淘汰）。"""
    producers = set(_background_labels_from_source())
    registered = set(protocols.BACKGROUND_PRIORITY)
    unregistered = sorted(producers - registered)
    assert not unregistered, (
        "这些块真的会进背景池，却没在 BACKGROUND_PRIORITY 里登记——"
        "它们会落到默认优先级 9，被旧记忆挤掉："
        + "、".join(unregistered))


def test_recovery_tools_only_name_real_blocks():
    """取回工具表里的每个标签，也必须是真实存在的块。"""
    producers = set(_background_labels_from_source())
    orphans = sorted(set(protocols._BG_RECOVERY_TOOLS) - producers)
    assert not orphans, (
        "取回工具表登记了不存在的块（替她发明了一个永远用不上的取回路径）："
        + "、".join(orphans) + "\n历史：「补读」就是这样被登记过（T64 已删）。")
