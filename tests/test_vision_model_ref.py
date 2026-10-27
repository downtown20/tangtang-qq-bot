# -*- coding: utf-8 -*-
"""识图也能「选档位」（2026-10-28 第 3 件）。

## 为什么

主人：「在『AI 模型』统一添加……然后在对应位置选择添加的模型即可」。
打标已经能用 `image_tagging.model_ref` 挑档位，识图还停在手填
`provider / base_url / model / api_key` 四行——同一个模型要填两遍。

## 这里钉什么

1. `llm.vision.model_ref` 存在时，用档位的 base_url / model / key 覆盖手填值
2. 档位是云端连接 → `provider` 一律归成 `api`（选档位 = 走云端那条路）
3. **覆盖进来的值也要解析 `${VAR}`**（只解析一次就会把字面量发出去 → 401）
4. 档位 id 写错时给 `model_ref_error`，而不是静默用回手填值
5. 没有 `model_ref` 时行为完全不变（向后兼容）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import vision_router  # noqa: E402

BASE = """\
llm:
  vision:
    enabled: true
    provider: main
    api_key: ${QWEN_KEY}
    base_url: https://old.example/v1
    model: old-vl
    model_ref: ''
  models:
  - id: vl
    label: 视觉档
    provider: custom
    base_url: https://example.com/v1
    model: qwen-vl-max
    api_key: ${VL_KEY}
    input: [text, image]
"""


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(BASE, encoding="utf-8")
    monkeypatch.setattr(vision_router, "CONFIG_PATH", path)
    monkeypatch.delenv("QWEN_KEY", raising=False)
    monkeypatch.delenv("VL_KEY", raising=False)
    return path


def test_no_model_ref_keeps_legacy_behaviour(cfg_path):
    cfg = vision_router._load_vision_cfg()
    assert cfg["provider"] == "main"
    assert cfg["base_url"] == "https://old.example/v1"
    assert cfg["model"] == "old-vl"


def test_model_ref_overrides_connection(cfg_path, monkeypatch):
    monkeypatch.setenv("VL_KEY", "sk-vl-from-env")
    text = cfg_path.read_text(encoding="utf-8").replace("model_ref: ''", "model_ref: vl")
    cfg_path.write_text(text, encoding="utf-8")

    cfg = vision_router._load_vision_cfg()
    assert cfg["provider"] == "api", "选了云端档位就走 api 那条路"
    assert cfg["base_url"] == "https://example.com/v1"
    assert cfg["model"] == "qwen-vl-max"
    # 档位里的 ${VL_KEY} 必须被解析——只解析原配置会把字面量发出去 → 401
    assert cfg["api_key"] == "sk-vl-from-env"
    assert cfg["model_ref_label"] == "视觉档"


def test_unknown_ref_reports_which(cfg_path):
    text = cfg_path.read_text(encoding="utf-8").replace("model_ref: ''", "model_ref: nope")
    cfg_path.write_text(text, encoding="utf-8")
    cfg = vision_router._load_vision_cfg()
    assert "nope" in cfg.get("model_ref_error", "")


def test_env_ref_without_value_stays_literal(cfg_path):
    """环境变量没有时保留字面量——与既有行为一致（调用方会看到 401 并记日志）。"""
    text = cfg_path.read_text(encoding="utf-8").replace("model_ref: ''", "model_ref: vl")
    cfg_path.write_text(text, encoding="utf-8")
    cfg = vision_router._load_vision_cfg()
    assert cfg["api_key"] == "${VL_KEY}"
