"""AI 画图落盘的事件循环边界回归测试。"""

import asyncio
import threading
import time
from pathlib import Path
from unittest import mock

from agent.image_gen import ImageGenEngine


def test_generated_image_write_does_not_block_event_loop(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    writes = []
    original_write = Path.write_bytes

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload=None, content=b""):
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload or {}

    class Client:
        async def post(self, *_args, **_kwargs):
            return Response({
                "output": {
                    "task_id": "task-1",
                    "task_status": "SUCCEEDED",
                    "results": [{"url": "https://example.invalid/generated.png"}],
                }
            })

        async def get(self, *_args, **_kwargs):
            return Response(content=b"generated-image")

    def slow_write(path_obj, data):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data)

    engine = ImageGenEngine(api_key="test")
    engine._client = Client()
    monkeypatch.chdir(tmp_path)

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
            result = await engine.generate("一只戴帽子的猫")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch.object(Path, "write_bytes", slow_write):
        ticks, result = asyncio.run(scenario())

    assert result and "[CQ:image" in result
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)
    assert list((tmp_path / "generated_images").glob("*.png"))
