# -*- coding: utf-8 -*-
"""模型切换器要显示「你在 AI 模型页起的名字」（2026-10-28）。

主人报：「预览界面的模型名字没有和 AI 模型设置的名字同步」。
截图里那两行是 `deepseek-flash` / `pmvro2c5y` —— 一个是模型 **ID**、一个是
provider 组 **id**，都不是人起的那两个名字。

根因两处（`ModelSwitcher.tsx` 与 `/api/models`）：
  1. 主行写的是 `p.model || p.label` —— 优先模型 ID，起的显示名只在副行当附注；
  2. 副行写的是 `p.provider` —— 组 id，而 `api_models()` 根本没返回 `provider_label`，
     前端想显示人话名也拿不到。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "desktop" / "src"

BASE_CFG = """\
llm:
  providers:
  - id: pmvro2c5y
    label: 官方Deepseek
    api: openai-completions
    base_url: https://api.deepseek.com
    api_key: ${DEEPSEEK_KEY}
  models:
  - id: flash
    label: 快速档（我起的名字）
    provider: pmvro2c5y
    model: deepseek-flash
  active_model: flash
"""


def _load_bridge():
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_switch_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_api_models_returns_provider_human_label(tmp_path, monkeypatch):
    """`/api/models` 必须带上 provider 的人话名，否则前端只能显示组 id。"""
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE_CFG, encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)

    out = mod.api_models()
    profile = out["profiles"][0]
    assert profile["label"] == "快速档（我起的名字）", "显示名要原样带出来"
    assert profile["provider"] == "pmvro2c5y"
    assert profile["provider_label"] == "官方Deepseek", (
        f"没有 provider_label，切换器只能显示组 id：{profile}")
    assert "api_key" not in profile, "Key 仍然不许出网关"


def test_switcher_renders_label_first_and_provider_label():
    """静态契约：主行 label 优先、副行用 provider_label（模型 ID 退到附注）。"""
    text = (SRC / "components" / "ModelSwitcher.tsx").read_text(encoding="utf-8")
    assert "{p.label || p.model}" in text, "主行必须是 label 优先"
    assert "{p.model || p.label}" not in text, "别退回去按模型 ID 显示"
    assert "p.provider_label || p.provider" in text, "副行要用 provider 的人话名"
