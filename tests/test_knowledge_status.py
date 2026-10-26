# -*- coding: utf-8 -*-
"""知识库两条通道的闸门（2026-10-26 第二轮验收第 5 条）。

## 这组用例防的是什么

主人问「在『检索命中』里加新文档，需要新建索引才有用，对吧」——对，
但**只对检索通道有效**。这个模块的作用是把两条通道的数字摆清楚，
所以最危险的失效方式是**口径漂移**：

1. 「可检索文档数」如果自己 glob 一遍，就会把敏感目录算进来
   （`色色参考` 是故意排除的，算进来会显示「索引缺 15 篇」这种假警报）；
2. `needs_sync` 如果判成「磁盘 != 索引」而不看两边的定义，会永远为真。

## 阳性对照

- 把 `discover_document_files` 换成裸 `rglob` → `test_sensitive_dir_excluded` 红；
- `needs_sync` 改成 `True` → `test_no_gap_means_no_sync` 红；
- 场景触发通道改成扫所有子目录 → `test_scenario_channel_counts_digit_slots_only` 红。
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import knowledge_status as ks


@pytest.fixture()
def kroot(tmp_path, monkeypatch):
    """一个假知识库：3 篇普通文档 + 1 个敏感目录 + 1 个直投槽位。"""
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "a.md").write_text("# A\n\n内容\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\n内容\n", encoding="utf-8")
    (root / "c.txt").write_text("内容\n", encoding="utf-8")
    pack = root / ks.PACKAGE_DIRNAME if hasattr(ks, "PACKAGE_DIRNAME") else None
    # 敏感目录名从生产代码取，不在这里写字面量（真名改了这里要跟着改，但只改一处）
    from agent.knowledge import SENSITIVE_DIRECTORY_NAMES

    sensitive = next(iter(n for n in SENSITIVE_DIRECTORY_NAMES if n not in ("adult", "nsfw")))
    (root / sensitive / "00-核心").mkdir(parents=True)
    (root / sensitive / "00-核心" / "槽位.md").write_text("# 槽位\n\n内容\n", encoding="utf-8")
    (root / sensitive / "_原文").mkdir()
    (root / sensitive / "_原文" / "归档.md").write_text("# 归档\n", encoding="utf-8")

    monkeypatch.setattr(ks, "KNOWLEDGE_DIR", root)
    monkeypatch.setattr(ks, "INDEX_DB", root / ".knowledge_index.sqlite3")
    return root


def _make_index(root: Path, rows: list[tuple[str, str]]) -> None:
    con = sqlite3.connect(str(root / ".knowledge_index.sqlite3"))
    try:
        con.execute(
            "CREATE TABLE documents (document_id TEXT, relative_path TEXT, sha256 TEXT,"
            " mtime_ns INTEGER, chars INTEGER, updated_at TEXT)")
        con.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?)",
            [(f"id-{p}", p, "sha", 0, 1, "2026-10-26T00:00:00+00:00") for p, _ in rows])
        con.commit()
    finally:
        con.close()


def test_sensitive_dir_excluded(kroot):
    """敏感目录不进「可检索」分母——算进来会报假缺口。"""
    out = ks.status()
    # 磁盘上 5 个 md/txt，其中敏感目录里 2 个不参与检索 → 分母是 3
    assert out["retrieval"]["disk"] == 3, out["retrieval"]["missing"]
    # 没建索引 → 这 3 篇全算缺失；关键断言是**敏感目录那 2 篇不在里面**
    assert sorted(out["retrieval"]["missing"]) == ["a.md", "b.md", "c.txt"]
    # 敏感目录名必须来自生产代码那份常量
    assert out["protected_dirs"], "没报告受保护目录，界面上就说不清为什么少了几篇"


def test_no_gap_means_no_sync(kroot):
    _make_index(kroot, [("a.md", ""), ("b.md", ""), ("c.txt", "")])
    out = ks.status()
    assert out["retrieval"]["indexed"] == 3
    assert out["needs_sync"] is False, (out["retrieval"]["missing"], out["retrieval"]["orphan"])


def test_missing_detected(kroot):
    _make_index(kroot, [("a.md", "")])
    out = ks.status()
    assert sorted(out["retrieval"]["missing"]) == ["b.md", "c.txt"]
    assert out["needs_sync"] is True


def test_orphan_detected(kroot):
    _make_index(kroot, [("a.md", ""), ("b.md", ""), ("c.txt", ""), ("没了.md", "")])
    out = ks.status()
    assert out["retrieval"]["orphan"] == ["没了.md"]
    assert out["needs_sync"] is True


def test_scenario_channel_counts_digit_slots_only(kroot):
    """直投通道只认数字前缀目录；`_原文/` 这类归档不算。"""
    out = ks.status()
    assert out["scenario_channel"]["slots"] == ["00-核心"], out["scenario_channel"]
    assert out["scenario_channel"]["files"] == 1


def test_index_absent_does_not_crash(kroot):
    out = ks.status()
    assert out["retrieval"]["indexed"] == 0
    assert out["retrieval"]["index_readable"] is True, "索引库根本没建，不该报「读不出来」"


def test_sync_hint_names_the_real_command(kroot):
    """提示语必须给出可执行的动作，否则用户知道有问题也不知道怎么办。"""
    out = ks.status()
    assert "知识" in out["sync_hint"] or "重载" in out["sync_hint"]
