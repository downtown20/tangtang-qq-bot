"""依赖落盘位置（tools/依赖位置.py）的契约测试。

为什么单独给它建测试：安装器 / 依赖迁移 / 控制台都从这一处取「装到哪儿」，
两份规则一旦漂移就是「装了却读不到」——本仓已因「同一规则的两份拷贝」踩坑四次。

⚠ 全程不碰项目的真 `依赖位置.json`：所有用例把 CONFIG_PATH 打到 tmp_path，
   monkeypatch 收尾自动还原。
⚠ 全程不弹真对话框：choose_directory 只测「取消」路径，Qt / tkinter 两个后端都打桩。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
LOCATION = BASE / "tools" / "依赖位置.py"
INSTALLER = BASE / "tools" / "安装糖糖.py"


def _load(name: str, path: Path):
    """按路径加载（中文文件名没法直接 import，与其它测试同一套路）"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def loc(tmp_path, monkeypatch):
    """依赖位置模块，读写落点隔到 tmp_path，环境变量清空。"""
    mod = _load("_deps_location_under_test", LOCATION)
    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "依赖位置.json")
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    return mod


_INSTALLER_CACHE: dict = {}


def _installer():
    """安装器模块（导入即 exec tools/同步记忆.py，无重副作用；导入期不落盘）"""
    if "mod" not in _INSTALLER_CACHE:
        _INSTALLER_CACHE["mod"] = _load("_installer_deps_wiring", INSTALLER)
    return _INSTALLER_CACHE["mod"]


# ═══════════════════════════════════════════════════════
# 一、依赖根本身：默认值 / 优先级 / 落盘 / 降级
# ═══════════════════════════════════════════════════════

def test_默认依赖根指向项目内的依赖目录(loc):
    """默认值必须是**项目内**的 依赖/——不是 C 盘的 Python 目录（本模块存在的理由）"""
    assert loc.DEFAULT_DEPS_ROOT == BASE / "依赖"
    assert loc.PROJECT_ROOT == BASE
    assert loc.get_deps_root() == BASE / "依赖"
    assert "默认" in loc.describe_deps_root()


def test_默认依赖根不在系统盘(loc):
    """项目装在 D 盘时，默认落点也不该在 C 盘。"""
    if loc.is_on_system_drive(loc.PROJECT_ROOT):
        pytest.skip(f"项目本身装在系统盘（{loc.PROJECT_ROOT.drive}）——"
                    "项目内目录只可能在 C 盘，此断言跳过（装到别的盘时才有意义）")
    assert not loc.is_on_system_drive(loc.get_deps_root())


def test_环境变量优先于配置文件(loc, tmp_path, monkeypatch):
    """优先级：环境变量 TANGTANG_DEPS_ROOT > 依赖位置.json > 项目内 依赖/"""
    loc.set_deps_root(tmp_path / "配置里选的")
    assert loc.get_deps_root() == (tmp_path / "配置里选的").resolve()
    assert "依赖位置.json" in loc.describe_deps_root()

    monkeypatch.setenv("TANGTANG_DEPS_ROOT", str(tmp_path / "环境变量给的"))
    assert loc.get_deps_root() == tmp_path / "环境变量给的"
    assert "TANGTANG_DEPS_ROOT" in loc.describe_deps_root()


def test_set_deps_root_写盘并生效(loc, tmp_path):
    """选了位置就立刻落盘——避免「选了但没生效」"""
    target = tmp_path / "另一块盘" / "糖糖依赖"
    p = loc.set_deps_root(target)

    assert p == target.resolve()
    assert target.is_dir(), "写配置前就该把目录建出来"
    assert loc.get_deps_root() == target.resolve()

    cfg = tmp_path / "依赖位置.json"
    assert loc.CONFIG_PATH.name == "依赖位置.json"
    assert cfg.is_file()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["依赖根"] == str(target.resolve())
    assert "依赖位置.json" in loc.describe_deps_root()


@pytest.mark.parametrize("content", [
    None,                      # 文件不存在
    "",                        # 空文件
    "{ 这不是 json",           # 半截 JSON
    "[1, 2, 3]",               # JSON 但不是对象
    '{"依赖根": ""}',          # 键在但是空的
    '{"依赖根": 3}',           # 键在但不是字符串
    '{"别的键": "D:/x"}',      # 根本没有这个键
])
def test_配置损坏或缺失时降级到默认不抛异常(loc, tmp_path, content):
    if content is not None:
        loc.CONFIG_PATH.write_text(content, encoding="utf-8")

    assert loc.get_deps_root() == loc.DEFAULT_DEPS_ROOT
    assert "默认" in loc.describe_deps_root()


def test_subdir_在依赖根之下(loc):
    root = loc.get_deps_root()
    assert loc.subdir("models") == root / "models"
    assert loc.subdir("cache/pip") == root / "cache" / "pip"


def test_is_on_system_drive_认盘符(loc):
    """警告用，不做硬拒绝——项目在 C 盘时也给放行"""
    assert loc.is_on_system_drive(loc.get_deps_root()) == (
        loc.get_deps_root().drive.rstrip("\\/").casefold()
        == (__import__("os").environ.get("SystemDrive", "C:")).rstrip("\\/").casefold())


# ═══════════════════════════════════════════════════════
# 二、目录选择对话框：测试里不许真弹
# ═══════════════════════════════════════════════════════

def test_choose_directory_取消返回None且不弹框(loc, tmp_path, monkeypatch):
    """取消 = 返回 None（调用方据此继续用默认值，不算失败）"""
    tkinter = pytest.importorskip("tkinter")
    import tkinter.filedialog as filedialog

    class _FakeTk:
        def withdraw(self):
            pass

        def attributes(self, *a, **k):
            pass

        def destroy(self):
            pass

    monkeypatch.setattr(tkinter, "Tk", lambda *a, **k: _FakeTk())
    monkeypatch.setattr(filedialog, "askdirectory", lambda **k: "")

    # Qt 分支：包一个假模块，保证不会走到真的 QFileDialog（有 Qt 环境时）
    fake_qt = types.ModuleType("PySide6.QtWidgets")

    class _FakeApp:
        @staticmethod
        def instance():
            return None

    class _FakeDialog:
        Option = type("Option", (), {"ShowDirsOnly": 0})

        @staticmethod
        def getExistingDirectory(*a, **k):
            raise AssertionError("测试里不该打开真的 Qt 目录对话框")

    fake_qt.QApplication = _FakeApp
    fake_qt.QFileDialog = _FakeDialog
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", fake_qt)

    assert loc.choose_directory("测试标题", tmp_path) is None


# ═══════════════════════════════════════════════════════
# 三、安装器接线：所有落盘目标都从依赖根取
# ═══════════════════════════════════════════════════════

def test_落盘目标接在依赖根之下(tmp_path, monkeypatch):
    """临时文件 / pip 缓存的目录必须由 _deps_target 解析（不手拼 BASE / …）"""
    mod = _installer()
    fake_root = tmp_path / "别的盘" / "糖糖依赖"
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: fake_root)
    monkeypatch.setattr(mod, "BASE", tmp_path / "项目")

    assert mod._deps_target("temp_files") == fake_root / "temp_files"
    assert mod._deps_target("cache/pip") == fake_root / "cache" / "pip"


def test_模型目录写在依赖根并把老路径联接到新位置(tmp_path, monkeypatch):
    """依赖根换盘后，agent/*.py 硬编码的项目内路径仍要读得到（目录联接）"""
    mod = _installer()
    proj = tmp_path / "项目"
    proj.mkdir()
    fake_root = tmp_path / "别的盘" / "糖糖依赖"
    monkeypatch.setattr(mod, "BASE", proj)
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: fake_root)

    target = mod._deps_path("models/BAAI/bge-small-zh-v1.5", link_back=True)
    assert target == fake_root / "models" / "BAAI" / "bge-small-zh-v1.5"

    legacy = proj / "models"
    if not mod._is_link_dir(legacy):
        pytest.skip("这台机器建不了目录联接（非 Windows / 权限不足）——跳过可见性断言")
    # 运行期读的是 ./models/...（agent/embeddings.py:24）——写新位置，老路径要看得见
    target.mkdir(parents=True, exist_ok=True)   # 真正的下载器会建出这层目录
    (target / "config.json").write_text("{}", encoding="utf-8")
    assert (legacy / "BAAI" / "bge-small-zh-v1.5" / "config.json").is_file()


def test_依赖根等于项目根时就地写_老行为不变(tmp_path, monkeypatch):
    """项目装在 D 盘、依赖根没动过 → 落点与改动前逐字相同"""
    mod = _installer()
    monkeypatch.setattr(mod, "BASE", tmp_path)
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: tmp_path)

    assert mod._deps_target("models") == tmp_path / "models"
    assert mod._deps_path("models/BAAI/x") == tmp_path / "models" / "BAAI" / "x"
    assert not mod._is_link_dir(tmp_path / "models")


def test_老位置已有真实目录时不搬用户数据(tmp_path, monkeypatch):
    """BASE/models 里已经躺着 6 G 模型——换了依赖根也只能原地用，绝不自动搬"""
    mod = _installer()
    proj = tmp_path / "项目"
    (proj / "models").mkdir(parents=True)
    monkeypatch.setattr(mod, "BASE", proj)
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: tmp_path / "别的盘")

    assert mod._deps_path("models/BAAI/x", link_back=True) == proj / "models" / "BAAI" / "x"
    assert (proj / "models").is_dir()
    assert not mod._is_link_dir(proj / "models")


def test_目录检测项不再写死项目内绝对路径():
    """按类型开天窗的反面：DIR_CHECKS 里不许再出现盘符/绝对路径"""
    mod = _installer()
    checked = 0
    for entries in mod.DIR_CHECKS.values():
        for item in entries:
            rel = item[1]
            checked += 1
            assert isinstance(rel, str), f"DIR_CHECKS 还留着 Path 对象：{rel}"
            assert ":" not in rel, f"DIR_CHECKS 还写着绝对路径：{rel}"
            assert not rel.startswith(("/", "\\")), f"DIR_CHECKS 还写着绝对路径：{rel}"
    assert checked, "DIR_CHECKS 一条都没扫到——判据失效了？"


def test_下载目标都经过依赖根(tmp_path, monkeypatch):
    """fetch_models 里那张表的目标位置，逐条解析都必须落在依赖根下"""
    mod = _installer()
    fake_root = tmp_path / "别的盘" / "糖糖依赖"
    monkeypatch.setattr(mod, "BASE", tmp_path / "项目")
    (tmp_path / "项目").mkdir()
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: fake_root)

    rels = [spec[1] for specs in mod.MODELS.values() for spec in specs if spec[1]]
    assert rels, "MODELS 里一个带落点的条目都没有——判据失效了？"
    for rel in rels:
        resolved = mod._deps_path(rel, link_back=True)
        assert resolved == fake_root / Path(rel), f"{rel} 没落在依赖根下：{resolved}"


def test_deps_root_开关直接落盘且不弹对话框(tmp_path, monkeypatch):
    """--deps-root PATH：自动化场景不弹框"""
    mod = _installer()
    picked = tmp_path / "命令行指定的"
    recorded = []

    def _fake_set(path):
        recorded.append(str(path))
        picked.mkdir(parents=True, exist_ok=True)
        return picked

    monkeypatch.setattr(mod.依赖位置, "set_deps_root", _fake_set)
    monkeypatch.setattr(mod.依赖位置, "is_on_system_drive", lambda *a, **k: False)
    monkeypatch.setattr(mod.依赖位置, "choose_directory",
                        lambda *a, **k: pytest.fail("给了 --deps-root 就不该弹对话框"))

    assert mod._confirm_deps_root(str(picked), False) == picked
    assert recorded == [str(picked)]


def test_交互式取消对话框时用默认值继续(tmp_path, monkeypatch, capsys):
    """取消 ≠ 失败：接着用默认位置往下装"""
    mod = _installer()
    fallback = tmp_path / "依赖"
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(mod.依赖位置, "get_deps_root", lambda: fallback)
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: fallback)
    monkeypatch.setattr(mod.依赖位置, "choose_directory", lambda *a, **k: None)
    monkeypatch.setattr(mod.依赖位置, "describe_deps_root", lambda: "X（默认：项目内 依赖/）")
    monkeypatch.setattr(mod.依赖位置, "is_on_system_drive", lambda *a, **k: False)

    assert mod._confirm_deps_root(None, False) == fallback
    out = capsys.readouterr().out
    assert "取消" in out or "未选" in out
    assert "安装位置" in out


def test_落在系统盘只警告不中止(tmp_path, monkeypatch, capsys):
    """C 盘只警告（提示换盘符），绝不中止安装"""
    mod = _installer()
    root = tmp_path / "依赖"
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: root)
    monkeypatch.setattr(mod.依赖位置, "describe_deps_root",
                        lambda: "C:" + chr(92) + "Program Files" + chr(92) + "糖糖依赖")
    monkeypatch.setattr(mod.依赖位置, "is_on_system_drive", lambda *a, **k: True)

    assert mod._confirm_deps_root(None, True) == root
    out = capsys.readouterr().out
    assert "警告" in out
    assert "C:" in out


def test_pip_安装命令把缓存目录放进依赖根(tmp_path, monkeypatch):
    """pip 的 wheel/http 缓存跟着依赖根走；包本身**不能**用 --target（会断 import）"""
    mod = _installer()
    fake_root = tmp_path / "别的盘" / "糖糖依赖"
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: fake_root)
    monkeypatch.setattr(mod, "BASE", tmp_path / "项目")
    captured = {}

    class _Done:
        returncode = 0

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    ok = mod._install(["httpx>=0.27.0"], {"index-url": "https://pypi.tuna.tsinghua.edu.cn/simple"})
    assert ok is True
    cmd = captured["cmd"]
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
    assert "httpx>=0.27.0" in cmd
    assert cmd[cmd.index("--cache-dir") + 1] == str(fake_root / "cache" / "pip")
    assert cmd[cmd.index("--index-url") + 1] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert "--target" not in cmd, "pip --target 会让 import 全部失效（依赖迁移.py 已论证）"


def test_非交互_yes_不读_stdin(tmp_path, monkeypatch):
    """--yes：不弹框、也不读 stdin（自动化不会卡在对话框上）"""
    mod = _installer()
    root = tmp_path / "依赖"
    monkeypatch.setattr(mod.依赖位置, "ensure_deps_root", lambda: root)
    monkeypatch.setattr(mod.依赖位置, "describe_deps_root", lambda: "X")
    monkeypatch.setattr(mod.依赖位置, "is_on_system_drive", lambda *a, **k: False)
    monkeypatch.setattr(mod.依赖位置, "choose_directory",
                        lambda *a, **k: pytest.fail("--yes 时不该弹对话框"))
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("--yes 时不该读输入"))

    assert mod._confirm_deps_root(None, True) == root
