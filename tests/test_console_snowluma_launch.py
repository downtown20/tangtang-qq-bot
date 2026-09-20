"""控制台启动 SnowLuma 的方式：结构闸门 + 与自检模块的接口契约。

## 为什么要有这个文件

2026-09-20 主人在笔记本上实机发现：点控制台「启动 SnowLuma」只弹出一个**空白窗口**，
拿不到 SnowLuma 首次启动打印的网页面板初始密码，只能自己跑去目录里双击 `launcher.bat`。

根因是一行参数：`CREATE_NEW_CONSOLE` 旁边还挂着 `stdout=DEVNULL, stderr=DEVNULL`
——窗口开了，子进程写的每个字节却都进了 NUL 设备。而**那行密码只在 stdout 上打印一次**
（SnowLuma 源码里是 `process.stdout.write`，绕过了 logger，所以日志文件里也没有），
DEVNULL 一开就永久丢失。

四种启动方式做过实测对照（判据是让子进程自读它那扇控制台的屏幕缓冲区找标记）：
  · 留着 DEVNULL          → 自己窗口搜不到（3000 行只花 2ms，写进空设备）
  · 去掉 DEVNULL          → 搜得到（0.74s）✅ 采用
  · `cmd /c start … cmd /k`→ 也行，但 bat 跑完会多留一个常驻空窗口
  · `os.startfile`        → **不能设 cwd**，而 launcher.bat 没有 `cd /d`，
                            它靠 cwd 找 `./index.mjs`，会直接报找不到文件
  · `stdout=PIPE`         → **子进程会写满 64KB 管道缓冲后卡死**（实测阻塞 20s+）

所以下面的断言不是形式主义：每一条都对应上面一个实测出来的失败模式。
"""

import ast
import importlib.util
from enum import Enum
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONSOLE = BASE / "糖糖控制台_qt.py"
CONN_CHECK = BASE / "tools" / "检查连接.py"


def _func_source(name: str) -> str:
    """取出控制台里某个函数的源码。

    用 AST 而不是 grep：源码里写着大量"当初错在哪"的注释，注释里当然会出现
    `DEVNULL` 这些字眼。按文本 grep 会把解释性注释误判成违规代码。
    """
    src = CONSOLE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"控制台里找不到函数 {name}（）——它被改名或删了吗？")


# ═══════════════════════════════════════════════════════
# 1. 启动方式：把「空白窗口」钉死
# ═══════════════════════════════════════════════════════

def _launch_call(name: str) -> ast.Call:
    """在函数体里找到那次 `subprocess.Popen(...)`。"""
    body = _func_source(name)
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "Popen":
                return node
    raise AssertionError(f"{name} 里没有 subprocess.Popen —— 启动方式换了？请更新本闸门")


def _kwargs_of(call: ast.Call) -> dict:
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}


def test_snowluma_launch_does_not_silence_child_output():
    """不许再把子进程的 stdout/stderr 丢掉。

    这是主人实机报告的那个 bug 本身。丢掉它 = SnowLuma 首次启动的初始密码
    永久丢失（那行只走 stdout，日志文件里没有），用户进不了网页面板。
    """
    kw = _kwargs_of(_launch_call("_start_snowluma"))
    for key in ("stdout", "stderr"):
        assert key not in kw, (
            f"_start_snowluma 又给 {key} 设了值（{kw[key]}）。\n"
            f"  设成 DEVNULL = 空白窗口 + 密码永久丢失；设成 PIPE = 写满 64KB 后卡死。\n"
            f"  两种都要让子进程直接继承新控制台的句柄——也就是**不要传这个参数**。")


def test_snowluma_launch_keeps_new_console_and_cwd():
    """`CREATE_NEW_CONSOLE` 和 `cwd` 都不能丢。

    cwd：`launcher.bat` 里**没有** `cd /d "%~dp0"`，它靠当前目录找
         `./index.mjs`（实测：换成 `os.startfile` 就因为这个直接报找不到文件）。
    新控制台：没有它就没有窗口，用户看不到任何输出。
    """
    kw = _kwargs_of(_launch_call("_start_snowluma"))
    assert "creationflags" in kw and "CREATE_NEW_CONSOLE" in kw["creationflags"], (
        f"启动 SnowLuma 时没开新控制台（creationflags={kw.get('creationflags')}）——"
        f"用户就没有任何地方能看到它的输出")
    assert "cwd" in kw, (
        "启动 SnowLuma 时没传 cwd。launcher.bat 没有 `cd /d`，"
        "它靠 cwd 找 `./index.mjs`，不传就会报找不到文件。")


def test_snowluma_launch_does_not_use_startfile():
    """`os.startfile` 有过一次实测淘汰：设不了 cwd，而且拿不到 PID。

    ⚠ 这里查的是 **AST 调用节点**，不是源码文本——`ast.get_source_segment` 会把
    注释一起切回来，而 `_start_snowluma` 的注释里正写着"为什么淘汰 os.startfile"，
    按文本查会把自己的说明误判成违规（第一版就是这么红的）。
    """
    body = _func_source("_start_snowluma")
    calls = [n.func.attr for n in ast.walk(ast.parse(body))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "startfile" not in calls, (
        "改用 os.startfile 了？它**设不了 cwd**，而 launcher.bat 依赖 cwd 找 "
        "./index.mjs（实测会报 `can't open file`）。另外它也不返回 PID，"
        "自动重启就没法按 PID 杀进程了。")


# ═══════════════════════════════════════════════════════
# 2. 控制台 ↔ 检查连接.py 的接口契约
# ═══════════════════════════════════════════════════════

def _load_connect_check():
    """完全照抄控制台 `_run_connection_check` 的加载方式（中文文件名没法直接 import）。"""
    spec = importlib.util.spec_from_file_location("conn_check", CONN_CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_console_loader_can_load_the_check_module(tmp_path):
    """控制台按路径加载它、并调用 `check_all(项目根)`——这条接口不能断。

    断了的表现：用户点「连接自检」只看到一句"跑不起来"，而那正是他最需要帮助的时候。
    """
    mod = _load_connect_check()
    assert hasattr(mod, "check_all"), "检查连接.py 没有 check_all——控制台的按钮调它"
    results = mod.check_all(tmp_path)
    assert results, "check_all 在空目录上返回了空列表——至少该报「SnowLuma 没装」"


def test_check_results_carry_the_fields_console_prints(tmp_path):
    """控制台打印的是 status / detail / next_step 三个字段，一个都不能少。"""
    mod = _load_connect_check()
    for r in mod.check_all(tmp_path):
        assert hasattr(r, "detail") and hasattr(r, "next_step"), (
            f"{r!r} 缺 detail/next_step——控制台会 AttributeError")
        assert isinstance(r.status, Enum), (
            f"{r.status!r} 不是枚举。控制台按 status.value 查标记表，"
            f"传裸字符串会静默落进兜底的 '[?]'")


def _console_mark_table() -> dict:
    """从 `_run_connection_check` 里**解析出**那张 `{状态: 标记}` 字典字面量。

    ⚠ 不能用「字面量在源码里出现过」来判：`ast.get_source_segment` 会把**注释**
    一起切回来，而那段代码的注释里正写着 `"[ ]" 表示还没走到` 这类说明文字。
    第一版就是这么形同虚设的——把 `"skip": "[ ]"` 从字典里**删掉**，两条闸门
    照样全绿（审查方用阳性对照坐实）。那正是 CLAUDE.md 反模式 #30 的形态：
    检查跑了，但它宣称要拦的场景它拦不住。
    """
    body = _func_source("_run_connection_check")
    for node in ast.walk(ast.parse(body)):
        # 形如 `mark = {...}.get(x, "[?]")` → 取那个 Dict 字面量
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Dict)):
            d = node.func.value
            return {ast.literal_eval(k): ast.literal_eval(v)
                    for k, v in zip(d.keys, d.values)}
    raise AssertionError(
        "没在 _run_connection_check 里找到 `{状态: 标记}.get(...)` 那张表——"
        "它被重构了吗？请一并更新本闸门")


def test_every_status_has_a_mark_in_the_console():
    """`检查连接.py` 的每个状态都要在控制台那张标记表里有对应项。

    否则新增一种状态会静默显示成 `[?]`——用户看到了结论却看不懂它是哪一类。
    """
    mod = _load_connect_check()
    table = _console_mark_table()
    missing = {s.value for s in mod.Status} - set(table)
    assert not missing, (
        f"这些状态在控制台的标记表里没有对应项，会显示成 [?]：{sorted(missing)}")


def test_console_check_marks_use_bracket_family():
    """自检结果的标记要用 bracket 家族（`[√] [×] [ ]`）。

    ⚠ 注意适用范围：只管**结果标记**。那几行常被用户复制粘贴到别处，也会出现在
    `tools/检查连接.py` 的命令行输出里——那里是 Win10 传统 conhost，字体链不含
    Segoe UI Emoji，✅❌ 会渲染成空心方框。控制台日志里那些装饰性 emoji（⚠ ✅）
    **不算违规**：那是 Qt 控件，渲染没问题，而且全文件一贯如此。
    """
    bad = {v for v in _console_mark_table().values()
           if any(ch in v for ch in "✅❌⚠")}
    assert not bad, f"这些标记用了 Win10 画不出的 emoji：{sorted(bad)}"


# ═══════════════════════════════════════════════════════
# 3. 「连接自检」按钮的处理函数：端到端跑一遍
# ═══════════════════════════════════════════════════════

def _console_module():
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("console_under_test", CONSOLE)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_connection_check_button_runs_end_to_end(monkeypatch):
    """点「连接自检」→ 真的加载工具、真的检查、真的产出日志行。

    `_run_connection_check` 只依赖 `self._log` 和模块级 `BASE`，所以可以脱离
    Qt 窗口直接调——这正是它最容易坏的地方（importlib 那段胶水一旦断了，
    用户看到的只是一句"跑不起来"，而那正是他最需要帮助的时候）。

    ⚠ 只断言**形状**不断言结论：它会对本机真实的 5099/3000/3001 做 TCP 探测，
    结论取决于跑测试时 SnowLuma/糖糖开没开。断言结论 = 随机红，比不写还糟。
    """
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    mod = _console_module()

    class _Dummy:
        def __init__(self):
            self.logs = []
            self.navigated = []

        def _log(self, msg):
            self.logs.append(msg)

        def _navigate(self, page):
            # 按钮长在仪表盘上，结论写在日志页——处理函数会先把用户带过去
            self.navigated.append(page)

    d = _Dummy()
    mod.TangTangQtConsole._run_connection_check(d)

    assert d.logs, "按钮点下去一行日志都没有"
    joined = "\n".join(d.logs)
    assert "连接自检" in joined, f"没有起止标记，用户不知道这是自检结果：\n{joined}"
    assert "[√]" in joined or "[×]" in joined, (
        f"一条结论标记都没有——加载工具那段可能静默失败了：\n{joined}")
    assert "跑不起来" not in joined and "没找到 tools" not in joined, (
        f"自检模块没加载起来（用户只会看到这句）：\n{joined}")
    assert all(isinstance(x, str) for x in d.logs), "日志里混进了非字符串"
    assert d.navigated == ["log"], (
        "按钮在仪表盘上、结论在日志页——处理函数必须先把用户带过去，"
        f"否则他点完看到的是一个没反应的按钮（实际跳转：{d.navigated}）")


# ═══════════════════════════════════════════════════════
# 4. 选哪个 SnowLuma 安装目录——导入期不许抛异常
# ═══════════════════════════════════════════════════════

def test_version_key_survives_renamed_directories(tmp_path):
    """目录名不是规范版本号时**不许抛异常**。

    审查实测：原实现是裸 `int(x)` 解析目录名，跑在**模块导入期**——用户把目录
    改名成 `SnowLuma-v1.14.9备份` 之类，ValueError 会让**整个控制台起不来**，
    屏幕上只有一段裸 traceback。解析不出来的按"最旧"处理即可。
    """
    mod = _console_module()
    key = mod._sl_version_key
    for weird in ("SnowLuma-v1.14.9备份", "SnowLuma-v旧版", "SnowLuma-v", "SnowLuma-v1.x"):
        key(tmp_path / weird)          # 不抛就算过
    # 正常版本号仍要排得对（**不能按字典序**：那样 1.9 会排在 1.14 后面）
    a = tmp_path / "SnowLuma-v1.14.9-win-x64"
    b = tmp_path / "SnowLuma-v1.9.0-win-x64"
    assert key(a) > key(b), "版本号比较退化成字典序了（1.9 会压过 1.14）"
    assert key(a) > key(tmp_path / "SnowLuma-v1.14.9备份"), "乱码目录名不该排在最前"


def test_version_key_prefers_a_complete_install(tmp_path):
    """同一版本号下，带 launcher.bat 的那份优先。

    与 `tools/检查连接.py` 的判据保持一致——否则会出现「自检说装好了」而
    「控制台让你重新下载」这种自相矛盾。
    """
    mod = _console_module()
    good = tmp_path / "SnowLuma-v1.14.9-win-x64"
    lite = tmp_path / "SnowLuma-v1.14.9-win-x64-lite"
    good.mkdir(); lite.mkdir()
    (good / "launcher.bat").write_text("@echo off\n", encoding="utf-8")
    assert mod._sl_version_key(good) > mod._sl_version_key(lite)


# ═══════════════════════════════════════════════════════
# 5. 陈旧的 20 秒定时器不许误报
# ═══════════════════════════════════════════════════════

def test_stale_stuck_timer_does_not_fire_against_a_new_process():
    """「启动 → 停止 → 再启动」时，第一次的定时器不许拿第二次的状态判定。

    2026-09-20 独立复核实测：`QTimer.singleShot(20000, …)` 挂了就没法取消，
    回调只读**当下**的 `_sugar_connected` / `_sugar_process` —— 于是 run#1 的定时器
    会在 run#2 只跑了 10 秒时（而不是 20 秒）就报「糖糖已经起来了，但 SnowLuma
    一直没连过来」，并把卡片涂成橙色。修复方式是给回调带一个「代次」，
    对不上就直接返回。
    """
    mod = _console_module()

    class _Proc:
        def state(self):
            # 必须返回真的枚举成员：`QProcess.Running` 是 enum，拿 int 比会不相等，
            # 于是函数会在"进程没在跑"那一关提前 return，测出来的红是假的
            return mod.QProcess.Running

    class _Label:
        def __init__(self):
            self.text = ""

        def setText(self, t):
            self.text = t

    class _Card:
        def __init__(self):
            self._val_label = _Label()

    class _Dummy:
        def __init__(self):
            self.logs = []
            self._sugar_connected = False   # 新进程还没连上（正是会误报的状态）
            self._sugar_process = _Proc()
            self._card_online = _Card()
            self._pal = {"orange": "#f80"}

        def _log(self, msg):
            self.logs.append(msg)

        def _set_status_color(self, label, color):
            label.color = color

    # 模拟：当前是第 2 次启动，但回调是第 1 次留下的
    d = _Dummy()
    d._sugar_run_id = 2
    mod.TangTangQtConsole._check_sugar_connection_stuck(d, 1)
    assert not d.logs, (
        f"陈旧的定时器（run=1，当前 run=2）还是误报了：{d.logs}")

    # 同一个代次、且确实没连上 → 应当照常提醒
    d2 = _Dummy()
    d2._sugar_run_id = 2
    mod.TangTangQtConsole._check_sugar_connection_stuck(d2, 2)
    assert d2.logs, "同一代次下不提醒了——护栏写过头，把正常提示也关掉了"
