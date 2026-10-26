"""提取 prompt 的「将来事 → open_loop」判据闸门（2026-10-25 第 14 轮）。

为什么需要这一层：第 13 轮把 L3 注入 / L4 收尾 / L5 过期全接好了，但第 14 轮取证
发现提取器**几乎不产出 open_loop**——1791 个提取批次里，含将来时词的 158 条只有
**2 条**被判成 open_loop，**116 条**落进了 `event`（「明天去学校拿录取通知书」→ event、
「明天10点有事，得有人叫醒我」→ event）。链路接在一条没有原料的管道上，等于没跑
（第 12 轮「接在死方法上」的近亲：那次错在调用点不可达，这次错在数据不产出）。

本文件的断言**不看源码字符串，而是抓真实发给 LLM 的 system prompt**——
教训 #37：断言行为，别断言注释（注释里写「已教它 open_loop」不构成证据）。
"""

import asyncio

from agent.memory import MemorySystem


def _capture_prompt(store) -> dict:
    """跑一次提取（LLM 被 fake 掉），把真正发给模型的 system prompt 抓出来。"""
    captured = {}

    async def fake_llm(system_prompt, user_message):
        captured["system"] = system_prompt
        captured["user"] = user_message
        return "[]"

    memory = MemorySystem(store=store)
    messages = [{"id": 1, "message": "我明天要去面试",
                 "timestamp": "2026-10-25 10:00", "is_bot_reply": False}]
    asyncio.run(memory.extract_semantic_memories(
        messages=messages, nickname="闸门", qq_id="10001", llm_call=fake_llm,
    ))
    return captured


def _event_definition_line(system_prompt: str) -> str:
    """把 prompt 里 `- event:` 那一行取出来——判据要落在这一行上，不是全文任意位置。"""
    for line in system_prompt.splitlines():
        if line.strip().startswith("- event:"):
            return line
    return ""


def test_prompt_tells_llm_unfinished_things_go_to_open_loop(store):
    """判据句必须在 prompt 里，且必须是**可执行的那一句**（含箭头指向 open_loop）。

    ⚠ 只断「还没发生」四个字会被别处的措辞蒙混过去（第一版就是这么假绿的）——
    断言必须唯一到「还没发生 → open_loop」这个判定本身。
    """
    system_prompt = _capture_prompt(store)["system"]
    assert "还没发生 → open_loop" in system_prompt


def test_event_definition_is_limited_to_happened_things(store):
    """event 的定义行必须写明「已经发生」——否则 LLM 会把将来的事当普通事件。

    判据落在 `- event:` 那一行上：全文别处出现「已经发生」不算数
    （open_loop 段落里也有这个词，第一版就是这么假绿的）。
    """
    system_prompt = _capture_prompt(store)["system"]
    line = _event_definition_line(system_prompt)
    assert line, "prompt 里必须有 `- event:` 类型定义行"
    assert "已经发生" in line


def test_prompt_demands_one_row_per_event(store):
    """同一件事只输出一条——第 13 轮端到端实证：一场复查被拆成两行，她收了两次尾。"""
    system_prompt = _capture_prompt(store)["system"]
    assert "同一件事只输出一条" in system_prompt


def test_prompt_carries_real_data_example_not_only_textbook(store):
    """真实数据里被错判成 event 的原句必须出现在 prompt 里当正例。

    只写「明天要去面试」这种教科书句不够——2026-10-25 实测：构造句 4/4 命中，
    真实句 0/3 命中。尺子必须能看见真实数据那种写法。
    """
    system_prompt = _capture_prompt(store)["system"]
    assert "明天去学校拿录取通知书" in system_prompt


def test_prompt_keeps_completed_event_as_negative_control(store):
    """强调 open_loop 的同时不能丢掉反向判据：已经做完的事仍是 event。"""
    system_prompt = _capture_prompt(store)["system"]
    assert "面试完了" in system_prompt
