"""私聊分条端到端：从 LLM 原始输出到 QQ 气泡（2026-09-22）。

## 为什么是「两半」而不是一个大用例

私聊链路是 `handle_private_message`（约 600 行的巨型函数）→ `_enrich_split_reply`
→ `_checked_send` → `ReplyPipeline.send`。把整个 handle_private_message 拖进测试
成本极高，所以这里拆成两段**都调用真实生产代码**的用例：

  A. `_enrich_split_reply`（拆条 + 逐段 enrich）——证明**不泄漏**
  B. `_checked_send` + 真 `ReplyPipeline.send`——证明**真的发 N 条**

两半之间唯一的空隙是 handle_private_message 里的两行接线，那由
`tests/test_integration_contract.py` 的 AST 闸门钉住。三者合起来才是闭环。

## 阳性对照（本文件的价值全在这）

教训 #30：**安全检查必须做阳性对照**，只测「跑起来没报错」等于没测。
每个用例的 docstring 里写了「删掉哪一行会让它变红」，并且那几行都**实际跑过**，
结果记在 2026-09-22 的提交说明里。
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline  # noqa: E402
from onebot.ws_client import SendResult  # noqa: E402

# 今天的真实素材（chat_log id=250619，2026-09-22 01:51:56）：糖糖自己说
# 「我压根分不了，我这边敲完就是一大坨，空行是我自己硬凑的」——那条回复本身
# 就是 5 段挤在一个气泡里。这里取它做回放样本，因为它是这次改动的起因。
TODAYS_REPLY = (
    "哦——原来是换行，不是分条[分段]"
    "那你看，这就露馅了：我压根分不了，我这边敲完就是一大坨[分段]"
    "所以你刚才说「顺眼点」，是我自欺欺人喵[分段]"
    "那以后我只说短的，一句是一句[分段]"
    "……行了吧，别盯我了，困。"
)


class FakeNapcat:
    def __init__(self):
        self.private_calls = []
        self.group_calls = []

    async def send_private_message(self, target, content, group_id=""):
        self.private_calls.append(content)
        return SendResult(True, True, message_id=len(self.private_calls))

    async def send_group_message(self, target, content):
        self.group_calls.append(content)
        return SendResult(True, True, message_id=len(self.group_calls))


class FakeStickers:
    """只认「开心」一个关键词——用来证明 [贴图:xx] 是被**解析**掉的，不是被删掉的。"""

    def match_by_emotion_text(self, keyword, embed_engine=None, count=1,
                              excluded=frozenset()):
        if keyword.strip() == "开心":
            return ["[CQ:image,file=0123456789abcdef0123456789abcdef]"]
        return []


def _real_pipeline(nc):
    return ReplyPipeline(
        napcat=nc, stickers=FakeStickers(), store=None,
        short_term={}, bot_nicknames=["糖糖"], bot_qq="10000",
    )


def _handler(reply_pipeline, nc):
    """构造一个只够跑 _enrich_split_reply / _checked_send 的 MessageHandler。"""
    from agent.handler import MessageHandler
    from agent.self_check import ReplySelfCheck

    handler = object.__new__(MessageHandler)
    handler.self_check = ReplySelfCheck()
    handler.reply = reply_pipeline
    handler.memory = SimpleNamespace(
        store=SimpleNamespace(record_reply_metric=MagicMock())
    )
    handler._allowed_groups = set()
    # 私聊发送前会查「目标最近群」（非好友走群临时会话兜底）——这里给空，
    # 走纯私聊直发。_run_store_io 是实例属性查找，可以直接替身。
    handler._run_store_io = AsyncMock(return_value="")
    return handler


# ═══════════════════════════════════════════════════════════════════
# A. 拆条 + 逐段 enrich：不泄漏
# ═══════════════════════════════════════════════════════════════════

def test_split_reply_never_leaks_protocol_or_thinking_or_raw_tags():
    """**本文件最重要的一条。**

    阳性对照：把 `_enrich_split_reply` 里那句 `await self._enrich_reply_async(part, ...)`
    改成直接 `cleaned = part`（跳过 enrich），本用例必须变红——`<思考>`、`[贴图:`
    会原样出现在段里。
    """
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    raw = (
        "<思考>她刚问我能不能分条，我得解释一下</思考>"
        "先说结论：能分了[分段]"
        "[贴图:开心]后面这段带着表情包喵"
    )

    reply, parts = asyncio.run(handler._enrich_split_reply(raw, "你能分条吗", 0))

    assert parts is not None and len(parts) == 2, parts
    for segment in parts:
        assert "[分段]" not in segment, f"标记漏进气泡: {segment!r}"
        assert "<思考>" not in segment, f"思考段漏进气泡: {segment!r}"
        assert "[贴图:" not in segment, f"贴图标签未解析: {segment!r}"
    # 贴图是**被解析**掉了，不是被删掉了——第二段应当带上真正的 CQ 图
    assert "[CQ:image,file=0123456789abcdef0123456789abcdef]" in parts[1]
    # 整条文本 = 段的换行拼接（供日志/记忆/语音使用）
    assert reply == "\n".join(parts)


def test_blank_line_split_end_to_end_private():
    """T32 端到端：**真实形态**的多段回复（空行分段）→ 拆成多条气泡，逐段润色。

    为什么加这条：这也是本轮的行为改变本身。取样自生产形态——近 30 天私聊
    **64.4%** 的回复含空行分段、**平均 2.15 段**、段落长度中位 **45 字**，
    而真正发出的多气泡只 **9.2%**。以前这条会被当成「一个 132 字的单气泡」发出去，
    这是纯文本层面的期望形态（不依赖发送层，也就不依赖 napcat 替身的行为）。
    """
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    raw = (
        "<思考>她问我周末安排</思考>"
        "周末啊，我想想——\n\n"
        "[贴图:开心]大概周六在家躺着，哪也不想去。\n\n"
        "你呢，又要加班？"
    )
    reply, parts = asyncio.run(handler._enrich_split_reply(raw, "周末干嘛", 0))

    assert parts is not None and len(parts) == 3, ("空行没被当作段边界: %r" % (parts,))
    for seg in parts:
        assert "<思考>" not in seg, ("思考段漏进气泡: %r" % seg)
        assert "[贴图:" not in seg, ("贴图标签未解析: %r" % seg)
        assert "\n\n" not in seg, ("气泡内不该再留空行边界: %r" % seg)
    # 贴图是被**解析**掉而不是删掉
    assert "[CQ:image,file=0123456789abcdef0123456789abcdef]" in parts[1]
    # 整条文本 = 段的换行拼接（日志/记忆/语音读的是它）
    assert reply == "\n".join(parts)


def test_single_newline_reply_stays_one_part_end_to_end():
    """窄边界端到端护栏：**单个换行**的回复仍然只发一条。

    这是 T32 刻意选择的失败方向——「同一句话被切成两条」比
    「两个想法挤在一条」更伤可读性，所以宁可少拆。
    """
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    raw = "第一行\n第二行"
    reply, parts = asyncio.run(handler._enrich_split_reply(raw, "在吗", 0))

    assert parts is None, ("单个换行被误拆成多条: %r" % (parts,))
    assert reply == raw


def test_no_marker_returns_none_parts_and_unchanged_text():
    """零变化契约：没有边界时段列表必须是 None，整条文本与旧路径一致。"""
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    raw = "普通回复，没有任何标记。"
    reply, parts = asyncio.run(handler._enrich_split_reply(raw, "在吗", 0))

    assert parts is None
    assert reply == raw


def test_voice_source_is_marker_free():
    """语音路径直接读 reply（_text_to_voice_script(voice_source=reply)）——
    标记一旦活到这里就会被**念出来**。所以拆条必须在 enrich 之前完成。"""
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    reply, _parts = asyncio.run(
        handler._enrich_split_reply("第一句[分段]第二句", "在吗", 0))

    assert "[分段]" not in reply
    assert "分段" not in reply  # 连字面都不留


# ═══════════════════════════════════════════════════════════════════
# B. 发送：真的发 N 条
# ═══════════════════════════════════════════════════════════════════

def test_checked_send_private_delivers_every_segment_in_order(monkeypatch):
    """阳性对照：把 `ReplyPipeline.send` 里 `if presplit_parts and len(...) > 1`
    那个分支删掉（退回「私聊恒 [reply]」），本用例必须变红——只会发出 1 条。

    ⚠ 本用例**不覆盖** handle_private_message 里的接线（它直接传 parts 调
    _checked_send）。那段接线由 test_integration_contract.py 的 AST 闸门管。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    reply, parts = asyncio.run(handler._enrich_split_reply(TODAYS_REPLY, "在吗", 0))
    ok = asyncio.run(handler._checked_send(
        "private", "123", reply, presplit_parts=parts,
    ))

    assert ok is True
    assert len(nc.private_calls) == len(parts) == 3  # [分段]×4 → 5 段被并到上限 3
    assert nc.private_calls[0].startswith("哦——原来是换行")
    assert nc.private_calls[-1].endswith("困。")
    # 每个气泡里都不许有协议痕迹
    for bubble in nc.private_calls:
        assert "[分段]" not in bubble


def test_checked_send_without_presplit_stays_single_bubble(monkeypatch):
    """不给拆分权 ⇒ 私聊仍是一条。这是「零行为变化」在发送层的证据。"""
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)

    ok = asyncio.run(handler._checked_send("private", "123", "整条回复"))

    assert ok is True
    assert nc.private_calls == ["整条回复"]


def test_self_check_runs_on_the_whole_reply_not_per_segment(monkeypatch):
    """自检必须跑在整条上——话痨/复读判定需要完整上下文，逐段会把每段都判成
    「太短」「不像复读」而失去意义。"""
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    nc = FakeNapcat()
    handler = _handler(_real_pipeline(nc), nc)
    seen = []
    original = handler.self_check.check

    def spy(reply, **kwargs):
        seen.append(reply)
        return original(reply, **kwargs)

    handler.self_check.check = spy

    parts = ["第一段", "第二段"]
    asyncio.run(handler._checked_send(
        "private", "123", "第一段\n第二段", presplit_parts=parts,
    ))

    assert seen == ["第一段\n第二段"], f"自检被调用了 {len(seen)} 次: {seen}"
