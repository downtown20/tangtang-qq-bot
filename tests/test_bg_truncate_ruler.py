# -*- coding: utf-8 -*-
"""背景块裁剪的「尺子一致性」闸门（2026-10-25 第 8 轮）

## 这个文件防的是一类具体的腐烂

`truncate_bg_block` 负责「按 token 预算把背景块裁短」，`estimate_tokens` 负责
「估算一段文本值多少 token」。两者是**同一个物理量的两个换算点**，必须同一口径。

历史：`estimate_tokens` 的中文系数在第 6 轮从 1.8 改成 **0.72**（旧估算高估
2.36 倍，实测 API `prompt_tokens` 1926 vs 旧估算 4542）；
`context_builder._truncate_text` 跟着改成 `/ 0.72`——**唯独 `truncate_bg_block`
漏了**，还在用 `/ 1.8`。

后果：按新尺子量出「这个块 70t」，按旧尺子只准留 `70/1.8 = 38` 个字。
第 8 轮实测：记忆块 `294t → 70t` 之后只剩标题行
`📋 名字(ID:xxxx) | 聊N次 | 等级`，**一条记忆都没有**——而日志照样写
「记忆块保留了」，界面上看不出来。

它是被 `✏️` 纠正优先标记「一个都没进 prompt」暴露的：离线调
`format_compact_memories` 明明有标记，线上 `turn_traces.prompt_digest`
的 `memory_marks.corrected` 却是 0——中间只差裁剪这一步。

## 闸门怎么才算真红

只断言「裁完不超预算」是不够的：`/ 1.8` 的旧实现**也**不超预算（它裁得太狠，
永远超不了）。所以必须有**反向断言**——裁完要尽量用满预算。
两条一起才钉得住「两把尺子一致」。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols


class TestTruncateRulerMatchesEstimator:
    """裁剪后的体积必须贴近预算——不能超，也不能只用一小半。"""

    @pytest.mark.parametrize("limit", [60, 100, 200])
    def test_truncated_block_fills_its_budget(self, limit):
        # 无分隔符的连续文本：走「切不动就加省略号」分支，最便于精确计量
        text = "中" * 2000
        out = protocols.truncate_bg_block(text, limit)
        used = protocols._bg_est(out)
        assert used <= limit, f"limit={limit}: 裁完仍超预算（{used}t）"
        assert used >= limit * 0.8, (
            f"limit={limit}: 只用了 {used}t，浪费 {(1 - used / limit) * 100:.0f}% 预算"
            " —— 裁剪尺子与估算尺子不同口径（历史：/1.8 vs /0.72）")

    def test_short_text_is_untouched(self):
        text = "很短的一段记忆内容"
        assert protocols.truncate_bg_block(text, 200) == text

    def test_empty_text_safe(self):
        assert protocols.truncate_bg_block("", 100) == ""

    def test_line_boundary_preferred_over_mid_word(self):
        """整行优先：不能在半条事实中间切（记忆条目通常一行）。"""
        text = "第一行完整的记忆内容\n第二行完整的记忆内容\n第三行完整的记忆内容"
        out = protocols.truncate_bg_block(text, 15)   # 15t ≈ 20 字符
        assert out.startswith("第一行完整的记忆内容")
        # 要么不带第二行，要么带完整的第二行——不许切一半
        if "\n" in out:
            assert "第二行完整的记忆内容" in out

    def test_never_harder_than_estimator_allows(self):
        """对任意长度的真实记忆块，裁剪结果都必须 ≤ 预算（不被尺子骗）。"""
        for n in (50, 200, 600, 1500):
            text = "关于这件事的记忆：" + "细节" * n
            out = protocols.truncate_bg_block(text, 120)
            assert protocols._bg_est(out) <= 120


class TestMemoryBlockSurvivesTruncation:
    """端到端：记忆块被裁之后，**至少第一条记忆**还在（只剩标题行就是故障）。"""

    def test_first_memory_line_survives(self):
        block = (
            "📋 成展(ID:0083) | 聊1668次 | 👑 灵魂绑定\n"
            f"  {protocols.CORRECTED_MARK} 成展是大学大二学生，读湘南学院\n"
            "  💭 成展在杭州上班\n"
        )
        out = protocols.truncate_bg_block(block, 70)
        assert "大二学生" in out, (
            f"70t 的预算裁完只剩 {out!r} —— 记忆块被裁到没有记忆，"
            "只剩标题行（历史故障形态）")
