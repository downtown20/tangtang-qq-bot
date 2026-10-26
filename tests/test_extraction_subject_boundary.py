"""2026-10-25 第七轮自查：第三人称转述被记成本人事实

## 事故现场

`mem#18121`（属于主人 2524950083）：

> 「今年7月刚靠指标生名额考上一中，是高一新生」
> trust=verified conf=0.95，证据 `chat#199048`

而 `chat#199048` 的原话是：

> 「帮我修正一下「穷到吃外卖」的记忆：他说的「33岁上班族」是开玩笑的，不是事实。
> **他**实际是今年7月刚靠指标生名额考上一中的高一新生，8月19号要去军训。
> 用 correct_memory 工具修正一下。」

**主人在让糖糖去修正另一个人的记忆**（「穷到吃外卖」= 2377255095），
句中「他」指的是那个人。同一时刻 `mem#17485` 也正确地写进了 **2377255095**
名下（`origin=corrected`）。

**但第二天提取器把这句话提取成了主人自己的身份记忆**，而且因为它有逐字证据
（原文确实这么说过），信任校验完全通过，直接 verified。

## 根因

提取 prompt 的 🚫 清单里有「引用转述——『他说』『XX说』」，覆盖的是
**引用别人的话**；但不覆盖 **「主语是第三方的描述」**——「他实际是高一新生」
不是引用，是用户对第三方的陈述。

两者差别：
- 「他说他很累」→ 引用（旧边界覆盖）
- 「他实际是高一新生」→ 关于别人的陈述（**旧边界没覆盖**）

## 本轮验证方式（行为级，不是文本级）

改 prompt 前后各跑一次**真实提取**（同模型、同输入 = `chat#199048` 原文）：

| | 提取结果 |
|---|---|
| 改前 | `[identity] 今年7月刚靠指标生名额考上一中，是高一新生` + `[open_loop] 8月19号要去军训` |
| **改后** | **`[]`（空数组）** |

反例（确认没有误伤）：输入「我老婆叫米雪儿，她是我最重要的人。」
- 改后仍提取 `[relationship] 老婆叫米雪儿` + `[note] 认为老婆米雪儿是自己最重要的人` ✅

**判据是「看主语，不看句子里有没有他/她」**——这条必须写进 prompt，
否则以后有人为了「减少误提取」把规则简化成「含他/她就跳过」，
会把「我老婆叫米雪儿」这类正常自述一起杀掉。
"""
import asyncio
import pytest

BOUNDARY_MARKERS = ("关于别人的陈述", "主语是第三方", "张冠李戴")
COUNTER_EXAMPLE = "我老婆叫米雪儿"


def _capture_system_prompt(store) -> str:
    """跑一次（假 LLM）提取，把 system prompt 截下来。"""
    from agent.memory import MemorySystem
    captured = {}

    async def fake_llm(system_prompt, user_message):
        captured["system"] = system_prompt
        captured["user"] = user_message
        return "[]"

    mem = MemorySystem(store=store)
    asyncio.run(mem.extract_semantic_memories(
        [{"id": 1, "message": "我今天很累", "timestamp": "2026-10-25 08:00:00",
          "is_bot_reply": False}],
        "测试昵称", "10000", fake_llm,
    ))
    return captured.get("system", "")


class TestThirdPersonBoundary:
    def test_prompt_has_third_person_boundary(self, store):
        """prompt 必须写明「主语是第三方不是自述」——这是本轮的核心修复"""
        sys_p = _capture_system_prompt(store)
        missing = [m for m in BOUNDARY_MARKERS if m not in sys_p]
        assert not missing, (
            f"提取 prompt 缺少第三人称边界标记 {missing}——"
            "mem#18121 那种「帮别人修正记忆，结果记到自己头上」会复发")

    def test_boundary_says_judge_by_subject_not_by_pronoun(self, store):
        """边界必须写清「看主语」，并给出反例——防后人简化成「含他她就跳过」

        这是一条**防过度矫正**的闸门：`我老婆叫米雪儿` 是自述，
        句子里有「她」也不该被毙。
        """
        sys_p = _capture_system_prompt(store)
        assert COUNTER_EXAMPLE in sys_p, (
            "prompt 里没有「我老婆叫米雪儿」这个正例——"
            "规则很容易被简化成「句子里有他/她就不提取」，那会误杀正常自述")
        assert "看主语" in sys_p or "主语是「我」" in sys_p, (
            "prompt 没写「判据看主语」，只写「有第三方」的话，"
            "提取器可能把所有提过别人的自述都丢掉")

    def test_existing_boundaries_not_removed(self, store):
        """回归：原有边界不能被这次改动顶掉（问句/玩笑/角色扮演/引用转述）"""
        sys_p = _capture_system_prompt(store)
        for marker in ("问句", "反问", "角色扮演", "玩笑", "引用转述", "自贬"):
            assert marker in sys_p, f"原有边界「{marker}」不见了"


class TestSubjectBoundaryDocs:
    """把行为验证的结论钉在测试里，避免以后有人以为这条规则没用。"""

    def test_behavior_evidence_recorded(self):
        """行为验证是 2026-10-25 实跑的（不该每次测试都调 API）：

        输入 `chat#199048` 原文（删除线内容为「帮别人修正记忆」那条），
        - 改前：提取出 `[identity] 今年7月刚靠指标生名额考上一中，是高一新生`
        - 改后：`[]`
        反例「我老婆叫米雪儿…」改后仍提取 2 条。

        这个测试只断言 `mem#18121` 这条真实数据仍在库里的位置（防止有人
        以为它被删了）——**它没有被删**，本轮只防未来，存量是一条活案例。
        """
        import sqlite3
        from pathlib import Path
        db = Path(__file__).resolve().parents[1] / "memory.db"
        if not db.is_file():
            pytest.skip("没有生产库（CI 环境）")
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT qq_id, value FROM memories WHERE id=18121").fetchone()
        except sqlite3.Error:
            pytest.skip("库结构不同")
        finally:
            conn.close()
        if row is None:
            pytest.skip("mem#18121 已不在（可能被清理）")
        assert str(row[0]) == "2524950083"
        assert "高一" in str(row[1])
