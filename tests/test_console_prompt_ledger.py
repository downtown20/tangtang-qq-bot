# -*- coding: utf-8 -*-
"""「她怎么想的」账本 + 工具效果闸门（2026-10-25 第 47 轮）。

## 这组用例防的是什么

主人原话：

> 后端还有很多参数可以调节，把必要的参数放到前端，比如各环节的 token
> 预算等等……「她怎么想的」这部分展开需要的完整流程、她做了什么、她决定了
> 什么这三个部分的有效信息太少了。作为运营者我需要更多的参数显示，
> 以及有效的预警，作为一个取代日志的存在。

调研发现三缺：**后端采集了（`turn_traces.prompt_digest` 26 个字段）→
`/replay` 没出 → 前端显示 0 个**。本轮把投影层补上，这里钉住三条纪律：

| 纪律 | 为什么 | 对应的测试 |
|---|---|---|
| 分母不混 | 上一轮「9265t / 预算 6000t = 超支 54%」就是两个分母相除造出来的假警报 | `test_tools_section_is_outside_context_budget` |
| 缺数据说缺数据 | `context.used=0` 却判 ok，等于告诉运营者「没花钱」（实测 6/8 条如此） | `test_missing_sections_is_not_green` |
| 预警带证据 | 没有 evidence 的预警是让人「信我」，不是让人能自己查 | `test_every_warning_has_evidence` |

## 阳性对照（教训 #30/#37：闸门必须能红）

- 把 `tools` 也算进 `context.used` → `test_tools_section_is_outside_context_budget` 红；
- `partial` 恒 False → `test_missing_sections_is_not_green` 红；
- 按长度判空手（而不是按分类+文案）→ `test_decision_tool_short_result_is_not_empty` 红；
- 预警不带 evidence → `test_every_warning_has_evidence` 红；
- 无 digest 时判 error 而不是 skip → `test_no_digest_is_skip_not_error` 红。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.console_api import (  # noqa: E402
    _build_prompt_ledger,
    _build_tool_effects,
    _build_turn_flow,
)

#: 账本的完整字段集（前端 `ConsolePromptLedger` 按这套渲染）
LEDGER_FIELDS = {
    "sections", "context", "measured_total", "background", "system_sections",
    "user_bg_labels", "memory_marks", "history_count", "total_chars",
    "partial", "warnings", "verdict", "headline",
}

SECTION_FIELDS = {"key", "label", "tokens", "in_context", "share", "doc"}

#: 四态（`skip` 不是故障——痕迹功能上线前的回合没有账本是正常的）
VERDICTS = {"ok", "warn", "error", "skip"}


# ── 夹具 ──────────────────────────────────────────────────────────

def _digest_full():
    """一次「什么都有」的回合（数字全部取自 2026-10-25 真实 trace #253）。

    真实值很重要：`tools=5954` 正是「工具定义比整个上下文预算还大」那条
    真实现象的来源，测试里改成 1405 就再也测不出分母混算了。
    """
    return {
        "system_chars": 3314, "user_chars": 1448, "history_chars": 442,
        "history_count": 14, "total_chars": 5204,
        "system_sections": ["你是谁", "性格", "此刻"],
        "user_bg_labels": ["关系档案", "协议", "悬着的事", "窗口", "缺口", "自忆", "记忆"],
        "memory_marks": {"corrected": 1, "uncertain": 2},
        "token_sections": {"tools": 5954, "system": 2386,
                           "history": 318, "user": 1042},
        "token_total": 9240,
        "backgrounds": {
            "budget": {"limit": 1100, "used": 1033,
                       "protected_overflow": False,
                       "evidence_reserve_used": False},
            "gap_notice": True,
            "blocks": [
                {"label": "记忆", "priority": 7, "tokens": 217,
                 "tokens_before": 297, "status": "truncated",
                 "preview": "💭 答应她周末去看画展"},
                {"label": "关系档案", "priority": 4, "tokens": 160,
                 "status": "kept", "preview": "林晚 22岁"},
                {"label": "自忆", "priority": 6, "tokens": 111,
                 "status": "kept", "preview": "💭 说过 10-14 那我等你"},
                {"label": "窗口", "priority": 5, "tokens": 208,
                 "status": "kept", "preview": "在吗"},
                {"label": "协议", "priority": 1, "tokens": 99,
                 "status": "kept", "preview": "分条协议"},
                {"label": "悬着的事", "priority": 5, "tokens": 20,
                 "status": "kept", "preview": "#3 下周要交报告"},
            ],
        },
    }


def _digest_partial():
    """只有 prompt 结构摘要、没有 token 分账（实测 19:07~19:40 那批）。"""
    return {
        "system_chars": 3235, "user_chars": 1320, "history_chars": 1052,
        "history_count": 24, "total_chars": 5607,
        "system_sections": ["你是谁", "性格"],
        "user_bg_labels": ["交叉", "关系", "协议", "窗口"],
        "memory_marks": {"corrected": 0, "uncertain": 0},
    }


def _steps():
    """真实出现过的四种工具（检索查到了 / 检索空手 / 决定 / 动作）。"""
    return [
        {"name": "search_facts",
         "args": '{"query": "复查 时间"}',
         "result": "可信事实（回答前仍应结合当前对话核验）：\n- [said | 2026-10-14 | 来源 chat_log#252543] 需要上课",
         "ms": 17},
        {"name": "search_memories",
         "args": '{"query": "陶艺"}',
         "result": "(记忆库中没有关于'陶艺'的内容)",
         "ms": 9},
        {"name": "resolve_open_loop",
         "args": '{"loop_id": 4, "outcome": "asked", '
                 '"note": "问明天复查的具体时间（八点还是十点）"}',
         "result": "记下了：这件事问过了",
         "ms": 8},
        {"name": "set_reminder",
         "args": '{"text": "叫他起床", "when": "明早八点"}',
         "result": "已设置提醒",
         "ms": 12},
    ]


# ── 账本：结构与分母纪律 ──────────────────────────────────────────

def test_ledger_field_set_is_complete():
    led = _build_prompt_ledger(digest=_digest_full(), prompt_chars=5204)
    assert set(led) == LEDGER_FIELDS, (
        "账本字段集变了。前端 `ConsolePromptLedger` 按这套渲染，"
        "增删字段必须同步 `desktop/src/lib/api.ts`（教训 #4）。"
    )
    for s in led["sections"]:
        assert set(s) == SECTION_FIELDS, f"分段字段集不对：{set(s) ^ SECTION_FIELDS}"
    assert led["verdict"] in VERDICTS


def test_sections_come_from_digest_verbatim():
    """账本**不许自己算 token**——只许原样搬运 digest 里的值。

    自己再估一遍就有两个实现点，迟早和日志/API 对不上（教训 #38）。
    """
    dg = _digest_full()
    led = _build_prompt_ledger(digest=dg, prompt_chars=5204)
    got = {s["key"]: s["tokens"] for s in led["sections"]}
    assert got == dg["token_sections"], f"账本改写了 token：{got} != {dg['token_sections']}"


def test_tools_section_is_outside_context_budget():
    """工具定义不在上下文预算里——两者相加/相除就是假超支。

    真实数据：`tools=5954` 与 `BUDGET_TOTAL=6000` 几乎相等，混算必然得出
    「超支 54%」这种结论。所以 `context.used` 只累加 system/history/user。
    """
    dg = _digest_full()
    led = _build_prompt_ledger(digest=dg, prompt_chars=5204)
    by_key = {s["key"]: s for s in led["sections"]}

    assert by_key["tools"]["in_context"] is False, "工具定义被算进上下文预算了"
    assert by_key["tools"]["share"] is None, "工具那段不该有同分母占比"
    assert by_key["system"]["in_context"] is True

    expect_used = (dg["token_sections"]["system"] + dg["token_sections"]["history"]
                   + dg["token_sections"]["user"])
    assert led["context"]["used"] == expect_used, (
        f"context.used 混进了工具定义：{led['context']['used']} != {expect_used}")
    assert led["measured_total"] == dg["token_total"], "实测合计要原样透传"
    # 两个数必须同时存在且不等——界面据此并列显示、禁止相除
    assert led["measured_total"] != led["context"]["used"]


def test_context_budget_comes_from_code_constant():
    """预算取自 `ContextBuilder.BUDGET_TOTAL`，不是 digest 里的历史 `token_limit`。

    `token_limit` 是**旧版本遗留字段**（当前生产不再写），拿它当分母会在
    常量调整后悄悄失真。
    """
    led = _build_prompt_ledger(digest=_digest_full(), prompt_chars=5204)
    from agent.context_builder import ContextBuilder
    assert led["context"]["budget"] == ContextBuilder.BUDGET_TOTAL


def test_missing_sections_is_not_green():
    """**假绿防线**：有摘要、没分账时不许说「材料齐」。

    实测 8 条 trace 里 6 条是这样（`token_sections` 缺失），旧判据会显示
    `context.used=0` 再判 ok —— 等于告诉运营者「这一轮没花钱」。
    """
    led = _build_prompt_ledger(digest=_digest_partial(), prompt_chars=5607)
    assert led["partial"] is True, "分账缺失却没标 partial"
    assert led["verdict"] == "warn", f"缺分账却判了 {led['verdict']}（不能是 ok）"
    assert led["context"]["used"] == 0
    assert led["measured_total"] == 0, "没有 token_total 时不能拿字符数冒充 token"
    assert led["total_chars"] == 5607, "字符数要单独留着"
    assert any(w["key"] == "ledger_partial" for w in led["warnings"])


def test_no_digest_is_skip_not_error():
    """痕迹功能上线前的回放没有账本——这是 `skip`，不是故障。

    标红会让人去修一个不存在的问题（四态纪律：`skip` 不是失败）。
    """
    led = _build_prompt_ledger(digest=None, prompt_chars=0)
    assert led["verdict"] == "skip"
    assert led["sections"] == []
    assert led["warnings"] == [], "没有账本不该报任何预警"
    assert "没有" in led["headline"]

    led2 = _build_prompt_ledger(digest={}, prompt_chars=0)
    assert led2["verdict"] == "skip"


# ── 账本：背景块与预警 ────────────────────────────────────────────

def test_background_block_fate_is_reported():
    """每块的命运（保留/被裁/被淘汰）要逐块出，且裁剪比例由后端算。"""
    led = _build_prompt_ledger(digest=_digest_full(), prompt_chars=5204)
    bg = led["background"]
    assert bg["measured"] is True
    assert bg["injected"] is True
    assert bg["limit"] == 1100 and bg["used"] == 1033
    assert bg["kept"] == 5 and bg["truncated"] == 1 and bg["dropped"] == 0
    cut = [b for b in bg["blocks"] if b["status"] == "truncated"]
    assert len(cut) == 1
    assert cut[0]["before"] == 297 and cut[0]["tokens"] == 217
    # 297 → 217 裁掉 26.9%
    assert 26 < cut[0]["cut_pct"] < 28, f"裁剪比例算错了：{cut[0]['cut_pct']}"


def test_background_not_injected_is_not_zero_percent():
    """没注入背景材料 ≠ 预算用了 0%——两件事必须分开显示。

    `injected`（有没有材料参与）与 `measured`（预算/命运采到没有）是**两个**
    事实，实测四种组合都出现过：
      · 有材料 + 有预算  → 正常回合（#253）
      · 有材料 + 无预算  → 19:07~19:40 那批（有 bg_labels，没有 backgrounds）
      · 无材料 + 无预算  → 纯文本回合
    """
    # 有标签、没有预算数据：注入了，但用量没采到
    dg = _digest_partial()
    led = _build_prompt_ledger(digest=dg, prompt_chars=5607)
    bg = led["background"]
    assert bg["injected"] is True, "有背景标签却当成没注入"
    assert bg["measured"] is False, "没有预算数据却当成采到了"

    # 连标签都没有：确实没注入
    dg2 = {k: v for k, v in dg.items() if k != "user_bg_labels"}
    bg2 = _build_prompt_ledger(digest=dg2, prompt_chars=5607)["background"]
    assert bg2["injected"] is False, "没有任何背景材料却当成注入过"
    assert bg2["measured"] is False


def test_budget_usage_alone_is_not_a_warning():
    """**预算用满不是预警**——每轮都响的预警等于没有预警。

    `fit_backgrounds` 的设计就是按优先级把预算填满（实测连续两轮
    1033/1100、992/1100），第一版这里报了「≥90% → warn」，端到端一看
    就是噪音。真正的信号是**有没有东西被丢掉**。

    ## 阳性对照
    谁把「用量 ≥90%」那条预警加回来，`len(warnings)` 就不再是 1，本测试红。
    """
    dg = _digest_full()
    # 去掉「被裁」的那块 → 只剩「用量 94%」这一个事实
    dg["backgrounds"]["blocks"] = [
        b for b in dg["backgrounds"]["blocks"] if b["status"] != "truncated"]
    led = _build_prompt_ledger(digest=dg, prompt_chars=5204)
    assert led["background"]["used_pct"] > 90, "夹具应当是接近满的"
    assert led["warnings"] == [], (
        f"用量高本身不该报警，现在报了：{[w['key'] for w in led['warnings']]}")
    assert led["verdict"] == "ok"


def test_truncated_block_warns_and_mentions_budget():
    """有块被裁才是真信号；预算用量作为附注带在同一句里（信息不丢，但不单独吵）。"""
    led = _build_prompt_ledger(digest=_digest_full(), prompt_chars=5204)
    keys = [w["key"] for w in led["warnings"]]
    assert keys == ["bg_truncated"], f"应当只有「被裁」这一条预警，实际：{keys}"
    text = led["warnings"][0]["text"]
    assert "记忆" in text and "297" in text and "217" in text
    assert "1100" in text, "预算用量应当作为附注出现在同一句里"
    assert led["verdict"] == "warn"


def test_dropped_block_is_reported():
    """整块被淘汰必须报——她那一轮真的看不到这块材料。"""
    dg = _digest_full()
    dg["backgrounds"]["blocks"].append(
        {"label": "反思", "priority": 7, "tokens": 0,
         "tokens_before": 90, "status": "dropped", "preview": ""})
    led = _build_prompt_ledger(digest=dg, prompt_chars=5204)
    assert led["background"]["dropped"] == 1
    assert any(w["key"] == "bg_dropped" for w in led["warnings"])


def test_every_warning_has_evidence():
    """预警必须带**原始字段**——「你去核对这里」，不是「信我」。

    这条是主人「有效的预警，取代日志」的字面要求：日志的价值在于可核对。
    """
    led = _build_prompt_ledger(digest=_digest_full(), prompt_chars=5204)
    assert led["warnings"], "夹具应该触发预警"
    for w in led["warnings"]:
        assert w["level"] in ("warn", "error"), f"预警级别非法：{w['level']}"
        assert w["text"].strip(), "预警没有文案"
        assert w["evidence"].strip(), f"预警没带证据字段：{w['key']}"
        assert w["key"].strip()


def test_ledger_is_pure_function_on_garbage():
    """脏输入不许抛异常——回放是观察窗口，不能因为一个字段怪就整块塌。"""
    for bad in (
        {"token_sections": "not-a-dict"},
        {"token_sections": {"system": "abc"}},
        {"backgrounds": {"blocks": [None, 42, {"label": None}]}},
        {"backgrounds": {"budget": {"limit": "x", "used": None}}},
        {"memory_marks": [], "system_sections": None},
    ):
        led = _build_prompt_ledger(digest=bad, prompt_chars=0)
        assert led["verdict"] in VERDICTS


# ── 工具效果：分类 + 空手 + 决策理由 ──────────────────────────────

def test_tool_effects_field_set():
    eff = _build_tool_effects(_steps())
    assert set(eff) == {
        "items", "counts", "lookups", "empty_rate", "summary",
        "rationale", "warnings", "verdict", "note",
    }
    assert eff["verdict"] in VERDICTS
    assert len(eff["items"]) == 4


def test_tools_are_classified_by_kind():
    """同一个「返回很短」在不同类里意义相反，所以必须先分类。

    实测 119 次调用里：检索 74 次、决定 11 次、动作 11 次、感知 23 次。
    """
    eff = _build_tool_effects(_steps())
    kinds = {i["name"]: i["kind"] for i in eff["items"]}
    assert kinds["search_facts"] == "retrieval"
    assert kinds["search_memories"] == "retrieval"
    assert kinds["resolve_open_loop"] == "decision"
    assert kinds["set_reminder"] == "action"
    # 认不出的工具不能丢，也不能误分类
    assert _build_tool_effects([{"name": "brand_new_tool"}])["items"][0]["kind"] == "unknown"


def test_empty_retrieval_is_flagged():
    """检索返回「没查到」要标空手——这是「她不知道」的直接证据。"""
    eff = _build_tool_effects(_steps())
    by_name = {i["name"]: i for i in eff["items"]}
    assert by_name["search_memories"]["empty"] is True
    assert by_name["search_memories"]["verdict"] == "empty"
    assert by_name["search_facts"]["empty"] is False
    assert by_name["search_facts"]["verdict"] == "hit"
    assert eff["counts"]["empty"] == 1
    assert eff["lookups"] == 2
    assert 0.4 < eff["empty_rate"] < 0.6  # 2 次检索里 1 次空手


def test_decision_tool_short_result_is_not_empty():
    """**关键阳性对照**：决定类工具返回 9 个字是回执，不是「空手」。

    `skip_response` 返回「好的，这一轮不回复」、`resolve_open_loop` 返回
    「记下了」——按长度判会把实测 7 次正常的沉默决定全标成异常，
    假警报会让人把整个面板关掉（那比没有预警更糟）。
    """
    eff = _build_tool_effects([
        {"name": "skip_response",
         "args": '{"reason": "两个人在对话，我插话反而打断"}',
         "result": "好的，这一轮不回复", "ms": 0},
    ])
    item = eff["items"][0]
    assert item["chars"] < 20, "夹具应当极短"
    assert item["empty"] is False, "决定类短回执被误判成空手"
    assert item["verdict"] == "decision"
    assert eff["counts"]["empty"] == 0


def test_decision_rationale_is_extracted():
    """她的决策理由要从工具入参里取原文——这是「她凭什么这么决定」的唯一实证。"""
    eff = _build_tool_effects(_steps())
    assert "问明天复查的具体时间" in eff["rationale"]


def test_skip_response_reason_becomes_rationale():
    eff = _build_tool_effects([
        {"name": "skip_response",
         "args": '{"reason": "两个人在对话，我插话反而打断"}',
         "result": "好的，这一轮不回复", "ms": 0},
    ])
    assert eff["rationale"] == "两个人在对话，我插话反而打断"


def test_no_tools_is_skip_not_error():
    eff = _build_tool_effects([])
    assert eff["verdict"] == "skip"
    assert eff["items"] == []
    assert eff["warnings"] == []
    # 传 None 也不能炸
    assert _build_tool_effects(None)["verdict"] == "skip"


def test_tool_summary_is_a_conclusion_not_a_count():
    """汇总句要能直接读成结论（「查了 2 次，1 次拿到材料」），不是「调用了 4 个工具」。"""
    eff = _build_tool_effects(_steps())
    s = eff["summary"]
    assert "查了" in s and "次" in s, f"汇总句太机械：{s}"
    assert "4 个工具" not in s


# ── 接线：九段流程里的第 6 站要带上 effects ─────────────────────

def test_tools_phase_carries_effects():
    """第 6 站要带结构化 effects——「完整流程」和「她做了什么」用同一份数据，
    两处各算一遍必然漂移（教训 #38）。"""
    flow = _build_turn_flow(
        message=None, digest=_digest_full(), prompt_chars=5204,
        trace={"model": "m", "reasoning_chars": 10, "duration_ms": 100,
               "tools": _steps()},
        decision=None, feedback=None,
    )
    tools_phase = {p["key"]: p for p in flow}["tools"]
    eff = tools_phase["effects"]
    assert eff["counts"]["total"] == 4
    assert eff["items"][1]["empty"] is True
    # 站点的结论句来自 effects，不是自己再数一遍
    assert "查了" in tools_phase["did"]
    # 有工具、且有空手 → 该站应打折（warn）
    assert tools_phase["status"] == "warn"


# ── 老 trace（有标签、没逐块命运）不能自相矛盾 ──────────────────
#
# 2026-10-25 视觉验收实测发现的两个自相矛盾（`v2:private:…1792899595` 那轮）：
#   ① 第 4 站写「0 块进了 prompt」，同一站下面又列着 6 个注入过的标签；
#   ② 第 5 站 items 写「记忆块：已注入」，did 却写「记忆块没进 prompt」。
# 原因都是 `backgrounds.blocks` 当天才落库，老 trace 只有 `user_bg_labels`。
# **同一站内自相矛盾比信息少更糟**：运营者不知道该信哪一行。

def _build_flow_for_digest(digest, tools=None):
    return {p["key"]: p for p in _build_turn_flow(
        message=None, digest=digest, prompt_chars=5607,
        trace={"model": "m", "reasoning_chars": 10, "duration_ms": 100,
               "tools": tools or []},
        decision=None, feedback=None,
    )}


def _digest_labels_only():
    """老 trace 的样子：`user_bg_labels` 齐（7 个，与实测 #253 一致），
    但 `backgrounds.blocks` 完全没采到（逐块命运当天才落库）。"""
    dg = _digest_partial()
    dg["user_bg_labels"] = ["关系档案", "协议", "悬着的事", "窗口",
                            "缺口", "自忆", "记忆"]
    return dg


def test_inject_phase_counts_labels_when_blocks_missing():
    flow = _build_flow_for_digest(_digest_labels_only())
    inject = flow["inject"]
    # 7 个标签 → 不能说 0 块
    assert any("7 块" in str(i["v"]) for i in inject["items"]), inject["items"]
    assert "0 块进了 prompt" not in inject["did"], inject["did"]
    assert "按注入标签统计" in inject["did"], "用了标签兜底却没标口径"
    # 有注入但看不出每块命运 → 打折，不是 ok
    assert inject["status"] == "warn"
    # 被淘汰/预算这两项没采到时要明说，不能显示 0 / —
    assert any(i["k"] == "被淘汰" and "未采到" in str(i["v"]) for i in inject["items"])


def test_recall_phase_is_not_self_contradictory():
    """items 说「已注入」，did 就不能说「没进 prompt」。"""
    flow = _build_flow_for_digest(_digest_labels_only())
    recall = flow["recall"]
    did = recall["did"]
    assert "没进 prompt" not in did, f"与 items 矛盾：{did}"
    assert "进了 prompt" in did, did
    assert "没采到" in did, "应当说明「看不出有没有被裁」"
    # items 与 did 必须一致
    block_item = next(i for i in recall["items"] if i["k"] == "记忆块")
    assert "已注入" in str(block_item["v"]), block_item
