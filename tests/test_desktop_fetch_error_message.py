# -*- coding: utf-8 -*-
"""401 的提示语要指对方向（2026-10-28）。

主人截图上看到「401 鉴权失败：这个 Key 与该接口地址不匹配」，于是问
「key 与接口地址不匹配，是协议问题吗？是否有必要扩展新协议？」

但 401 是**鉴权层**的事：协议决定的是请求体/响应体格式，在鉴权之前就死了。
实测（同一把 Key）：
    https://api.deepseek.com/models     -> 401  Your api key: ****0665 is invalid
    https://api.deepseek.com/v1/models  -> 401  同上
两个地址、同一套协议，都 401 —— 就是 Key 本身无效。

所以提示语必须把「Key 无效 / 不属于这家」说清楚，并附服务商原文，
不要让人以为是协议或地址选错了。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_bridge():
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_401_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_fetch_models_401_points_at_the_key_not_the_protocol(monkeypatch):
    import httpx

    body = '{"error":{"message":"Authentication Fails, Your api key: ****0665 is invalid"}}'

    def fake_get(url, **kwargs):
        return _Resp(401, body)

    # `httpx` 是在函数体里 import 的，模块级没有 `mod.httpx`——patch 全局模块本身
    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(ValueError) as ei:
        _load_bridge().api_models_fetch({"base_url": "https://api.deepseek.com",
                                         "api_key": "sk-not-a-real-key"})
    msg = str(ei.value)
    assert "Key 无效" in msg or "这把 Key 无效" in msg, msg
    assert "不是协议" in msg, f"提示语要把「不是协议问题」说明白：{msg}"
    assert "invalid" in msg, f"要附服务商原文，用户才有线索：{msg}"
    assert "不匹配" not in msg or "不属于" in msg, (
        "不要在结论位置留下「地址不匹配」这种误导性说法")


def test_fetch_models_other_4xx_keeps_raw_text(monkeypatch):
    import httpx

    def fake_get(url, **kwargs):
        return _Resp(404, "not found")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(ValueError, match="404"):
        _load_bridge().api_models_fetch(
            {"base_url": "https://example.test", "api_key": "k"})
