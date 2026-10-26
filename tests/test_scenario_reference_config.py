# -*- coding: utf-8 -*-
"""特殊模式的配置契约：多包 + TTL 可配（默认永久）+ 场景中立。

主人 2026-10-26 第五轮：
    「B，TTL 也在前端改，你不说我也不知道还有这玩意，默认永久。
     特殊模式换成陪伴场景只是对于我的设置不适合，不代表别的用户不适合。」

最后一句改掉了设计的价值前提：**架构不判断「哪类场景该配范本」**。
所以这里有一条测试专门守「psychology 也能配」——不是因为它好用，
而是因为拦住它就是把使用者的判断权收走了。

## 阳性对照

- `_scenario_packs` 不读配置 → `test_reads_packs_list` 红
- TTL 默认写成 6 小时 → `test_ttl_defaults_to_forever` 红
- `_is_sed_active` 不认 exp==0 → `test_permanent_activation_never_expires` 红
- 只返回第一个包 → `test_multiple_packs` 红
"""
import sys
import time as _time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import handler as handler_mod  # noqa: E402
from agent import scenario_packs as sp  # noqa: E402


class _Dummy:
    """只借几个纯函数式的方法，不构造整个 handler。"""

    _scenario_packs = handler_mod.MessageHandler._scenario_packs
    _pack_for_scenario = handler_mod.MessageHandler._pack_for_scenario
    _is_sed_active = handler_mod.MessageHandler._is_sed_active
    _PACK_DEFAULTS = handler_mod.MessageHandler._PACK_DEFAULTS

    def __init__(self, config):
        self.config = config
        self._sed_active = {}


# ── 旧格式兼容 ─────────────────────────────────────────────────────────

def test_legacy_flat_format_becomes_one_pack():
    """旧配置（trigger_scenario + label）必须还能用——不能让主人的配置直接失效。"""
    h = _Dummy({"scenario_reference": {"trigger_scenario": "seductive", "label": "色色范本"}})
    packs = h._scenario_packs()
    assert len(packs) == 1
    assert packs[0]["scenario"] == "seductive"
    assert packs[0]["label"] == "色色范本"
    assert packs[0]["require_marker"] is True


def test_empty_config_falls_back_to_default_pack():
    packs = _Dummy({})._scenario_packs()
    assert packs and packs[0]["scenario"] == "seductive"


# ── 多包 ───────────────────────────────────────────────────────────────

def test_reads_packs_list():
    h = _Dummy({"scenario_reference": {"packs": [
        {"id": "a", "scenario": "seductive", "dir": "knowledge/X", "label": "甲",
         "budget": 9000, "require_marker": True, "ttl_seconds": 3600},
        {"id": "b", "scenario": "psychology", "dir": "knowledge/Y", "label": "乙",
         "budget": 5000, "require_marker": False, "ttl_seconds": 0},
    ]}})
    packs = h._scenario_packs()
    assert [p["id"] for p in packs] == ["a", "b"]
    assert packs[1]["require_marker"] is False
    assert packs[0]["ttl_seconds"] == 3600


def test_multiple_packs_are_independent():
    """两个包必须各自可命中——只返回第一个包是常见退化。"""
    h = _Dummy({"scenario_reference": {"packs": [
        {"scenario": "seductive", "dir": "knowledge/X"},
        {"scenario": "psychology", "dir": "knowledge/Y", "require_marker": False},
    ]}})
    assert h._pack_for_scenario("seductive")["dir"] == "knowledge/X"
    assert h._pack_for_scenario("psychology")["dir"] == "knowledge/Y"
    assert h._pack_for_scenario("event_manager") is None


def test_psychology_can_be_a_special_mode():
    """场景中立：架构不判断「哪类场景该配范本」。

    主人原话：「特殊模式换成陪伴场景只是对于我的设置不适合，不代表别的用户
    不适合。」——所以这条不是「推荐这么配」，而是「不许拦住这么配」。
    """
    h = _Dummy({"scenario_reference": {"packs": [
        {"scenario": "psychology", "dir": "knowledge/陪伴范本", "require_marker": False},
    ]}})
    pack = h._pack_for_scenario("psychology")
    assert pack is not None
    assert pack["require_marker"] is False


def test_pack_without_scenario_is_skipped():
    """没写 scenario 的条目是无效配置，跳过而不是抛（一个手误不该让整页失效）。"""
    h = _Dummy({"scenario_reference": {"packs": [
        {"id": "broken"},
        {"scenario": "seductive", "dir": "knowledge/X"},
    ]}})
    assert [p["scenario"] for p in h._scenario_packs()] == ["seductive"]


# ── TTL ────────────────────────────────────────────────────────────────

def test_ttl_defaults_to_forever():
    """主人：「默认永久」。默认值写成 6 小时就是又把它藏起来了。"""
    assert handler_mod.MessageHandler._PACK_DEFAULTS["ttl_seconds"] == 0
    pack = _Dummy({"scenario_reference": {"packs": [{"scenario": "x", "dir": "d"}]}}) \
        ._scenario_packs()[0]
    assert pack["ttl_seconds"] == 0


def test_permanent_activation_never_expires():
    """exp == 0 = 永久激活。用 0 而不是 None：持久化层存的是 float，
    0.0 天然能往返，不引入第二种结构。"""
    h = _Dummy({})
    h._sed_active["u1"] = 0.0
    assert h._is_sed_active("u1") is True


def test_positive_ttl_still_expires():
    """配了 ttl 就要真的会过期——不然「可配」是假的。"""
    h = _Dummy({})
    h._sed_active["u1"] = _time.time() - 1
    assert h._is_sed_active("u1") is False
    h._sed_active["u2"] = _time.time() + 100
    assert h._is_sed_active("u2") is True


# ── 代码卫生 ───────────────────────────────────────────────────────────

def test_handler_has_no_hardcoded_seductive_judgement():
    """不许再有 `name == "seductive"` 这种判据——那正是被修掉的东西。"""
    src = Path(handler_mod.__file__).read_text(encoding="utf-8")
    assert 'private_scenario.name == "seductive"' not in src
    assert "_pack_for_scenario" in src
    assert "_scenario_packs" in src


def test_shipped_config_uses_packs_or_legacy():
    """出厂配置必须是两者之一，不能是「什么都不写」（那样默认值永远吃不到验证）。"""
    cfg = yaml.safe_load(
        (Path(handler_mod.__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
    ref = cfg.get("scenario_reference") or {}
    assert isinstance(ref.get("packs"), list) or ref.get("trigger_scenario"), ref
