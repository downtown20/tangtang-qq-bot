"""心跳必须分清「掉线」和「鉴权失败」（2026-09-23）。

## 事故

Token 对不上时 HTTP 全 401，而心跳把它报成：

    🔴 HTTP 心跳连续失败，QQ 可能已离线
    🔴 QQ 账号已掉线！请检查是否被挤下线或网络异常
    🟢 QQ 账号已恢复在线            ← 30 秒后又"恢复"

它在掉线/恢复之间来回跳——因为 **WS（不鉴权、正常）说在线，HTTP（401）说掉线**。
真正的原因（`401 ... unauthorized`）明明白白就在上面四行，**但结论行把人往
网络故障上带**，主人为此来回浪费了两次。

更贵的两个副作用：
  1. 控制台靠 `"QQ 账号已掉线"` 这个子串把界面切成离线态（糖糖控制台_qt.py:6599）
     ——账号根本没掉线，是界面在骗人；
  2. 心跳失败计数爬到 10 会触发**重启 SnowLuma**（`should_restart`）——
     而重启修不好 Token，只会反复重启用户的 QQ 网关。

所以修法是：**认得出鉴权失败就只报实情，不改在线状态、不进重启链。**
"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from onebot.ws_client import _is_auth_failure  # noqa: E402

# 事故现场的原样形状（从 tangtang.log 2026-09-23 00:29:48 那几行抄下来的）
INCIDENT_401 = {
    "status": "failed",
    "retcode": 401,
    "msg": '{"status":"failed","retcode":1401,"data":null,"wording":"unauthorized"}',
    "_transport_error": "HTTP_4XX",
}


# ═══════════════════════════════════════════════════════════════════
# 1. 判据本身
# ═══════════════════════════════════════════════════════════════════

def test_incident_shape_is_recognised():
    """**阳性对照**：把 `_is_auth_failure` 改成 `return False` 本用例立刻变红。
    这是那次事故的真实响应体，认不出它就等于没修。"""
    assert _is_auth_failure(INCIDENT_401) is True


@pytest.mark.parametrize("result", [
    {"status": "failed", "retcode": 1401, "wording": "unauthorized"},
    {"status": "failed", "retcode": 403},
    {"status": "failed", "wording": "Forbidden"},
    {"status": "failed", "msg": "invalid access token"},
])
def test_other_auth_shapes_are_recognised(result):
    assert _is_auth_failure(result) is True


@pytest.mark.parametrize("result", [
    {"status": "failed", "_transport_error": "NETWORK"},
    {"status": "failed", "_transport_error": "NETWORK_UNCERTAIN"},
    {"status": "failed", "_transport_error": "HTTP_5XX", "retcode": 502},
    {"status": "failed", "retcode": 100, "wording": "bad request"},
    {"status": "ok"},
    {},
    None,
    "不是字典",
])
def test_non_auth_failures_are_not_misclassified(result):
    """**宁可漏判，不可误判**：误判会让真正的掉线被报成鉴权问题，
    同样把人带偏。判据只认「明确说了是鉴权」的形状。"""
    assert _is_auth_failure(result) is False


def test_network_error_wins_over_auth_words():
    """网络失败即使文案里带 token 字样也不算鉴权——transport 层的信息更硬。"""
    assert _is_auth_failure({
        "status": "failed", "_transport_error": "NETWORK",
        "msg": "connection reset while sending access token",
    }) is False


# ═══════════════════════════════════════════════════════════════════
# 2. 心跳分支：行为验证（不是源码断言）
# ═══════════════════════════════════════════════════════════════════

def _bare_client():
    from onebot.ws_client import NapCatClient
    nc = object.__new__(NapCatClient)
    nc._running = True
    nc._heartbeat_fail_count = 0
    nc._qq_online = True
    nc._offline_since = 0.0
    nc._ever_connected = True
    nc._never_connected_warned = False
    nc._heartbeat_auth_failed = False
    nc._auth_failed_warned = False
    nc.on_qq_offline = None
    nc.on_qq_online = None
    return nc


def _drive_heartbeat(nc, monkeypatch, result, ticks=4):
    calls = {"n": 0}

    async def fake_call(action, params):
        calls["n"] += 1
        if calls["n"] >= ticks:
            nc._running = False
        return result

    nc._call_api = fake_call
    monkeypatch.setattr("onebot.ws_client.asyncio.sleep", AsyncMock())
    asyncio.run(nc._heartbeat_loop())
    return calls["n"]


def test_auth_failure_does_not_mark_qq_offline(monkeypatch, caplog):
    """阳性对照：把心跳里 `if self._heartbeat_auth_failed:` 那段删掉
    （退回无条件标记离线），本用例变红。"""
    nc = _bare_client()
    with caplog.at_level(logging.WARNING):
        _drive_heartbeat(nc, monkeypatch, INCIDENT_401, ticks=25)

    assert nc._qq_online is True, "账号没掉线，被误标成离线——控制台会显示「QQ 掉线」"
    # 真正的不变量不是「计数为 0」，而是**永远到不了重启阈值**
    assert nc._heartbeat_fail_count < 10, (
        f"计数爬到了 {nc._heartbeat_fail_count} → 会触发重启 SnowLuma，"
        "而重启修不好 Token，只会反复重启用户的 QQ 网关")
    assert any("鉴权失败" in r.message for r in caplog.records), (
        "没有报出鉴权失败的真因，用户还是会去查网络")


def test_auth_failure_warns_only_once_per_outage(monkeypatch, caplog):
    """连续失败不该每 90 秒刷一遍同样的错——那是噪音，不是帮助。"""
    nc = _bare_client()
    with caplog.at_level(logging.WARNING):
        _drive_heartbeat(nc, monkeypatch, INCIDENT_401, ticks=10)

    hints = [r for r in caplog.records if "鉴权失败" in r.message]
    assert len(hints) == 1, f"喊了 {len(hints)} 次"


def test_real_offline_still_reports_offline(monkeypatch, caplog):
    """真正的掉线必须保持原样——别把这次的修复变成掩盖真掉线。"""
    nc = _bare_client()
    fired = []

    async def on_offline():
        fired.append(True)

    nc.on_qq_offline = on_offline
    with caplog.at_level(logging.WARNING):
        _drive_heartbeat(nc, monkeypatch, {"status": "failed",
                                           "_transport_error": "NETWORK"})

    assert nc._qq_online is False
    assert fired, "真掉线时 on_qq_offline 回调没被触发"
    assert any("QQ 可能已离线" in r.message for r in caplog.records)
    assert not any("鉴权失败" in r.message for r in caplog.records)


def test_recovery_rearms_the_warning(monkeypatch):
    """恢复了要重新武装：下一次坏掉还得喊，不能一次之后就永久闭嘴。"""
    nc = _bare_client()
    nc._auth_failed_warned = True
    monkeypatch.setattr("onebot.ws_client.asyncio.sleep", AsyncMock())

    calls = {"n": 0}

    async def ok_then_stop(action, params):
        calls["n"] += 1
        nc._running = False
        return {"status": "ok", "data": {"user_id": "10001"}}

    nc._call_api = ok_then_stop
    asyncio.run(nc._heartbeat_loop())
    assert nc._auth_failed_warned is False


# ═══════════════════════════════════════════════════════════════════
# 3. 接线闸门：错误文案必须留下可搜索的指纹
# ═══════════════════════════════════════════════════════════════════

def test_console_parses_the_offline_string_still_exists():
    """控制台用 `"QQ 账号已掉线"` 这个子串切界面状态（糖糖控制台_qt.py:6599）——
    改文案时不许把它改掉，否则主界面会永远显示在线。"""
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "QQ 账号已掉线" in src, "控制台的日志解析会失配"

    console = (Path(__file__).resolve().parents[1]
               / "运维" / "糖糖控制台_qt.py").read_text(encoding="utf-8")
    assert "QQ 账号已掉线" in console
