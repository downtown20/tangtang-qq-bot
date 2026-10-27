# -*- coding: utf-8 -*-
"""`model_refs.resolve_conn` —— 「选一个模型」的唯一换算点（2026-10-28）。

## 为什么有这个模块

主人原话：「识图方式和打标方式这类方式选择完全不用了，只需要给一个像图库选择
模型的那样就行了，总要选一个模型，没有别的选择。」

改之前是**两个维度相乘**：「方式(main/local/api) × 模型」。组合里大半是坏的——
选 api 没 Key、选 local 没装模型、main 又是纯文本档位。改成**一个字符串**：

    ''                  → 当前聊天档位（llm.active_model）
    'ollama:模型名'      → 本机 Ollama（端点 {host}/v1）
    '<档位 id>'          → 那个档位（连接信息走 llm.providers 组表）

本文件钉的就是这三条语义 + 「别处不许再自己翻 providers」。
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agent.model_refs import (  # noqa: E402
    OLLAMA_PREFIX,
    active_profile,
    ollama_host_of,
    resolve_conn,
)


def _config(profiles=None, active="", providers=None, tag=None, llm_extra=None):
    llm = {
        "provider": "custom",
        "base_url": "https://flat.example/",
        "api_key": "${FLAT_KEY}",
        "model": "flat-model",
        "active_model": active,
    }
    if profiles is not None:
        llm["models"] = profiles
    if providers is not None:
        llm["providers"] = providers
    if llm_extra:
        llm.update(llm_extra)
    cfg = {"llm": llm}
    if tag is not None:
        cfg["image_tagging"] = tag
    return cfg


PROFILES = [
    {
        "id": "default",
        "label": "主聊天档",
        "provider": "custom",
        "model": "deepseek-v4.1-flash",
        "input": ["text"],
        "context_window": 131072,
    },
    {
        "id": "vl",
        "label": "视觉档",
        "provider": "custom",
        "model": "qwen-vl-max",
        "input": ["text", "image"],
    },
]

PROVIDERS = [
    {
        "id": "custom",
        "label": "自建中转",
        "api": "openai-completions",
        "base_url": "https://buddybackend.cloud/",
        "api_key": "${CUSTOM_LLM_KEY}",
    }
]


class TestEmptyMeansMain:
    """留空 = 当前聊天档位（这不再是「没配置」，而是一个明确的选项）。"""

    def test_empty_uses_active_profile(self):
        cfg = _config(PROFILES, active="vl", providers=PROVIDERS)
        conn = resolve_conn(cfg, "")
        assert conn["kind"] == "main"
        assert conn["model"] == "qwen-vl-max"
        # 连接信息来自组表，不是档位里的副本
        assert conn["base_url"] == "https://buddybackend.cloud/"
        assert conn["api_key"] == "${CUSTOM_LLM_KEY}"
        assert "image" in conn["input"]
        assert conn["context_window"] == 0  # vl 没写窗口
        assert conn["error"] == ""

    def test_empty_without_active_falls_back_to_first_profile(self):
        cfg = _config(PROFILES, active="", providers=PROVIDERS)
        conn = resolve_conn(cfg, "")
        assert conn["kind"] == "main"
        assert conn["model"] == "deepseek-v4.1-flash"
        assert conn["context_window"] == 131072

    def test_empty_without_any_profile_uses_flat_llm(self):
        """最老的结构（只有扁平 llm.*）：不许崩，也不许返回空连接。"""
        cfg = _config(None, active="")
        conn = resolve_conn(cfg, "")
        assert conn["kind"] == "flat"
        assert conn["base_url"] == "https://flat.example/"
        assert conn["model"] == "flat-model"


class TestOllamaPrefix:
    def test_ollama_prefix_builds_openai_compatible_endpoint(self):
        cfg = _config(PROFILES, active="default", providers=PROVIDERS,
                      tag={"ollama_host": "http://127.0.0.1:11434"})
        conn = resolve_conn(cfg, "ollama:qwen3-vl:8b")
        assert conn["kind"] == "ollama"
        assert conn["model"] == "qwen3-vl:8b"
        # Ollama 的 /v1 是 OpenAI 兼容端点（2026-10-28 实测能传 base64 图片）
        assert conn["base_url"] == "http://127.0.0.1:11434/v1"
        assert conn["api_key"] == "ollama"  # 客户端要求这个头非空
        assert conn["input"] == []  # 能力未知就别瞎猜
        assert conn["error"] == ""

    def test_ollama_host_comes_from_image_tagging(self):
        cfg = _config(tag={"ollama_host": "http://192.168.1.9:11434/"})
        assert ollama_host_of(cfg) == "http://192.168.1.9:11434"
        conn = resolve_conn(cfg, "ollama:minicpm-v:latest")
        assert conn["base_url"] == "http://192.168.1.9:11434/v1"

    def test_explicit_host_argument_wins(self):
        cfg = _config(tag={"ollama_host": "http://a:1"})
        conn = resolve_conn(cfg, "ollama:x", ollama_host="http://b:2")
        assert conn["base_url"] == "http://b:2/v1"

    def test_empty_ollama_name_is_an_error_not_a_crash(self):
        conn = resolve_conn(_config(), OLLAMA_PREFIX)
        assert conn["error"]
        assert conn["base_url"] == ""


class TestProfileId:
    def test_profile_id_resolves_through_provider_group(self):
        cfg = _config(PROFILES, active="default", providers=PROVIDERS)
        conn = resolve_conn(cfg, "vl")
        assert conn["kind"] == "profile"
        assert conn["label"] == "视觉档"
        assert conn["model"] == "qwen-vl-max"
        assert conn["base_url"] == "https://buddybackend.cloud/"
        assert "image" in conn["input"]

    def test_unknown_profile_id_reports_which_one(self):
        cfg = _config(PROFILES, active="default", providers=PROVIDERS)
        conn = resolve_conn(cfg, "nope")
        assert conn["error"].startswith("找不到模型「nope」")
        assert conn["kind"] == ""


class TestSingleSourceOfTruth:
    """`resolve_conn` 之外，不许再有第二个「翻 llm.providers」的地方（教训 #38）。"""

    @pytest.mark.parametrize("rel", [
        "agent/vision_router.py",
        "agent/image_tagger.py",
        "agent/handler.py",
    ])
    def test_no_module_reads_llm_providers_directly(self, rel):
        source = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            # 只看字面量下标：cfg["llm"]["providers"] 这类写法
            if not isinstance(node, ast.Subscript):
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and key.value == "providers":
                offenders.append(getattr(node, "lineno", 0))
        assert not offenders, (
            f"{rel} 直接读了 providers（第 {offenders} 行）——请改走 "
            "`agent/model_refs.provider_conn()` / `resolve_conn()`")


def test_both_consumers_use_resolve_conn():
    """识图和打标必须用同一个解析函数，否则两处口径又会漂。"""
    for rel in ("agent/vision_router.py", "agent/image_tagger.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "resolve_conn(" in source, f"{rel} 没走 resolve_conn"
        assert "ollama_host_of(" in source, f"{rel} 没走 ollama_host_of"


def test_active_profile_helper_matches_config_shape():
    cfg = _config(PROFILES, active="vl")
    assert active_profile(cfg)["id"] == "vl"
    assert active_profile(_config(None, active="")) is None
