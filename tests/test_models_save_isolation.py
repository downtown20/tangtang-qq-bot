# -*- coding: utf-8 -*-
"""模型目录 vs 设置页快照：两个写入口不许互相踩（2026-10-28 实测 bug 的回归）。

## 事故（主人报的）

「AI 模型」页里给模型填「上下文窗口」65536 → 点「保存参数」→ 值被清空。

取证结论（Lead 已端到端复现）：`api_models_save()` 本身**是对的**（保存前
context_window=0，保存后 65536，落盘 YAML 也是 65536）。根因在另一个写入口：
设置页底部那个「保存」走 `POST /api/config` → `api_config_save()`，它带的是
**打开设置页那一刻**拉的整份配置快照，那份快照里 `context_window` 还是 0。
用户先点「保存参数」写 65536，再点底部「保存」，旧快照把 65536 覆盖回 0。

## 这里钉什么

1. `api_config_save` **永不写** `llm.models` / `llm.active_model` / `llm.providers`
   —— 旧快照回写不生效（教训 #51 同一事实只许一个派生点 / #54 组级属性只留一份）
2. 但**也不能删**它们：`api_config_save` 是整份 dict 重写，键不在就等于把模型
   目录从文件里删掉（用户所有档位凭空消失）。所以是「旧值不生效、现役值保留」
3. 剥模型目录**不等于**整段拒绝保存：设置页其余字段照常落盘
4. 请求体脏（`llm` 不是 dict / `models` 是字符串 None 或含垃圾）时不许抛、
   不许把文件写坏
5. `context_window` 以字符串提交 → 落盘必须是 int（类型契约见
   `desktop/src/lib/api.ts:54` `context_window?: number | null`；读侧到处
   `int()` 兜底只会把坏值掩盖掉，和 max_tokens 的 2026-10-24 事故同族）
6. 端到端：`api_models_save` 存新值后，再走一次 `api_config_save`（带旧快照），
   值仍是新值

## 阳性对照（每一道闸门都要能红，见 变异/配置写入隔离.json）

- `for key in MODEL_CATALOG_KEYS:` 整段失效 → 第 1 条红
- `MODEL_CATALOG_KEYS` 只留 models → 第 2 条红
- 覆盖改成 `setdefault`（旧值仍生效）→ 第 1 条红
- 数值收口去掉 context_window → 第 5 条红
- `llm` 非 dict 时把脏值落盘 → 第 4 条红

**绝不碰真实 `config.yaml`**：CONFIG_PATH / ROOT / ENV_PATH 全部 monkeypatch 到
tmp_path（与 tests/test_models_picker.py 同口径）。
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 磁盘上的现状：context_window 已经是对的 65536（用户点过「保存参数」之后）
BASE_CFG = """\
# 顶部注释：这份文件的注释是它价值的一半
llm:
  provider: custom
  model: chat-model
  models:
  - id: chat
    label: 聊天档
    provider: custom
    model: chat-model
    max_tokens: 64000
    temperature: 0.8
    context_window: 65536
  active_model: chat
  providers:
  - id: custom
    label: 中转站
    base_url: https://example.com/v1
    api_key: ${CUSTOM_LLM_KEY}
behavior:
  interjection_thirst: 0.23
"""


def _load_bridge():
    """每次 exec 一份新模块：模块级常量（CONFIG_PATH 等）可被 monkeypatch。"""
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_models_iso_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_bytes(BASE_CFG.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(mod, "_example_bootstrapped", False)
    return mod


def _cfg(bridge) -> dict:
    """读回磁盘上的配置（唯一判据：落盘后的文件，不看返回值）。"""
    return yaml.safe_load(bridge.CONFIG_PATH.read_bytes().decode("utf-8"))


def _stale_snapshot(bridge) -> dict:
    """模拟「设置页打开那一刻拉的快照」——里面模型目录还是旧值。

    真实场景：用户打开设置页时 `llm.models[].context_window` 还是 0，
    页面上那份 React state 一直没刷新，点底部「保存」时原样发了回来。
    """
    snap = copy.deepcopy(_cfg(bridge))
    for item in snap["llm"]["models"]:
        item["context_window"] = 0
        item["max_tokens"] = 4096
    snap["llm"]["active_model"] = ""
    snap["llm"]["providers"] = []
    return snap


# ── 1. 旧快照不许覆盖 context_window（本次事故本体）─────────────
def test_config_save_cannot_overwrite_context_window(bridge):
    snap = _stale_snapshot(bridge)
    assert snap["llm"]["models"][0]["context_window"] == 0, "前提：快照里是旧值"

    out = bridge.api_config_save({"config": snap})

    assert out["ok"] is True
    assert "llm.models" in out["preserved_models"], "要说得清这次保住了什么"
    models = _cfg(bridge)["llm"]["models"]
    assert [m["id"] for m in models] == ["chat"]
    assert models[0]["context_window"] == 65536, "65536 被旧快照的 0 覆盖了"
    assert models[0]["max_tokens"] == 64000, "整个 llm.models 都不许被回写"


# ── 2. providers / active_model 同样不许被回写 ──────────────────
def test_config_save_cannot_overwrite_providers_or_active_model(bridge):
    snap = _stale_snapshot(bridge)

    out = bridge.api_config_save({"config": snap})

    assert out["ok"] is True
    assert {"llm.providers", "llm.active_model"} <= set(out["preserved_models"])
    llm = _cfg(bridge)["llm"]
    assert llm["active_model"] == "chat", "当前档位被清空了"
    assert [p["id"] for p in llm["providers"]] == ["custom"], "provider 组表被清空了"
    assert llm["providers"][0]["base_url"] == "https://example.com/v1"
    assert llm["providers"][0]["api_key"] == "${CUSTOM_LLM_KEY}"


# ── 3. 剥掉模型目录 ≠ 拒绝保存：其余字段照旧 ───────────────────
def test_config_save_still_saves_other_fields(bridge):
    snap = _stale_snapshot(bridge)
    snap["behavior"]["interjection_thirst"] = 0.77
    snap["scenario_targets"] = {"2524950083": "亲密模式"}
    snap["tuning"] = {"context_soft_limit": 40000}

    out = bridge.api_config_save({"config": snap})

    assert out["ok"] is True
    cfg = _cfg(bridge)
    assert cfg["behavior"]["interjection_thirst"] == 0.77, "设置页的字段必须还能存"
    assert cfg["scenario_targets"] == {"2524950083": "亲密模式"}
    assert cfg["tuning"] == {"context_soft_limit": 40000}
    # 顺手复查：这次保存没有把模型目录弄丢
    assert cfg["llm"]["models"][0]["context_window"] == 65536


def test_config_save_keeps_catalog_when_body_omits_llm(bridge):
    """`merge_missing_fields=False` 的调用方（群管理/高级调参）只发自己那段。

    这种 body 里根本没有 `llm`。整份 dict 重写会让模型目录直接消失，
    所以必须从磁盘补回来——不能只想着「剥掉请求体里的值」。
    """
    out = bridge.api_config_save({"config": {"behavior": {"interjection_thirst": 0.42}}},
                                 merge_missing_fields=False)

    assert out["ok"] is True
    cfg = _cfg(bridge)
    assert cfg["behavior"]["interjection_thirst"] == 0.42
    assert cfg["llm"]["models"][0]["context_window"] == 65536, "模型目录被整段删掉了"
    assert cfg["llm"]["active_model"] == "chat"


# ── 4. 脏请求体：不抛、不写坏 ────────────────────────────────────
@pytest.mark.parametrize("llm_value", ["oops", None, [], 0, "models: []"])
def test_config_save_survives_non_dict_llm(bridge, llm_value):
    """`llm` 不是对象时：不许 500，也不许把 `llm: oops` 写进文件。"""
    before = _cfg(bridge)
    snap = {"llm": llm_value, "behavior": {"interjection_thirst": 0.5}}

    out = bridge.api_config_save({"config": snap})     # 不许抛异常

    assert out["ok"] is True
    cfg = _cfg(bridge)                                  # 文件仍可解析
    assert isinstance(cfg["llm"], dict), "llm 段被脏值写坏了"
    assert cfg["llm"]["models"] == before["llm"]["models"]
    assert cfg["behavior"]["interjection_thirst"] == 0.5, "其它字段仍要存"


@pytest.mark.parametrize("models_value", [
    "not-a-list",
    None,
    42,
    [{"id": "chat", "context_window": 0}, "垃圾", 42],
    [{"id": "chat", "context_window": "0"}],
    {},
])
def test_config_save_survives_dirty_models(bridge, models_value):
    """`llm.models` 是脏数据时：落盘后必须还是磁盘上那一份，且没坏。"""
    before = _cfg(bridge)
    snap = _stale_snapshot(bridge)
    snap["llm"]["models"] = models_value

    out = bridge.api_config_save({"config": snap})     # 不许抛异常

    assert out["ok"] is True
    cfg = _cfg(bridge)
    assert cfg["llm"]["models"] == before["llm"]["models"], "脏 models 被写进盘了"
    assert cfg["llm"]["models"][0]["context_window"] == 65536


# ── 5. context_window 的写法收口（字符串 → int）────────────────
def test_models_save_coerces_string_context_window(bridge):
    """前端把数字当字符串发过来 → 落盘必须是 int。

    和 2026-10-24 的 max_tokens 事故同族，但**更隐蔽**：字符串 max_tokens 会让
    网关 500（看得见）；字符串 context_window 谁也不报错——`_int_or()`
    （agent/model_refs.py:243）和 `_context_badge()` 都用 `int()` 兜底静静接受。
    读侧兜底掩盖了写侧没归一，所以这里钉落盘类型。
    """
    out = bridge.api_models_save({"models": [{
        "id": "chat",
        "label": "聊天档",
        "provider": "custom",
        "model": "chat-model",
        "max_tokens": "64000",
        "temperature": "0.8",
        "context_window": "65536",
    }]})

    assert out["ok"] is True
    assert "llm.models[0].context_window" in out["coerced"]
    model = _cfg(bridge)["llm"]["models"][0]
    assert model["context_window"] == 65536
    assert not isinstance(model["context_window"], str), "字符串原样落盘了"


def test_coerce_llm_numbers_covers_context_window(bridge):
    """换算点本身的单元对照（不经 HTTP 层，直接看它改了什么）。"""
    cfg = {"llm": {"models": [{"id": "chat", "context_window": "131072"}]}}
    fixed = bridge._coerce_llm_numbers(cfg)
    assert fixed == ["llm.models[0].context_window"]
    assert cfg["llm"]["models"][0]["context_window"] == 131072


def test_coerce_llm_numbers_leaves_valid_values_alone(bridge):
    """已经是 int 的不动（避免把 0 也当成"没有窗口"改掉）。"""
    cfg = {"llm": {"models": [{"id": "chat", "context_window": 0}]}}
    assert bridge._coerce_llm_numbers(cfg) == []
    assert cfg["llm"]["models"][0]["context_window"] == 0


# ── 6. 端到端：先「保存参数」，再点底部「保存」───────────────────
def test_models_save_then_config_save_keeps_new_value(bridge):
    """事故的完整复现路径：旧快照 → 保存参数 → 底部保存 → 值必须还在。"""
    snap = _stale_snapshot(bridge)        # ① 设置页打开时的快照（ctx=0）

    bridge.api_models_save({"models": [{  # ② 「保存参数」
        "id": "chat",
        "label": "聊天档",
        "provider": "custom",
        "model": "chat-model",
        "max_tokens": 64000,
        "temperature": 0.8,
        "context_window": 131072,
    }]})
    assert _cfg(bridge)["llm"]["models"][0]["context_window"] == 131072

    out = bridge.api_config_save({"config": snap})   # ③ 底部「保存」（旧快照）

    assert out["ok"] is True
    model = _cfg(bridge)["llm"]["models"][0]
    assert model["context_window"] == 131072, "又在底部「保存」这一步被清回去了"
    assert model["max_tokens"] == 64000


# ── 结构闸门：三个键的归属只能有一处声明 ─────────────────────────
def test_ownership_keys_are_exactly_the_three(bridge):
    """清单本身要钉住：多一个键 = 顺手禁了别人的字段；少一个 = 隔离有洞。"""
    assert set(bridge.MODEL_CATALOG_KEYS) == {"models", "active_model", "providers"}


def test_config_save_reports_backup(bridge):
    """写盘链路照旧要有备份（隔离逻辑不许把备份绕掉）。"""
    out = bridge.api_config_save({"config": _stale_snapshot(bridge)})
    assert out["backup"]
    assert list(bridge.CONFIG_PATH.parent.glob("config.yaml.bak_*"))


# ── 7. thinking / thinking_effort 必须成对同步到扁平字段 ───────────
def test_models_save_clears_flat_effort_when_thinking_off(bridge):
    """切「关闭思考」后，扁平 `llm.thinking_effort` 不许留着上一档的值。

    2026-10-28 在浏览器里实测到的：档位改成 `thinking=off` + `effort=''` 之后，
    档位本身是对的，但扁平 `llm.thinking_effort` 还是 `'medium'` —— 因为原来的
    同步写的是 `for key in (...): if sel.get(key): llm[key] = sel[key]`，空串是
    假值，被跳过。状态灯和 `/api/status` 读的正是扁平字段，于是界面显示「中等推理」
    而实际已经关掉（教训 #51：同一事实两处派生必须互校）。
    """
    bridge.api_models_save({"models": [{
        "id": "chat",
        "label": "聊天档",
        "provider": "custom",
        "model": "chat-model",
        "thinking": "on",
        "thinking_effort": "medium",
    }]})
    llm = _cfg(bridge)["llm"]
    assert (llm["thinking"], llm["thinking_effort"]) == ("on", "medium")

    bridge.api_models_save({"models": [{
        "id": "chat",
        "label": "聊天档",
        "provider": "custom",
        "model": "chat-model",
        "thinking": "off",
        "thinking_effort": "",
    }]})
    llm = _cfg(bridge)["llm"]
    assert llm["thinking"] == "off"
    assert llm["thinking_effort"] == "", (
        "关掉思考后扁平 effort 还留着旧档 —— 状态灯会显示「中等推理」")
    # 档位里也必须成对（前端写的就是这一对）
    model = llm["models"][0]
    assert (model["thinking"], model["thinking_effort"]) == ("off", "")


def test_models_save_restores_effort_when_thinking_back_on(bridge):
    """反向：再开思考时 effort 要跟着回来，不能一直空着。"""
    bridge.api_models_save({"models": [{
        "id": "chat", "label": "聊天档", "provider": "custom", "model": "chat-model",
        "thinking": "off", "thinking_effort": "",
    }]})
    bridge.api_models_save({"models": [{
        "id": "chat", "label": "聊天档", "provider": "custom", "model": "chat-model",
        "thinking": "on", "thinking_effort": "high",
    }]})
    llm = _cfg(bridge)["llm"]
    assert (llm["thinking"], llm["thinking_effort"]) == ("on", "high")


# ── 8. 保存时按 id 合并：前端不认识的键不许被抹掉 ────────────────────
def test_models_save_keeps_unknown_keys_from_disk(bridge):
    """`llm.models` 原来是全量覆盖：请求体里没有的键会被从 config 里删掉。

    2026-10-28 的实际影响：前端为了让老档位的 `kinds` 不丢，只能「不显示但原样
    透传」——那是治表。按 id 合并之后，前端没发的键一律保留（字段的拥有者是
    「这一条档位」，那就只改前端送来的那几个键）。
    """
    import yaml
    cfg = yaml.safe_load(bridge.CONFIG_PATH.read_text(encoding="utf-8"))
    model = cfg["llm"]["models"][0]
    model["kinds"] = ["chat", "tag"]          # 老档位的用途标注
    model["future_field"] = {"nested": 1}     # 以后才会有的字段
    bridge.CONFIG_PATH.write_text(
        yaml.dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 模拟「前端只发了它认识的键」
    bridge.api_models_save({"models": [{
        "id": model["id"],
        "label": "改过的名字",
        "provider": model.get("provider") or "custom",
        "model": model.get("model") or "chat-model",
    }]})

    after = _cfg(bridge)["llm"]["models"][0]
    assert after["label"] == "改过的名字", "请求体里的键要生效"
    assert after["kinds"] == ["chat", "tag"], "前端没发的键被抹掉了"
    assert after["future_field"] == {"nested": 1}, "未知字段被抹掉了"


def test_models_save_still_strips_connection_fields(bridge):
    """合并不能把 base_url / api_key 又带回来（那俩归 provider 组表）。"""
    bridge.api_models_save({"models": [{
        "id": "chat", "label": "聊天档", "provider": "custom", "model": "chat-model",
        "base_url": "https://should-not-persist.example/v1",
        "api_key": "sk-should-not-persist",
    }]})
    model = _cfg(bridge)["llm"]["models"][0]
    assert "base_url" not in model
    assert "api_key" not in model
