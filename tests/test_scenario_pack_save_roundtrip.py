# -*- coding: utf-8 -*-
"""特殊模式包保存的往返契约：保存一次，配置里原有的字段不能消失。

## 为什么有这份测试（2026-10-26 真实事故）

`_backups/config_bak_20261026/config.yaml.bak_20261026_223143`（22:31:43）
里 `scenario_reference.packs[0]` 有 `allow_groups: true`；之后 22:55:28、
22:56:52 两份备份以及当前 `config.yaml` **都没有这个键了**（不是 false，是没有）。

根因：`tools/desktop_bridge.py::api_scenario_packs_save` 用**白名单重建**每个
pack 对象（`cleaned.append({...})`），白名单里没有 `allow_groups` ——
于是每次从桌面端「特殊模式」页保存一次，这个字段就静默消失一次。

危害不是「配置变丑」：`agent/handler.py:4238` 的守卫是
`if _group_pack is not None and not _group_pack.get("allow_groups")`，
**键缺失等同 False**，于是「群聊允许注入这个包」被静默关掉，
而界面上看不出任何异常。这是教训 #54（组级属性丢失）的同族：
**对象级属性被白名单重建吃掉**。

## 钉住什么

1. 传 `allow_groups: true` → 落盘仍是 true（核心回归）
2. 不传 `allow_groups` → 落盘写出显式 `false`，而不是「键消失」
   （「键消失」正是这次事故的形态，必须能从配置里读出来）
3. 保存只动 `scenario_reference`，其它段（`llm` / `bot` / 任意自造段）必须原样保留
4. 备份文件被创建（保存前有一份可回退的）

阳性对照：把 `cleaned.append` 里的 `"allow_groups"` 一行删掉，第 1、2 条必红。
"""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_CFG = """\
llm:
  provider: deepseek
  model: x
bot:
  qq_id: '10001'
scenario_reference:
  packs:
  - id: main
    scenario: seductive
    dir: knowledge/k
    label: 范本
    budget: 13500
    require_marker: true
    ttl_seconds: 21600
    allow_groups: true
custom_untouched_section:
  keep: me
"""


def _load_bridge():
    """每次 exec 一份新模块：模块级常量（CONFIG_PATH 等）可被 monkeypatch。"""
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_pack_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE_CFG, encoding="utf-8", newline="")
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    return mod


def _read(cfg_path: Path) -> dict:
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


def test_allow_groups_survives_save(bridge):
    """核心回归：传 true 保存一次，读回必须还是 true。

    修复前这里会是 `KeyError` 或读到缺键（`None`）——正是事故形态。
    """
    cfg = bridge.CONFIG_PATH
    bridge.api_scenario_packs_save({
        "packs": [{
            "id": "main", "scenario": "seductive", "dir": "knowledge/k",
            "label": "范本", "budget": 13500, "require_marker": True,
            "ttl_seconds": 21600, "allow_groups": True,
        }]
    })
    pack = _read(cfg)["scenario_reference"]["packs"][0]
    assert "allow_groups" in pack, (
        "allow_groups 被保存链路丢掉了——键必须显式存在（缺失 == False，"
        "会让群聊注入静默关闭）")
    assert pack["allow_groups"] is True


def test_missing_allow_groups_is_written_as_explicit_false(bridge):
    """调用方没给这个字段时，落盘要写显式的 false，不能「键消失」。

    「键消失」是本轮事故的确切形态——配置里看不出来它曾经是 true，
    也没法从文件判断是「用户关掉了」还是「代码吃掉了」。
    """
    cfg = bridge.CONFIG_PATH
    bridge.api_scenario_packs_save({
        "packs": [{
            "id": "main", "scenario": "seductive", "dir": "knowledge/k",
            "label": "范本", "budget": 100, "require_marker": False,
            "ttl_seconds": 0,
        }]
    })
    pack = _read(cfg)["scenario_reference"]["packs"][0]
    assert "allow_groups" in pack, "缺省也要显式写出来，不能留空键"
    assert pack["allow_groups"] is False


def test_save_preserves_unrelated_sections(bridge):
    """保存 pack 只许动 `scenario_reference`，别把别的段一起吃改。"""
    cfg = bridge.CONFIG_PATH
    before = _read(cfg)
    bridge.api_scenario_packs_save({
        "packs": [{
            "id": "main", "scenario": "seductive", "dir": "knowledge/k",
            "label": "新标签", "budget": 999, "require_marker": True,
            "ttl_seconds": 60, "allow_groups": True,
        }]
    })
    after = _read(cfg)
    assert after["llm"] == before["llm"]
    assert after["bot"] == before["bot"]
    assert after["custom_untouched_section"] == {"keep": "me"}
    assert after["scenario_reference"]["packs"][0]["label"] == "新标签"


def test_save_creates_backup(bridge):
    """保存前要留一份备份（文本级替换绕过了 api_config_save 的备份链路）。"""
    cfg = bridge.CONFIG_PATH
    out = bridge.api_scenario_packs_save({
        "packs": [{
            "id": "main", "scenario": "seductive", "dir": "d", "label": "l",
            "budget": 1, "require_marker": True, "ttl_seconds": 0,
        }]
    })
    backup_name = out.get("backup")
    assert backup_name, "返回值里必须带 backup 文件名"
    assert (cfg.parent / backup_name).is_file(), "备份文件必须真的存在"
