"""消息拆分标记 [分段] 的解析契约（2026-09-22）。

## 为什么要有这个

主人 2026-09-22 01:50–01:55 连问三次「为什么你没次都把内容都压缩到一个消息上呢」
「你一次只能发一条消息吗」；糖糖当时的回答是「我压根分不了，我这边敲完就是一大坨，
空行是我自己硬凑的，看着像分开了而已」。此前切分权在系统手里（按长度随机拆、
最多 2 段、私聊恒不拆），现在把「在哪切」交给 LLM。

## 业界对照（2026-10-12 更正）

主流框架（AstrBot 核心 / SillyTavern）确实没有「LLM 决定切分」的成品；但**生态里
至少三个先例**——moeru-ai/airi（独立拆分模型，只切不改）、
astrbot_plugin_private_companion（分层解析）、sirius-chat（`<MSG_SPLIT>`，
且有标记泄漏事故记录）。先前那句"没有任何成品"是调研口径错误（2026-09-23 全项目
审计第七节自记），**不要再引用它**。

AstrBot 核心撞过一模一样的需求
（github.com/AstrBotDevs/AstrBot/issues/338，标记 `|分段|`），当场撞上标记泄漏
「你好 |分段|很高兴见到你」，最后没采纳这条路线、只补了洗标记的规则。

所以本协议的失败方向必须是「**宁可多剥，不可漏出**」：
  - 解析窄（只认半角 `[分段]`）——不误拆正常正文
  - 兜底宽（clean() 连全角 ［分段］ 一起剥）——标记绝不进任何发送路径

本文件钉住的第一条（`test_no_marker_is_byte_identical`）是「零行为变化」的地基：
调用方靠 `len(...) == 1` 判断走旧路径，所以「无标记」必须真的逐字节相同。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.protocols import (  # noqa: E402
    MAX_REPLY_PARTS,
    SPLIT_MARKER,
    parse_split_markers,
    strip_split_markers,
)


# ---------------------------------------------------------------------------
# 1. 无边界 = 零变化（地基）
# ---------------------------------------------------------------------------
# 2026-10-14 T32：边界有**两种**——[分段] 标记，与空行（真空白行）。
# 本节的语义随之从「无标记」收紧为「无任何边界」。
# 改动理由：实测近 30 天私聊 64.4% 的回复含空行分段、平均 2.15 段、
# 段落长度中位 45 字（正好是真人一条消息的长度），而真正发出的多气泡只 9.2%
# ⇒ 投递层丢掉了她 55 个百分点的分条意图。空行是 LLM 自己输出的边界，
# 把它当分条信号是「读她已经表达的意图」，不是「系统替她决定」。


def test_no_boundary_is_byte_identical():
    """没有任何边界时必须返回单元素且与入参逐字节相同——调用方据此走旧路径。

    注意样例刻意**不含空行**：原版这里用的是「你好呀，今天怎么样？\\n\\n我挺好的喵。」
    ——那串里的空行是随手写得自然，但在 T32 之后的契约下它**就是**一个边界。
    所以样例换成单换行，才真正测到「无边界」这个前提。
    """
    text = "你好呀，今天怎么样？\n我挺好的喵。"
    parts = parse_split_markers(text)
    assert len(parts) == 1
    assert parts[0] == text
    assert parts[0] is text or parts[0] == text


def test_blank_line_is_a_boundary():
    """空行是边界（T32 新契约）——这是本轮的核心行为改变。"""
    assert parse_split_markers("第一段\n\n第二段") == ["第一段", "第二段"]


def test_single_newline_is_not_a_boundary():
    """**单个换行不是边界**——那是同一句话内部的换行，拆了就碎。

    这条是 T32 的窄边界护栏：不拆比多拆安全，
    因为「同一句话被切两半」比「两个想法挤一条」更伤可读性。
    """
    text = "第一行\n第二行"
    assert parse_split_markers(text) == [text]


def test_blank_line_with_spaces_is_a_boundary():
    """空行里夹空格/制表符仍算空行（真实输出常见）。"""
    assert parse_split_markers("甲\n  \n乙") == ["甲", "乙"]


def test_multiple_blank_lines_are_one_boundary():
    """连续多个空行只算一个边界，不产生空段。"""
    assert parse_split_markers("甲\n\n\n\n乙") == ["甲", "乙"]


def test_marker_and_blank_line_can_coexist():
    """两种边界可共存：先按标记切，再把每块按空行切。"""
    assert parse_split_markers("a[分段]b\n\nc") == ["a", "b", "c"]


def test_blank_line_respects_max_parts():
    """空行边界同样受 MAX_REPLY_PARTS 约束——超出的并进最后一段，不丢字。"""
    parts = parse_split_markers("a\n\nb\n\nc\n\nd")
    assert len(parts) == MAX_REPLY_PARTS
    assert parts[0] == "a" and parts[1] == "b"
    assert "c" in parts[2] and "d" in parts[2]


@pytest.mark.parametrize("text", [
    "",                       # 空串
    "喵",
    "（括号动作）说点什么",     # 括号不是方括号，不误拆
    "正文里提到分段但没方括号",
    "数学 [x] 和 [数组] 都不是标记",
])
def test_text_without_marker_never_splits(text):
    assert parse_split_markers(text) == [text]


# ---------------------------------------------------------------------------
# 2. 正常切分
# ---------------------------------------------------------------------------

def test_two_parts():
    assert parse_split_markers("第一段[分段]第二段") == ["第一段", "第二段"]


def test_three_parts():
    assert parse_split_markers("一[分段]二[分段]三") == ["一", "二", "三"]


def test_leading_and_trailing_marker_dropped_as_empty():
    """首/尾/连续标记产生的空段必须丢掉——空段发出去就是一个空气泡。"""
    assert parse_split_markers("[分段]开头就有[分段][分段]结尾也有[分段]") == [
        "开头就有", "结尾也有",
    ]


def test_marker_with_inner_whitespace():
    assert parse_split_markers("前[ 分段 ]后") == ["前", "后"]


def test_parts_are_stripped():
    assert parse_split_markers("  前  [分段]  后  ") == ["前", "后"]


# ---------------------------------------------------------------------------
# 3. 退化路径：绝不抛异常
# ---------------------------------------------------------------------------

def test_only_markers_returns_single_empty():
    """只有标记 → 返回 [""]，让调用方走既有「清洗后为空」分支，而不是抛异常。"""
    assert parse_split_markers(SPLIT_MARKER) == [""]
    assert parse_split_markers("[分段][分段][分段]") == [""]


# ---------------------------------------------------------------------------
# 4. 段数上限：并段而不是丢字
# ---------------------------------------------------------------------------

def test_overflow_merges_into_last_part_not_dropped():
    """丢字比多发一条严重得多——超限时把多出的并进最后一段。"""
    parts = parse_split_markers("a[分段]b[分段]c[分段]d[分段]e")
    assert len(parts) == MAX_REPLY_PARTS
    assert parts[:MAX_REPLY_PARTS - 1] == ["a", "b"]
    assert parts[-1] == "c\nd\ne"


def test_overflow_preserves_every_character():
    raw = "a[分段]b[分段]c[分段]d[分段]e"
    joined = "".join(parse_split_markers(raw)).replace("\n", "")
    assert joined == "abcde"


def test_cap_is_three():
    """护栏值本身钉住——放开它应当是一次显式修改，不是悄悄漂移。"""
    assert MAX_REPLY_PARTS == 3


# ---------------------------------------------------------------------------
# 5. 兜底剥离（clean() 的第二道防线，只剥不拆）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("前[分段]后", "前后"),
    ("前［分段］后", "前后"),          # 全角——LLM 偶尔会写全角括号
    ("前[ 分段 ]后", "前后"),
    ("前[分段 ]后", "前后"),
    ("没有标记", "没有标记"),
    ("正文里提到分段但没方括号", "正文里提到分段但没方括号"),
])
def test_strip_is_wide_and_safe(text, expected):
    assert strip_split_markers(text) == expected


def test_strip_is_idempotent():
    once = strip_split_markers("前[分段]后")
    assert strip_split_markers(once) == once


def test_parse_is_not_wide_but_strip_is():
    """**解析窄、兜底宽**——这是刻意的非对称：
    解析认全角会在正文里误拆（代价：把一条消息劈成两条）；
    兜底剥全角最多多掉两个方括号（代价：少两个字符）。
    两个代价不对称，所以两个正则也不对称。"""
    assert parse_split_markers("前［分段］后") == ["前［分段］后"]   # 解析不认
    assert strip_split_markers("前［分段］后") == "前后"             # 兜底认
