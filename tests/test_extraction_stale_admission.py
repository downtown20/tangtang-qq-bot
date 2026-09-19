"""E2：记忆积压调度卡住路径修复（2026-08-28 协作任务包，审查 Critical 2）

卡住路径：_maybe_extract_stale 的准入先按 backlog 快照（SQL 层用户消息计数
≥10），再从混合窗口（include_bot_replies=True, limit=20）数 user_count——
bot 上下文占满窗口时 user_count<10 → continue 永久跳过，即使该用户积压
用户消息已达阈值。新用户消息永远排在窗口外，游标不推进 → 可证明卡住。

验收点：
  1. 用户消息凑够 10 条 + 11 条 bot 上下文穿插（20 条窗口内用户消息仅 9 条）
     → 不得被错误卡住：必须按真实用户消息（backlog SQL 计数）准入
  2. job 输入保留有限 bot 上下文（include_bot_replies 语义不丢）
  3. 同一窗口不重复建 job（create_extraction_job 幂等）
  4. 用户消息不足 10 条 → 不入队（阈值语义保留）
"""
import asyncio
import types
from datetime import datetime

from agent.handler_autonomy import AutonomyMixin


def _run(coro):
    return asyncio.run(coro)


class _Mem:
    """最小 memory stub：store 透传 + get_unprocessed_messages 游标 0 全量"""

    def __init__(self, store):
        self.store = store

    def get_unprocessed_messages(self, qq_id, limit=20, include_bot_replies=False):
        return self.store.get_unprocessed_messages(
            qq_id, 0, limit, include_bot_replies=include_bot_replies)


def _autonomy(store):
    a = object.__new__(AutonomyMixin)
    a.memory = _Mem(store)
    a.bot_qq = "10000"
    a.owner_qq = "9999"
    a._extracting_users = set()
    a.metrics = types.SimpleNamespace(incr=lambda *a, **k: None)
    return a


def _fill(store, qq, n_user, n_bot, group="g1"):
    """用户消息与 bot 私聊消息按 id 穿插，bot 全在前 20 条内。
    返回用户消息 chat id 列表。"""
    # 顺序：11 条 bot 在前（id 1-11），10 条用户在后（id 12-21）——
    # 前 20 条窗口 = 11 bot + 9 user → 旧 user_count=9 <10 卡住
    for i in range(n_bot):
        # 生产发送路径按对话对象 qq_id 记录 bot 回复；私聊上下文因此会和
        # 同一用户的群消息进入同一未处理窗口，正是旧 user_count 二次门控的
        # 真实触发方式。
        store.insert_chat(qq, f"bot上下文{i}", group_id="",
                          is_bot=True,
                          timestamp=f"2026-08-28 08:{i:02d}:00")
    user_ids = []
    for i in range(n_user):
        cid = store.insert_chat(qq, f"用户消息{i}", group_id=group,
                                timestamp=f"2026-08-28 09:{i:02d}:00")
        user_ids.append(cid)
    return user_ids


# ═══════════════════════════════════════════════════════
# 1. 核心：10 条用户消息 + 11 条 bot 上下文 → 不得卡住
# ═══════════════════════════════════════════════════════

def test_stale_admission_not_blocked_by_mixed_window(store):
    """backlog SQL 计数（用户消息 10 条 ≥10）已通过准入，
    混合 20 条窗口内 user_count 仅 9 → 旧实现 continue 永久卡住。
    修复后按真实用户消息准入，job 正常创建。"""
    _fill(store, "1001", n_user=10, n_bot=11)

    a = _autonomy(store)
    admitted = _run(a._maybe_extract_stale(max_per_cycle=6, max_open=8))

    assert admitted >= 1  # 1001 被准入——不再被混合窗口卡住
    jobs = store.list_resumable_extraction_jobs(limit=10)
    assert any(str(job["qq_id"]) == "1001" for job in jobs)
    # job 输入 = 20 条混合窗口（含 bot 上下文——私聊语境保留）
    job = next(j for j in jobs if str(j["qq_id"]) == "1001")
    assert job["message_ids"].count(",") + 1 == 20


# ═══════════════════════════════════════════════════════
# 2. 同一窗口不重复建 job（幂等）
# ═══════════════════════════════════════════════════════

def test_stale_admission_idempotent_per_window(store):
    _fill(store, "1001", n_user=10, n_bot=11)
    a = _autonomy(store)
    first = _run(a._maybe_extract_stale(max_per_cycle=6, max_open=8))
    second = _run(a._maybe_extract_stale(max_per_cycle=6, max_open=8))
    assert first >= 1
    # 第二轮回合：该用户已在 open_users/job 中——不再重复准入
    jobs = store.list_resumable_extraction_jobs(limit=10)
    assert sum(1 for j in jobs if str(j["qq_id"]) == "1001") == 1
    # 且 admitted 不为同用户重复计数（无新 job → second 不新增该用户）
    assert second == 0 or second <= first


def test_stale_admission_does_not_recount_active_lease(store):
    """未过期 leased 任务属于 open 用户，不得被再次准入计数。"""
    _fill(store, "1004", n_user=10, n_bot=0)
    job = store.create_extraction_job(
        "1004", store.get_unprocessed_messages("1004", 0, 20),
        direction="forward",
    )
    assert store.lease_extraction_job(job["id"])

    admitted = _run(_autonomy(store)._maybe_extract_stale(
        max_per_cycle=6, max_open=8,
    ))

    assert admitted == 0
    assert store.get_extraction_queue_health()["leased"] == 1


# ═══════════════════════════════════════════════════════
# 3. 用户消息不足 10 → 不入队（阈值语义保留）
# ═══════════════════════════════════════════════════════

def test_stale_admission_below_threshold_not_admitted(store):
    _fill(store, "1002", n_user=9, n_bot=11)  # 仅 9 条用户消息 < 10
    a = _autonomy(store)
    admitted = _run(a._maybe_extract_stale(
        max_per_cycle=6,
        max_open=8,
        now=datetime(2026, 8, 28, 10, 0, 0),
    ))
    assert admitted == 0  # 未达阈值——等待凑够（不卡住，只是未到期）
    assert store.list_resumable_extraction_jobs(limit=10) == []


def test_stale_admission_flushes_three_message_tail_after_seven_days(store):
    for index in range(3):
        store.insert_chat(
            "quiet-familiar",
            f"低频消息{index}",
            timestamp="2026-08-01 09:00:00",
        )

    admitted = _run(_autonomy(store)._maybe_extract_stale(
        max_per_cycle=6,
        max_open=8,
        now=datetime(2026, 8, 8, 9, 0, 0),
    ))

    assert admitted == 1
    assert store.list_resumable_extraction_jobs(limit=10)[0]["qq_id"] == "quiet-familiar"


def test_stale_admission_flushes_one_message_tail_only_after_twenty_one_days(store):
    store.insert_chat(
        "one-off-user",
        "只出现过一次",
        timestamp="2026-08-01 09:00:00",
    )
    handler = _autonomy(store)

    before_due = _run(handler._maybe_extract_stale(
        max_per_cycle=6,
        max_open=8,
        now=datetime(2026, 8, 22, 8, 59, 59),
    ))
    at_due = _run(handler._maybe_extract_stale(
        max_per_cycle=6,
        max_open=8,
        now=datetime(2026, 8, 22, 9, 0, 0),
    ))

    assert before_due == 0
    assert at_due == 1


# ═══════════════════════════════════════════════════════
# 4. 多用户公平：两个达标用户都准入
# ═══════════════════════════════════════════════════════

def test_stale_admission_multiple_users(store):
    _fill(store, "1001", n_user=10, n_bot=11)
    _fill(store, "1003", n_user=12, n_bot=3)
    a = _autonomy(store)
    admitted = _run(a._maybe_extract_stale(max_per_cycle=6, max_open=8))
    assert admitted >= 2
    jobs = store.list_resumable_extraction_jobs(limit=10)
    assert {str(j["qq_id"]) for j in jobs} >= {"1001", "1003"}
