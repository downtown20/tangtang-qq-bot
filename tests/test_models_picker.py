# -*- coding: utf-8 -*-
"""「选一个模型」面板 = 一份清单 + 面板内直接切换（2026-10-28 重写）。

## 为什么重写

2026-10-27 的版本是「打标方式(api/ollama) × 模型」两个维度，面板选一次要同时改
`provider` / `model_ref` / `ollama_model` 三个键。主人 2026-10-28 的判断：
「识图方式和打标方式这类方式选择完全不用了，只需要给一个像图库选择模型的那样
就行了，总要选一个模型，没有别的选择。」

所以现在**只有一个 `model_ref`**：

    ''                    → 当前聊天模型（面板里的 `main` 项）
    '<档位 id>'            → 那个档位（面板 `ref:<id>`）
    'ollama:<模型名>'      → 本机 Ollama（面板 `ollama:<模型名>`）

## 这里钉什么

1. picker 有三组：当前聊天模型 / 云端档位 / 本机 Ollama，每项带 kind
2. `current` 三种形态都由 `model_ref` 一个值派生（不再看 provider 猜）
3. 面板里选模型 → **只写 `model_ref` 一个键**（provider 那种第二维度不再写）
4. 就地改配置必须保留注释 + 保留 CRLF（`_patch_image_tagging` 的核心约束）
5. 改之前必须留备份
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_CFG = """\
# 顶部注释：这份文件的注释是它价值的一半
llm:
  models:
  - id: vl
    label: 视觉档
    provider: custom
    base_url: https://example.com/v1
    model: vl-model
    input: [text, image]
  - id: chat
    label: 聊天档
    provider: custom
    base_url: https://example.com/v1
    model: chat-model
    input: [text]
image_tagging:
  # 打标模型：只由 model_ref 决定
  model_ref: ''
  ollama_host: http://127.0.0.1:11434
  max_side: 768
"""


def _load_bridge():
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_picker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(BASE_CFG.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    from agent import image_tagger

    monkeypatch.setattr(image_tagger, "PROJECT_ROOT", tmp_path)
    return mod


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _patch_ollama(mod, models):
    """让 picker 的 Ollama 部分返回固定清单（不打真端口）。"""
    setattr(
        mod,
        "api_ollama_models",
        lambda body: {"ok": True, "host": "http://127.0.0.1:11434", "models": models},
    )


# ── picker 清单 ─────────────────────────────────────────────────────
def test_picker_has_main_cloud_and_ollama_groups(bridge):
    _patch_ollama(bridge, [
        {"id": "qwen3-vl:8b", "name": "qwen3-vl:8b", "vision": True,
         "context_window": 262144, "family": "qwen2vl"},
        {"id": "gemma4:12b", "name": "gemma4:12b", "vision": False,
         "context_window": 0, "family": "gemma4"},
    ])
    out = bridge.api_models_picker({})
    assert out["ok"] is True
    by_group = {g["key"]: g for g in out["groups"]}
    assert set(by_group) == {"main", "profiles", "ollama"}

    # 当前聊天模型是一等选项（主人的「总要选一个模型」）
    main = by_group["main"]["items"][0]
    assert main["kind"] == "main"
    assert main["value"] == ""

    cloud = by_group["profiles"]["items"]
    assert {i["value"] for i in cloud} == {"vl", "chat"}
    assert next(i for i in cloud if i["value"] == "vl")["vision"] is True
    assert next(i for i in cloud if i["value"] == "chat")["vision"] is False

    ollama = by_group["ollama"]["items"]
    assert ollama[0]["badge"] == "256K", "262144 / 1024 = 256"
    assert ollama[0]["vision"] is True
    assert ollama[0]["key"] == "ollama:qwen3-vl:8b"


def test_picker_current_main_when_ref_empty(bridge):
    _patch_ollama(bridge, [])
    assert bridge.api_models_picker({})["current"] == "main"


def test_picker_current_ref(bridge):
    _patch_ollama(bridge, [])
    bridge._patch_image_tagging({"model_ref": "vl"})
    assert bridge.api_models_picker({})["current"] == "ref:vl"


def test_picker_current_ollama(bridge):
    """`ollama:` 前缀原样就是 current —— 不再需要看 provider 二次推断。"""
    _patch_ollama(bridge, [])
    bridge._patch_image_tagging({"model_ref": "ollama:qwen3-vl:8b"})
    assert bridge.api_models_picker({})["current"] == "ollama:qwen3-vl:8b"


def test_picker_reports_ollama_error(bridge):
    setattr(
        bridge,
        "api_ollama_models",
        lambda body: {"ok": False, "host": "h", "models": [], "error": "连不上 Ollama"},
    )
    out = bridge.api_models_picker({})
    assert out["ollama_error"] == "连不上 Ollama"
    assert out["groups"][-1]["key"] == "ollama"


# ── 面板内选模型：只写 model_ref ────────────────────────────────────
def test_select_ref_writes_only_model_ref(bridge):
    cfg = bridge.CONFIG_PATH
    out = bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
    assert out["ok"] is True
    text = _read(cfg)
    assert "  model_ref: vl" in text
    # 第二维度不该再被写：image_tagging 块里没有「打标方式」这个键了
    # （用 `\n  provider:` 而不是 `  provider:`——后者会误匹配到 llm.models 里
    #  缩进 4 空格的 `provider: custom`，这种「断言太宽」的坑踩过一次就够）
    assert "\n  provider:" not in text
    assert "# 顶部注释" in text, "注释必须保留（不是整份 yaml 重写）"
    assert "# 打标模型" in text


def test_select_ollama_writes_prefixed_ref(bridge):
    cfg = bridge.CONFIG_PATH
    bridge.api_gallery_tag_model({"kind": "ollama", "value": "qwen3-vl:8b"})
    text = _read(cfg)
    assert "  model_ref: ollama:qwen3-vl:8b" in text
    assert "\n  ollama_model" not in text, "模型名只有一个出处（model_ref 里）"


def test_select_main_clears_ref(bridge):
    cfg = bridge.CONFIG_PATH
    bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
    bridge.api_gallery_tag_model({"kind": "main", "value": ""})
    assert "  model_ref: ''" in _read(cfg)


def test_select_preserves_crlf(bridge):
    """就地改配置不许把文件的 CRLF 变成 LF（`.env` 上踩过同一形态）。"""
    cfg = bridge.CONFIG_PATH
    assert b"\r\n" in cfg.read_bytes()
    bridge.api_gallery_tag_model({"kind": "ollama", "value": "x:1b"})
    raw = cfg.read_bytes()
    assert b"\r\n" in raw, "保存一次不许把整份文件改成 LF"
    assert b"\n" not in raw.replace(b"\r\n", b""), "不应出现裸 LF"


@pytest.mark.parametrize("body", [
    {"kind": "bogus", "value": "x"},
    {"kind": "", "value": "x"},
    {"kind": "ref", "value": ""},
    {"kind": "ollama", "value": "  "},
])
def test_select_rejects_bad_input(bridge, body):
    with pytest.raises(ValueError):
        bridge.api_gallery_tag_model(body)


def test_patch_creates_backup(bridge):
    cfg = bridge.CONFIG_PATH
    bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
    backups = list(cfg.parent.glob("config.yaml.bak_*"))
    assert backups, "改配置前必须留备份"


def test_context_badge(bridge):
    assert bridge._context_badge(0) == ""
    assert bridge._context_badge(None) == ""
    assert bridge._context_badge(8192) == "8K"
    assert bridge._context_badge(262144) == "256K"
    assert bridge._context_badge(1048576) == "1M"
    assert bridge._context_badge("bad") == ""


# ── 「没有第二维度」的结构闸门 ──────────────────────────────────────
def test_picker_does_not_infer_current_from_provider(bridge):
    """残留的 `provider` 键不许再影响 current（旧配置里它可能还是 `api`）。"""
    _patch_ollama(bridge, [])
    bridge._patch_image_tagging({"provider": "api", "model_ref": ""})
    assert bridge.api_models_picker({})["current"] == "main"
