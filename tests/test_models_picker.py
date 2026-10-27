# -*- coding: utf-8 -*-
"""「选模型」面板 = 一份清单（云端档位 + 本机 Ollama）+ 面板内直接切换。

## 为什么（2026-10-27 主人）

「图库打标设置界面的打标方式和不同打标方式的模型设置都是**分开的、不统一**，
可以按照如图2的方式选择很方便」——图2 是 DSH Harness 的 picker：搜索框 +
按 provider 分组 + 当前项打勾。

「打标方式」（api/ollama）与「模型」是同一个决定的两半，拆成两个控件就会出现
「方式选了 api、模型还停在 ollama 的名字」这种自相矛盾的组合。

## 这里钉什么

1. picker 把两边合成一份**分组**列表（profiles / ollama），每项带 kind
2. `current` 三种形态都算对（ref / ollama / api 老配置）
3. 面板里选模型：`kind=ref` → 档位；`kind=ollama` → 本机模型；**方式与模型同时改**
4. **就地改配置必须保留注释 + 保留 CRLF**（`_patch_image_tagging` 的核心约束）
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
    kinds: [vision, tag]
  - id: chat
    label: 聊天档
    provider: custom
    base_url: https://example.com/v1
    model: chat-model
image_tagging:
  # 打标方式（off / ollama / api）
  provider: 'api'
  model_ref: ''
  ollama_host: http://127.0.0.1:11434
  ollama_model: old-ollama-model
  api_base: https://old.example/v1
  api_model: old-api-model
  api_key: ''
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
    # 打标配置也读同一个项目根
    from agent import image_tagger

    monkeypatch.setattr(image_tagger, "PROJECT_ROOT", tmp_path)
    return mod


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _patch_ollama(mod, models):
    """让 picker 里的 Ollama 部分返回固定清单（不打真端口）。

    ⚠ 必须 setattr 到**被测的那个模块实例**：`_load_bridge()` 用 importlib 现加载
    一份 `bridge_picker_under_test`，与 `tools.desktop_bridge` 是两个不同的模块对象。
    """
    setattr(
        mod,
        "api_ollama_models",
        lambda body: {"ok": True, "host": "http://127.0.0.1:11434", "models": models},
    )


# ── picker 清单 ─────────────────────────────────────────────────────
def test_picker_groups_cloud_and_ollama(bridge, monkeypatch):
    _patch_ollama(bridge, [
        {"id": "qwen3-vl:8b", "name": "qwen3-vl:8b", "vision": True,
         "context_window": 262144, "family": "qwen2vl"},
        {"id": "gemma4:12b", "name": "gemma4:12b", "vision": False,
         "context_window": 0, "family": "gemma4"},
    ])
    out = bridge.api_models_picker({})
    assert out["ok"] is True
    labels = [g["label"] for g in out["groups"]]
    assert any("云端" in l for l in labels)
    assert any("Ollama" in l for l in labels)

    by_group = {g["key"]: g for g in out["groups"]}
    cloud = by_group["profiles"]["items"]
    assert {i["value"] for i in cloud} == {"vl", "chat"}
    assert next(i for i in cloud if i["value"] == "vl")["vision"] is True
    assert next(i for i in cloud if i["value"] == "chat")["vision"] is False

    ollama = by_group["ollama"]["items"]
    assert ollama[0]["badge"] == "256K", "262144 / 1024 = 256"
    assert ollama[0]["vision"] is True
    assert ollama[0]["key"] == "ollama:qwen3-vl:8b"


def test_picker_current_ref(bridge, monkeypatch):
    _patch_ollama(bridge, [])
    bridge._patch_image_tagging({"model_ref": "vl"})
    assert bridge.api_models_picker({})["current"] == "ref:vl"


def test_picker_current_ollama(bridge, monkeypatch):
    _patch_ollama(bridge, [])
    bridge._patch_image_tagging({"provider": "ollama", "model_ref": ""})
    assert bridge.api_models_picker({})["current"].startswith("ollama:")


def test_picker_current_ollama_wins_over_stale_ref(bridge, monkeypatch):
    """provider=ollama 时 model_ref 不生效（load_config 就这么判），
    面板的 current 也必须先看 provider——否则会把「正在用本机模型」
    标成「云端档位 X」（2026-10-27 实测：current=ref:default，
    界面却显示 qwen3-vl:8b-instruct-q8_0）。"""
    _patch_ollama(bridge, [{"id": "qwen3-vl:8b", "name": "qwen3-vl:8b",
                            "vision": True, "context_window": 0, "family": ""}])
    bridge._patch_image_tagging({"model_ref": "vl", "provider": "ollama",
                                 "ollama_model": "qwen3-vl:8b"})
    assert bridge.api_models_picker({})["current"] == "ollama:qwen3-vl:8b"


def test_picker_current_legacy_api(bridge, monkeypatch):
    """老配置：provider=api 但没选档位 → current 指向 api_model。"""
    _patch_ollama(bridge, [])
    assert bridge.api_models_picker({})["current"] == "api:old-api-model"


def test_picker_reports_ollama_error(bridge, monkeypatch):
    setattr(
        bridge,
        "api_ollama_models",
        lambda body: {"ok": False, "host": "h", "models": [], "error": "连不上 Ollama"},
    )
    out = bridge.api_models_picker({})
    assert out["ollama_error"] == "连不上 Ollama"
    assert out["groups"][-1]["key"] == "ollama"


# ── 面板内选模型 ────────────────────────────────────────────────────
def test_select_ref_sets_provider_and_model_ref(bridge):
    cfg = bridge.CONFIG_PATH
    out = bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
    assert out["ok"] is True
    text = _read(cfg)
    assert "  model_ref: vl" in text
    assert "  provider: api" in text
    assert "# 顶部注释" in text, "注释必须保留（不是整份 yaml 重写）"
    assert "# 打标方式" in text


def test_select_ollama_switches_provider_and_clears_ref(bridge):
    cfg = bridge.CONFIG_PATH
    # 先选档位，再切回本机模型：model_ref 必须清空，否则档位会盖掉本地设置
    bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
    bridge.api_gallery_tag_model({"kind": "ollama", "value": "qwen3-vl:8b"})
    text = _read(cfg)
    assert "  provider: ollama" in text
    assert "  ollama_model: qwen3-vl:8b" in text
    assert "  model_ref: ''" in text


def test_select_preserves_crlf(bridge):
    """就地改配置不许把文件的 CRLF 变成 LF（`.env` 上踩过同一形态）。"""
    cfg = bridge.CONFIG_PATH
    assert b"\r\n" in cfg.read_bytes()
    bridge.api_gallery_tag_model({"kind": "ref", "value": "vl"})
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
