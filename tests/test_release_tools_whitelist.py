"""发布包 tools/ 覆盖闸门（2026-11-14 事故，教训 #35 的同类变体）。

## 为什么有这条闸门

v1.08.00 的用户装不上。根因不是缺文件——仓库里 `tools/` 有 108 个模块，
`依赖位置.py`、`desktop_bridge.py` 都在——而是 `准备发布.py` 的
`TOOLS_KEEP_NAMES` 只放行 20 个，剩下 88 个被默认剔掉：

  · `依赖位置.py` 在 `tools/安装糖糖.py` 的**模块级**被加载（第 52-58 行），
    → 包内 `import` 期直接 FileNotFoundError，一键安装器连菜单都出不来。
  · `desktop_bridge.py` 被 `desktop/electron/main.ts` 用来判断项目根，
    → 桌面端认不出根目录。
  · 还有 14 个被 tests/ 引用的模块不在包内 → 包内 pytest 必红。

而当时的注释写着「闸门同上（test_referenced_tools_are_whitelisted）」——
**那个测试全仓不存在**（grep 只命中这条注释本身）。于是「够不够」从来没人检查。
这条闸门就是补上它，判据是**代码里的引用**，不是人手维护的清单。
"""

import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "prep_release_for_whitelist", BASE / "tools" / "准备发布.py")
_prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prep)


# ═══════════════════════════════════════════════════════
# 1. 覆盖性：凡被引用的，必须在随包集合里
# ═══════════════════════════════════════════════════════

def test_scan_finds_references_at_all():
    """先确认扫描器不是空转——否则下面那条断言会因为「集合为空」而假绿。"""
    ref = _prep.referenced_tool_modules()
    assert len(ref) >= 10, f"只扫到 {len(ref)} 个引用，正则/目录表失效了：{sorted(ref)}"


def test_every_referenced_tool_is_packed():
    ref = _prep.referenced_tool_modules()
    missing = ref - _prep.tools_to_pack()
    assert not missing, (
        f"这些模块被运行期/测试引用，但不会进发布包：{sorted(missing)}\n"
        "修法：加进 TOOLS_KEEP_NAMES，或让 tools_to_pack() 的自动并集覆盖它")


def test_packed_tools_all_exist_on_disk():
    for name in _prep.tools_to_pack():
        assert (_prep.BASE / "tools" / name).is_file(), \
            f"{name} 在白名单/引用集合里，但磁盘上没有——准备发布.py 会静默跳过它"


# ═══════════════════════════════════════════════════════
# 2. 事故现场的回归钉死
# ═══════════════════════════════════════════════════════

def test_incident_modules_are_packed():
    """v1.08.00 的两个直接元凶。它们回归 = 用户又装不上 / 桌面端起不来。"""
    for name in ("依赖位置.py", "desktop_bridge.py"):
        assert name in _prep.tools_to_pack(), f"{name} 不在随包集合里——事故复发"
        assert (_prep.BASE / "tools" / name).is_file()


# ═══════════════════════════════════════════════════════
# 3. 阳性对照 / 反例：扫描器认得名字形状，也不吞不存在的东西
# ═══════════════════════════════════════════════════════

def test_regex_matches_ascii_and_chinese_names():
    got = set(_prep._REF_TOOLS_RE.findall(
        "python tools/依赖位置.py; node tools/desktop_bridge.py"))
    assert got == {"依赖位置.py", "desktop_bridge.py"}


def test_nonexistent_module_is_not_treated_as_reference():
    """测试文件里种过 `__planted_*` 假模块名——它们磁盘上没有，不得进包。"""
    planted = "__planted_whitelist_probe__.py"
    assert not (_prep.BASE / "tools" / planted).is_file()
    assert planted not in _prep.referenced_tool_modules()
    assert planted not in _prep.tools_to_pack()


# ═══════════════════════════════════════════════════════
# 4. 第二跳：随包模块自己 import 的 tools 模块（2026-11-14 二次事故）
#
#     desktop_bridge.py → asset_edit.py → scenario_rename.py
#     前两个被外部提到、进了包；第三跳只在 asset_edit 的 import 行里，
#     于是包内 bridge 一启动就 ModuleNotFoundError，用户看到「后端桥退出」。
# ═══════════════════════════════════════════════════════

def test_scan_sees_python_module_import_form():
    """`from tools.X import` 这种写法必须被算作引用（原先只认路径字面量）。"""
    got = _prep._refs_in_text(
        "from tools.scenario_rename import sync_rename\nimport tools.asset_edit\n"
        "python tools/依赖位置.py")
    assert got == {"scenario_rename.py", "asset_edit.py", "依赖位置.py"}, got


def test_pack_set_is_closed_under_imports():
    """闭合性：包里每个模块 `from tools.X` 引用的模块，也必须在包里。"""
    pack = _prep.tools_to_pack()
    holes = {}
    for name in sorted(pack):
        p = _prep.BASE / "tools" / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for ref in _prep._refs_in_text(text):
            if (_prep.BASE / "tools" / ref).is_file() and ref not in pack:
                holes.setdefault(ref, []).append(name)
    assert not holes, (
        "这些模块被包内模块 import，却不在随包集合里（用户会在第二跳崩）：\n"
        + "\n".join(f"  {k} ← {v}" for k, v in sorted(holes.items())))


def test_second_hop_incident_module_is_packed():
    """事故回归钉死：asset_edit 的 import 目标必须随包。"""
    assert "asset_edit.py" in _prep.tools_to_pack()
    assert "scenario_rename.py" in _prep.tools_to_pack(), (
        "scenario_rename.py 不在随包集合里——desktop_bridge → asset_edit 的 import 链会断，"
        "桌面端一启动就「后端桥退出」")


def test_closure_is_actually_doing_something():
    """覆盖面自证：闭包若被人删成恒等函数，上面那条会假绿。"""
    extra = _prep.tools_to_pack() - (_prep.TOOLS_KEEP_NAMES | _prep.referenced_tool_modules())
    assert extra, ("闭包没有带进任何模块——要么判据失效，要么真的没有第二跳依赖"
                   "（后者请先确认 desktop_bridge → asset_edit → scenario_rename 这条链还在）")
    assert "scenario_rename.py" in extra, f"闭包带进的是 {sorted(extra)}，没有 scenario_rename"
