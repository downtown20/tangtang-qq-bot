# -*- coding: utf-8 -*-
"""provider 组表（`llm.providers`）的契约测试（2026-10-28）。

## 为什么要这一层

`base_url` / `api_key` 是**整组共享**的连接信息，原来在每个模型档位里各存一份
（教训 #54 的反模式：组级属性存成条目级副本，改哪份生效靠遍历顺序，加一个
provider 就要重填一遍地址和 key）。现在 `llm.providers[]` 是唯一源，档位只写
`provider: <组id>` 引用。形状对齐 DSH Harness 的
`llm-pi-ai.providers.<id>`（api / baseURL / apiKeyEnv / models[]）。

这里锁三件事：

  1. `provider_conn()` **逐字段**回退：组表 → 档位 → 扁平 `llm.*`
     （老配置零迁移可用，不是「迁移完才跑得起来」）
  2. `virtual_providers()` 把散在档位/扁平字段里的连接归并成组，标 `derived`
  3. `handler._get_llm_config()` 真的走组表——不能只有 model_refs 自己知道
     （教训 #12：写了 infra ≠ 接进了业务）
"""
from __future__ import annotations

import types

from agent import model_refs as mr
from agent.handler import MessageHandler

PROF = {
    "id": "m1",
    "label": "主力",
    "provider": "custom",
    "model": "deepseek-v4.1-flash",
}


def _cfg(providers=None, models=None, **llm_extra) -> dict:
    llm: dict = {"base_url": "https://flat.example/", "api_key": "sk-flat"}
    if providers is not None:
        llm["providers"] = providers
    if models is not None:
        llm["models"] = models
    llm.update(llm_extra)
    return {"llm": llm}


# ── provider_conn：逐字段回退 ────────────────────────────────
def test_provider_conn_reads_group_table():
    cfg = _cfg(
        providers=[{"id": "custom", "base_url": "https://group.example/",
                    "api_key": "${CUSTOM_LLM_KEY}"}],
        models=[PROF],
    )
    conn = mr.provider_conn(cfg, PROF)
    assert conn["base_url"] == "https://group.example/"
    assert conn["api_key"] == "${CUSTOM_LLM_KEY}"
    assert conn["api"] == "openai-completions"
    assert conn["provider"] == "custom"


def test_group_table_wins_over_stale_profile_copy():
    """组表与档位副本冲突时以组表为准——这是「改一处，全组生效」的前提。"""
    prof = dict(PROF, base_url="https://stale.example/", api_key="sk-stale")
    cfg = _cfg(
        providers=[{"id": "custom", "base_url": "https://group.example/",
                    "api_key": "sk-group"}],
        models=[prof],
    )
    conn = mr.provider_conn(cfg, prof)
    assert conn["base_url"] == "https://group.example/"
    assert conn["api_key"] == "sk-group"


def test_missing_group_field_falls_back_to_profile():
    """组表建了但 key 还没搬进去 → 档位里的 key 继续可用（迁移期不断线）。"""
    prof = dict(PROF, base_url="https://old.example/", api_key="sk-old")
    cfg = _cfg(providers=[{"id": "custom", "base_url": "https://group.example/"}],
               models=[prof])
    conn = mr.provider_conn(cfg, prof)
    assert conn["base_url"] == "https://group.example/"  # 组里有 → 用组
    assert conn["api_key"] == "sk-old"                   # 组里没有 → 退档位


def test_no_group_falls_back_to_flat_llm():
    cfg = _cfg(models=[PROF])
    conn = mr.provider_conn(cfg, PROF)
    assert conn["base_url"] == "https://flat.example/"
    assert conn["api_key"] == "sk-flat"


def test_group_without_api_key_does_not_blank_profile_key():
    """组里显式写空 key（服务商不需要鉴权）不该把档位里能用的 key 抹掉。

    逐字段回退的意义就在这里：`""` 视同「没写」，不是「故意清空」。
    """
    prof = dict(PROF, api_key="sk-old")
    cfg = _cfg(providers=[{"id": "custom", "base_url": "https://g.example/",
                           "api_key": ""}], models=[prof])
    assert mr.provider_conn(cfg, prof)["api_key"] == "sk-old"


def test_providers_written_as_mapping_also_works():
    """YAML 里手写成映射也认（`providers: {custom: {...}}` 同样自然）。"""
    cfg = _cfg(providers={"custom": {"base_url": "https://map.example/",
                                     "api_key": "sk-map"}}, models=[PROF])
    assert mr.provider_conn(cfg, PROF)["base_url"] == "https://map.example/"
    assert mr.provider_by_id(cfg, "custom")["id"] == "custom"


# ── virtual_providers：老配置在界面里也能看到组 ──────────────
def test_virtual_providers_derives_from_legacy_profiles():
    prof = dict(PROF, base_url="https://old.example/", api_key="sk-old")
    groups = mr.virtual_providers(_cfg(models=[prof]))
    assert [g["id"] for g in groups] == ["custom"]
    assert groups[0]["base_url"] == "https://old.example/"
    assert groups[0]["api_key"] == "sk-old"
    assert groups[0]["derived"] is True


def test_virtual_providers_keeps_real_group_authoritative():
    prof = dict(PROF, base_url="https://stale.example/", api_key="sk-stale")
    cfg = _cfg(
        providers=[{"id": "custom", "label": "中转站",
                    "base_url": "https://group.example/", "api_key": "sk-group"}],
        models=[prof],
    )
    groups = mr.virtual_providers(cfg)
    assert len(groups) == 1
    assert groups[0]["base_url"] == "https://group.example/"
    assert groups[0]["label"] == "中转站"
    assert not groups[0].get("derived")


def test_virtual_providers_merges_same_provider_across_profiles():
    cfg = _cfg(models=[
        dict(PROF, id="m1"),
        dict(PROF, id="m2", label="备用"),
    ])
    groups = mr.virtual_providers(cfg)
    assert len(groups) == 1
    assert groups[0]["base_url"] == "https://flat.example/"


def test_virtual_providers_covers_pure_flat_config():
    """连档位都没有的老配置（纯扁平 llm.*）也要有一组，否则界面空白。"""
    groups = mr.virtual_providers({"llm": {"provider": "deepseek",
                                          "base_url": "https://flat.example/",
                                          "api_key": "${CUSTOM_LLM_KEY}"}})
    assert [g["id"] for g in groups] == ["deepseek"]
    assert groups[0]["base_url"] == "https://flat.example/"
    assert groups[0]["api_key"] == "${CUSTOM_LLM_KEY}"


# ── resolve_model_ref 走组表（识图 / 打标的唯一入口）──────────
def test_resolve_model_ref_uses_group_connection():
    cfg = _cfg(
        providers=[{"id": "custom", "label": "中转站",
                    "base_url": "https://group.example/", "api_key": "sk-group"}],
        models=[dict(PROF, input=["text", "image"])],
    )
    got = mr.resolve_model_ref(cfg, "m1")
    assert got["base_url"] == "https://group.example/"
    assert got["api_key"] == "sk-group"
    assert got["provider_label"] == "中转站"
    assert got["api"] == "openai-completions"
    assert got["input"] == ["text", "image"]


# ── handler 真的走组表 ───────────────────────────────────────
def _h(llm: dict):
    return types.SimpleNamespace(llm_config=llm)


def test_handler_takes_connection_from_group_table():
    llm = {
        "base_url": "https://flat.example/",
        "api_key": "sk-flat",
        "providers": [{"id": "custom", "base_url": "https://group.example/",
                       "api_key": "sk-group"}],
        "models": [{"id": "m1", "model": "model-a", "provider": "custom"}],
        "active_model": "m1",
    }
    out = MessageHandler._get_llm_config(_h(llm))
    assert out["base_url"] == "https://group.example/"
    assert out["api_key"] == "sk-group"
    assert out["model"] == "model-a"


def test_handler_keeps_profile_copy_when_group_is_missing():
    """没有组表时行为与 2026-10-27 完全一致（老配置回归防线）。"""
    llm = {
        "base_url": "https://flat.example/",
        "api_key": "sk-flat",
        "models": [{"id": "m1", "model": "model-a", "provider": "custom",
                    "base_url": "https://a.example/", "api_key": "sk-a"}],
        "active_model": "m1",
    }
    out = MessageHandler._get_llm_config(_h(llm))
    assert out["base_url"] == "https://a.example/"
    assert out["api_key"] == "sk-a"
