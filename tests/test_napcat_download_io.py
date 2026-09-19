"""NapCat 入站媒体下载文件 I/O 的事件循环边界回归测试。"""

import asyncio
import base64
import threading
import time
from pathlib import Path
from unittest import mock

from napcat.ws_client import NapCatClient


def _slow_write_probe():
    main_thread = threading.get_ident()
    writes = []
    original_write = Path.write_bytes

    def slow_write(path_obj, data, *args, **kwargs):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write(path_obj, data, *args, **kwargs)

    return main_thread, writes, slow_write


def _ticker(coro):
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
            result = await coro()
        finally:
            stopped = True
            await task
        return ticks, result

    return scenario


def test_download_record_write_does_not_block_event_loop(tmp_path):
    main_thread, writes, slow_write = _slow_write_probe()
    client = NapCatClient(testing_mode=True)

    async def call_api(*_args, **_kwargs):
        return {"status": "ok", "data": {"file": "https://example.invalid/voice.amr"}}

    class Response:
        status_code = 200
        content = b"voice-bytes"

    class Http:
        async def get(self, *_args, **_kwargs):
            return Response()

    client._call_api = call_api
    client._http = Http()
    save_path = tmp_path / "voice.amr"

    with mock.patch.object(Path, "write_bytes", slow_write):
        ticks, result = asyncio.run(
            _ticker(lambda: client.download_record("record-1", str(save_path)))()
        )

    assert result is True
    assert save_path.is_file()
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)


def test_download_file_base64_write_does_not_block_event_loop(tmp_path):
    main_thread, writes, slow_write = _slow_write_probe()
    client = NapCatClient(testing_mode=True)
    payload = base64.b64encode(b"file-bytes").decode()

    async def call_api(*_args, **_kwargs):
        return {"status": "ok", "data": {"file_name": "memo.txt", "base64": payload}}

    client._call_api = call_api
    save_dir = tmp_path / "files"

    with mock.patch.object(Path, "write_bytes", slow_write):
        ticks, result = asyncio.run(
            _ticker(lambda: client.download_file("file-1", str(save_dir)))()
        )

    assert result and Path(result).is_file()
    assert Path(result).read_bytes() == b"file-bytes"
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)


def test_restart_signal_write_does_not_block_event_loop(tmp_path, monkeypatch):
    import napcat.ws_client as ws_mod

    (tmp_path / "napcat").mkdir()
    monkeypatch.setattr(ws_mod, "__file__", str(tmp_path / "napcat" / "ws_client.py"))
    main_thread, writes, _ = _slow_write_probe()
    original_write_text = Path.write_text

    def slow_write_text(path_obj, data, *args, **kwargs):
        writes.append(threading.get_ident())
        time.sleep(0.05)
        return original_write_text(path_obj, data, *args, **kwargs)

    client = NapCatClient(testing_mode=True)

    with mock.patch.object(Path, "write_text", slow_write_text):
        ticks, result = asyncio.run(
            _ticker(lambda: client._restart_napcat())()
        )

    assert result is True
    assert (tmp_path / ".trigger_restart_snowluma").is_file()
    assert ticks > 0
    assert writes and all(thread_id != main_thread for thread_id in writes)
