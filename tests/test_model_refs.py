# -*- coding: utf-8 -*-
"""模型档位的「用途」标记与引用解析（2026-10-27）。

## 为什么有这份测试

主人的诉求是「在 AI 模型页统一添加聊天 / 识图 / 本地三种，然后在对应位置选」。
落地时把「用途」和「部署」拆成两个维度（见 `agent/model_refs.py` 的 docstring），
于是「某个用途下有哪些档位」「档位 id → 连接信息」就成了两处会被重复实现的东西。

2026-10-26 的 `allow_groups` 事故就是这种形状：同一个字段表写了两份，加字段时
漏一处 → 静默失效。所以这里钉住的是**单一来源**，不是某个具体字段。

另外两条是「统一到 .env」的落地：
- `api_key: ${VAR}` 必须解析（bridge 进程不 load_dotenv，不解析会把字面量发出去）
- 进程环境没有时回落读 `.env`（bot 能读到、bridge 读不到 = 两套口径）
"""
from __future__ import annotations

import pytest

from agent import model_refs as mr


# ── 用途标记 ────────────────────────────────────────────────────────
def test_kinds_default_to_chat_for_legacy_profiles():
    """老档位没有 `kinds` → 按聊天档处理（它们本来就是聊天档）。"""
    assert mr.kinds_of({"id": "a"}) == ["chat"]
    assert mr.kinds_of({}) == ["chat"]
    assert mr.kinds_of(None) == ["chat"]


def test_kinds_filters_unknown_and_dedups():
    assert mr.kinds_of({"kinds": ["tag", "bogus", "tag", "vision"]}) == ["tag", "vision"]


def test_kinds_accepts_single_string():
    """手写配置容易把数组写成字符串，别让它整个档位失效。"""
    assert mr.kinds_of({"kinds": "tag"}) == ["tag"]


def test_kinds_empty_list_falls_back_to_chat():
    assert mr.kinds_of({"kinds": []}) == ["chat"]


def test_profiles_with_kind_only_returns_marked_ones():
    cfg = {"llm": {"models": [
        {"id": "a", "label": "聊天档", "kinds": ["chat"]},
        {"id": "b", "label": "视觉档", "kinds": ["vision", "tag"]},
    ]}}
    assert [p["id"] for p in mr.profiles_with_kind(cfg, "tag")] == ["b"]
    assert [p["id"] for p in mr.profiles_with_kind(cfg, "chat")] == ["a"]


def test_profiles_with_kind_rejects_unknown_kind():
    """未知用途不返回任何档位——而不是返回全部（fail-closed）。"""
    cfg = {"llm": {"models": [{"id": "a", "kinds": ["chat"]}]}}
    assert mr.profiles_with_kind(cfg, "bogus") == []


def test_profiles_with_kind_skips_entries_without_id():
    cfg = {"llm": {"models": [{"label": "没有 id", "kinds": ["tag"]}]}}
    assert mr.profiles_with_kind(cfg, "tag") == []


# ── 引用解析 ────────────────────────────────────────────────────────
def test_resolve_model_ref_returns_connection_info():
    cfg = {"llm": {"models": [{
        "id": "vl", "label": "视觉档", "kinds": ["tag"],
        "provider": "custom", "base_url": "https://example.com/v1",
        "model": "vl-model", "api_key": "${VISION_KEY}",
    }]}}
    hit = mr.resolve_model_ref(cfg, "vl")
    assert hit is not None
    assert hit["base_url"] == "https://example.com/v1"
    assert hit["model"] == "vl-model"
    # key 原样返回（可能是引用）——解析引用是使用方的事，这里不加工
    assert hit["api_key"] == "${VISION_KEY}"
    assert hit["kinds"] == ["tag"]


def test_resolve_model_ref_missing_returns_none():
    cfg = {"llm": {"models": [{"id": "a"}]}}
    assert mr.resolve_model_ref(cfg, "nope") is None
    assert mr.resolve_model_ref(cfg, "") is None
    assert mr.resolve_model_ref({}, "a") is None


# ── 打标侧落地 ──────────────────────────────────────────────────────
@pytest.fixture()
def tagger_home(tmp_path, monkeypatch):
    """把 image_tagger 的项目根指向临时目录（只影响它读 config/.env）。"""
    from agent import image_tagger

    monkeypatch.setattr(image_tagger, "PROJECT_ROOT", tmp_path)
    return image_tagger, tmp_path


CONFIG_WITH_REF = """\
llm:
  models:
  - id: vl
    label: 视觉档
    provider: custom
    base_url: https://example.com/v1
    model: vl-model
    api_key: ${VISION_TEST_KEY}
    kinds: [tag]
    input: [text, image]
image_tagging:
  provider: api
  model_ref: vl
  api_base: https://ignored.example/v1
  api_model: ignored-model
  api_key: ''
"""


def test_load_config_model_ref_overrides_flat_fields(tagger_home, monkeypatch):
    """选了档位，就以档位为准——扁平字段留在配置里只是兼容旧写法。"""
    tagger, home = tagger_home
    (home / "config.yaml").write_text(CONFIG_WITH_REF, encoding="utf-8")
    monkeypatch.setenv("VISION_TEST_KEY", "sk-from-env")

    cfg = tagger.load_config()
    assert cfg["provider"] == "api"
    assert cfg["api_base"] == "https://example.com/v1"
    assert cfg["api_model"] == "vl-model"
    assert cfg["api_key"] == "sk-from-env", "`${VAR}` 必须解析，不能把字面量当 key"
    assert tagger._provider_ready(cfg) == ""


def test_load_config_falls_back_to_dotenv_file(tagger_home, monkeypatch):
    """进程环境没有该变量时读 `.env`——bridge 进程不 load_dotenv，必须自己读。"""
    tagger, home = tagger_home
    monkeypatch.delenv("VISION_TEST_KEY", raising=False)
    (home / ".env").write_bytes(
        b"# comment\r\nOTHER=1\r\nVISION_TEST_KEY=sk-from-dotenv\r\n")
    (home / "config.yaml").write_text(CONFIG_WITH_REF, encoding="utf-8")

    cfg = tagger.load_config()
    assert cfg["api_key"] == "sk-from-dotenv"


def test_load_config_missing_model_ref_says_which(tagger_home):
    """档位写错时要说清是哪个档位找不到，不许退化成「模型名是空的」。"""
    tagger, home = tagger_home
    (home / "config.yaml").write_text(
        CONFIG_WITH_REF.replace("model_ref: vl", "model_ref: nope"), encoding="utf-8")
    cfg = tagger.load_config()
    reason = tagger._provider_ready(cfg)
    assert "nope" in reason, reason


def test_load_config_literal_key_still_works(tagger_home):
    """不写 model_ref 的老配置必须照旧能用（向后兼容是硬要求）。"""
    tagger, home = tagger_home
    (home / "config.yaml").write_text(
        """\
image_tagging:
  provider: api
  api_base: https://old.example/v1
  api_model: old-model
  api_key: sk-old-literal
""", encoding="utf-8")
    cfg = tagger.load_config()
    assert cfg["api_base"] == "https://old.example/v1"
    assert cfg["api_model"] == "old-model"
    assert cfg["api_key"] == "sk-old-literal"
    assert tagger._provider_ready(cfg) == ""

# ── 档位能力：打标必须能看图（2026-10-28）────────────────────────────
def test_chat_only_profile_is_rejected_for_tagging(tagger_home):
    """选了聊天档（`input` 只有 text）→ 开跑前就拦住。

    实测事故形态：`model_ref=default` 把 api_model 换成 deepseek-v4.1-flash、
    key 从 115 变 51 字符，打标会一路打到模型那边才失败。
    """
    tagger, home = tagger_home
    (home / "config.yaml").write_text(
        """\
llm:
  models:
  - id: chat
    label: 聊天档
    provider: custom
    base_url: https://example.com/v1
    model: chat-model
    api_key: sk-chat
    input: [text]
image_tagging:
  provider: api
  model_ref: chat
""",
        encoding="utf-8",
    )
    cfg = tagger.load_config()
    reason = tagger._provider_ready(cfg)
    assert "不能看图" in reason, reason
    assert "聊天档" in reason, reason


def test_vision_capable_profile_passes_ready_check(tagger_home):
    tagger, home = tagger_home
    (home / "config.yaml").write_text(
        """\
llm:
  models:
  - id: vl
    label: 视觉档
    provider: custom
    base_url: https://example.com/v1
    model: vl-model
    api_key: sk-vl
    input: [text, image]
image_tagging:
  provider: api
  model_ref: vl
""",
        encoding="utf-8",
    )
    cfg = tagger.load_config()
    assert tagger._provider_ready(cfg) == ""
    assert cfg["model_input"] == ["text", "image"]

def test_legacy_profile_without_capability_is_not_blocked(tagger_home):
    """既没写 `input` 也没写 `kinds` 的老档位 —— **不能**被拦。

    「未知」不等于「不能看图」：2026-10-27 之前建的所有档位都属于这一类，
    一律拦住会把老配置全挡在门外。所以 `input_of` 对这种情况返回空表，
    `_provider_ready` 只在「明确标注了能力且不含 image」时才报错。
    """
    tagger, home = tagger_home
    (home / "config.yaml").write_text(
        """\
llm:
  models:
  - id: legacy
    label: 老档位
    provider: custom
    base_url: https://example.com/v1
    model: legacy-model
    api_key: sk-legacy
image_tagging:
  provider: api
  model_ref: legacy
""",
        encoding="utf-8",
    )
    cfg = tagger.load_config()
    assert cfg["model_input"] == [], "未知能力必须是空表，不能猜成 [text]"
    assert tagger._provider_ready(cfg) == "", "老档位不该被挡"
