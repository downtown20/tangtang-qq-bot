# -*- coding: utf-8 -*-
"""项目根临时文件的清扫（2026-10-27 立）。

## 为什么需要

`pilk`（SILK→WAV 解码，`agent/asr.py` 用它解 QQ 语音）的 `silk_to_wav` 里写着：

    pcm_path = tempfile.mktemp(suffix='.pcm')
    decode(silk, pcm_path, pcm_rate=rate)
    with open(pcm_path, 'rb') as pcm:   # ← 读完就完了，从不 unlink

于是每解一条语音就留一个 pcm。2026-10-27 实测根目录躺着 10 个
`tmpXXXXXXXX.pcm`，合计约 55MB，最早 10-26 23:03、最新 10-27 07:34。

**为什么一直没人发现**：`*.pcm` 早在 .gitignore 里（第 121 行），所以
`git status` 永远干净，`git clean` 也清不掉它——只有
`Get-ChildItem *.pcm` 这种直接看目录的动作才暴露。

## 闸门钉什么

1. 超过阈值的老 pcm 要删掉
2. **正在被写的不能删**（mtime 闸，pilk 是秒级产物，2 小时足够安全）
3. 只碰 `tmp*.pcm`：正常音频 `song.pcm`、别的临时文件 `tmp123.tmp` 都不许动
4. 一个目录里同时有老有新时，只删老的

阳性对照见 `变异/根目录清扫.json`：去掉 mtime 闸 → 第 2 条必红；把 glob
放宽成 `*` → 第 3 条必红。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from agent.paths import sweep_stale_runtime_files


def _make(path: Path, age_seconds: float) -> Path:
    """造一个「age_seconds 秒之前被写过」的文件。"""
    path.write_bytes(b"\x00" * 16)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_old_pcm_is_removed(tmp_path):
    p = _make(tmp_path / "tmpabc123.pcm", 3 * 3600)
    removed = sweep_stale_runtime_files(root=tmp_path)
    assert p in removed, "两小时前的 pcm 应该被清掉"
    assert not p.exists()


def test_fresh_pcm_is_kept(tmp_path):
    """正在被写的文件绝不能被删——清扫只删够老的。"""
    p = _make(tmp_path / "tmpfresh.pcm", 30)
    assert sweep_stale_runtime_files(root=tmp_path) == []
    assert p.exists(), "刚生成的 pcm 可能正被 pilk 读写，删了会解码失败"


def test_non_matching_files_are_untouched(tmp_path):
    """只碰 `tmp*.pcm`：正常音频和别的临时文件都不许动。"""
    keeper = _make(tmp_path / "song.pcm", 5 * 3600)
    other = _make(tmp_path / "tmp123.tmp", 5 * 3600)
    assert sweep_stale_runtime_files(root=tmp_path) == []
    assert keeper.exists() and other.exists()


def test_mixed_dir_only_removes_stale(tmp_path):
    old = _make(tmp_path / "tmpold.pcm", 3 * 3600)
    fresh = _make(tmp_path / "tmpnew.pcm", 60)
    removed = sweep_stale_runtime_files(root=tmp_path)
    assert [p.name for p in removed] == ["tmpold.pcm"]
    assert fresh.exists()


def test_missing_root_is_silent(tmp_path):
    """目录不存在时不能抛异常（启动收尾工作不该让 bot 起不来）。"""
    assert sweep_stale_runtime_files(root=tmp_path / "nope") == []
