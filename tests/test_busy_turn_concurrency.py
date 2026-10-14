import asyncio

from agent.handler import MessageHandler


def _handler_stub():
    handler = object.__new__(MessageHandler)
    handler._busy = False
    handler._busy_owner = None
    handler._busy_holders = {}
    handler._pending_reply = []
    return handler


def test_busy_turn_holders_do_not_overwrite_or_clear_each_other():
    async def scenario():
        handler = _handler_stub()
        entered = asyncio.Event()
        release_private = asyncio.Event()

        async def group_turn():
            handler._acquire_busy_turn()
            entered.set()
            await release_private.wait()
            assert handler._busy is True
            handler._release_busy_turn()

        async def private_turn():
            await entered.wait()
            handler._acquire_busy_turn()
            # The first holder remains authoritative; ownership is not clobbered.
            assert handler._busy is True
            assert len(handler._busy_holders) == 2
            handler._release_busy_turn()
            # Releasing the private turn must not clear the group backpressure.
            assert handler._busy is True
            release_private.set()

        await asyncio.gather(group_turn(), private_turn())
        assert handler._busy is False
        assert handler._busy_owner is None
        assert handler._busy_holders == {}

    asyncio.run(scenario())


def test_busy_turn_guard_only_recovers_current_task_holder():
    async def scenario():
        handler = _handler_stub()

        async def callback(self):
            self._acquire_busy_turn()
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        guarded = __import__("agent.handler", fromlist=["_busy_turn_guard"])._busy_turn_guard(callback)
        try:
            await guarded(handler)
        except RuntimeError:
            pass
        assert handler._busy is False
        assert handler._busy_owner is None
        assert handler._busy_holders == {}

    asyncio.run(scenario())


def test_busy_turn_guard_releases_on_task_cancellation():
    async def scenario():
        handler = _handler_stub()
        waiting = asyncio.Event()

        async def callback(self):
            self._acquire_busy_turn()
            waiting.set()
            await asyncio.Event().wait()

        guarded = __import__("agent.handler", fromlist=["_busy_turn_guard"])._busy_turn_guard(callback)
        task = asyncio.create_task(guarded(handler))
        await waiting.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert handler._busy is False
        assert handler._busy_owner is None
        assert handler._busy_holders == {}

    asyncio.run(scenario())


def test_post_release_exception_drains_pending_without_masking_original():
    async def scenario():
        from agent.handler import _busy_turn_guard
        dummy = _handler_stub()
        dummy._busy_released_tasks = set()
        dummy._pending_dispatch_reserved = False
        dummy._pending_reply = [{"nickname": "queued", "msg": {}}]
        dummy.scheduled = 0

        def safe_task(coro, name=""):
            dummy.scheduled += 1
            coro.close()

        dummy._safe_task = safe_task
        async def process_pending(pending):
            return None
        dummy._process_pending_reply = process_pending

        async def callback(self):
            self._acquire_busy_turn()
            self._release_busy_turn()
            raise RuntimeError("original failure")

        guarded = _busy_turn_guard(callback)
        try:
            await guarded(dummy)
        except RuntimeError as exc:
            assert str(exc) == "original failure"
        assert dummy.scheduled == 1
        assert dummy._pending_reply == []

    asyncio.run(scenario())


def test_guard_preserves_original_exception_without_legacy_scheduler():
    async def scenario():
        from agent.handler import _busy_turn_guard

        # Older lightweight handler doubles may not expose the new scheduler
        # helpers.  Exception cleanup must still preserve the callback error.
        dummy = object.__new__(MessageHandler)
        dummy._busy = True
        dummy._busy_owner = asyncio.current_task()
        dummy._pending_reply = [{"nickname": "queued", "msg": {}}]
        dummy._schedule_pending_reply = None

        async def callback(self):
            raise ValueError("callback failure")

        guarded = _busy_turn_guard(callback)
        try:
            await guarded(dummy)
        except ValueError as exc:
            assert str(exc) == "callback failure"
        else:
            raise AssertionError("callback exception was swallowed")
        assert dummy._busy is False
        assert dummy._busy_owner is None
        assert dummy._pending_reply

    asyncio.run(scenario())


def test_pending_reservation_waits_for_private_holder_before_starting():
    async def scenario():
        handler = _handler_stub()
        handler._busy_idle_event = asyncio.Event()
        handler._busy_idle_event.set()
        handler._busy_released_tasks = set()
        started = asyncio.Event()

        async def fake_handle(message):
            # Mirror handle_group_message's synchronous token consumption.
            message.pop("_pending_admission_token", None)
            assert handler._busy is True
            assert asyncio.current_task() in handler._busy_holders
            started.set()
            await asyncio.sleep(0)
        handler.handle_group_message = fake_handle
        handler._pending_dispatch_reserved = False
        tasks = []
        handler._safe_task = lambda coro, name="": tasks.append(asyncio.create_task(coro)) or tasks[-1]
        handler._pending_reply.append({"nickname": "queued", "msg": {}})
        handler._schedule_pending_reply()
        # A private turn can acquire during the reserved dispatch window.
        handler._acquire_busy_turn()
        await asyncio.sleep(0)
        assert not started.is_set()
        handler._release_busy_turn()
        await asyncio.wait_for(tasks[0], timeout=1)
        assert started.is_set()
        assert handler._busy is False

    asyncio.run(scenario())


def test_pending_cancellation_while_waiting_rolls_back_fifo():
    async def scenario():
        handler = _handler_stub()
        handler._busy_idle_event = asyncio.Event()
        tasks = []
        handler._safe_task = lambda coro, name="": tasks.append(asyncio.create_task(coro)) or tasks[-1]
        pending = {"nickname": "queued", "user_id": "u1", "msg": {}}
        handler._pending_reply.append(pending)
        handler._schedule_pending_reply()
        # Keep a real holder so the pending task is suspended in idle_event.wait().
        handler._acquire_busy_turn()
        await asyncio.sleep(0)
        tasks[0].cancel()
        try:
            await tasks[0]
        except asyncio.CancelledError:
            pass
        assert handler._pending_dispatch_reserved is False
        assert handler._pending_reply == [pending]
        assert handler._busy is True  # private holder still owns the busy line
        handler._release_busy_turn()
        assert handler._busy is False

    asyncio.run(scenario())


def test_pending_queue_coalesces_and_bounds_overflow():
    handler = _handler_stub()
    handler._pending_reply_limit = 2

    def entry(user_id, *, at=False, msg_id=0):
        return {"group_id": "g", "user_id": user_id,
                "nickname": user_id, "msg_id": msg_id,
                "msg": {"is_at_bot": at}}

    assert handler._enqueue_pending_reply(entry("u1", msg_id=7)) is True
    # Same platform event keeps only the newest copy (replay dedup).
    assert handler._enqueue_pending_reply(entry("u1", msg_id=7)) is True
    assert len(handler._pending_reply) == 1
    assert handler._enqueue_pending_reply(entry("u2")) is True
    assert handler._enqueue_pending_reply(entry("u3")) is False
    assert len(handler._pending_reply) == 2


def test_pending_queue_owner_replaces_oldest_equal_priority():
    handler = _handler_stub()
    handler.owner_qq = "owner"
    handler._pending_reply_limit = 2

    def entry(user_id, group_id="g"):
        return {"group_id": group_id, "user_id": user_id,
                "nickname": user_id, "msg": {}}

    assert handler._enqueue_pending_reply(entry("owner", "g1")) is True
    assert handler._enqueue_pending_reply(entry("u2")) is True
    # New owner event in another group supersedes the oldest equal-priority
    # item at a full queue instead of being silently dropped.
    assert handler._enqueue_pending_reply(entry("owner", "g2")) is True
    assert len(handler._pending_reply) == 2


def test_pending_exception_does_not_retain_completed_task_marker():
    async def scenario():
        handler = _handler_stub()
        handler._busy_idle_event = asyncio.Event()
        handler._busy_idle_event.set()
        handler._busy_released_tasks = set()
        tasks = []
        handler._safe_task = lambda coro, name="": tasks.append(asyncio.create_task(coro)) or tasks[-1]
        handler._pending_reply.append({"nickname": "queued", "user_id": "u1", "msg": {}})

        async def failing_handle(message):
            message.pop("_pending_admission_token", None)
            raise RuntimeError("pending failure")

        handler.handle_group_message = failing_handle
        handler._schedule_pending_reply()
        try:
            await tasks[0]
        except RuntimeError:
            pass
        else:
            raise AssertionError("pending exception was swallowed")
        assert handler._busy_released_tasks == set()
        assert handler._busy is False

    asyncio.run(scenario())
