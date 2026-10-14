"""SnowLuma 更新检查器（`tools/检查SnowLuma更新.py`）——两条「假绿」的闸门。

## 这个检查器曾经**永久静默**，而且不报错

2026-09-20 独立复核实测：本机 SnowLuma 1.14.9、线上最新 **v1.14.17**，
**落后 8 个版本，一条提示都没有**。两个成因，都在同一个文件里：

  ① 本地版本号写死目录名 `SnowLuma-v1.14.9-win-x64`（安装器/README 却让用户
     解压**任意** `SnowLuma-vX.Y.Z-win-x64`）→ 用户的目录名 ≠ 写死那个 →
     `_local_version()` 返回 "" → `main()` 在 `if not local` 早退。
  ② 取线上版本时 `subprocess.run(..., text=True)` **没指定编码** → Windows
     中文版用 cp936 解码 GitHub 的 UTF-8 JSON（响应里带 emoji 就炸）→
     `UnicodeDecodeError` → `out.stdout` 是 None → `json.loads(None)` 抛错 →
     被裸 `except` 吞掉 → 返回 "" → 又早退。

两种成因的症状**完全一样**：「跑了、退出码 0、什么都不说」——
和「没有新版本」长得一模一样。这正是 CLAUDE.md 反模式 #30 的老形态：
检查器写了，但从没生效，而它不会报错，所以没人发现。

下面每条断言都对应上面一个具体成因，并且都能被改坏而变红。
"""
import importlib.util
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
UPDATER = BASE / "tools" / "检查SnowLuma更新.py"


def _load():
    spec = importlib.util.spec_from_file_location("_updater_under_test", UPDATER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════
# 成因①：本地版本不能绑死某个目录名
# ═══════════════════════════════════════════════════════════

def test_local_version_follows_whatever_version_dir_exists(tmp_path, monkeypatch):
    """目录名换成别的版本，照样要读得到。"""
    import json
    (tmp_path / "SnowLuma-v9.9.9-win-x64").mkdir(parents=True)
    (tmp_path / "SnowLuma-v9.9.9-win-x64" / "package.json").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path, raising=False)
    assert mod._local_version() == "9.9.9", (
        "版本号还绑在某个具体目录名上——用户装别的版本就会永久静默")


def test_local_version_picks_the_highest_of_several(tmp_path, monkeypatch):
    """并排放着好几个版本时取最高的（开发机上真有四个）。"""
    import json
    for v in ("1.11.4", "1.12.0", "1.14.8", "1.14.9"):
        d = tmp_path / f"SnowLuma-v{v}-win-x64"
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps({"version": v}), encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path, raising=False)
    assert mod._local_version() == "1.14.9"


def test_missing_snowluma_says_so_instead_of_silence(tmp_path, monkeypatch, capsys):
    """没装 SnowLuma 时说一句人话而不是装死（控制台不读 stderr，用户手跑看得到）。"""
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path / "没有这个目录", raising=False)
    monkeypatch.setattr(mod, "_latest_version", lambda: "9.9.9")
    assert mod.main() == 0
    assert "没找到本机的 SnowLuma" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════
# 成因②：子进程输出必须按 UTF-8 解码
# ═══════════════════════════════════════════════════════════

def test_latest_version_decodes_subprocess_output_as_utf8(monkeypatch):
    """**核心闸门**：`subprocess.run` 必须显式指定编码。

    不指定 → Windows 用 locale（cp936）解码 → GitHub 的 UTF-8 JSON 里
    有任何非 ASCII 字节就 `UnicodeDecodeError` → `stdout=None` → 静默。
    所以这里不看结果，看**调用参数**：encoding 必须传了 utf-8。
    """
    seen = {}

    class _Out:
        stdout = '{"tag_name": "v1.2.3"}'

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _Out()

    monkeypatch.setattr(subprocess, "run", fake_run)
    mod = _load()
    assert mod._latest_version() == "1.2.3"
    assert seen.get("encoding", "").lower().startswith("utf"), (
        f"子进程没指定 UTF-8 编码（实际 encoding={seen.get('encoding')!r}）——"
        "Windows 中文版会用 GBK 解码，一个 emoji 就让整个检查永久静默")


def test_latest_version_survives_none_stdout(monkeypatch):
    """哪怕 stdout 真的是 None，也只当"查不到"，不许把异常漏出去。"""
    class _Out:
        stdout = None

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Out())
    mod = _load()
    assert mod._latest_version() == ""


def test_network_failure_is_reported_as_unknown(tmp_path, monkeypatch, capsys):
    """"查不到"要说出来，不能和"没有新版本"混为一谈。"""
    import json
    (tmp_path / "SnowLuma-v1.0.0-win-x64").mkdir(parents=True)
    (tmp_path / "SnowLuma-v1.0.0-win-x64" / "package.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(mod, "_latest_version", lambda: "")
    assert mod.main() == 0
    assert "查不到线上最新版本" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════
# 有新版时必须真的报出来
# ═══════════════════════════════════════════════════════════

def test_newer_version_is_announced(tmp_path, monkeypatch, capsys):
    """**阳性对照**：这次装的是"有新版"，它必须吭声 + 返回码 1。

    没有这一条，上面那些"不报错"的断言在一个永远返回 0 的实现上也全绿。
    """
    import json
    (tmp_path / "SnowLuma-v1.14.9-win-x64").mkdir(parents=True)
    (tmp_path / "SnowLuma-v1.14.9-win-x64" / "package.json").write_text(
        json.dumps({"version": "1.14.9"}), encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(mod, "_latest_version", lambda: "1.14.17")
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "有新版本" in out and "1.14.17" in out


def test_up_to_date_is_silent(tmp_path, monkeypatch, capsys):
    import json
    (tmp_path / "SnowLuma-v1.14.17-win-x64").mkdir(parents=True)
    (tmp_path / "SnowLuma-v1.14.17-win-x64" / "package.json").write_text(
        json.dumps({"version": "1.14.17"}), encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod, "SNOWLUMA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(mod, "_latest_version", lambda: "1.14.17")
    assert mod.main() == 0
    cap = capsys.readouterr()
    assert not cap.out.strip() and not cap.err.strip()


# ═══════════════════════════════════════════════════════════
# 与脚本本身的守卫 / 控制台的接线
# ═══════════════════════════════════════════════════════════

def test_script_has_utf8_stdout_guard():
    """这个脚本曾**是唯一没有 stdout 守卫的**，而它要打印 🔔。

    管道/重定向下 GBK 会抛 UnicodeEncodeError ——「有新版」反而比「没新版」更糟：
    退出码 1、stdout 空、一堆 traceback。其它兄弟脚本（安装糖糖/同步记忆/体检）都有。
    """
    src = UPDATER.read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8"' in src, "缺 UTF-8 输出守卫"


def test_console_runs_the_check_off_the_gui_thread():
    """控制台调用它时**不能阻塞 GUI 线程**。

    `subprocess.run(timeout=30)` 直接跑在 QTimer 回调里时，一个"连得上但不应答"
    的地址实测要 20.2 秒才返回 → 启动 8 秒后界面冻 20 秒、
    Windows 给窗口标题挂上「(无响应)」，而那 20 秒换来的还是静默。
    """
    console = (BASE / "糖糖控制台_qt.py").read_text(encoding="utf-8")
    assert "_SnowLumaUpdateWorker" in console, "更新检查没走线程"
    import ast
    tree = ast.parse(console)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_check_snowluma_update"), None)
    assert fn is not None, "控制台里没有 _check_snowluma_update"

    # ⚠ 用 AST 找**调用点**，不要拿源码文本 grep：那个函数的 docstring 里
    #   正解释着"原先这里是 subprocess.run(...) 直接跑在回调里"——
    #   按文本匹配会命中注释、把解释误判成违规（这坑本项目踩过好几次）。
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "subprocess"]
    assert not calls, "GUI 线程里又出现 subprocess 调用了"
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "start" for n in ast.walk(fn)), (
        "没有看到 .start()——更新检查没被真正启动")
