"""仪表盘的「记忆自动同步」开关（2026-09-20 主人需求）。

原话：「自动打包解包需要根据个人需要，所以在控制台也给一个开关，
关闭的话就没有弹窗提示的解包、打包，也不会执行相应程序」。

## 这个开关的难点不在"加个复选框"，在**关干净**

打包/解包有五个触发点：30 分钟定时、停止糖糖、关闭控制台、Windows 关机/注销、
开机 5 秒解包。每一个都是独立一条路，**漏掉任何一个，用户看到的就是
"我明明关了它还在动"**——比没有开关更糟（他会以为自己理解错了）。

所以实现把判定收成一个点 `_memory_sync_auto()`，本文件钉住：
  · 五个触发点全部经过它
  · 关掉时**连日志都不留**（否则又制造了一种新噪音）
  · 关掉时控制台**仍然关得掉**——`_do_exit` 原本等 `_pack_done_sig` 才退出，
    而那个信号只有打包真的启动过才会发。开关关着时若不改道，
    用户会点关闭却关不掉，且看不出为什么。

用 `object.__new__` 造一个**不带 GUI 的方法宿主**：这些方法只碰
`self.cfg` / `self._log` / `self._pack_timer` / `self._memory_sync_auto()`，
没必要为了测它们把窗口开起来（开了还要处理单实例锁和定时器）。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
CONSOLE = BASE / "糖糖控制台_qt.py"


@pytest.fixture(scope="module")
def console():
    """加载控制台模块（只定义类，不建 QApplication、不碰磁盘）"""
    spec = importlib.util.spec_from_file_location("_console_under_test", CONSOLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeTimer:
    def __init__(self):
        self.started_with = None
        self.active = False

    def start(self, ms):
        self.started_with = ms
        self.active = True

    def stop(self):
        self.active = False

    def isActive(self):
        return self.active


class _Sig:
    def __init__(self):
        self.emitted = []

    def emit(self, *a):
        self.emitted.append(a)


class _Host:
    """不带 GUI 的方法宿主。

    不能 `object.__new__(TangTangQtConsole)`——它是 QObject 子类，
    PySide 会拒绝（`not safe, use TangTangQtConsole.__new__()`），
    而真去调 `__new__` 又要有 QApplication。所以换个方向：
    把类里那几个**纯逻辑**方法取出来，绑到这个普通类上。
    方法体只碰 `self.cfg` / `_log` / `_pack_timer` / `_memory_sync_auto()`，
    以及模块级的 `BASE`/`sys`/`QApplication`（都在函数 __globals__ 里）。
    """
    __slots__ = ("cfg", "_pack_timer", "_pack_done_sig", "_packing", "_logs", "_log",
                 "_sugar_process", "_exiting", "_sync_hint")


_HOST_METHODS = ("_memory_sync_auto", "_toggle_memory_sync", "_refresh_sync_hint",
                 "_auto_pack_memory", "_auto_unpack_memory", "_on_commit_data",
                 "_do_exit")


def _host(console, cfg, **extra):
    cls = type("_Host", (_Host,), {
        name: console.TangTangQtConsole.__dict__[name] for name in _HOST_METHODS})
    w = cls()
    w.cfg = cfg
    w._pack_timer = _FakeTimer()
    w._pack_done_sig = _Sig()
    w._packing = False
    w._exiting = False
    w._sugar_process = None
    w._logs = []
    w._log = w._logs.append
    for k, v in extra.items():
        setattr(w, k, v)
    return w


# ═══════════════════════════════════════════════════════════
# 默认值与持久化
# ═══════════════════════════════════════════════════════════

def test_default_is_on_for_installs_without_the_key(console):
    """老配置里根本没有 `memory_sync` 这一段——默认必须是**开**（行为不变）。"""
    w = _host(console, {})
    assert w._memory_sync_auto() is True


def test_toggle_off_is_persisted_and_stops_the_timer(console, monkeypatch):
    saved = {}
    monkeypatch.setattr(console, "save_config_file",
                        lambda data: saved.update(data or {}))
    w = _host(console, {})
    w._pack_timer.start(30 * 60 * 1000)

    w._toggle_memory_sync(False)

    assert w.cfg["memory_sync"]["auto"] is False
    assert saved.get("memory_sync") == {"auto": False}, "没写回 config.yaml——重启就失忆"
    assert not w._pack_timer.isActive(), "关掉了但 30 分钟定时还在跑"
    assert w._memory_sync_auto() is False


def test_toggle_back_on_restarts_the_timer(console, monkeypatch):
    monkeypatch.setattr(console, "save_config_file", lambda data: None)
    w = _host(console, {"memory_sync": {"auto": False}})
    w._toggle_memory_sync(True)
    assert w._pack_timer.isActive()
    assert w._pack_timer.started_with == 30 * 60 * 1000


# ═══════════════════════════════════════════════════════════
# 关掉 = 真的不执行（用真的 subprocess.Popen 观察）
# ═══════════════════════════════════════════════════════════

def test_disabled_pack_never_spawns_the_helper(console, monkeypatch):
    """关掉时调 `_auto_pack_memory` 不许启动 `同步记忆.py`——一次都不许。"""
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append(a))

    w = _host(console, {"memory_sync": {"auto": False}})
    w._auto_pack_memory()

    assert calls == [], f"开关关着，却启动了打包进程：{calls}"
    assert w._logs == [], f"开关关着还留了日志（新的噪音源）：{w._logs}"
    assert w._packing is False


def test_enabled_pack_does_spawn_the_helper(console, monkeypatch):
    """**阳性对照**：同一个方法、开关开着时，必须真的启动。

    没有这一条，上面那条测试在一个"永远不打包"的实现上也是绿的。
    """
    import subprocess
    calls = []

    class _P:
        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append(a) or _P())

    w = _host(console, {"memory_sync": {"auto": True}})
    w._auto_pack_memory()

    assert calls, "开关开着却什么都没启动"
    argv = calls[0][0]
    assert any("同步记忆.py" in str(x) for x in argv), argv
    assert "打包" in argv


def test_disabled_unpack_returns_before_touching_the_snapshot_dir(console, tmp_path, monkeypatch):
    """关掉时开机解包连目录都不该看（原实现会 glob + 弹窗）。"""
    w = _host(console, {"memory_sync": {"auto": False}})
    w._navigate = lambda *a: pytest.fail("关掉时不该有任何导航/弹窗动作")
    console.QMessageBox.warning = staticmethod(
        lambda *a, **kw: pytest.fail("关掉时弹了窗"))
    w._auto_unpack_memory()          # 不应抛、不应碰任何东西
    assert w._logs == []


# ═══════════════════════════════════════════════════════════
# 关掉时控制台仍然关得掉（最容易漏的一条路径）
# ═══════════════════════════════════════════════════════════

def test_exit_quits_directly_when_disabled(console, monkeypatch):
    """关掉时 `_do_exit` 必须直接 quit。

    原路是「触发打包 → 等 `_pack_done_sig` → 再退」。开关关着时打包立刻返回、
    信号永不来 → 控制台停在「正在关闭」，用户点关闭却关不掉，且看不出为什么。
    """
    quit_called = []
    monkeypatch.setattr(console.QApplication, "quit",
                        staticmethod(lambda: quit_called.append(1)))
    w = _host(console, {"memory_sync": {"auto": False}})
    w._sugar_process = None          # 简化：不涉及停止糖糖的问答
    w._auto_pack_memory = lambda: pytest.fail("关掉时不该尝试打包")

    w._do_exit()

    assert quit_called, "开关关着时 _do_exit 没有退出——控制台会卡住"


def test_exit_still_waits_for_pack_when_enabled(console, monkeypatch):
    """**阳性对照**：开着时仍走"等打包完成再退"的原路（不因新分支漏掉旧行为）。"""
    quit_called = []
    monkeypatch.setattr(console.QApplication, "quit",
                        staticmethod(lambda: quit_called.append(1)))
    packed = []
    w = _host(console, {"memory_sync": {"auto": True}})
    w._sugar_process = None
    w._auto_pack_memory = lambda: packed.append(1)

    w._do_exit()

    assert packed == [1], "开着却没打包"
    assert not quit_called, "开着时不该立刻退出——要等 _on_pack_done"


# ═══════════════════════════════════════════════════════════
# 其余触发点也必须在闸门后面
# ═══════════════════════════════════════════════════════════

def test_shutdown_hook_does_not_log_when_disabled(console, monkeypatch):
    """Windows 关机/注销那条：**判定要在 `_log` 之前**。

    原实现在 `_auto_pack_memory()` 前先打了一行「尽力打包记忆快照…」，
    而 `_auto_pack_memory` 自己虽然会早返回，日志已经出去了——
    正是 `_memory_sync_auto` docstring 警告的那种「我明明关了它还在动」。
    （Code Review C-1）
    """
    w = _host(console, {"memory_sync": {"auto": False}})
    w._auto_pack_memory = lambda: pytest.fail("关掉时不该打包")
    w._on_commit_data()
    assert w._logs == [], f"关机时仍然留了日志：{w._logs}"


def test_shutdown_hook_packs_when_enabled(console, monkeypatch):
    packed = []
    w = _host(console, {"memory_sync": {"auto": True}})
    w._auto_pack_memory = lambda: packed.append(1)
    w._on_commit_data()
    assert packed == [1]


def test_all_pack_triggers_go_through_the_single_gate():
    """结构闸门：`_auto_pack_memory` / `_auto_unpack_memory` 的第一行必须是判定。

    五个触发点各自加 `if` 迟早漏一个；判定收在方法首行，
    调用点就不必（也不该）自己判断。
    """
    import ast
    tree = ast.parse(CONSOLE.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "_auto_pack_memory", "_auto_unpack_memory", "_memory_sync_auto"):
            found[node.name] = node
    assert set(found) == {"_auto_pack_memory", "_auto_unpack_memory", "_memory_sync_auto"}

    for name in ("_auto_pack_memory", "_auto_unpack_memory"):
        body = found[name].body
        # 跳过 docstring
        stmts = [s for s in body
                 if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        first = stmts[0]
        assert isinstance(first, ast.If), f"{name} 的第一条语句不是开关判定：{ast.dump(first)[:80]}"
        src = ast.get_source_segment(CONSOLE.read_text(encoding="utf-8"), first.test) or ""
        assert "_memory_sync_auto" in src, f"{name} 首行的判定不是 _memory_sync_auto：{src}"


def test_dashboard_hosts_the_toggle_card_next_to_the_control_card():
    """卡片位置：控制卡片**右边**（主人指定），且默认值取自配置而不是写死。"""
    src = CONSOLE.read_text(encoding="utf-8")
    assert "_build_ctrl_row" in src, "控制行没有抽出来，没法把新卡片放右边"
    assert "row.addWidget(ctrl, 3)" in src and "row.addWidget(sync, 2)" in src
    assert "self._sync_check.setChecked(self._memory_sync_auto())" in src, (
        "复选框的初始值写死了——重启后显示的状态会和实际行为不一致")
