# -*- coding: utf-8 -*-
"""「高级调参」台账 ↔ 代码常量的双向契约（2026-10-26 第 49 轮 B4）。

## 这组用例防的是什么

主人要的是「常量也开放，做成高级调参页」——每个值在界面上要看到
**当前值 / 默认值 / 作用 / 文件:行**，改完保存、重启生效。

台账（`agent/tuning.py` 的 `TUNING_SPECS`）因此成了**第二个事实源**：
代码里的兜底字面量在模块顶层，台账里的 default 在另一处。两处一旦漂移，
界面显示的「默认值」就是假的——而用户看不出来（教训 #4 的变体，
也是 #51「同一事实只许一个派生点」的同族）。

所以钉四条：

| 方向 | 断言 | 防的是 |
|---|---|---|
| 台账→代码 | 每个 spec 的 default 必须等于代码里 `tuning_int("key", <字面量>)` 的字面量 | 界面默认值撒谎 |
| 代码→台账 | 每个 `tuning_int/float("key", …)` 的 key 必须登记在 SPECS | 加了可调项但界面看不见（#4） |
| 双向 | 每个 spec 必须真的被某个模块引用 | 台账里躺着僵尸项，界面上却「可调」 |
| 定位 | `locate(spec)` 的行号必须指向该常量赋值行 | 「文件:行」指错地方，用户按图索骥找不到 |

## 阳性对照（教训 #30/#37：闸门必须能红）

- 把 `protocols.py` 里 `PROFILE_MAX_LEN = tuning_int("profile_max_len", 120)`
  的 120 改成 200 → `test_code_fallback_matches_registry` 红；
- 把某处 key 拼错（`engaged_windows`）→ `test_code_calls_are_all_registered` 红；
- 从 SPECS 删掉一项但代码还在用 → 同上；代码不再用但 SPECS 留着
  → `test_registry_items_are_all_wired` 红；
- 在 `PROFILE_MAX_LEN` 上方插 3 行注释 → `locate` 仍对（行号是 AST 算的，
  不是手抄的）；但把赋值改成 `PROFILE_MAX_LEN: int = tuning_int(...)`
  → 也仍然对（AnnAssign 也覆盖）。

（前三条已进 `变异_高级调参契约.json`。）
"""

import ast
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent import tuning  # noqa: E402

AGENT_DIR = BASE / "agent"


# ═══════════════════════════════════════════════════════════════════
# 夹具：每个用例都从「无缓存 + 不读真配置」的干净状态开始
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """还原全局缓存与配置路径——否则一条用例把 `_cache` 填上，
    后面的用例就测不到自己的覆盖（假绿的经典来源）。"""
    monkeypatch.setattr(tuning, "_FORCE_CONFIG", False, raising=False)
    monkeypatch.setattr(tuning, "_cache", None, raising=False)
    monkeypatch.setattr(tuning, "_lineno_cache", {}, raising=False)
    yield
    tuning._cache = None


def _write_cfg(tmp_path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _use_config(monkeypatch, path: Path):
    """让本用例真的读配置文件（默认 pytest 进程内是不读的）。"""
    monkeypatch.setattr(tuning, "_FORCE_CONFIG", True, raising=False)
    monkeypatch.setattr(tuning, "_CONFIG_PATH", path, raising=False)
    monkeypatch.setattr(tuning, "_cache", None, raising=False)


# ═══════════════════════════════════════════════════════════════════
# 一、台账自身的结构
# ═══════════════════════════════════════════════════════════════════

class TestRegistryShape:
    def test_keys_are_unique(self):
        keys = [s.key for s in tuning.TUNING_SPECS]
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"调参键重复：{sorted(dupes)}"

    def test_groups_are_declared(self):
        declared = {g for g, _ in tuning.GROUPS}
        used = {s.group for s in tuning.TUNING_SPECS}
        assert used <= declared, f"用了未声明的分组：{sorted(used - declared)}"
        # 空分组不该出现在导航里（点了是空白页）
        empty = declared - used
        assert not empty, f"分组里没有任何调参项：{sorted(empty)}"

    def test_defaults_are_within_range(self):
        for spec in tuning.TUNING_SPECS:
            assert spec.lo <= spec.default <= spec.hi, (
                f"{spec.key} 默认值 {spec.default} 不在 [{spec.lo}, {spec.hi}] 内")

    def test_ranges_are_sane(self):
        for spec in tuning.TUNING_SPECS:
            assert spec.lo < spec.hi, f"{spec.key} 的范围是空的"
            if spec.kind == "int":
                assert isinstance(spec.lo, int) and isinstance(spec.hi, int), (
                    f"{spec.key} 是 int 型，边界却不是整数")
                assert isinstance(spec.default, int), f"{spec.key} 默认值不是整数"

    def test_every_item_is_explained(self):
        """#48：参数全摆出来 ≠ 人看得懂。label/desc 都不许空。"""
        for spec in tuning.TUNING_SPECS:
            assert spec.label.strip(), f"{spec.key} 没有中文名"
            assert spec.desc.strip(), f"{spec.key} 没有「作用」说明"
            assert len(spec.desc) >= 6, f"{spec.key} 的说明太短，等于没说"

    def test_module_paths_exist(self):
        for spec in tuning.TUNING_SPECS:
            assert (BASE / spec.module).is_file(), f"{spec.key} 指向不存在的模块 {spec.module}"


# ═══════════════════════════════════════════════════════════════════
# 二、双向契约：台账 ↔ 代码
# ═══════════════════════════════════════════════════════════════════

def _tuning_calls(path: Path):
    """AST 找 `tuning_int/float("key", <literal>)` → [(key, literal, lineno)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else None)
        if name not in ("tuning_int", "tuning_float"):
            continue
        if len(node.args) < 2:
            continue
        key_node, lit_node = node.args[0], node.args[1]
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        if isinstance(lit_node, ast.Constant):
            found.append((key_node.value, lit_node.value, node.lineno))
        else:
            found.append((key_node.value, None, node.lineno))
    return found


def _all_calls():
    calls = []
    for path in sorted(AGENT_DIR.glob("*.py")):
        for key, lit, lineno in _tuning_calls(path):
            calls.append((key, lit, f"{path.relative_to(BASE).as_posix()}:{lineno}"))
    return calls


class TestBidirectionalContract:
    def test_code_calls_are_all_registered(self):
        """代码里读了但台账没登记 → 界面看不见、日志刷 warning。"""
        unregistered = [(k, where) for k, _lit, where in _all_calls()
                        if k not in tuning._SPEC_BY_KEY]
        assert not unregistered, (
            "这些调参调用没登记在 TUNING_SPECS，界面上看不到：\n"
            + "\n".join(f"  {k} @ {w}" for k, w in unregistered))

    def test_registry_items_are_all_wired(self):
        """台账里登记了但代码从不引用 → 界面上的开关是假的。"""
        used = {k for k, _lit, _w in _all_calls()}
        dead = [s.key for s in tuning.TUNING_SPECS if s.key not in used]
        assert not dead, f"台账里有没人用的僵尸项：{dead}"

    def test_code_fallback_matches_registry(self):
        """最要紧的一条：界面显示的「默认值」必须就是代码里的兜底值。"""
        by_key = {s.key: s for s in tuning.TUNING_SPECS}
        drift = []
        for key, lit, where in _all_calls():
            spec = by_key.get(key)
            if spec is None or lit is None:
                continue        # 未登记的由上一条用例负责报
            if lit != spec.default:
                drift.append(f"{key}: 代码兜底 {lit!r} @ {where} ≠ 台账 {spec.default!r}")
        assert not drift, "默认值和代码漂移了（界面会撒谎）：\n" + "\n".join(drift)

    def test_registry_module_matches_code_location(self):
        """台账写的 module 必须就是真正调用它的那个模块。"""
        by_key = {s.key: s for s in tuning.TUNING_SPECS}
        mismatch = []
        for key, _lit, where in _all_calls():
            spec = by_key.get(key)
            if spec is None:
                continue
            mod = where.split(":", 1)[0]
            if mod != spec.module:
                mismatch.append(f"{key}: 台账写 {spec.module}，实际在 {mod}")
        assert not mismatch, "\n".join(mismatch)


# ═══════════════════════════════════════════════════════════════════
# 三、源码定位（「文件:行」必须真能指到那行）
# ═══════════════════════════════════════════════════════════════════

class TestSourceLocate:
    def test_locate_is_file_and_line(self):
        for spec in tuning.TUNING_SPECS:
            loc = tuning.locate(spec)
            path_part, _, line_part = loc.partition(":")
            assert path_part == spec.module, f"{spec.key} 定位到别的文件：{loc}"
            assert line_part.isdigit(), f"{spec.key} 没解析出行号：{loc}（定位退化了）"

    def test_located_line_is_the_constant(self):
        for spec in tuning.TUNING_SPECS:
            loc = tuning.locate(spec)
            path_part, _, line_part = loc.partition(":")
            lines = (BASE / path_part).read_text(encoding="utf-8").split("\n")
            text = lines[int(line_part) - 1].lstrip()
            assert text.startswith(spec.const), (
                f"{spec.key} 定位到 {loc}，但那行是 {text[:60]!r}")


# ═══════════════════════════════════════════════════════════════════
# 四、运行时：不读配置 / 读配置 / 坏配置 / 越界
# ═══════════════════════════════════════════════════════════════════

class TestRuntimeResolution:
    def test_without_config_uses_defaults(self):
        """pytest 进程内默认不读 config.yaml——主人一调参不能让全量测试变红。"""
        current = tuning.load()
        for spec in tuning.TUNING_SPECS:
            assert current[spec.key] == spec.default

    def test_config_override_is_applied(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  profile_max_len: 200\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 200
        assert tuning.value("background_budget_tokens") == 1100   # 没写的仍用默认

    def test_out_of_range_falls_back(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  profile_max_len: 9999\n")
        _use_config(monkeypatch, cfg)
        # 「糖糖必须还能起来」：坏值回落默认，不抛异常
        assert tuning.value("profile_max_len") == 120

    def test_wrong_type_falls_back(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, 'tuning:\n  profile_max_len: "很长"\n')
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 120

    def test_bool_is_not_a_number(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  background_budget_tokens: true\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("background_budget_tokens") == 1100

    def test_float_for_int_is_rejected(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  profile_max_len: 120.5\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 120

    def test_broken_yaml_falls_back(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  - : [unclosed\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 120

    def test_missing_section_uses_defaults(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "llm:\n  model: something\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 120

    def test_unregistered_key_in_config_is_ignored(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  not_a_real_key: 5\n  profile_max_len: 130\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 130
        assert "not_a_real_key" not in tuning.load()

    def test_reload_picks_up_changes(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  profile_max_len: 130\n")
        _use_config(monkeypatch, cfg)
        assert tuning.value("profile_max_len") == 130
        cfg.write_text("tuning:\n  profile_max_len: 150\n", encoding="utf-8")
        assert tuning.value("profile_max_len") == 130      # 有缓存，不自己变
        tuning.reload()
        assert tuning.value("profile_max_len") == 150

    def test_value_of_unknown_key_raises(self):
        with pytest.raises(KeyError):
            tuning.value("nope_not_here")

    def test_helpers_are_typed(self):
        assert isinstance(tuning.tuning_int("profile_max_len", 120), int)
        assert isinstance(tuning.tuning_float("voice_speed_min", 0.75), float)

    def test_unregistered_key_falls_back_instead_of_crashing(self):
        """代码写错 key 时宁可给兜底值也不能让糖糖起不来。"""
        assert tuning.tuning_int("totally_unknown_key", 7) == 7


# ═══════════════════════════════════════════════════════════════════
# 五、保存前的校验（validate_updates）
# ═══════════════════════════════════════════════════════════════════

class TestValidateUpdates:
    def test_accepts_valid_value(self):
        clean, errors = tuning.validate_updates({"profile_max_len": 150})
        assert errors == []
        assert clean == {"profile_max_len": 150}

    def test_rejects_out_of_range(self):
        clean, errors = tuning.validate_updates({"profile_max_len": 9999})
        assert clean == {}
        assert errors and "profile_max_len" in errors[0]

    def test_rejects_below_range(self):
        _clean, errors = tuning.validate_updates({"profile_max_len": 1})
        assert errors

    def test_rejects_unknown_key(self):
        clean, errors = tuning.validate_updates({"made_up": 1})
        assert clean == {}
        assert any("made_up" in e for e in errors)

    def test_accepts_numeric_string_from_form(self):
        """前端 `<input type="number">` 的 value 永远是字符串，宽容解析。"""
        clean, errors = tuning.validate_updates({"profile_max_len": "150"})
        assert errors == []
        assert clean == {"profile_max_len": 150}

    def test_rejects_non_numeric_string(self):
        _clean, errors = tuning.validate_updates({"profile_max_len": "很长"})
        assert errors

    def test_rejects_fractional_for_int(self):
        _clean, errors = tuning.validate_updates({"profile_max_len": "120.5"})
        assert errors

    def test_rejects_bool(self):
        _clean, errors = tuning.validate_updates({"profile_max_len": True})
        assert errors

    def test_rejects_non_object(self):
        _clean, errors = tuning.validate_updates([("a", 1)])
        assert errors

    def test_mixed_batch_keeps_good_and_reports_bad(self):
        clean, errors = tuning.validate_updates(
            {"profile_max_len": 150, "max_background_block_tokens": 99999})
        assert clean == {"profile_max_len": 150}
        assert len(errors) == 1

    def test_cross_check_block_over_total_budget(self):
        """单块上限 > 总预算：两个值都合法，放一起矛盾（第一块吃光预算）。"""
        clean, errors = tuning.validate_updates({"max_background_block_tokens": 900})
        # 默认总预算 1100，900 还没超 → 不算错
        assert clean == {"max_background_block_tokens": 900}, errors
        clean, errors = tuning.validate_updates(
            {"background_budget_tokens": 500, "max_background_block_tokens": 900})
        assert clean == {}
        assert any("预算" in e for e in errors)

    def test_cross_check_uses_current_value_for_the_other_side(self, tmp_path, monkeypatch):
        """只改一边时，另一边取**当前值**而不是默认值。"""
        cfg = _write_cfg(tmp_path, "tuning:\n  background_budget_tokens: 600\n")
        _use_config(monkeypatch, cfg)
        _clean, errors = tuning.validate_updates({"max_background_block_tokens": 800})
        assert errors, "总预算当前是 600，单块 800 应该被拦下"


# ═══════════════════════════════════════════════════════════════════
# 六、界面载荷 + 路由注册
# ═══════════════════════════════════════════════════════════════════

class TestUiPayload:
    def test_shape(self):
        p = tuning.ui_payload()
        assert p["total"] == len(tuning.TUNING_SPECS)
        assert p["config_key"] == "tuning"
        assert "重启" in p["note"]
        assert [g["key"] for g in p["groups"]] == [g for g, _ in tuning.GROUPS]

    def test_every_item_has_the_four_answers(self):
        """主人点名的四件事：当前值 / 默认值 / 作用 / 文件:行。"""
        for group in tuning.ui_payload()["groups"]:
            for item in group["items"]:
                assert item["value"] is not None
                assert item["default"] is not None
                assert item["desc"]
                assert ":" in item["source"]
                assert item["kind"] in ("int", "float")

    def test_changed_flag(self, tmp_path, monkeypatch):
        cfg = _write_cfg(tmp_path, "tuning:\n  profile_max_len: 150\n")
        _use_config(monkeypatch, cfg)
        p = tuning.ui_payload()
        assert p["changed"] == 1
        item = [i for g in p["groups"] for i in g["items"]
                if i["key"] == "profile_max_len"][0]
        assert item["changed"] is True
        assert item["value"] == 150 and item["default"] == 120

    def test_nothing_changed_by_default(self):
        assert tuning.ui_payload()["changed"] == 0


class TestBridgeRoutes:
    def _route_keys(self, name: str):
        tree = ast.parse((BASE / "tools/desktop_bridge.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    keys = set()
                    for k in node.value.keys:            # Dict
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
                    return keys
        return set()

    def test_tuning_endpoints_are_registered(self):
        assert "/api/tuning" in self._route_keys("ROUTES_GET")
        assert "/api/tuning/save" in self._route_keys("ROUTES_POST")

    def test_tuning_api_functions_exist(self):
        src = (BASE / "tools/desktop_bridge.py").read_text(encoding="utf-8")
        assert "def api_tuning(" in src
        assert "def api_tuning_save(" in src
