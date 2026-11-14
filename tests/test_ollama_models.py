# -*- coding: utf-8 -*-
"""Ollama 模型自动获取（2026-10-27）。

## 为什么需要

主人原话：「ollama 的模型需要**自动获取**并通过选项选择模型，可以完全对标现在
ollama 接入 dsh harness 的方式」。

现状是 `image_tagging.ollama_model` 和档位里的 `model` 都靠**手填字符串**，
填错要等真跑起来（一张图 1.5 秒 + 报错）才发现。

DSH Harness 那边的形状（`~/.ollama/launch/dsh/settings.yaml` 实证）：

    llm-pi-ai:
      providers:
        ollama:
          baseURL: http://127.0.0.1:11434/v1
          models:
            - id: qwen3-vl:8b
              contextWindow: 262144
              input: [text, image]     # ← 这个直接决定「能不能识图/打标」

这里对齐同一个形状：`/api/models/ollama` 返回 `{id, name, input, context_window, ...}`，
`input` 含 `image` 就说明这个模型能看图。

## 数据来源与降级

- `GET  {root}/api/tags` → 列表
- `POST {root}/api/show` → `capabilities`（含 `vision`）+ `context_length`

`/api/show` 探不到时按名字猜（`vl` / `llava` / `minicpm-v` …）——宁可多标一个
能看图的，也不要让主人对着列表瞎猜。Ollama 没启动时返回 `ok: False` 而不是抛错。
"""
from __future__ import annotations

import httpx
import pytest

from tools import desktop_bridge as bridge


@pytest.fixture(autouse=True)
def _pretend_port_open(monkeypatch):
    """把本机 TCP 预检打成「端口通」，让用例走到它真正要测的 HTTP 逻辑。

    2026-11-14：`api_ollama_models` 在发 HTTP 之前先做 0.4s 的 TCP 预检（本机端点
    没启动时直接返回一句人话，避免拖住云端档位）。测试机上 11434 通常**本来就没开**，
    于是所有用例都被短路在预检分支——测的是一条分支，却以为是另一条。
    需要测预检分支的用例在自身内部再覆盖一次即可（后设的赢）。
    """
    monkeypatch.setattr(bridge, "_port_open", lambda *a, **k: True)


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def _patch_ollama(monkeypatch, models, caps=None, tags_status=200, show_fail=()):
    caps = caps or {}
    seen = {"get": 0, "post": 0}

    def fake_get(url, timeout=None, **kwargs):
        seen["get"] += 1
        if tags_status >= 400:
            return _Resp({}, status=tags_status)
        return _Resp({"models": models})

    def fake_post(url, json=None, timeout=None, **kwargs):
        seen["post"] += 1
        name = (json or {}).get("model", "")
        if name in show_fail:
            return _Resp({}, status=500)
        return _Resp(caps.get(name, {"capabilities": ["completion"], "model_info": {}}))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_lists_models_and_marks_vision(monkeypatch):
    models = [
        {"name": "gemma4:12b", "size": 456, "details": {"family": "gemma4"}},
        {"name": "qwen3-vl:8b", "size": 123,
         "details": {"family": "qwen2vl", "parameter_size": "8B"}},
    ]
    caps = {
        "qwen3-vl:8b": {"capabilities": ["completion", "vision"],
                        "model_info": {"qwen2vl.context_length": 262144}},
        "gemma4:12b": {"capabilities": ["completion"],
                       "model_info": {"gemma4.context_length": 8192}},
    }
    _patch_ollama(monkeypatch, models, caps)

    out = bridge.api_ollama_models({"host": "http://127.0.0.1:11434"})
    assert out["ok"] is True
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["qwen3-vl:8b"]["input"] == ["text", "image"]
    assert by_id["qwen3-vl:8b"]["context_window"] == 262144
    assert by_id["qwen3-vl:8b"]["vision"] is True
    assert by_id["gemma4:12b"]["input"] == ["text"]
    assert out["models"][0]["id"] == "qwen3-vl:8b", "能看图的排前面（打标/识图最常用）"


def test_v1_suffix_is_stripped_for_native_api(monkeypatch):
    """设置里填 `.../v1`（OpenAI 兼容）也要能找到 `/api/tags`。"""
    _patch_ollama(monkeypatch, [{"name": "m:latest"}])
    out = bridge.api_ollama_models({"host": "http://127.0.0.1:11434/v1"})
    assert out["host"] == "http://127.0.0.1:11434"


def test_vision_falls_back_to_name_hint(monkeypatch):
    """/api/show 失败时按名字猜——不让主人在「能不能看图」上瞎猜。"""
    _patch_ollama(monkeypatch, [{"name": "llava:13b"}], show_fail=("llava:13b",))
    out = bridge.api_ollama_models({"host": "http://127.0.0.1:11434"})
    assert out["models"][0]["vision"] is True


def test_unreachable_ollama_reports_error_not_raise(monkeypatch):
    """Ollama 没启动：返回 ok=False + 原因，界面可以显示「拉不到」。

    ⚠ 必须先把 TCP 预检打成「端口通」，否则走的是预检那条分支（见下一条测试），
    这条就测不到它想测的东西——「HTTP 层 ConnectError 不往上抛」。
    """
    monkeypatch.setattr(bridge, "_port_open", lambda *a, **k: True)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    out = bridge.api_ollama_models({"host": "http://127.0.0.1:11434"})
    assert out["ok"] is False
    assert out["models"] == []
    assert "连不上 Ollama" in out["error"]


def test_closed_port_is_reported_before_any_http(monkeypatch):
    """本机端口不通时先走预检分支，一句人话，且**一次 HTTP 都不发**。

    这条是与上一条配对的分支覆盖：预检短路是 2026-10-28 加的（真机上 Ollama 没启动
    会让「选模型」下拉转圈 6 秒，还连带拖住云端档位）。两处错误文案不同，但都必须
    含「连不上」——界面只按这一句判「拉不到」。
    """
    monkeypatch.setattr(bridge, "_port_open", lambda *a, **k: False)
    seen = {"get": 0}

    def counting_get(*args, **kwargs):
        seen["get"] += 1
        raise AssertionError("预检已经判定不通，不该再发 HTTP")

    monkeypatch.setattr(httpx, "get", counting_get)
    out = bridge.api_ollama_models({"host": "http://127.0.0.1:11434"})
    assert out["ok"] is False
    assert out["models"] == []
    assert "连不上" in out["error"]
    assert seen["get"] == 0, "预检分支必须短路掉 HTTP 请求"


def test_missing_models_list_is_reported(monkeypatch):
    _patch_ollama(monkeypatch, [])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"not_models": []}))
    out = bridge.api_ollama_models({})
    assert out["ok"] is False
    assert "models" in out["error"]


@pytest.mark.parametrize("raw,expect", [
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434"),
    ("http://127.0.0.1:11434/api", "http://127.0.0.1:11434"),
    ("", "http://127.0.0.1:11434"),
])
def test_ollama_root_normalization(raw, expect):
    assert bridge._ollama_root(raw) == expect
