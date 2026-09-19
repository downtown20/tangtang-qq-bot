"""生日后台任务的 Store 线程边界回归测试。"""

import asyncio
import threading
import time

from agent.personalization import BirthdayGreeter
from onebot.ws_client import SendResult


def test_birthday_store_reads_and_writes_do_not_block_event_loop():
    main_thread = threading.get_ident()
    calls = []

    class SlowStore:
        def kv_get(self, key):
            calls.append(("get", threading.get_ident()))
            time.sleep(0.03)
            return None

        def kv_set(self, key, value):
            calls.append(("set", threading.get_ident()))
            time.sleep(0.03)

        def get_today_birthdays(self):
            time.sleep(0.01)
            return [{"qq_id": "u1", "nickname": "小明"}]

        def get_or_create_person(self, qq_id):
            calls.append(("person", threading.get_ident()))
            time.sleep(0.03)
            return {"nickname": "小明"}

    async def llm(*_args, **_kwargs):
        return "生日快乐喵～"

    async def send(*_args, **_kwargs):
        return SendResult(True, True, message_id=1)

    greeter = BirthdayGreeter(send, SlowStore(), llm, lambda: ["g1"])

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            await greeter._check_birthdays()
        finally:
            stopped = True
            await task
        return ticks

    ticks = asyncio.run(scenario())

    assert ticks > 0
    assert calls and all(thread_id != main_thread for _name, thread_id in calls)
