# -*- coding: utf-8 -*-
"""「获取可用模型」顺手带回容量（2026-10-28）。

主人最烦的一步是「上下文窗口该填多少，得自己去查」。DeepSeek 的 `/models` 其实
就返回了 `context_window: 1048576` / `max_output_tokens`（实测），所以后端把它
原样带回去（`details`），前端添加模型时自动填进新档位。

判据：拿不到就**不填**——不同中转站的字段名不一样，猜错比空着更糟。
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
    spec = importlib.util.spec_from_file_location("bridge_fetch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = "{}"

    def json(self):
        return self._payload


def _patch(monkeypatch, payload):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp(payload))
    return _load_bridge()


DEEPSEEK_SHAPE = {
    "object": "list",
    "data": [
        {"id": "deepseek-flash", "object": "model",
         "context_window": 1048576, "max_output_tokens": 262144},
        {"id": "deepseek-reasoner", "object": "model",
         "context_window": 131072},
        {"id": "mystery-model", "object": "model"},          # 什么容量都没报
    ],
}


def test_fetch_returns_capacity_details(monkeypatch):
    mod = _patch(monkeypatch, DEEPSEEK_SHAPE)
    out = mod.api_models_fetch({"base_url": "https://api.deepseek.com", "api_key": "k"})
    assert out["models"] == ["deepseek-flash", "deepseek-reasoner", "mystery-model"]
    by_id = {d["id"]: d for d in out["details"]}
    assert by_id["deepseek-flash"]["context_window"] == 1048576
    assert by_id["deepseek-flash"]["max_tokens"] == 262144
    assert by_id["deepseek-reasoner"]["context_window"] == 131072
    assert "max_tokens" not in by_id["deepseek-reasoner"], "没报就别瞎填"
    assert by_id["mystery-model"] == {"id": "mystery-model"}, "什么都没有就只有 id"


def test_fetch_keeps_models_list_backward_compatible(monkeypatch):
    """老前端只读 `models`，这个字段不能因为加了 details 就变形。"""
    mod = _patch(monkeypatch, DEEPSEEK_SHAPE)
    out = mod.api_models_fetch({"base_url": "https://x.test", "api_key": "k"})
    assert isinstance(out["models"], list)
    assert out["count"] == len(out["models"]) == 3


def test_fetch_ignores_non_positive_capacity(monkeypatch):
    """0 / 负数 / 字符串都当「没报」，不许写成 0 或 NaN。"""
    mod = _patch(monkeypatch, {"data": [
        {"id": "a", "context_window": 0, "max_output_tokens": -1},
        {"id": "b", "context_window": "131072", "max_output_tokens": "64000"},
    ]})
    out = mod.api_models_fetch({"base_url": "https://x.test", "api_key": "k"})
    by_id = {d["id"]: d for d in out["details"]}
    assert by_id["a"] == {"id": "a"}
    assert by_id["b"] == {"id": "b"}, "字符串不当数字用（不同端点含义不一，别猜）"


def test_fetch_still_rejects_non_openai_shape(monkeypatch):
    mod = _patch(monkeypatch, {"result": "not-a-list"})
    with pytest.raises(ValueError, match="data"):
        mod.api_models_fetch({"base_url": "https://x.test", "api_key": "k"})
