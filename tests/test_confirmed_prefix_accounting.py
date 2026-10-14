"""T2.5 门禁：部分送达按「平台确认粒度」归账 + 多气泡边界（2026-10-13）。

审计 P0-3 / ADR-002:64,72 / ADR-003:23,128 的机器闸门。生产代码已定稿
（agent/reply_pipeline.py 的 last_delivered_parts / last_delivered_chunk_ids，
agent/handler.py 的 _checked_send + _account_confirmed_prefix），本文件**只驱动**
它们，一行生产代码都不改。

## 这组用例防的是什么

审计复现（docs/审查/架构审计执行_T21只读复现_20261013.md）确认了两条现状，
它们是**一体两面**，必须同一次改——所以本文件同时钉住两条：

  ① **零归账**：首段平台已确认（message_id=501）、次段失败时 chat_log 一行没有；
  ② **多气泡压成一条**：两段全确认时账本只有 1 条，正文被 "".join 粘连。

外加四条边界：不确定 ≠ 已送达（零归账）、超上限整体放弃（不截断）、重复归账幂等、
**跨回合残留不得归账**（回合令牌，2026-10-13 收紧）。

## 回合令牌（2026-10-13 生产 API 收紧）

`_last_checked_send_parts` 只在 `_checked_send` 开头重置，而 handler 有若干
**不经过 `_checked_send`** 的回合（语音独发 / 纯唱歌 / 自检拦截）——它们读该字段
会拿到上一回合的**残留**，把上一回合的气泡原样复制进账本（过度记账）。
因此回合起点必须 `_reset_send_evidence()` 领令牌，归账时 `turn_token=` 原样传回：
令牌缺失/不符一律 fail-closed 零归账 + ERROR 日志。

## 桩与真值

- napcat：ScriptedNapcat（脚本化 SendResult），写法抄自 tests/test_reply_send.py:83-100；
- handler：object.__new__(MessageHandler) + 真 ReplySelfCheck + 真 ReplyPipeline，
  写法抄自 tests/test_split_e2e_private.py:78-93；
- **账本断言走真 SQLite**：真 Store(tmp_path) + 真 MemorySystem.log_chat，
  另开一条 sqlite3 连接直接 SELECT chat_log（不经过被测路径，避免自证）。
- _run_store_io 是实例属性查找（AutonomyMixin._run_store_io → 有界线程池），
  不替换也能跑通——用真路径更真。

## 阳性对照（教训 #30：不种样本的检查器等于没测）

每条用例的 docstring 里都写了「把生产代码改成什么会让它变红」，并且**实际执行过**：
字节级备份 → 改坏 → 跑本条 → 红 → 字节还原 → SHA256 与原始一致。
实测结果见文件末尾 _MUTATION_RECORDS。
"""

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.memory import MemorySystem  # noqa: E402
from agent.reply_pipeline import ReplyPipeline  # noqa: E402
from agent.store import Store  # noqa: E402
from onebot.ws_client import SendResult  # noqa: E402


class ScriptedNapcat:
    """按脚本逐次返回发送结果；同时记录平台实际收到的正文（平台侧真值）。"""

    def __init__(self, results):
        self._results = list(results)
        self.private_calls: list[str] = []
        self.group_calls: list[str] = []

    def _next(self):
        return self._results.pop(0) if self._results else True

    async def send_private_message(self, target, content, group_id=""):
        self.private_calls.append(content)
        return self._next()

    async def send_group_message(self, target, content):
        self.group_calls.append(content)
        return self._next()


def _harness(scripted, tmp_path):
    """真 Store(SQLite 文件) + 真 MemorySystem + 真 ReplyPipeline + 真 _checked_send。"""
    from agent.handler import MessageHandler
    from agent.self_check import ReplySelfCheck

    db_path = str(tmp_path / "memory.db")
    store = Store(db_path)
    memory = MemorySystem(store=store)
    napcat = ScriptedNapcat(scripted)
    pipeline = ReplyPipeline(
        napcat=napcat, stickers=MagicMock(), store=store,
        short_term={}, bot_nicknames=["糖糖"], bot_qq="10000",
    )
    handler = object.__new__(MessageHandler)
    handler.self_check = ReplySelfCheck()
    handler.reply = pipeline
    handler.memory = memory
    handler._allowed_groups = set()
    return SimpleNamespace(handler=handler, pipeline=pipeline, napcat=napcat,
                           memory=memory, store=store, db_path=db_path)


def _run_turn(harness, *, target_type, target_id, reply, presplit_parts=None,
              user_id="u1", group_id="", source_chat_id=0):
    """复刻两条生产调用点（群 handler.py:4180-4252 / 私 :5227-5330）。

    顺序与原文**逐行一致**（这三步的顺序本身就是契约）：
      1. 回合起点 _reset_send_evidence() 领令牌；
      2. _checked_send(...) 产生本回合证据（它内部只 clear、不作废令牌）；
      3. 按 if ok: 的语义决定 joined_reply，带 turn_token= 归账。
    """
    turn_token = harness.handler._reset_send_evidence()
    ok = asyncio.run(harness.handler._checked_send(
        target_type, target_id, reply, presplit_parts=presplit_parts,
    ))
    joined_reply = (
        getattr(harness.handler, "_last_checked_send_payload", "") or reply
    ) if ok else ""
    row_id = asyncio.run(harness.handler._account_confirmed_prefix(
        target_type=target_type, target_id=target_id, user_id=user_id,
        group_id=group_id, source_chat_id=source_chat_id,
        joined_reply=joined_reply, turn_token=turn_token,
    ))
    return ok, row_id


def _bot_rows(db_path):
    """直接查真 SQLite——不经过被测路径，避免用被测代码证明被测代码。"""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, qq_id, message, is_bot_reply, group_id, event_key "
            "FROM chat_log WHERE is_bot_reply=1 ORDER BY id"
        ).fetchall()


def _bodies(db_path):
    return [row[2] for row in _bot_rows(db_path)]


# ═══════════════════════════════════════════════════════════════════
# ① 零归账的正解：只记平台已确认的前缀
# ═══════════════════════════════════════════════════════════════════

def test_partial_delivery_accounts_only_the_confirmed_prefix(monkeypatch, tmp_path):
    """首段确认（id=501）、次段确定失败 ⇒ 账本只记**已确认前缀**。

    平台侧真值：两段都发出去了（第二段是平台明确拒绝，不是「没发」）。
    所以这不是「发失败就不记账」，而是「按确认粒度记账」。

    阳性对照（实测 M1）：把 _account_confirmed_prefix 单段分支的送达证据门
        if not joined_reply and not (delivered or chunk_ids): return 0
    改回 if not joined_reply: return 0 → 本用例红（0 行 = 旧「零归账」）。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    harness = _harness([
        SendResult(True, True, message_id=501),
        SendResult(False, False, error="BOT_ERROR", retryable=False),
    ], tmp_path)

    ok, row_id = _run_turn(
        harness, target_type="group", target_id="g1", user_id="u1", group_id="g1",
        reply="第一段内容第二段内容",
        presplit_parts=["第一段内容", "第二段内容"],
        source_chat_id=77,
    )

    assert ok is False, "次段确定失败 ⇒ 整条未确认"
    assert harness.napcat.group_calls == ["第一段内容", "第二段内容"]

    # 确认粒度的证据层：只有第 1 段进了 delivered
    assert harness.handler._last_checked_send_parts == ["第一段内容"]
    assert harness.handler._last_checked_send_chunk_ids == (501,)
    assert harness.pipeline.last_delivered_parts == ["第一段内容"]
    assert harness.pipeline.last_confirmed_payload == ""  # 旧字段：整条未确认

    # 账本真值（真 SQLite）
    rows = _bot_rows(harness.db_path)
    assert [row[2] for row in rows] == ["第一段内容"], "只许记平台已确认的那一段"
    assert row_id == rows[0][0] and row_id != 0
    assert rows[0][1] == "u1" and rows[0][3] == 1 and rows[0][4] == "g1"


# ═══════════════════════════════════════════════════════════════════
# ② 多气泡压成一条的正解：逐条写，保边界
# ═══════════════════════════════════════════════════════════════════

def test_all_confirmed_bubbles_are_accounted_as_separate_rows(monkeypatch, tmp_path):
    """两段全确认 ⇒ 账本是**两条**，正文不粘连（T2.3 的验收）。

    last_confirmed_payload 仍按旧语义保留 "".join（旧调用方读它），
    但**账本**必须按条写——平台发了两个气泡，账本就得有两行。

    阳性对照（实测 M2）：把多段分支的 enumerate(parts) 改成
    enumerate(["".join(parts)]) → 账本只剩 1 条拼接正文 → 本用例红。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    harness = _harness([
        SendResult(True, True, message_id=501),
        SendResult(True, True, message_id=502),
    ], tmp_path)

    ok, row_id = _run_turn(
        harness, target_type="group", target_id="g1", user_id="u1", group_id="g1",
        reply="第一段内容第二段内容",
        presplit_parts=["第一段内容", "第二段内容"],
        source_chat_id=77,
    )

    assert ok is True
    assert harness.napcat.group_calls == ["第一段内容", "第二段内容"]

    rows = _bot_rows(harness.db_path)
    assert len(rows) == 2, "平台两个气泡 ⇒ 账本两条"
    assert [row[2] for row in rows] == ["第一段内容", "第二段内容"], "顺序保真"
    assert row_id == rows[0][0]
    # 反粘连：账本里不许出现拼接后的整条
    assert "第一段内容第二段内容" not in [row[2] for row in rows]
    # 旧字段语义零变化：整条拼接仍然只服务旧调用方
    assert harness.pipeline.last_confirmed_payload == "第一段内容第二段内容"


# ═══════════════════════════════════════════════════════════════════
# ③ 单段：与旧行为逐字等价（零行为回归的设备）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("target_type,target_id,group_id,expected_group_arg", [
    ("group", "g1", "g1", "g1"),
    ("private", "u1", "", ""),
])
def test_single_bubble_accounting_is_byte_identical_to_legacy(
        target_type, target_id, group_id, expected_group_arg, tmp_path):
    """单段（绝大多数回合）⇒ 与旧 log_chat(...) 调用**逐字等价**。

    旧调用点形状：log_chat(user_id, payload, group_id, is_bot=True)
    ——没有 event_key、没有额外关键字。单段分支多传任何一个参数都是行为回归
    （例如给单段也塞 event_key，会让同一回合的二次归账被幂等静默吞掉）。

    阳性对照（实测 M3）：给单段分支的 log_chat 调用加 event_key="x" →
    kwargs 不等 → 本用例红。
    """
    harness = _harness([SendResult(True, True, message_id=601)], tmp_path)
    calls = []
    original = harness.memory.log_chat

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    harness.memory.log_chat = _spy
    reply = "只有一段正文"
    ok, row_id = _run_turn(
        harness, target_type=target_type, target_id=target_id, user_id="u1",
        group_id=group_id, reply=reply, source_chat_id=88,
    )

    assert ok is True
    assert len(calls) == 1, "单段只写一次"
    args, kwargs = calls[0]
    assert args == ("u1", reply, expected_group_arg)
    assert kwargs == {"is_bot": True}

    rows = _bot_rows(harness.db_path)
    assert [row[2] for row in rows] == [reply], "正文逐字不变"
    assert rows[0][5] == "", "单段不带幂等键（与旧调用一致）"
    assert row_id == rows[0][0]


# ═══════════════════════════════════════════════════════════════════
# ④ 不确定 ≠ 已送达：零归账
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("target_type,target_id,group_id", [
    ("group", "g1", "g1"),
    ("private", "u1", ""),
])
def test_uncertain_without_chunk_id_is_never_accounted(
        target_type, target_id, group_id, tmp_path):
    """NETWORK_UNCERTAIN 且无 chunk_id ⇒ **零归账**（ADR-002 §1）。

    这是「过度记账」方向的闸门（08-30 事故：段 2 明确失败仍打「已发送」）。
    uncertain 表示副作用**可能已发生**——禁止自动重放，也禁止写进账本。

    阳性对照（实测 M4）：把 ReplyPipeline.send 收集段的过滤
        if not result.delivered: continue
    改成 if result.delivery_state == "failed": continue →
    不确定的段被当成已送达 → 本用例红（证据层先脏，账本随后）。
    """
    harness = _harness([
        SendResult(False, False, error="NETWORK_UNCERTAIN",
                   retryable=False, uncertain=True),
    ], tmp_path)
    reply = "也许已经发出去了"
    ok, row_id = _run_turn(
        harness, target_type=target_type, target_id=target_id, user_id="u1",
        group_id=group_id, reply=reply, source_chat_id=88,
    )

    assert ok is False
    detail = harness.pipeline.last_send_result
    assert detail.delivery_state == "uncertain", "平台没给送达证据"
    assert detail.chunk_ids == ()

    # 证据层：没有任何一段被当成已送达
    assert harness.pipeline.last_delivered_parts == []
    assert harness.pipeline.last_delivered_chunk_ids == ()
    assert harness.handler._last_checked_send_parts == []
    assert harness.handler._last_checked_send_chunk_ids == ()
    assert harness.handler._last_checked_send_delivered is False

    # 账本真值：零行
    assert _bodies(harness.db_path) == []
    assert row_id == 0


# ═══════════════════════════════════════════════════════════════════
# ⑤ T2.2b（缺陷 ④）：单段确定失败必须带出失败原因
# ═══════════════════════════════════════════════════════════════════

def test_single_definite_failure_exposes_reason_not_empty_string(tmp_path):
    """单段确定失败 ⇒ aggregate.error 非空且 retcode 保真（T2.1 记录 §3 缺陷 ④）。

    根因：first_problem 按定义是「第一个没有 delivered 的段」，而
    SendResult.__bool__ 只返回 ok（onebot/ws_client.py:49-52）⇒ 它恒为假值；
    用 if first_problem 取字段会把 error/retcode 静默吞成空串，上层只看到
    state=failed、看不到为什么。判据必须落在 is not None。

    阳性对照（实测 M5）：把聚合里的 first_problem is not None 改回
    first_problem → error 变 "" → 本用例红。
    """
    harness = _harness([
        SendResult(False, False, error="BOT_ERROR", retcode=100, retryable=False),
    ], tmp_path)

    ok = asyncio.run(harness.handler._checked_send(
        "group", "g1", "发不出去的一段",
    ))

    assert ok is False
    detail = harness.pipeline.last_send_result
    assert detail.error == "BOT_ERROR", "失败原因不许被吞成空串"
    assert detail.retcode == 100
    assert detail.delivery_state == "failed"
    assert "PARTIAL_DELIVERY" not in (detail.error or ""), "单段失败不是部分送达"
    # 零归账：确定失败没有任何送达证据
    assert _bodies(harness.db_path) == []
    assert harness.handler._last_checked_send_parts == []


# ═══════════════════════════════════════════════════════════════════
# ⑥ 安全阀：超上限整体放弃，绝不截断
# ═══════════════════════════════════════════════════════════════════

def test_confirmed_parts_over_cap_are_abandoned_wholesale(monkeypatch, tmp_path):
    """确认段数 > max_parts(默认 8) ⇒ **整体放弃**并出声，不做截断。

    截断会写出半截对话——比不写更糟（_account_confirmed_prefix 边界 3）。

    阳性对照（实测 M6）：把 if len(parts) > max_parts: 改成
    if len(parts) > max_parts + 8: → 9 段全部进账本 → 本用例红。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    parts = ["第%d段" % index for index in range(9)]
    harness = _harness([
        SendResult(True, True, message_id=700 + index) for index in range(9)
    ], tmp_path)

    ok, row_id = _run_turn(
        harness, target_type="private", target_id="u1", user_id="u1",
        reply="\n".join(parts), presplit_parts=list(parts), source_chat_id=99,
    )

    assert ok is True
    assert len(harness.napcat.private_calls) == 9
    assert len(harness.handler._last_checked_send_parts) == 9, "证据层有 9 段"
    assert _bodies(harness.db_path) == [], "超上限 ⇒ 一条都不写（不许截断）"
    assert row_id == 0


# ═══════════════════════════════════════════════════════════════════
# ⑦ 幂等：同一回合重复归账不写重复行
# ═══════════════════════════════════════════════════════════════════

def test_repeated_accounting_for_same_source_is_idempotent(monkeypatch, tmp_path):
    """同一源消息重复归账 ⇒ 账本行数不变（多段分支的 event_key 幂等）。

    event_key = f"delivery:{source_chat_id}:{index}" 是账本的幂等键；
    没有它，任何一次重入（崩溃恢复 / 上层重试）都会把同一批气泡记两遍。

    阳性对照（实测 M7）：删掉多段分支调用里的 event_key=(...) 关键字 →
    第二次归账再写 2 行（共 4 行）→ 本用例红。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    harness = _harness([
        SendResult(True, True, message_id=501),
        SendResult(True, True, message_id=502),
    ], tmp_path)

    ok, first_id = _run_turn(
        harness, target_type="group", target_id="g1", user_id="u1", group_id="g1",
        reply="第一段内容第二段内容",
        presplit_parts=["第一段内容", "第二段内容"],
        source_chat_id=77,
    )
    assert ok is True
    assert [row[5] for row in _bot_rows(harness.db_path)] == [
        "delivery:77:0", "delivery:77:1"], "逐条幂等键 = 源消息 + 段序号"

    # 重入：同回合令牌 + 同源消息（崩溃恢复 / 上层重试的形状）
    second_id = asyncio.run(harness.handler._account_confirmed_prefix(
        target_type="group", target_id="g1", user_id="u1", group_id="g1",
        source_chat_id=77, joined_reply="第一段内容第二段内容",
        turn_token=harness.handler._send_turn_token,
    ))

    rows = _bot_rows(harness.db_path)
    assert len(rows) == 2, "重入不许再写（幂等）"
    assert [row[2] for row in rows] == ["第一段内容", "第二段内容"]
    assert second_id == 0
    assert first_id == rows[0][0]


# ═══════════════════════════════════════════════════════════════════
# ⑧ 回合令牌：跨回合残留不得归账（2026-10-13 缺陷回归锁）
# ═══════════════════════════════════════════════════════════════════

def test_forgotten_turn_start_is_rejected_fail_closed(monkeypatch, tmp_path, caplog):
    """**忘记开回合 ⇒ fail-closed 零归账并出声**（令牌门锁「过度记账」）。

    缺陷现场（2026-10-13，impact-auditor 报告 + Lead 复现 + 本文件独立复现）：
    `_last_checked_send_parts` 只在 `_checked_send` 开头重置，而 handler 有若干
    **不经过 `_checked_send`** 的回合（语音独发群 4162 / 私 5203、纯唱歌 reply 为空、
    自检拦截直接 return False）。这些回合读该字段会拿到**上一回合的残留**，
    把上一回合的气泡原样复制进账本——独立复现实测：零文字回合写入了上一回合的
    2 条正文，且 event_key 按新 source_chat_id 生成，**幂等键拦不住**。

    ⚠ 这条用例的形状选择是有讲究的（踩过坑才定下来的）：
    「开新回合、却传**回合 1 的旧令牌**」测不出令牌门——因为
    `_reset_send_evidence()` 在发新令牌的同时就清空了证据，那种写法在令牌检查
    被删掉之后**依然**会因证据为空而零归账（实测：红不了）。
    只有「调用点根本没开回合（turn_token=0）」这一形状会把**未被清空的残留**
    带进归账，删掉令牌检查后它立刻变成真写入。

    阳性对照（实测 M8）：把 `_account_confirmed_prefix` 开头的令牌检查
    （`if int(turn_token) <= 0 or ...`）改成 `if False:` → 残留的 2 段被写进账本
    → 本用例红（账本 4 行，且出现上一回合正文）。
    """
    import logging

    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    harness = _harness([
        SendResult(True, True, message_id=501),
        SendResult(True, True, message_id=502),
    ], tmp_path)

    # ── 回合 A：正常开回合 → 两段全确认 → 归账 ──
    ok, first_id = _run_turn(
        harness, target_type="group", target_id="g1", user_id="u1", group_id="g1",
        reply="上一回合第一段上一回合第二段",
        presplit_parts=["上一回合第一段", "上一回合第二段"],
        source_chat_id=77,
    )
    assert ok is True and first_id != 0
    assert _bodies(harness.db_path) == ["上一回合第一段", "上一回合第二段"]
    # 回合 A 结束后证据仍在（这正是残留的来源）
    assert harness.handler._last_checked_send_parts == [
        "上一回合第一段", "上一回合第二段"]

    # ── 回合 B：调用点**忘记开回合**（语音独发/纯唱歌/自检拦截的形状）──
    with caplog.at_level(logging.ERROR, logger="糖糖.Handler"):
        rejected_id = asyncio.run(harness.handler._account_confirmed_prefix(
            target_type="group", target_id="g1", user_id="u1", group_id="g1",
            source_chat_id=78, joined_reply="", turn_token=0,
        ))

    rows = _bot_rows(harness.db_path)
    assert rejected_id == 0, "跨回合残留不得归账"
    assert [row[2] for row in rows] == [
        "上一回合第一段", "上一回合第二段"], "零文字回合不许新增任何行"
    assert len(rows) == 2
    # 残留不许被复制成新的 source 事件（event_key 是新的，幂等键拦不住）
    assert [row[5] for row in rows] == ["delivery:77:0", "delivery:77:1"]
    # 静默拒绝会让约束失去可观测性——必须出声
    assert "归账被拒绝" in caplog.text, "拒绝必须出声（可观测），不能静默"
    assert "回合令牌不符" in caplog.text


def test_turn_start_clears_previous_turn_evidence(monkeypatch, tmp_path):
    """**回合起点必须清空上一回合证据**（清空机制锁「同回合内无法误用旧令牌」）。

    这是与令牌门**互补**的第二道保护：令牌门挡「没开回合的调用点」，
    本方法挡「开了回合之后残留还在」——两者锁的形状不同，缺一不可。

    ⚠ 本用例**不能**用「开新回合后拿旧令牌归账」来测令牌门（那条路两重保护都会
    生效，删掉令牌门也红不了——实测确认）。它测的是「开回合后残留是否真的没了」：
    清空一旦被删，同一形状下残留就会留在字段里，令牌门虽然仍会拒绝，
    但「字段里没有上一回合数据」这个更强的保证就丢了——而那正是缺陷的源头。

    阳性对照（实测 M9）：把 `_reset_send_evidence` 里的 `self._clear_send_evidence()`
    删掉 → 开回合后 `_last_checked_send_parts` 仍是上一回合的两段 → 本用例红。
    """
    monkeypatch.setattr("agent.reply_pipeline.asyncio.sleep", AsyncMock())
    harness = _harness([
        SendResult(True, True, message_id=501),
        SendResult(True, True, message_id=502),
    ], tmp_path)

    # ── 回合 1：完整走一遍（开回合 → 发送 → 归账），留下残留 ──
    first_token = harness.handler._reset_send_evidence()
    ok = asyncio.run(harness.handler._checked_send(
        "group", "g1", "上一回合第一段上一回合第二段",
        presplit_parts=["上一回合第一段", "上一回合第二段"],
    ))
    assert ok is True
    first_row = asyncio.run(harness.handler._account_confirmed_prefix(
        target_type="group", target_id="g1", user_id="u1", group_id="g1",
        source_chat_id=77, joined_reply="上一回合第一段上一回合第二段",
        turn_token=first_token,
    ))
    assert first_row != 0
    assert [row[2] for row in _bot_rows(harness.db_path)] == [
        "上一回合第一段", "上一回合第二段"]
    assert harness.handler._last_checked_send_parts == [
        "上一回合第一段", "上一回合第二段"], "前置：本回合证据仍在（残留的来源）"

    second_token = harness.handler._reset_send_evidence()

    # 令牌必须**递增**（旧令牌就此失效，不可重放）
    assert second_token > first_token
    assert second_token == harness.handler._send_turn_token
    # 证据必须**清空**（这是本用例真正钉住的东西）
    assert harness.handler._last_checked_send_parts == [], "开回合必须清空上一回合残留"
    assert harness.handler._last_checked_send_chunk_ids == ()
    assert harness.handler._last_checked_send_delivered is False
    assert harness.handler._last_checked_send_payload == ""

    # 零文字回合：连 token 正确也必须零归账（没有任何本回合证据）
    blank_id = asyncio.run(harness.handler._account_confirmed_prefix(
        target_type="group", target_id="g1", user_id="u1", group_id="g1",
        source_chat_id=78, joined_reply="", turn_token=second_token,
    ))
    assert blank_id == 0
    assert [row[2] for row in _bot_rows(harness.db_path)] == [
        "上一回合第一段", "上一回合第二段"], "零文字回合不得新增行"


# ═══════════════════════════════════════════════════════════════════
# 变异记录（阳性对照实测，2026-10-13）
#
# 驱动器：字节级备份 → 精确替换（锚点必须恰好命中 1 次）→ 跑整文件
# → 记录失败集合 → 字节还原 → SHA256 与原始一致。
# handler.py 是 CRLF-only、reply_pipeline.py 是 LF —— 锚点按实际行尾转换。
# ═══════════════════════════════════════════════════════════════════

_MUTATION_RECORDS = [
    # (用例, 变异文件, 改法, 结果)
    ("test_partial_delivery_accounts_only_the_confirmed_prefix",
     "agent/handler.py", "单段分支送达证据门 → if not joined_reply: return 0", "RED"),
    ("test_all_confirmed_bubbles_are_accounted_as_separate_rows",
     "agent/handler.py", "enumerate(parts) → enumerate([''.join(parts)])", "RED"),
    ("test_single_bubble_accounting_is_byte_identical_to_legacy",
     "agent/handler.py", "单段分支 log_chat 加 event_key='x'", "RED"),
    ("test_uncertain_without_chunk_id_is_never_accounted",
     "agent/reply_pipeline.py",
     "段收集过滤 → if result.delivery_state == 'failed': continue", "RED"),
    ("test_single_definite_failure_exposes_reason_not_empty_string",
     "agent/reply_pipeline.py", "first_problem is not None → first_problem", "RED"),
    ("test_confirmed_parts_over_cap_are_abandoned_wholesale",
     "agent/handler.py", "len(parts) > max_parts → len(parts) > max_parts + 8", "RED"),
    ("test_repeated_accounting_for_same_source_is_idempotent",
     "agent/handler.py", "多段分支删掉 event_key=(...)", "RED"),
    ("test_forgotten_turn_start_is_rejected_fail_closed",
     "agent/handler.py", "令牌检查（if int(turn_token) <= 0 or ...）→ if False:", "RED"),
    ("test_turn_start_clears_previous_turn_evidence",
     "agent/handler.py", "_reset_send_evidence 删掉 self._clear_send_evidence()", "RED"),
    # M10：Lead 2026-10-13 实际犯过并已修的错（教训 #16/#26/#28 同类）——
    # 删掉定义 delivered_parts 的那几行却漏改引用它的那一行：
    # 语法合法、旧用例照绿，只有 pyflakes undefined + 本文件这条用例能抓住。
    ("test_partial_delivery_accounts_only_the_confirmed_prefix",
     "agent/handler.py",
     "len(self._last_checked_send_parts) → len(delivered_parts)（复现 NameError）", "RED"),
]
