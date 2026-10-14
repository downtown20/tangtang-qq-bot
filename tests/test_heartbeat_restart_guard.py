"""心跳重启链的护栏：「从没连上过」不等于「掉线了」。

## 为什么要有这个文件

2026-09-20 新手路径审计发现的误导源：
SnowLuma 面板里的 `WS 客户端` 还没配时，糖糖这边 `get_login_info` 当然一直失败，
连续 10 次（约 5 分钟）就触发 `_restart_napcat()` → 控制台收到重启信号 →
做一次（本来就杀不中进程的）"重启"，然后 `await asyncio.sleep(25)` 再重来。

结果：日志里反复刷 `🔄 尝试重启 SnowLuma...` / `✅ SnowLuma 已自动重启`，
把**真病因**（压根还没接上）讲成一桩"掉线事故"。用户按"掉线"去查，永远查不到。

判据：只有**见过连上**（`_ever_connected`）才允许走重启链；没连上过就只提醒一次。
"""

import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from onebot.ws_client import NapCatClient


def _drive(*, ever_connected: bool, fail_start: int, iterations: int = 14):
    """把心跳循环跑几圈（把 sleep 换成让出控制权），收集它到底重不重启。"""
    restarts: list = []

    async def scenario():
        client = NapCatClient(testing_mode=True)
        client._running = True
        client._ever_connected = ever_connected
        client._never_connected_warned = False
        client._heartbeat_fail_count = fail_start

        seen = {"n": 0}

        async def fake_call_api(action, params):
            seen["n"] += 1
            if seen["n"] >= iterations:
                client._running = False       # 跑够圈数就收工
            raise ConnectionError("没有服务端（模拟未连接）")

        async def fake_restart():
            restarts.append(seen["n"])
            return True

        client._call_api = fake_call_api
        client._restart_napcat = fake_restart

        real_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await real_sleep(0)               # 让出控制权但不真等

        asyncio.sleep = fast_sleep
        try:
            await client._heartbeat_loop()
        finally:
            asyncio.sleep = real_sleep
        return client

    client = asyncio.run(scenario())
    return restarts, client


def test_never_connected_does_not_trigger_restart():
    """从没连上过 → 一次重启都不许发。

    这是新手最常见、也最容易被误导的状态。发重启信号会把日志刷满
    「尝试重启 SnowLuma」，而真正要做的是去 SnowLuma 面板里建那条 WS 客户端。
    """
    restarts, client = _drive(ever_connected=False, fail_start=9)

    assert not restarts, (
        f"从没连上过却发了 {len(restarts)} 次重启信号——"
        f"日志会把「还没接上」讲成「掉线了」，用户按掉线去查永远查不到")


def test_never_connected_resets_counter_so_it_does_not_spam():
    """只提醒一次，且不再累计——否则每 5 分钟还是一轮噪音。"""
    _, client = _drive(ever_connected=False, fail_start=10)

    assert client._never_connected_warned, "从没连上过时应当提醒过一次"
    assert client._heartbeat_fail_count < 10, (
        f"计数没有归零（{client._heartbeat_fail_count}）——下一轮又会满足重启条件")


def test_after_a_real_connection_restart_still_works():
    """连上过再掉线 → **必须**照旧触发重启。护栏不能把真功能一起挡掉。"""
    restarts, _ = _drive(ever_connected=True, fail_start=9)

    assert restarts, (
        "连上过之后掉线，重启链没有触发——护栏写过头了，"
        "「掉线自动重启」这个功能被一起关掉了")
