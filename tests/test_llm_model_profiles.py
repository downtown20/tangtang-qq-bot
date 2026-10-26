# -*- coding: utf-8 -*-
"""多模型档位（llm.models + active_model）的契约测试。

背景（2026-10-23 桌面端）：桌面端允许自由添加模型档位并在 UI 里切换。
agent 侧的唯一入口是 ``_get_llm_config``——它必须：
  · 有 models 时按 active_model 覆盖扁平字段；
  · **没有 models 的旧配置行为一字不变**（线上老配置不能被这次改动影响）；
  · 色色栈 / 困难轮次的既有优先级不变（先定栈、再在栈内换模型）。
"""
from __future__ import annotations

import types

from agent.handler import MessageHandler


def _handler(llm: dict):
    """_get_llm_config 只读 self.llm_config，用哑对象即可（不构造整个 Handler）。"""
    return types.SimpleNamespace(llm_config=llm)


def _flat(**over):
    cfg = {
        "provider": "custom",
        "model": "flat-model",
        "base_url": "https://flat.example/",
        "api_key": "sk-flat",
        "max_tokens": 1000,
        "temperature": 0.5,
        "thinking": "on",
        "thinking_effort": "low",
    }
    cfg.update(over)
    return cfg


PROFILES = [
    {
        "id": "m1", "label": "主力", "provider": "custom",
        "base_url": "https://a.example/", "api_key": "sk-a",
        "model": "model-a", "max_tokens": 64000,
        "temperature": 0.8, "thinking": "on", "thinking_effort": "high",
    },
    {
        "id": "m2", "label": "备用", "provider": "deepseek",
        "base_url": "https://b.example/", "api_key": "sk-b",
        "model": "model-b", "max_tokens": 32000,
        "temperature": 0.3, "thinking": "off", "thinking_effort": "low",
    },
]


# ── 旧配置：行为必须一字不变 ──────────────────────────────────
def test_legacy_flat_config_is_unchanged():
    llm = _flat()
    out = MessageHandler._get_llm_config(_handler(llm))
    for key in ("provider", "model", "base_url", "api_key",
                "max_tokens", "temperature", "thinking", "thinking_effort"):
        assert out[key] == llm[key], key


def test_empty_profiles_list_falls_back_to_flat():
    llm = _flat(models=[], active_model="")
    out = MessageHandler._get_llm_config(_handler(llm))
    assert out["model"] == "flat-model"


# ── 多档位：按 active_model 覆盖 ──────────────────────────────
def test_active_model_overrides_flat_fields():
    llm = _flat(models=PROFILES, active_model="m2")
    out = MessageHandler._get_llm_config(_handler(llm))
    assert out["model"] == "model-b"
    assert out["provider"] == "deepseek"
    assert out["base_url"] == "https://b.example/"
    assert out["api_key"] == "sk-b"
    assert out["max_tokens"] == 32000
    assert out["temperature"] == 0.3
    assert out["thinking"] == "off"
    assert out["thinking_effort"] == "low"


def test_active_model_missing_id_falls_back_to_first_profile():
    """active 指向不存在的 id 时退回第一条，而不是悄悄用扁平旧模型。"""
    llm = _flat(models=PROFILES, active_model="nope")
    out = MessageHandler._get_llm_config(_handler(llm))
    assert out["model"] == "model-a"
    assert out["api_key"] == "sk-a"


def test_profile_blank_fields_keep_flat_value():
    """档位里留空的字段（如 base_url）不该把扁平值清成空串。"""
    prof = {"id": "m9", "model": "model-9", "base_url": "", "api_key": ""}
    llm = _flat(models=[prof], active_model="m9")
    out = MessageHandler._get_llm_config(_handler(llm))
    assert out["model"] == "model-9"
    assert out["base_url"] == "https://flat.example/"
    assert out["api_key"] == "sk-flat"


# ── 与既有优先级的交互 ────────────────────────────────────────
def test_seductive_stack_wins_over_active_profile():
    """色色栈是「独立 provider/base/key」，优先级高于模型档位。"""
    llm = _flat(
        models=PROFILES, active_model="m1",
        seductive_model="sed-model",
        seductive_provider="deepseek",
        seductive_base_url="https://sed.example/",
        seductive_api_key="sk-sed",
    )
    out = MessageHandler._get_llm_config(
        _handler(llm), scenario=types.SimpleNamespace(name="seductive"))
    assert out["model"] == "sed-model"
    assert out["base_url"] == "https://sed.example/"
    assert out["api_key"] == "sk-sed"


def test_hard_turn_still_switches_model_inside_active_profile():
    """困难轮次只换模型，端点仍是被选中档位的。"""
    llm = _flat(models=PROFILES, active_model="m1", hard_turn_model="hard-model")
    out = MessageHandler._get_llm_config(_handler(llm), hard_turn=True)
    assert out["model"] == "hard-model"
    assert out["base_url"] == "https://a.example/"
    assert out["api_key"] == "sk-a"


def test_non_seductive_scenario_keeps_active_profile():
    llm = _flat(models=PROFILES, active_model="m2")
    out = MessageHandler._get_llm_config(
        _handler(llm), scenario=types.SimpleNamespace(name="psychology"))
    assert out["model"] == "model-b"
