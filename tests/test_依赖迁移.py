"""依赖迁移工具的纯函数与安全闸门。

真正的迁移是**破坏性操作**，测试只覆盖不会碰真实目录的那部分：
目录联接判定、体积统计、备份命名、以及「dry-run 绝不动文件」。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
MIGRATE = TOOLS / "依赖迁移.py"


def _load():
    spec = importlib.util.spec_from_file_location("_migrate_under_test", MIGRATE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def _make_junction(link: Path, target: Path) -> bool:
    rc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    ).returncode
    return rc == 0 and link.exists()


# ── 目录联接判定 ─────────────────────────────────────────────────────
def test_is_junction_false_for_plain_dir(tmp_path, mod):
    d = tmp_path / "plain"
    d.mkdir()
    assert mod._is_junction(d) is False


def test_is_junction_false_for_missing_path(tmp_path, mod):
    assert mod._is_junction(tmp_path / "nope") is False


def test_is_junction_true_for_real_junction(tmp_path, mod):
    target = tmp_path / "target"
    target.mkdir()
    (target / "a.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    if not _make_junction(link, target):
        pytest.skip("本机建不了目录联接")
    assert mod._is_junction(link) is True
    # 联接必须真的读得到内容，且 rmdir 只删链接
    assert (link / "a.txt").read_text(encoding="utf-8") == "x"
    assert Path(os.path.realpath(link)) == target
    subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    assert target.exists(), "删联接不能连带删掉目标目录"


# ── 体积统计 ─────────────────────────────────────────────────────────
def test_stat_tree_counts_files_and_bytes(tmp_path, mod):
    root = tmp_path / "t"
    (root / "sub").mkdir(parents=True)
    (root / "a").write_bytes(b"12345")
    (root / "sub" / "b").write_bytes(b"123")
    files, size = mod._stat_tree(root)
    assert files == 2
    assert size == 8


def test_stat_tree_does_not_follow_child_junction(tmp_path, mod):
    """子目录里如果有联接，统计不能跟进去（否则会重复计数 / 无限递归）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * 1000)
    root = tmp_path / "root"
    root.mkdir()
    (root / "own").write_bytes(b"y" * 10)
    if not _make_junction(root / "j", outside):
        pytest.skip("本机建不了目录联接")
    files, size = mod._stat_tree(root)
    assert files == 1 and size == 10


# ── 备份命名 / 查找 ──────────────────────────────────────────────────
def test_backup_name_and_find(tmp_path, mod):
    src = tmp_path / "site-packages"
    src.mkdir()
    bak = mod._backup_name(src, "20261014-190335")
    assert bak.name == "site-packages.C盘备份-20261014-190335"
    assert mod._find_backup(src) is None
    bak.mkdir()
    assert mod._find_backup(src) == bak


def test_find_backup_picks_latest(tmp_path, mod):
    src = tmp_path / "x"
    src.mkdir()
    (tmp_path / "x.C盘备份-20260101-000000").mkdir()
    latest = tmp_path / "x.C盘备份-20261014-190335"
    latest.mkdir()
    assert mod._find_backup(src) == latest


# ── 迁移对象表 ───────────────────────────────────────────────────────
def test_items_cover_the_three_known_targets(mod):
    keys = {it.key for it in mod._items()}
    assert keys == {"site-packages", "user-site", "torch-home"}


def test_item_targets_live_under_deps_root(mod):
    deps = mod.get_deps_root()
    for it in mod._items():
        assert str(it.target).startswith(str(deps)), f"{it.key} 的目标不在依赖根下"


def test_apply_aborts_when_required_source_missing(monkeypatch, mod):
    """必迁项缺失时必须中止，不许「跳过继续」。"""

    class _Missing:
        key, label, target_rel = "site-packages", "假", "site-packages"
        must_exist = True

        @property
        def source(self):
            return Path("Z:/definitely/not/here")

        @property
        def target(self):
            return Path("Z:/also/not/here")

    monkeypatch.setattr(mod, "_items", lambda: [_Missing()])
    assert mod.cmd_apply() == 2


def test_dry_run_creates_nothing(tmp_path, monkeypatch, mod):
    """dry-run 只读：不许建目录、不许写配置。"""
    deps = tmp_path / "deps"
    monkeypatch.setattr(mod, "get_deps_root", lambda: deps)
    monkeypatch.setattr(mod, "ensure_deps_root", lambda: deps)
    assert mod.cmd_dry_run() == 0
    assert not deps.exists(), "dry-run 不得创建任何东西"
