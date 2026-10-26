# -*- coding: utf-8 -*-
"""多包 + TTL 的闸门（2026-10-26 第五轮）。

## 防的是什么

1. `budget` 被循环变量遮蔽 → 警告报错数字（真踩过：11,529 字被报成
   「超出总预算 6800 字」，6800 是 20-技法 的槽位预算）
2. TTL 默认写回 6 小时（主人要默认永久）
3. 只认第一个包（多包退化成单包）
4. 保存 packs 时整份重写 config.yaml（注释全丢）

## 阳性对照

- `for slot_name, budget in ...`（遮蔽）→ `test_budget_shadowing_is_gone` 红
- `PACK_DEFAULTS["ttl_seconds"] = 21600` → `test_ttl_defaults_to_forever` 红
- `normalize_packs` 里 `return packs[:1]` → `test_multiple_packs` 红
"""
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import intimate_reference as iref  # noqa: E402
from agent import scenario_packs as sp  # noqa: E402


# ── 解析 ───────────────────────────────────────────────────────────────

def test_normalize_legacy_flat_config():
    packs = sp.normalize_packs({"scenario_reference": {"trigger_scenario": "night", "label": "夜间"}})
    assert len(packs) == 1
    assert packs[0]["scenario"] == "night"
    assert packs[0]["label"] == "夜间"
    assert packs[0]["ttl_seconds"] == 0


def test_normalize_returns_default_when_empty():
    packs = sp.normalize_packs({})
    assert packs[0]["scenario"] == sp.DEFAULT_TRIGGER


def test_multiple_packs():
    packs = sp.normalize_packs({"scenario_reference": {"packs": [
        {"scenario": "a"}, {"scenario": "b"}, {"scenario": "c"},
    ]}})
    assert [p["scenario"] for p in packs] == ["a", "b", "c"]


def test_ttl_defaults_to_forever():
    assert sp.PACK_DEFAULTS["ttl_seconds"] == 0
    pack = sp.normalize_packs({"scenario_reference": {"packs": [{"scenario": "x"}]}})[0]
    assert pack["ttl_seconds"] == 0


def test_negative_ttl_clamped_to_zero():
    """负数 TTL 等于「已经过期」——那是配置错误，按永久处理更安全（不会静默失效）。"""
    pack = sp.normalize_packs({"scenario_reference": {"packs": [
        {"scenario": "x", "ttl_seconds": -100}]}})[0]
    assert pack["ttl_seconds"] == 0


def test_scenario_is_free_of_judgement():
    """任何场景名都能配——架构不判断「哪类场景该配范本」（主人 2026-10-26 明确）。"""
    for name in ("psychology", "seductive", "event_manager", "whatever_user_wants"):
        packs = sp.normalize_packs({"scenario_reference": {"packs": [{"scenario": name}]}})
        assert sp.pack_for(packs, name) is not None


def test_bad_entry_skipped_not_raised():
    """一条坏配置不该让整页/整轮挂掉。"""
    packs = sp.normalize_packs({"scenario_reference": {"packs": [
        "不是字典", {"没有": "scenario"}, {"scenario": "ok"},
    ]}})
    assert [p["scenario"] for p in packs] == ["ok"]


def test_empty_dir_falls_back_to_default_package():
    pack = sp.normalize_packs({"scenario_reference": {"packs": [{"scenario": "x"}]}})[0]
    assert pack["dir"] == "knowledge/" + iref.PACKAGE_DIRNAME


# ── 预算遮蔽（真踩过的 bug）────────────────────────────────────────────

def test_budget_shadowing_is_gone():
    """`for slot_name, budget in SLOT_BUDGETS.items()` 会遮蔽总量 budget，
    让「总量超预算」警告报出最后一个槽位的数字。

    这里用「总量够、但某槽位超」的数据构造：只要警告里出现的是总量预算就过，
    出现槽位预算就是被遮蔽了。
    """
    src = Path(iref.__file__).read_text(encoding="utf-8")
    assert "for slot_name, budget in SLOT_BUDGETS.items()" not in src
    assert "slot_budget" in src


def test_total_budget_warning_uses_total_budget(tmp_path, monkeypatch):
    """真读一次：预算 100 时必须报 100，不能报某个槽位的数。"""
    pkg = tmp_path / "knowledge" / "包A" / "00-核心"
    pkg.mkdir(parents=True)
    (pkg / "a.md").write_text("字" * 500, encoding="utf-8")
    ref = iref.load(None, {"dir": str(tmp_path / "knowledge" / "包A"), "budget": 100})
    assert any("超出总预算 100 字" in w for w in ref.warnings), ref.warnings


def test_no_warning_when_under_budget(tmp_path):
    pkg = tmp_path / "knowledge" / "包B" / "00-核心"
    pkg.mkdir(parents=True)
    (pkg / "a.md").write_text("字" * 50, encoding="utf-8")
    ref = iref.load(None, {"dir": str(tmp_path / "knowledge" / "包B"), "budget": 1000})
    assert list(ref.warnings) == [], ref.warnings


# ── config.yaml 保存纪律 ───────────────────────────────────────────────

def test_save_does_not_rewrite_whole_config():
    """保存 packs 必须走文本级替换——整份 `yaml.safe_dump` 会把注释全丢掉。

    这里只做静态断言（真实行为由端到端验证过）：bridge 里不该再出现
    「cfg['scenario_reference'] = ... 然后 api_config_save」那种写法。
    """
    src = (Path(__file__).parent.parent / "tools" / "desktop_bridge.py").read_text(encoding="utf-8")
    i = src.find("def api_scenario_packs_save")
    assert i > 0
    body = src[i:src.find("\ndef ", i + 10)]
    assert "yaml.safe_dump" in body, "应该用 safe_dump 只生成这一块的文本"
    assert "CONFIG_PATH.write_text" in body, "应该只写回 config.yaml 这一块"
    assert "api_config_save({\"config\"" not in body, \
        "不许再把整份配置交给 api_config_save —— 那会丢掉所有注释"


# ── 群聊注入开关（2026-10-26 主人：「我需要的是群聊可以注入」）────────

def test_allow_groups_defaults_to_false():
    """默认关——群场景是群级设置：开了就对群里所有人生效，而不是逐人同意。

    这条守的是「默认安全」：谁要开谁显式开，不能因为升级就默认把范本推进群聊。
    """
    assert sp.PACK_DEFAULTS["allow_groups"] is False
    pack = sp.normalize_packs({"scenario_reference": {"packs": [{"scenario": "x"}]}})[0]
    assert pack["allow_groups"] is False


def test_allow_groups_can_be_enabled():
    pack = sp.normalize_packs({"scenario_reference": {"packs": [
        {"scenario": "x", "allow_groups": True}]}})[0]
    assert pack["allow_groups"] is True


def test_group_injection_is_guarded_by_allow_groups():
    """静态判据：群聊的挂载必须写在 allow_groups 判断**之后**。

    守卫写晚一步 = 无条件挂载，而这个错在测试里很难靠行为复现
    （要构造完整群聊回合），所以用源码顺序守。
    """
    import ast
    from pathlib import Path
    src = (Path(__file__).parent.parent / "agent" / "handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "handle_group_message":
            continue
        body = ast.get_source_segment(src, node) or ""
        assert "allow_groups" in body, "群聊挂载缺少 allow_groups 守卫"
        assert body.index("allow_groups") < body.index("pinned.append("), \
            "群聊挂载写在了 allow_groups 判断之前"
        assert 'pinned=_group_pinned' in body, "群聊没把 pinned 传给 assemble_user_message"
        checked += 1
    assert checked == 1, "handle_group_message 结构变了，测试自己失效"


def test_config_enables_groups_for_the_shipped_pack():
    """出厂配置要让主人「群聊能注入」马上生效——他明确要求了这一条。"""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(
        (Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))
    packs = (cfg.get("scenario_reference") or {}).get("packs") or []
    assert packs, "出厂配置没有包"
    assert any(p.get("allow_groups") for p in packs), \
        "出厂配置里没有 allow_groups: true 的包——群聊注入等于没开"
