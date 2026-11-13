"""依赖根的**跨版本继承**契约（2026-11-14）。

## 为什么有这条闸门

用户升级的方式是「把新版本的包解压到一个新目录」。新目录里没有 `依赖位置.json`，
于是安装器把它当「第一次安装」，默认依赖根变成 `<新版本>/依赖`——而 2.6G 模型、
1.9G torch 与 pip 缓存全在**上一版目录**里。安装器看不见它们，用户看到的是
「又要重新下载」。

实机样本（2026-11-14）：新包在 `...\\tangtang-v1.08.07`，依赖却指回
`...\\tangtang-v1.08.05\\依赖`——用户这次是手动在目录框里选回去的。
没选的人就得重下 4.5G。

修法：给 `依赖位置` 增加**全局配置**（`%LOCALAPPDATA%/tangtang/依赖位置.json`），
优先级：环境变量 → 项目内 → 全局 → 项目内默认。安装器结束时 `sync_global()` 补一份，
下一个版本的包目录就能继承。

这条闸门钉的是：
1. 项目内没有配置、全局有 → `get_deps_root()` 必须返回全局那个（= 不重下）；
2. `describe_deps_root()` 要说明它来自全局（用户能看懂"为什么不是默认位置"）；
3. `set_deps_root()` 必须**双写**（项目内 + 全局），否则下一个版本继承不到；
4. 全局文件损坏/不可读 → 降级到默认，不抛异常（不能因为一个配置文件坏了装不上）；
5. 安装器在「项目内无配置但全局有」时**不弹系统目录框**（这是本次用户遇到的场景）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, BASE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def locus(tmp_path, monkeypatch):
    """把两条配置路径都指到 tmp，绝对不碰本机真实的依赖位置。"""
    mod = _load("deps_location_probe", "tools/依赖位置.py")
    local_cfg = tmp_path / "依赖位置.json"
    global_cfg = tmp_path / "localappdata" / "tangtang" / "依赖位置.json"
    deps = tmp_path / "deps"
    deps.mkdir()
    other = tmp_path / "old-version-deps"
    other.mkdir()

    monkeypatch.setattr(mod, "CONFIG_PATH", local_cfg)
    monkeypatch.setattr(mod, "GLOBAL_CONFIG_PATH", global_cfg)
    monkeypatch.setattr(mod, "DEFAULT_DEPS_ROOT", deps)
    monkeypatch.delenv("TANGTANG_DEPS_ROOT", raising=False)
    return mod, local_cfg, global_cfg, deps, other


def _write(path: Path, root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"依赖根": str(root)}, ensure_ascii=False), encoding="utf-8")


def test_global_config_is_inherited_when_package_dir_is_new(locus):
    """升级到新解压目录（无项目内配置）→ 必须继承全局那份，否则整包重下。"""
    mod, local_cfg, global_cfg, deps, old = locus
    assert not local_cfg.exists()
    _write(global_cfg, old)

    assert mod.get_deps_root() == old, (
        "新包目录里没有配置时没有回退到全局配置——用户升级后又得把 4.5G 重下一次")


def test_describe_says_it_came_from_global(locus):
    """用户要能看出「位置不是默认的、是继承来的」。"""
    mod, local_cfg, global_cfg, deps, old = locus
    _write(global_cfg, old)
    text = mod.describe_deps_root()
    assert str(old) in text
    assert "全局" in text, f"没说来源：{text}"


def test_set_deps_root_writes_both_copies(locus):
    """落盘必须双写——只写项目内的话，下一个版本的包目录继承不到。"""
    mod, local_cfg, global_cfg, deps, old = locus
    mod.set_deps_root(str(old))

    assert local_cfg.exists() and global_cfg.exists(), "全局那份没写"
    assert json.loads(local_cfg.read_text(encoding="utf-8"))["依赖根"] == str(old.resolve())
    assert json.loads(global_cfg.read_text(encoding="utf-8"))["依赖根"] == str(old.resolve())


def test_local_config_wins_over_global(locus):
    """优先级：项目内 > 全局（同一个包目录内换过位置，不能被全局拽回去）。"""
    mod, local_cfg, global_cfg, deps, old = locus
    _write(global_cfg, old)
    _write(local_cfg, deps)
    assert mod.get_deps_root() == deps


def test_broken_global_config_degrades_to_default(locus):
    """全局配置损坏 → 降级到默认位置，绝不抛异常（否则直接装不上）。"""
    mod, local_cfg, global_cfg, deps, old = locus
    global_cfg.parent.mkdir(parents=True, exist_ok=True)
    global_cfg.write_text("{ 这不是 json", encoding="utf-8")
    assert mod.get_deps_root() == deps


def test_sync_global_writes_current_root(locus):
    """老用户只有项目内配置 → 安装器收尾 sync_global() 要把全局补齐。"""
    mod, local_cfg, global_cfg, deps, old = locus
    _write(local_cfg, old)
    assert not global_cfg.exists()
    mod.sync_global()
    assert global_cfg.exists()
    assert json.loads(global_cfg.read_text(encoding="utf-8"))["依赖根"] == str(old)


def test_installer_does_not_pop_dialog_when_global_exists(tmp_path, monkeypatch, locus):
    """用户场景复现：新包目录 + 全局有配置 → 安装器不许弹框。"""
    mod, local_cfg, global_cfg, deps, old = locus
    _write(global_cfg, old)

    inst = _load("installer_global_probe", "tools/安装糖糖.py")
    # 把安装器看到的 依赖位置 换代成上面那份（同一个模块对象即可）
    monkeypatch.setattr(inst, "依赖位置", mod)
    monkeypatch.setattr(inst.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("不该问用户"))

    calls: list[str] = []
    monkeypatch.setattr(mod, "choose_directory",
                        lambda *a, **k: calls.append("dialog") or str(deps))

    got = inst._confirm_deps_root(None, assume_yes=False)

    assert calls == [], "全局已配置时仍然弹了系统目录框"
    assert got == old
    assert mod.has_local_config(), "继承后应当把值落到本项目，便于用户查看"
