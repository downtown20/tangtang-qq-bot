"""L2：未闭合事件（open_loops）的**提取识别**闸门。

## 这一批接的是什么

L1 已落地 `open_loops` 表和四个读写方法（`tests/test_open_loops.py` 19 条闸门），
但**没有任何东西往里写**——零调用闸门当时按「L2/L3/L4/L5 待接线」登记。

L2 的落点（方案 2.2）：**不新增 LLM 调用**，在既有语义提取的同一次输出里加一类
`type="open_loop"`。写侧三条职责：

1. 提取 prompt 必须告诉 LLM「什么算没结的事」——含判定边界（已完成不算、
   糖糖自己的承诺不算、无时间指向的空泛愿望不算）；
2. 提取器的后置校验必须把 open_loop 专有字段（`kind` / `expected_at`）透传出去，
   否则 LLM 就算识别对了，落库时也拿不到「什么时候到期」；
3. 落库必须走到 `Store.add_open_loop`，且**同时**留一条可检索的记忆——
   open_loop 回答「哪件事没结」，记忆回答「她什么时候说过这话」。

## 为什么这些闸门非有不可

- 少 (1)：LLM 会把「面试完了」也当没结的事 → 她追着问已完成的事（骚扰机器）。
- 少 (2)：`kind`/`expected_at` 在校验器里被丢 → 全部退化成「不晓得什么时候」，
  「到期优先」的排序（L3 注入）永远命中不到。
- 少 (3)：只写 open_loops 不写记忆 → 她记得「这事悬着」但说不出细节与出处。
"""

import asyncio
import json

import pytest

from agent.handler import MessageHandler
from agent.memory import MemorySystem


class _FakeStore:
    """只实现本批用到的两个接口，全部记录调用参数供断言。"""

    def __init__(self):
        self.open_loops = []
        self.memory_by_value = {}

    def add_open_loop(self, qq_id, topic, *, kind="plan", expected_at="",
                      source_memory_id=0, source_group_id="", today=""):
        self.open_loops.append({
            "qq_id": qq_id, "topic": topic, "kind": kind,
            "expected_at": expected_at, "source_memory_id": source_memory_id,
            "source_group_id": source_group_id,
        })
        return len(self.open_loops)

    def find_memory_by_value(self, qq_id, value):
        return self.memory_by_value.get((str(qq_id), str(value)))


class _FakeMemory:
    def __init__(self, store):
        self.store = store
        self.remembered = []

    def _deduped_remember(self, qq_id, key, value, **kwargs):
        self.remembered.append({"qq_id": qq_id, "key": key, "value": value, **kwargs})

    # 反例保护：open_loop 条目不该走 alias 通道
    def add_alias(self, *args, **kwargs):  # pragma: no cover - 触到就是错
        raise AssertionError("open_loop 不应该进 aliases")


def _bare_memory() -> MemorySystem:
    obj = MemorySystem.__new__(MemorySystem)
    obj.short_term = {}
    obj.short_term_size = 50
    obj._msg_seq = 0
    obj.store = _FakeStore()
    return obj


def _messages():
    return [
        {"id": 11, "message": "我明天要去面试", "timestamp": "2026-10-24 21:00",
         "is_bot_reply": False, "group_id": ""},
        {"id": 12, "message": "面试有点慌", "timestamp": "2026-10-24 21:01",
         "is_bot_reply": False, "group_id": ""},
    ]


def _run_extract(memory, llm_items, captured=None):
    async def llm_call(system_prompt, user_message):
        if captured is not None:
            captured["system_prompt"] = system_prompt
            captured["user_message"] = user_message
        return json.dumps(llm_items, ensure_ascii=False)

    return asyncio.run(memory.extract_semantic_memories(
        messages=_messages(), nickname="小明", qq_id="u1", llm_call=llm_call,
    ))


OPEN_LOOP_ITEM = {
    "type": "open_loop",
    "kind": "plan",
    "value": "明天要去面试",
    "expected_at": "2026-10-26",
    "cognitive": "episodic",
    "importance": 6,
    "confidence": 0.9,
    "claim_type": "stated",
    "evidence_quote": "我明天要去面试",
    "evidence_ids": [11],
}


# ── 1. prompt 必须定义「什么算没结的事」 ──────────────────


def test_prompt_declares_open_loop_type():
    captured = {}
    _run_extract(_bare_memory(), [], captured)
    system_prompt = captured["system_prompt"]
    # 断言「类型定义行」，不是任意出现——示例 JSON 里也有 open_loop 字样，
    # 只查子串会让「类型定义被删」这种变异活下来（教训 #37 假绿）。
    assert "- open_loop:" in system_prompt, "提取 prompt 没声明 open_loop 类型——LLM 不会产出它"


def _open_loop_section(system_prompt: str) -> str:
    """取出 prompt 里 `- open_loop:` 那一段（到「认知类型」为止）。

    为什么不用「从第一个 open_loop 起 +700 字符」：那是固定窗口，
    2026-10-25 第 14 轮给这一段加了判据句和 4 条真实正例（约 +300 字符），
    「已完成的不算」被挤出窗口 → 测试红，**而 prompt 的行为其实是变好了**。
    固定窗口会把「内容变多」误报成「内容丢失」（教训 #37 同族：判据测的不是行为）。
    """
    start = system_prompt.find("- open_loop:")
    if start < 0:
        return ""
    end = system_prompt.find("认知类型", start)
    if end < 0:
        end = start + 4000
    return system_prompt[start:end]


def test_prompt_has_exclusion_rules_for_open_loop():
    """边界必须写进 prompt：已完成 / 糖糖自己的承诺 / 无时间指向的空泛愿望。"""
    captured = {}
    _run_extract(_bare_memory(), [], captured)
    segment = _open_loop_section(captured["system_prompt"])
    assert segment, "提取 prompt 里找不到 `- open_loop:` 段"
    assert "完成" in segment, "没写「已完成的不算」——会把收了尾的事当没结"
    assert "糖糖" in segment, "没写「糖糖自己的承诺不算」——那条路有 remember_self"
    assert "愿望" in segment or "环游世界" in segment, "没写「空泛愿望不算」"


def test_prompt_asks_for_kind_and_expected_at():
    captured = {}
    _run_extract(_bare_memory(), [], captured)
    segment = _open_loop_section(captured["system_prompt"])
    assert "expected_at" in segment
    assert "kind" in segment


# ── 2. 校验器必须透传专有字段 ────────────────────────────


def test_validator_keeps_kind_and_expected_at():
    items, stats = _run_extract(_bare_memory(), [OPEN_LOOP_ITEM])
    assert stats["valid"] == 1, f"open_loop 条目被后置校验杀了：{stats}"
    got = items[0]
    assert got["type"] == "open_loop"
    assert got["kind"] == "plan"
    assert got["expected_at"] == "2026-10-26"
    assert got["value"] == "明天要去面试"


def test_validator_does_not_attach_fields_to_regular_items():
    """普通记忆不该因为这次改动多出 kind/expected_at（结构污染）。"""
    regular = {
        "type": "preference", "value": "偏爱美式咖啡不加糖", "importance": 4,
        "confidence": 0.9, "claim_type": "stated",
        "evidence_quote": "我每天都要喝咖啡", "evidence_ids": [12],
        "cognitive": "semantic",
    }
    items, _ = _run_extract(_bare_memory(), [regular])
    assert items and items[0]["type"] == "preference"
    assert "kind" not in items[0] and "expected_at" not in items[0]


# ── 3. 落库必须真的走到 Store.add_open_loop ──────────────


def _persist(items):
    store = _FakeStore()
    memory = _FakeMemory(store)
    # 记忆落完后，store 里能按 value 查到 id——模拟真实 add 后的回查
    for item in items:
        store.memory_by_value[("u1", item["value"])] = {"id": 777}
    fake_handler = type("FakeHandler", (), {})()
    fake_handler.memory = memory
    fake_handler.embed_engine = None
    count = MessageHandler._persist_extracted_items(fake_handler, "u1", items, origin="extracted")
    return count, store, memory


def test_open_loop_item_creates_row_and_keeps_memory():
    count, store, memory = _persist([dict(OPEN_LOOP_ITEM, **{"type": "open_loop"})])
    assert count == 1
    assert len(store.open_loops) == 1, "open_loop 条目没有落到 open_loops 表"
    row = store.open_loops[0]
    assert row["qq_id"] == "u1"
    assert row["topic"] == "明天要去面试"
    assert row["kind"] == "plan"
    assert row["expected_at"] == "2026-10-26"
    assert row["source_memory_id"] == 777, "source_memory_id 没指回那条记忆（丢证据链）"
    # 记忆也要留一条：open_loop 回答「哪件没结」，记忆回答「她什么时候说过」
    assert memory.remembered, "open_loop 条目没有留下可检索的记忆"


def test_regular_items_still_go_to_memory_only():
    regular = {
        "type": "preference", "value": "喜欢喝咖啡", "importance": 4,
        "confidence": 0.9, "claim_type": "stated",
        "evidence_quote": "我每天都要喝咖啡", "evidence_ids": [12],
        "cognitive": "semantic",
    }
    count, store, memory = _persist([regular])
    assert count == 1
    assert store.open_loops == [], "普通记忆被误写进 open_loops"
    assert len(memory.remembered) == 1


def test_low_confidence_open_loop_is_rejected():
    """置信度 < 0.5 的 open_loop 条目不该建行——宁缺毋滥，防止她追着问猜测的事。"""
    weak = dict(OPEN_LOOP_ITEM)
    weak["confidence"] = 0.3
    count, store, _ = _persist([weak])
    assert count == 0
    assert store.open_loops == []


def test_open_loop_without_topic_is_rejected():
    empty = dict(OPEN_LOOP_ITEM)
    empty["value"] = "   "
    count, store, _ = _persist([empty])
    assert count == 0
    assert store.open_loops == []
