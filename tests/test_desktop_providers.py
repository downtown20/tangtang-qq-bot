# -*- coding: utf-8 -*-
"""provider 组表在桌面端 bridge 上的契约（2026-10-28）。

## 为什么有这份测试

`base_url` / `api_key` 从「每个模型档位各存一份」收敛成 `llm.providers` 一张组表
（对齐 DSH Harness 的 `providers.<id>`，也修掉教训 #54 的反模式）。这一层跨了
两条链路，任何一条断了都会**静默用错端点**：

  · 读：`/api/models` 必须把组表一起给出去，否则界面画不出 provider 卡；
  · 写：`/api/providers/save` 改一处必须全组生效，并把档位里的过期副本清掉。

外加两条安全边界（都是这里踩过的形状）：
  · 组里的 `api_key` 一律掩码——明文 Key 不上界面；
  · 界面回传的东西可能**就是那个掩码**，它绝不能当成真 Key 写回磁盘
    （否则 `.env` 里那个 key 从此被静默无视，2026-09-20 真实踩过）。

阳性对照：把 `api_providers_save` 里的 `_is_masked` 判断删掉，第 4 条会红；
把「清档位副本」那段删掉，第 3 条会红；把 `api_models_activate` 改回读档位
自己的字段，第 7 条会红。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEGACY_CONFIG = """\
llm:
  provider: custom
  base_url: https://flat.example/
  api_key: ${CUSTOM_LLM_KEY}
  models:
  - id: default
    label: deepseek-v4.1-flash
    provider: custom
    base_url: https://old.example/
    api_key: ${CUSTOM_LLM_KEY}
    model: deepseek-v4.1-flash
    max_tokens: 64000
    temperature: 0.8
  active_model: default
"""


def _load_bridge():
    """每次 exec 一份新模块：模块级常量（CONFIG_PATH 等）可被 monkeypatch。"""
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_providers_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(LEGACY_CONFIG, encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(mod, "_example_bootstrapped", False)
    return mod


def _llm(bridge) -> dict:
    return yaml.safe_load(bridge.CONFIG_PATH.read_bytes().decode("utf-8"))["llm"]


# ── 读：组表要一起给出来，密钥必须掩码 ────────────────────────
def test_models_returns_derived_provider_group(bridge):
    data = bridge.api_models()
    assert len(data["providers"]) == 1
    group = data["providers"][0]
    assert group["id"] == "custom"
    assert group["base_url"] == "https://old.example/"
    assert group["derived"] is True, "老配置的组是从档位推出来的，要标记"


def test_provider_api_key_is_masked_never_plaintext(bridge):
    bridge.CONFIG_PATH.write_text(
        LEGACY_CONFIG.replace("${CUSTOM_LLM_KEY}", "sk-live-abcdef123456"),
        encoding="utf-8",
    )
    blob = json.dumps(bridge.api_models(), ensure_ascii=False)
    assert "sk-live-abcdef123456" not in blob, "组表把明文 Key 交给界面了"


def test_read_does_not_write_config(bridge):
    """打开界面不该改用户的 config（推导是纯读）。"""
    before = bridge.CONFIG_PATH.read_bytes()
    bridge.api_models()
    assert bridge.CONFIG_PATH.read_bytes() == before


# ── 写：一份定义，全组生效 ───────────────────────────────────
def test_save_provider_writes_group_and_cleans_profile_copies(bridge):
    out = bridge.api_providers_save({
        "id": "custom",
        "label": "中转站",
        "base_url": "https://new.example/",
        "api_key": "${CUSTOM_LLM_KEY}",
    })
    assert out["ok"] is True
    assert out["profiles_cleaned"] == 1

    llm = _llm(bridge)
    assert llm["providers"][0]["base_url"] == "https://new.example/"
    assert llm["providers"][0]["label"] == "中转站"
    # 组表是唯一源 → 档位里那份副本必须被清掉（教训 #54）
    assert "base_url" not in llm["models"][0]
    assert "api_key" not in llm["models"][0]
    # 推导标记落盘后不该还在
    assert "derived" not in llm["providers"][0]

    # 读回来是同一份事实
    assert bridge.api_models()["providers"][0]["base_url"] == "https://new.example/"


def test_save_provider_ignores_masked_key_echo(bridge):
    """界面回传的可能是掩码 `sk-l…456（20 字符）`——它不是 Key。

    前提必须是**明文** Key：`${VAR}` 引用在 `_mask_secret` 里原样返回，
    拿它测等于没测（第一版这里就是假绿——变异对照抓出来的）。
    """
    plain = "sk-live-abcdef123456"
    bridge.CONFIG_PATH.write_text(
        LEGACY_CONFIG.replace("${CUSTOM_LLM_KEY}", plain), encoding="utf-8")
    masked = bridge.api_models()["providers"][0]["api_key"]
    assert masked != plain, "前提不成立：读出来的不是掩码，这条测不到东西"

    bridge.api_providers_save({"id": "custom", "api_key": masked})
    assert _llm(bridge)["providers"][0]["api_key"] == plain


def test_save_provider_without_api_key_keeps_existing(bridge):
    bridge.api_providers_save({"id": "custom", "base_url": "https://n.example/"})
    assert _llm(bridge)["providers"][0]["api_key"] == "${CUSTOM_LLM_KEY}"


def test_save_provider_makes_backup(bridge):
    bridge.api_providers_save({"id": "custom", "base_url": "https://n.example/"})
    assert list(bridge.CONFIG_PATH.parent.glob("config.yaml.bak_*"))


# ── 删除：仍被引用就拒绝（不能留下没有端点的档位）──────────────
def test_remove_provider_in_use_is_refused(bridge):
    bridge.api_providers_save({"id": "custom", "base_url": "https://n.example/"})
    with pytest.raises(ValueError) as ei:
        bridge.api_providers_save({"id": "custom", "remove": True})
    assert "引用" in str(ei.value)
    assert _llm(bridge)["providers"], "拒绝之后组不该被删掉"


def test_remove_unused_provider_succeeds(bridge):
    bridge.api_providers_save({"id": "spare", "base_url": "https://s.example/"})
    bridge.api_providers_save({"id": "spare", "remove": True})
    ids = [g["id"] for g in _llm(bridge)["providers"]]
    assert "spare" not in ids
    assert "custom" in ids


# ── 激活：档位里已经没有连接了，必须从组表取 ──────────────────
def test_activate_takes_connection_from_group_table(bridge):
    bridge.api_providers_save({
        "id": "custom",
        "base_url": "https://new.example/",
        "api_key": "${CUSTOM_LLM_KEY}",
    })
    assert "base_url" not in _llm(bridge)["models"][0]  # 前提：副本已被清掉

    out = bridge.api_models_activate({"id": "default"})
    assert out["ok"] is True
    llm = _llm(bridge)
    assert llm["base_url"] == "https://new.example/"
    assert llm["api_key"] == "${CUSTOM_LLM_KEY}"


def test_route_registered(bridge):
    assert "/api/providers/save" in bridge.ROUTES_POST
    assert "/api/models/save" in bridge.ROUTES_POST
    assert "/api/models" in bridge.ROUTES_GET


# ── 模型目录写入：只经 /api/models/save（不碰设置页的配置快照）──────────────
def test_models_save_writes_catalog_and_strips_connection(bridge):
    out = bridge.api_models_save({
        "models": [{
            "id": "default", "label": "主力", "provider": "custom",
            "model": "deepseek-v4.1-flash", "max_tokens": "64000",
            "temperature": "0.8",
            "base_url": "https://stale.example/", "api_key": "sk-stale",
        }],
        "active": "default",
    })
    assert out["ok"] is True
    assert out["count"] == 1
    llm = _llm(bridge)
    assert "base_url" not in llm["models"][0], "档位里不许留连接副本（教训 #54）"
    assert "api_key" not in llm["models"][0]
    # 扁平字段仍要能解释「现在用哪个端点」——这里是回退到扁平 llm.*
    assert llm["base_url"] == "https://flat.example/"


def test_models_save_coerces_numeric_strings(bridge):
    """前端表单来的是字符串；原样写盘会让下一轮 LLM 调用 500。"""
    bridge.api_models_save({
        "models": [{"id": "a", "model": "m", "max_tokens": "32000",
                    "temperature": "0.5"}],
        "active": "a",
    })
    llm = _llm(bridge)
    assert llm["models"][0]["max_tokens"] == 32000
    assert llm["models"][0]["temperature"] == 0.5


def test_models_save_syncs_active_group_connection(bridge):
    bridge.api_providers_save({
        "id": "custom",
        "base_url": "https://g.example/",
        "api_key": "${CUSTOM_LLM_KEY}",
    })
    bridge.api_models_save({
        "models": [{"id": "a", "provider": "custom", "model": "m1"}],
        "active": "a",
    })
    llm = _llm(bridge)
    assert llm["base_url"] == "https://g.example/"
    assert llm["api_key"] == "${CUSTOM_LLM_KEY}"
    assert llm["model"] == "m1"


def test_models_save_rejects_unknown_active(bridge):
    """active 指向不存在的档位 → 拒绝（否则 `_get_llm_config` 静默退回第一条，
    用错模型还不报错）。"""
    with pytest.raises(ValueError) as ei:
        bridge.api_models_save({
            "models": [{"id": "a", "model": "m"}], "active": "nope"})
    assert "不存在" in str(ei.value)


def test_models_save_rejects_duplicate_ids(bridge):
    with pytest.raises(ValueError) as ei:
        bridge.api_models_save({
            "models": [{"id": "a", "model": "m1"}, {"id": "a", "model": "m2"}],
            "active": "a",
        })
    assert "重复" in str(ei.value)
