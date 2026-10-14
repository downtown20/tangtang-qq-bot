"""图片分享下载文件 I/O 的事件循环边界回归测试。"""

import asyncio
import threading
import time
from pathlib import Path
from unittest import mock

from agent.image_share import ImageShareScheduler


def test_download_image_write_does_not_block_event_loop(tmp_path):
    main_thread = threading.get_ident()
    writes = []
    original_write = Path.write_bytes

    class Response:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = b"image-bytes"

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, *_args, **_kwargs):
            return Response()

    def slow_write(path_obj, data):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data)

    scheduler = ImageShareScheduler(
        config={"enabled": True, "local_dir": str(tmp_path)},
        send_group_msg=lambda *_args: None,
        get_group_ids=lambda: [],
    )
    scheduler._tmp_dir = tmp_path

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
            result = await scheduler._download_image("https://example.invalid/image")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch("httpx.AsyncClient", Client), mock.patch.object(
        Path, "write_bytes", slow_write
    ):
        ticks, result = asyncio.run(scenario())

    assert result and result.is_file()
    assert result.suffix == ".png"
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)


def test_fetch_huaban_image_write_does_not_block_event_loop(tmp_path):
    main_thread = threading.get_ident()
    writes = []
    original_write = Path.write_bytes

    class Response:
        def __init__(self, *, payload=None):
            self.status_code = 200
            self.headers = {"content-type": "image/png"}
            self.content = b"image-bytes"
            self._payload = payload

        def json(self):
            return self._payload or {}

    pin = {
        "file": {
            "url": "https://example.invalid/image.png",
            "width": 1200,
            "height": 900,
        },
        "raw_text": "干净插画",
    }

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, *_args, **_kwargs):
            self.calls += 1
            if "search/pins" in url:
                return Response(payload={"pins": [pin]})
            return Response()

    def slow_write(path_obj, data):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data)

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
            from agent.image_share import fetch_huaban_image

            result = await fetch_huaban_image("猫")
        finally:
            stopped = True
            await task
        return ticks, result

    with mock.patch("httpx.AsyncClient", Client), mock.patch(
        "tempfile.gettempdir", return_value=str(tmp_path)
    ), mock.patch.object(Path, "write_bytes", slow_write):
        ticks, result = asyncio.run(scenario())

    assert "[已获取图片]" in result
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)
