"""本地 Ollama 识图请求契约回归测试。"""

import logging

from agent import vision_router
from agent.vision_local import MODEL, NUM_CTX, MiniCPMVision


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response=None):
        self.response = response or _Response(
            payload={"response": "一幅动漫风格的角色插画"},
        )
        self.calls = []

    def get(self, *_args, **_kwargs):
        return _Response(payload={"models": [{"name": MODEL}]})

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_local_vision_uses_bounded_context_to_avoid_ollama_oom():
    engine = MiniCPMVision()
    client = _Client()
    engine._client = client

    assert engine.available is True
    assert engine.describe(b"image-bytes", "描述图片") == "一幅动漫风格的角色插画"

    payload = client.calls[-1][1]["json"]
    assert payload["model"] == MODEL
    assert payload["options"]["num_ctx"] == NUM_CTX
    assert payload["options"]["num_predict"] == 160


def test_local_vision_logs_ollama_error_body(caplog):
    engine = MiniCPMVision()
    engine._client = _Client(
        _Response(status_code=400, text='{"error":"std::bad_alloc"}')
    )

    with caplog.at_level(logging.WARNING, logger="糖糖.Vision"):
        assert engine.describe(b"image-bytes", "描述图片") == ""

    assert any("Ollama 识图 HTTP 400" in record.message for record in caplog.records)
    assert any("std::bad_alloc" in record.message for record in caplog.records)


def test_vision_router_resolves_env_references(monkeypatch):
    monkeypatch.setenv("QWEN_KEY", "test-qwen-key")
    cfg = vision_router._load_vision_cfg()
    assert cfg["api_key"] == "test-qwen-key"
