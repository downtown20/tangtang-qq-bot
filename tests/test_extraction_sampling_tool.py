# -*- coding: utf-8 -*-
"""闸门：`tools/提取抽样.py` 这把尺子必须有牙齿（2026-10-25 第 12 轮）。

## 这里钉的是什么

第 12 轮查 open_loops 为什么 0 行时发现：**L2 的验收工具从诞生起每次都在撒谎**。
它的 llm_call 复用了没有关思考的 `make_llm_call`，推理模型返回空串 → 提取器记成
`invalid_json` → 工具却打印「（这一批没有识别出未闭合事件）」并汇总「合计 0 条」、
退出码 0。于是「调用失败」和「确实没有未结事」在输出里完全一样。

三条闸门，对应三种失效方式：

1. `judge()` —— 失败批次必须判负（全失败/部分失败都不许返回 ok）：
   只测「跑起来没报错」等于没测（教训 #30/#37）。
2. `check_cases()` —— 阳性对照：必提样本看不见、或对照组被误判，都必须判负。
   没有阳性对照的判据永远绿（教训 #37）。
3. AST 闸门 `test_tool_llm_call_disables_thinking` —— 请求体里必须真的带
   `thinking: disabled`。这是本轮缺陷的**原始成因**，靠人眼守不住。
"""
from __future__ import annotations

import ast
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "提取抽样.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("sampling_tool_ut", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _ok_batch(loops=None, total=2, protocol_ok=True, outcome="success_with_items"):
    return {"qq": "1", "nickname": "甲", "total": total, "loops": loops or [],
            "stats": {"protocol_ok": protocol_ok, "outcome": outcome,
                      "raw": total, "valid": total, "rejected": 0,
                      "raw_length": 100},
            "messages": []}


def _dead_batch():
    return {"qq": "2", "nickname": "乙", "total": 0, "loops": [],
            "stats": {"protocol_ok": False, "outcome": "invalid_json",
                      "raw": 0, "valid": 0, "rejected": 0, "raw_length": 0},
            "messages": []}


# ══════════════════════════════════════════════════════════════════
# 1. judge()：失败不许伪装成"没有未闭合事件"
# ══════════════════════════════════════════════════════════════════
class TestJudge:
    def test_all_batches_failed_is_not_a_pass(self, tool):
        """旧实现就在这里撒谎：全失败 → 打印"合计 0 条" → 退出码 0。"""
        verdict = tool.judge([_dead_batch(), _dead_batch()])
        assert verdict["ok"] is False, "全批失败被当成了通过"
        assert verdict["exit_code"] == 2
        assert verdict["failed"] == 2
        assert "尺子失效" in verdict["message"]

    def test_partial_failure_is_not_a_pass(self, tool):
        """有一批没拿到回复，合计数字就不完整——可能正好漏掉那一条。"""
        verdict = tool.judge([_ok_batch(), _dead_batch()])
        assert verdict["ok"] is False
        assert verdict["exit_code"] == 2
        assert verdict["failed"] == 1

    def test_clean_batches_with_zero_loops_is_a_legitimate_pass(self, tool):
        """全成功 + 0 条未结事 = 合法结论，不是失败。三档的区别就在这。"""
        verdict = tool.judge([_ok_batch(), _ok_batch()])
        assert verdict["ok"] is True
        assert verdict["exit_code"] == 0
        assert verdict["loops"] == 0

    def test_empty_result_set_is_not_a_pass(self, tool):
        """0 批 ≠ 0 条未结事：一个批次都没跑，不能报"通过"。

        断言消息措辞而不是只断言 ok/exit_code——否则"走全失败分支"和
        "走空集分支"的变异行为等价，变异对照会判成假变异（实测过）。
        """
        verdict = tool.judge([])
        assert verdict["ok"] is False
        assert verdict["exit_code"] == 2
        assert "没有抽到任何批次" in verdict["message"], verdict["message"]

    def test_loops_are_counted_across_batches(self, tool):
        verdict = tool.judge([_ok_batch(loops=[{"value": "a"}], total=3),
                              _ok_batch(loops=[{"value": "b"}, {"value": "c"}], total=4)])
        assert verdict["ok"] is True
        assert verdict["loops"] == 3
        assert verdict["items"] == 7


# ══════════════════════════════════════════════════════════════════
# 2. check_cases()：阳性对照必须能红
# ══════════════════════════════════════════════════════════════════
def _loop(*evidence_ids):
    return {"type": "open_loop", "value": "某件没结的事",
            "evidence_ids": list(evidence_ids)}


class TestPositiveControl:
    def test_all_groups_hit_passes(self, tool):
        """构造组 + 真实组都命中，才算这把尺子有效（第 14 轮加了真实数据组）。"""
        verdict = tool.check_cases([
            _loop(101), _loop(102), _loop(103), _loop(104),
            _loop(107), _loop(108), _loop(109),
        ])
        assert verdict["ok"] is True
        assert verdict["exit_code"] == 0
        assert verdict["hit"] == [101, 102, 103, 104]
        assert verdict["real_hit"] == [107, 108, 109]

    def test_missing_expected_hits_fails(self, tool):
        """尺子看不见 open_loop 时必须红——否则它量出来的 0 毫无意义。"""
        verdict = tool.check_cases([{"type": "event", "value": "明天要去面试",
                                     "evidence_ids": [101]}])
        assert verdict["ok"] is False
        assert verdict["exit_code"] == 2
        assert "只命中" in verdict["message"]

    def test_textbook_only_hits_fail(self, tool):
        """构造句全中、真实句全漏 = 尺子只在教科书上有效。

        2026-10-25 第 14 轮的实测形态：构造句 4/4、真实数据句 0/3——
        提取器把「明天去学校拿录取通知书」判成了 event。
        """
        verdict = tool.check_cases([
            _loop(101), _loop(102), _loop(103), _loop(104),
        ])
        assert verdict["ok"] is False
        assert verdict["exit_code"] == 2
        assert verdict["real_hit"] == []
        assert "真实数据句" in verdict["message"]

    def test_control_group_leak_fails(self, tool):
        """把"面试完了"判成未结的事，属于最坏的一类误判（追着问已收尾的事）。"""
        verdict = tool.check_cases([
            _loop(101), _loop(102), _loop(103), _loop(105),
            _loop(107), _loop(108),
        ])
        assert verdict["ok"] is False
        assert verdict["exit_code"] == 2
        assert verdict["leaked"] == [105]
        assert "对照组被误判" in verdict["message"]

    def test_empty_items_fail(self, tool):
        """模型什么都没输出（含调用失败）——不能算通过。"""
        assert tool.check_cases([])["ok"] is False

    def test_min_hit_threshold_is_three(self, tool):
        """构造组 4 条里命中 3 条算过（留 1 条波动），命中 2 条不算。"""
        two = tool.check_cases([_loop(101), _loop(102), _loop(107), _loop(108)])
        three = tool.check_cases([_loop(101), _loop(102), _loop(103),
                                  _loop(107), _loop(108)])
        assert two["ok"] is False
        assert three["ok"] is True

    def test_real_group_needs_two_of_three(self, tool):
        """真实组 3 条里至少 2 条——1 条也可以是蒙的，0 条说明分类没生效。"""
        one = tool.check_cases([_loop(101), _loop(102), _loop(103),
                                _loop(107)])
        two = tool.check_cases([_loop(101), _loop(102), _loop(103),
                                _loop(107), _loop(108)])
        assert one["ok"] is False
        assert two["ok"] is True


# ══════════════════════════════════════════════════════════════════
# 3. 请求口径：必须关思考（本轮缺陷的原始成因）
# ══════════════════════════════════════════════════════════════════
def _dict_keys(node) -> list[str]:
    keys = []
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.append(k.value)
    return keys


def test_tool_llm_call_disables_thinking(tool):
    """AST 断言：请求体里必须含 `thinking: {type: disabled}`。

    为什么用 AST 而不是字符串包含：读源码的人会看到注释写着"关思考"就以为真的关了，
    而这一轮的实际缺陷正是「注释/文档说关了，代码没关」。只有结构断言拦得住。
    """
    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "build_payload" in funcs, "工具里找不到 build_payload"
    fn = funcs["build_payload"]
    payloads = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)
                and "thinking" in _dict_keys(n)]
    assert payloads, "build_payload 的请求体里没有 thinking 参数——推理模型会返回空"
    values = []
    for payload in payloads:
        for key, value in zip(payload.keys, payload.values):
            if isinstance(key, ast.Constant) and key.value == "thinking":
                values.append(ast.unparse(value))
    assert any("disabled" in v for v in values), f"thinking 不是 disabled：{values}"


def test_make_llm_call_actually_uses_build_payload(tool):
    """光有 build_payload 不算数——请求必须真的走它，不能自己再拼一份。

    教训 #38：同一个物理量有两个换算点，只改了其中一个。请求体也一样，
    留一条"自己拼 payload"的路，闸门就只管住了没人用的那一份。
    """
    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "make_llm_call" in funcs
    calls = [n for n in ast.walk(funcs["make_llm_call"])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "build_payload"]
    assert calls, "make_llm_call 没有调用 build_payload——请求体是另一份手拼的"
    inline_dicts = [n for n in ast.walk(funcs["make_llm_call"])
                    if isinstance(n, ast.Dict) and "model" in _dict_keys(n)]
    assert not inline_dicts, "make_llm_call 里还在手拼请求体（两把尺子必然漂移）"


class TestRequestBudget:
    """预算必须来自 config.yaml，不许写死（第一版写死 900 → 长批次假红）。"""

    def test_reads_config_value(self, tool):
        assert tool.resolve_max_tokens({"max_tokens": 64000}) == 64000

    def test_default_matches_handler(self, tool):
        assert tool.resolve_max_tokens({}) == 512

    def test_bad_values_fall_back(self, tool):
        assert tool.resolve_max_tokens({"max_tokens": "abc"}) == 512
        assert tool.resolve_max_tokens({"max_tokens": -1}) == 512
        assert tool.resolve_max_tokens({"max_tokens": 0}) == 512

    def test_payload_carries_budget(self, tool):
        payload = tool.build_payload("s", "u", model="m", max_tokens=64000)
        assert payload["max_tokens"] == 64000
        assert payload["thinking"] == {"type": "disabled"}


def test_tool_does_not_reuse_legacy_llm_call(tool):
    """不许回退到 REBIRTH.make_llm_call——那条路没有 thinking 参数。"""
    source = TOOL_PATH.read_text(encoding="utf-8")
    body = source.split("def make_llm_call", 1)[1].split("def judge", 1)[0]
    assert "REBIRTH.make_llm_call(" not in body, "又回去用没关思考的那条调用了"


# ══════════════════════════════════════════════════════════════════
# 4. 输出层：失败批次必须显眼（不能长得像"没有未闭合事件"）
# ══════════════════════════════════════════════════════════════════
def test_render_marks_failed_batch_loudly(tool):
    buf = io.StringIO()
    with redirect_stdout(buf):
        tool.render(_dead_batch())
    text = buf.getvalue()
    assert "⚠" in text and "没拿到有效回复" in text, text
    assert "invalid_json" in text, "必须把 outcome 打出来，否则无法定位"


def test_render_does_not_flag_clean_batch(tool):
    buf = io.StringIO()
    with redirect_stdout(buf):
        tool.render(_ok_batch())
    assert "⚠" not in buf.getvalue()
