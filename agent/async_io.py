"""有界同步 I/O 适配器。

Store 使用同步 SQLite 连接，调用方必须把它移出事件循环；同时不能让持续
流量把无限多的工作排进默认线程池。每个事件循环共享一个有界门，取消时仍
等待已提交的线程收口。
"""
from __future__ import annotations

import asyncio
import time
from weakref import WeakKeyDictionary


STORE_IO_MAX_CONCURRENCY = 16
_loop_limiters: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)
BLOCKING_WORK_MAX_CONCURRENCY = 4
_blocking_loop_limiters: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    WeakKeyDictionary()
)
_write_loop_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    WeakKeyDictionary()
)

# SQLite 允许并发读，但同一数据库同一时刻只有一个写事务。调用方已经把
# Store 操作标成稳定的内部 operation 名称；在这个边界按操作名/函数名识别
# 写入，不读取用户文本，也不参与 LLM 行为决策。读路径仍保留 16 路并发。
_STORE_WRITE_PREFIXES = (
    "ack_", "add_", "append_", "apply_", "cancel_", "claim_", "clear_",
    "close_", "complete_", "consume_", "create_", "delete_", "enrich_",
    "enqueue_", "expire_", "fail_", "index_", "insert_", "lease_",
    "mark_", "migrate_", "persist_", "quarantine_", "record_", "recover_",
    "reduce_", "release_", "remove_", "repair_", "replace_", "requeue_",
    "retry_", "save_", "set_", "settle_", "touch_", "unquarantine_",
    "update_", "upsert_", "write_",
)


def _limiter_for_current_loop() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _loop_limiters.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(STORE_IO_MAX_CONCURRENCY)
        _loop_limiters[loop] = limiter
    return limiter


def _blocking_limiter_for_current_loop() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limiter = _blocking_loop_limiters.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(BLOCKING_WORK_MAX_CONCURRENCY)
        _blocking_loop_limiters[loop] = limiter
    return limiter


def _write_lock_for_current_loop() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _write_loop_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _write_loop_locks[loop] = lock
    return lock


def _is_store_write(operation: str, func) -> bool:
    """判断内部 Store 调用是否需要进入单写者门。"""
    candidates = (
        str(operation or "").rsplit(".", 1)[-1],
        getattr(func, "__name__", ""),
    )
    for raw in candidates:
        token = str(raw or "").lstrip("_").casefold()
        if token == "get_or_create_person":
            return True
        if token.startswith(_STORE_WRITE_PREFIXES):
            return True
    return False


async def run_bounded_store_io(
    operation: str,
    func,
    *args,
    logger=None,
    log_prefix: str = "Store SQLite 调用较慢",
    **kwargs,
):
    """在线程池执行一次同步 Store 调用，并限制全局并发。

    ``asyncio.to_thread`` 的外层任务取消后，底层线程不会自动停止；因此先
    等线程完成再传播取消，防止重启/临时数据库清理撞上仍打开的连接。
    ``queue_wait_ms`` 会随慢调用日志记录，区分线程池排队和 SQLite 本身变慢。
    """
    limiter = _limiter_for_current_loop()
    write_lock = _write_lock_for_current_loop() if _is_store_write(operation, func) else None
    started = time.perf_counter()
    lock_acquired = False
    limiter_acquired = False
    try:
        if write_lock is not None:
            await write_lock.acquire()
            lock_acquired = True
        await limiter.acquire()
        limiter_acquired = True
        acquired = time.perf_counter()
        io_task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
        return await asyncio.shield(io_task)
    except asyncio.CancelledError:
        if "io_task" in locals():
            await asyncio.shield(io_task)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        acquired_at = locals().get("acquired", started)
        queue_wait_ms = (acquired_at - started) * 1000
        if logger is not None and elapsed_ms >= 250:
            logger.warning(
                "%s | op=%s elapsed_ms=%.1f queue_wait_ms=%.1f limit=%d",
                log_prefix,
                operation,
                elapsed_ms,
                queue_wait_ms,
                STORE_IO_MAX_CONCURRENCY,
            )
        if limiter_acquired:
            limiter.release()
        if lock_acquired:
            write_lock.release()


async def run_bounded_blocking(
    operation: str,
    func,
    *args,
    logger=None,
    log_prefix: str = "同步阻塞任务较慢",
    **kwargs,
):
    """在线程池执行同步 CPU/文件任务，并限制同一事件循环的并发量。

    与 Store I/O 使用独立的门：知识检索、图片处理等计算不能挤占 SQLite
    配额，也不能把 1000 个用户的请求无限排进默认线程池。取消时等待已
    提交的线程收口，避免模型仍在后台运行而调用方误以为已结束。
    """
    limiter = _blocking_limiter_for_current_loop()
    started = time.perf_counter()
    await limiter.acquire()
    acquired = time.perf_counter()
    work_task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(work_task)
    except asyncio.CancelledError:
        await asyncio.shield(work_task)
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        queue_wait_ms = (acquired - started) * 1000
        if logger is not None and elapsed_ms >= 250:
            logger.warning(
                "%s | op=%s elapsed_ms=%.1f queue_wait_ms=%.1f limit=%d",
                log_prefix,
                operation,
                elapsed_ms,
                queue_wait_ms,
                BLOCKING_WORK_MAX_CONCURRENCY,
            )
        limiter.release()
