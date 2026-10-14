import asyncio
import time
from types import SimpleNamespace

from agent.async_io import run_bounded_store_io
from agent.handler import MessageHandler


class _SlowEmbed:
    ready = True

    def encode(self, text):
        time.sleep(0.04)
        return [0.1, 0.2]


class _IndexStore:
    def __init__(self):
        self.latest_calls = 0
        self.indexed = []

    def get_latest_chat_id(self, _qq_id):
        self.latest_calls += 1
        time.sleep(0.04)
        return 999

    def index_chat(self, chat_id, qq_id, clean, vector):
        time.sleep(0.04)
        self.indexed.append((chat_id, qq_id, clean, vector))


def _handler(store):
    return SimpleNamespace(
        embed_engine=_SlowEmbed(),
        memory=SimpleNamespace(store=store),
    )


def test_private_index_uses_logged_chat_id_without_latest_lookup():
    store = _IndexStore()
    handler = _handler(store)

    MessageHandler._index_private_message(
        handler, "1001", "回复内容", chat_id=123,
    )

    assert store.latest_calls == 0
    assert store.indexed and store.indexed[0][0] == 123


def test_private_index_slow_work_runs_outside_event_loop():
    store = _IndexStore()
    handler = _handler(store)
    ticks = 0

    async def run():
        nonlocal ticks
        task = asyncio.create_task(run_bounded_store_io(
            "test.private_index", MessageHandler._index_private_message,
            handler, "1001", "回复内容", 123,
        ))
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.005)
        await task

    asyncio.run(run())

    assert ticks >= 4
    assert store.indexed and store.indexed[0][0] == 123
