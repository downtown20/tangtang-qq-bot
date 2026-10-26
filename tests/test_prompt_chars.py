"""提示词字符数口径（2026-10-23 T65）。

起因：台账 T1 §五.3 的遗留——`sum(len(m.get("content","")) for m in messages)`
在 content 是**数组**时返回**元素个数**而不是字符数。识图改造（T1）之后
多模态消息正是数组形态，于是那条错误日志里的 `prompt=Nchars` 会小得离谱。

这个数字**只进日志与异常串**（报错路径），不是计费口径——所以这里不接
tokenizer，只要求「别把元素个数当字符数」。
"""
from __future__ import annotations

from agent.handler import _prompt_chars


def test_plain_string_content():
    msgs = [{"role": "system", "content": "你是糖糖"},
            {"role": "user", "content": "你好呀"}]
    assert _prompt_chars(msgs) == len("你是糖糖") + len("你好呀")


def test_multimodal_content_counts_text_not_elements():
    """多模态数组：只数 text 段，图片段不计（base64 不是提示词文本）。"""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    assert _prompt_chars(msgs) == len("这张图里是什么？")


def test_mixed_shapes_in_one_payload():
    msgs = [
        {"role": "system", "content": "规则" * 10},
        {"role": "user", "content": [{"type": "text", "text": "看图" * 5}]},
        {"role": "assistant", "content": "好的"},
    ]
    assert _prompt_chars(msgs) == 20 + 10 + 2


def test_tolerates_junk():
    """报错路径上的函数不许因为畸形 payload 自己再抛一次（那会把真错误盖掉）。"""
    assert _prompt_chars(None) == 0
    assert _prompt_chars([]) == 0
    assert _prompt_chars([None, 42, "x"]) == 0
    assert _prompt_chars([{"role": "user"}]) == 0
    assert _prompt_chars([{"content": None}]) == 0
    assert _prompt_chars([{"content": [None, {"no_text": 1}, "裸串"]}]) == len("裸串")


def test_old_naive_formula_really_was_wrong():
    """**阳性对照**：证明这个 helper 不是多余包装。

    如果哪天有人把实现改回 `sum(len(m.get("content","")))`，上面几条会红；
    而这条证明「旧写法在真实形态下确实算错」——它是这个改动的理由本身。
    """
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "很长很长的一段话" * 20},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    naive = sum(len(m.get("content", "")) for m in msgs)
    assert naive == 2, "旧写法的失真形态变了，请重新核对这条对照的前提"
    assert _prompt_chars(msgs) == len("很长很长的一段话" * 20)
    assert _prompt_chars(msgs) > naive * 10, (
        "新口径没有比旧写法更接近真实字符数——那这个改动没有意义")
