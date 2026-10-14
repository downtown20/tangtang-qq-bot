"""E3：自忆缓冲静默丢失修复（2026-08-28 协作任务包，审查 Important 15）

旧实现：_extract_self_memories 用一次性内存 list，达到 5 条且 LLM 锁空闲时
取 buffer[-5:] 后**清空整个 buffer**——锁忙/前置条目/任务失败/重启都可能
静默丢失。

验收点：
  1. 缓冲超过 5 条：前置条目最终仍会被处理（分批 claim，不截断丢弃）
  2. LLM 忙：条目保留在 pending，解锁后全部处理
  3. 提取失败：本批回滚（claim 未 ack），重试后全部处理，不丢条目
  4. 重启：持久 pending（state:self_memory_pending）恢复
  5. 有界：超上限丢最旧（防失控增长）
"""
import asyncio
import json
import time
import types

from agent.handler import MessageHandler


def _run(coro):
    return asyncio.run(coro)


def _handler(store, llm_batches, llm_fail_once=False, override_extract=True):
    """最小 handler stub：只挂载自忆 pending 链路依赖"""
    h = object.__new__(MessageHandler)
    h.memory = types.SimpleNamespace(
        store=store,
        remember_self=lambda *a, **k: 1,
    )
    h.metrics = types.SimpleNamespace(record_self_memory=lambda: None)
    h.bot_qq = "10000"
    h.embed_engine = None
    h._llm_lock = types.SimpleNamespace(locked=lambda: False)
    def _discard_task(coro, name=""):
        # 生产代码把 drain 协程交给 safe_task；这里直接调用 drain 做断言，
        # 必须关闭未调度的协程，避免测试产生 RuntimeWarning。
        coro.close()
        if name == "self_memory_retry":
            h._self_memory_retry_scheduled = False
    h._safe_task = _discard_task  # 不实际启动——测试直接调 drain
    h._self_memory_buffer = []
    h._self_memory_draining = False
    h._self_memory_retry_scheduled = False
    h._self_memory_retry_delay = 0.0
    h._save_state_kv = lambda k, v: (store.kv_set(k, json.dumps(v, ensure_ascii=False)) or True)

    state = {"fail_once": llm_fail_once, "failed": False}

    async def fake_extract(replies):
        if state["fail_once"] and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("LLM 挂了")
        llm_batches.append([r["reply"] for r in replies])
        return True

    if override_extract:
        h._extract_self_memories_llm = fake_extract
    return h


def _reply(h, i, user="1001"):
    h._extract_self_memories(f"回复{i}", target_qq=user,
                             group_id="g1", source_message_id=100 + i)


# ═══════════════════════════════════════════════════════
# 1. 缓冲超过 5 条：前置条目最终仍会被处理
# ═══════════════════════════════════════════════════════

def test_pending_over_batch_eventually_processed(store):
    llm_batches = []
    h = _handler(store, llm_batches)
    for i in range(8):          # 8 条 > 5 条批次
        _reply(h, i)
    _run(h._drain_self_memory_pending())
    # 第一批 5 条已处理；前置 3 条仍在 pending（未被截断丢弃）
    assert llm_batches == [["回复0", "回复1", "回复2", "回复3", "回复4"]]
    assert len(h._self_memory_buffer) == 3
    # 尾批（<5 条）保留等待凑批——补 2 条后一起处理，前置条目最终不丢
    _reply(h, 8)
    _reply(h, 9)
    _run(h._drain_self_memory_pending())
    assert llm_batches[1] == ["回复5", "回复6", "回复7", "回复8", "回复9"]
    assert h._self_memory_buffer == []


# ═══════════════════════════════════════════════════════
# 2. LLM 忙：条目保留，解锁后全部处理
# ═══════════════════════════════════════════════════════

def test_busy_lock_keeps_pending_until_free(store):
    llm_batches = []
    h = _handler(store, llm_batches)
    h._llm_lock = types.SimpleNamespace(locked=lambda: True)  # 锁忙
    for i in range(8):
        _reply(h, i)
    # 忙时不 drain（触发条件含锁检查——即使被触发，drain 内也应让位）
    _run(h._drain_self_memory_pending())
    assert llm_batches == []                        # 未处理
    assert len(h._self_memory_buffer) == 8          # 全部保留
    h._llm_lock = types.SimpleNamespace(locked=lambda: False)  # 解锁
    _run(h._drain_self_memory_pending())            # 第一批 5 条
    assert len(h._self_memory_buffer) == 3          # 尾批保留
    _reply(h, 8)
    _reply(h, 9)                                    # 凑够尾批
    _run(h._drain_self_memory_pending())
    assert [r for b in llm_batches for r in b] == [f"回复{i}" for i in range(10)]


# ═══════════════════════════════════════════════════════
# 3. 提取失败：本批回滚，重试后不丢
# ═══════════════════════════════════════════════════════

def test_failure_rolls_back_batch_not_lost(store):
    llm_batches = []
    h = _handler(store, llm_batches, llm_fail_once=True)
    for i in range(6):
        _reply(h, i)
    _run(h._drain_self_memory_pending())            # 第一批失败 → 回滚
    assert llm_batches == []                        # 失败批次未消费
    assert len(h._self_memory_buffer) == 6          # 全部保留（含回滚）
    _run(h._drain_self_memory_pending())            # 重试成功（5 条）
    assert llm_batches[0] == [f"回复{i}" for i in range(5)]
    assert len(h._self_memory_buffer) == 1          # 尾条保留
    for i in range(6, 10):
        _reply(h, i)                                # 补 4 条凑批
    _run(h._drain_self_memory_pending())
    assert [r for b in llm_batches for r in b] == [f"回复{i}" for i in range(10)]
    assert h._self_memory_buffer == []


# ═══════════════════════════════════════════════════════
# 4. 重启恢复：持久 pending
# ═══════════════════════════════════════════════════════

def test_restart_restores_pending(store):
    llm_batches = []
    h = _handler(store, llm_batches)
    for i in range(3):
        _reply(h, i)
    # 模拟重启：新实例从 kv 恢复（_restore_self_memory_pending）
    h2 = _handler(store, llm_batches)
    h2._restore_self_memory_pending()
    assert len(h2._self_memory_buffer) == 3         # 未确认条目恢复
    _reply(h2, 3)
    _reply(h2, 4)                                   # 凑够批次
    _run(h2._drain_self_memory_pending())           # 恢复后仍可处理
    assert [r for b in llm_batches for r in b] == [f"回复{i}" for i in range(5)]


# ═══════════════════════════════════════════════════════
# 5. 有界：超上限丢最旧
# ═══════════════════════════════════════════════════════

def test_pending_bounded(store):
    llm_batches = []
    h = _handler(store, llm_batches)
    for i in range(60):
        _reply(h, i)
    assert len(h._self_memory_buffer) <= 50         # 有界上限


def test_async_pending_persist_does_not_block_event_loop(store):
    """在线消息入口的慢 pending 写入必须在线程 worker 完成。"""
    llm_batches = []
    h = _handler(store, llm_batches)
    original_save = h._save_state_kv

    def slow_save(key, value):
        time.sleep(0.06)
        return original_save(key, value)

    h._save_state_kv = slow_save
    ticks = 0

    async def run():
        nonlocal ticks
        task = asyncio.create_task(h._extract_self_memories_async(
            "回复", target_qq="1001", group_id="g1", source_message_id=1,
        ))
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.005)
        await task

    asyncio.run(run())

    assert ticks >= 6
    assert len(h._self_memory_buffer) == 1


def test_async_pending_persist_survives_restart_restore(store):
    """异步入口完成后，新的 Handler 实例仍能恢复 pending。"""
    h = _handler(store, [])
    _run(h._extract_self_memories_async(
        "异步待恢复", target_qq="1001", group_id="g1", source_message_id=7,
    ))

    restored = _handler(store, [])
    restored._restore_self_memory_pending()

    assert [item["reply"] for item in restored._self_memory_buffer] == ["异步待恢复"]


def test_busy_lock_schedules_retry_and_drains_after_unlock(store):
    """锁忙不能把满批 pending 留成永久滞留；解锁后自动重试。"""
    llm_batches = []
    h = _handler(store, llm_batches)
    h._self_memory_buffer = [
        {"reply": f"回复{i}", "target_qq": "1001", "group_id": "g1", "source_message_id": 100 + i}
        for i in range(5)
    ]
    h._llm_lock = types.SimpleNamespace(locked=lambda: True)
    scheduled = []

    def capture(coro, name=""):
        scheduled.append((coro, name))

    h._safe_task = capture
    _run(h._drain_self_memory_pending())
    assert llm_batches == []
    assert len(h._self_memory_buffer) == 5
    assert [name for _, name in scheduled] == ["self_memory_retry"]

    h._llm_lock = types.SimpleNamespace(locked=lambda: False)
    retry_coro, _ = scheduled.pop()
    _run(retry_coro)
    assert llm_batches == [[f"回复{i}" for i in range(5)]]
    assert h._self_memory_buffer == []


def test_failed_batch_schedules_retry_without_permanent_stall(store):
    """LLM 批次失败后应保留并自动安排重试，不依赖下一条新回复。"""
    llm_batches = []
    h = _handler(store, llm_batches, llm_fail_once=True)
    h._self_memory_buffer = [
        {"reply": f"回复{i}", "target_qq": "1001", "group_id": "g1", "source_message_id": 100 + i}
        for i in range(5)
    ]
    scheduled = []

    def capture(coro, name=""):
        scheduled.append((coro, name))

    h._safe_task = capture
    _run(h._drain_self_memory_pending())
    assert llm_batches == []
    assert [item["reply"] for item in h._self_memory_buffer] == [f"回复{i}" for i in range(5)]
    assert [name for _, name in scheduled] == ["self_memory_retry"]

    retry_coro, _ = scheduled.pop()
    _run(retry_coro)
    assert llm_batches == [[f"回复{i}" for i in range(5)]]
    assert h._self_memory_buffer == []


def test_claim_persistence_failure_does_not_ack_batch(store):
    """无法持久化 claim 时禁止调用 LLM，避免崩溃后批次丢失。"""
    llm_batches = []
    h = _handler(store, llm_batches)
    h._self_memory_buffer = [
        {"reply": f"回复{i}", "target_qq": "1001", "group_id": "g1", "source_message_id": 100 + i}
        for i in range(5)
    ]
    h._save_state_kv = lambda *_args, **_kwargs: False
    scheduled = []

    def capture(coro, name=""):
        scheduled.append((coro, name))

    h._safe_task = capture
    _run(h._drain_self_memory_pending())
    assert llm_batches == []
    assert len(h._self_memory_buffer) == 5
    assert [name for _, name in scheduled] == ["self_memory_retry"]
    scheduled[0][0].close()


def test_restart_restores_inflight_and_pending_without_loss(store):
    """崩溃发生在 claim/LLM 之间时，inflight 与 pending 都应恢复。"""
    inflight = [{"reply": "在途", "target_qq": "1001", "group_id": "g1", "source_message_id": 100}]
    pending = [{"reply": "待处理", "target_qq": "1001", "group_id": "g1", "source_message_id": 101}]
    store.kv_set("state:self_memory_pending", json.dumps(pending, ensure_ascii=False))
    store.kv_set("state:self_memory_inflight", json.dumps(inflight, ensure_ascii=False))
    h = _handler(store, [], override_extract=False)
    h._restore_self_memory_pending()
    assert [item["reply"] for item in h._self_memory_buffer] == ["在途", "待处理"]


def test_empty_self_memory_batch_is_protocol_success_without_llm(store):
    """空批是合法 no-op，不应额外消耗一次 LLM。"""
    h = _handler(store, [], override_extract=False)
    calls = []

    async def fake_llm(*_args, **_kwargs):
        calls.append(True)
        return "[]"

    h._call_llm_light = fake_llm
    assert _run(h._extract_self_memories_llm([])) is True
    assert calls == []


def test_malformed_self_memory_llm_response_is_retryable(store):
    """非 JSON 数组响应不能被当成已确认，否则会静默丢批次。"""
    h = _handler(store, [], override_extract=False)

    async def fake_llm(*_args, **_kwargs):
        return "暂时无法判断"

    h._call_llm_light = fake_llm
    reply = [{"reply": "回复", "target_qq": "1001", "group_id": "g1", "source_message_id": 100}]
    assert _run(h._extract_self_memories_llm(reply)) is False


def test_empty_self_memory_llm_response_is_retryable(store):
    """空响应同样不能 ack，必须保留批次等待重试。"""
    h = _handler(store, [], override_extract=False)

    async def fake_llm(*_args, **_kwargs):
        return ""

    h._call_llm_light = fake_llm
    reply = [{"reply": "回复", "target_qq": "1001", "group_id": "g1", "source_message_id": 100}]
    assert _run(h._extract_self_memories_llm(reply)) is False
