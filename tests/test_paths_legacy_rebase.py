# -*- coding: utf-8 -*-
"""旧根重定位契约（2026-10-30，为项目搬家而加）。

背景：项目从 D: 搬到别的盘之后，**历史数据里仍存着旧盘符的绝对路径**
（chat_log.message 里的 `file:///D:/.../stickers/x.jpg`、状态 json 里的旧 CQ 码）。
这些历史不该被改写，但用户点开历史图片时必须还能看到。

本文件钉住三件事：
1. `paths.rebase_legacy_path()` 只做「旧根前缀 → 当前根前缀」的替换；
   相似前缀（D:/qq-小糖糖2）不得误伤，未命中必须原样返回。
2. 旧根来源只有两条：环境变量 TANGTANG_LEGACY_ROOTS 与 `状态/路径别名.json`。
3. `media_cache._local_path()` 接入后，历史旧根路径仍能解析；**项目外的路径
   （C:/Windows/...）替换后也出不去**——安全边界不因搬家而放宽。

阳性对照（教训 #30）：本文件里有真实旧根样本必须被重定位的用例，也有必须被
拒绝的用例；两条都断死，任何一个方向失效都会红。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import media_cache  # noqa: E402
from agent import paths  # noqa: E402


@pytest.fixture
def new_home(tmp_path, monkeypatch):
    """一套「已搬家」的现场：当前根在 tmp 下，旧根登记在 状态/路径别名.json。"""
    root = tmp_path / "新盘" / "qq-小糖糖"
    state = root / "状态"
    state.mkdir(parents=True)
    (state / paths.LEGACY_ROOTS_FILE_NAME).write_text(
        json.dumps({"旧根": ["D:/qq-小糖糖"]}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setattr(paths, "STATE_DIR", state)
    monkeypatch.setattr(paths, "_ALIAS_CACHE", None)
    monkeypatch.setattr(media_cache, "PROJECT_ROOT", root)
    monkeypatch.delenv("TANGTANG_LEGACY_ROOTS", raising=False)
    return root


class TestRebaseLegacyPath:
    def test_legacy_prefix_is_rewritten(self, new_home):
        old = Path("D:/qq-小糖糖/状态/.conversation_state.json")
        assert paths.rebase_legacy_path(old) == new_home / "状态" / ".conversation_state.json"

    def test_backslash_form_is_rewritten(self, new_home):
        old = Path(r"D:\qq-小糖糖\stickers\a.jpg")
        assert paths.rebase_legacy_path(old) == new_home / "stickers" / "a.jpg"

    def test_case_insensitive(self, new_home):
        old = "d:/QQ-小糖糖/stickers/a.jpg"
        assert paths.rebase_legacy_path(old) == new_home / "stickers" / "a.jpg"

    def test_similar_prefix_is_not_misplaced(self, new_home):
        # D:/qq-小糖糖2 不是 D:/qq-小糖糖 的子路径——前缀必须落在路径分隔符边界
        old = Path("D:/qq-小糖糖2/state.json")
        assert paths.rebase_legacy_path(old) == old

    def test_unknown_root_is_untouched(self, new_home):
        old = Path("E:/别的地方/x.json")
        assert paths.rebase_legacy_path(old) == old

    def test_current_root_is_untouched(self, new_home):
        inside = new_home / "状态" / "a.json"
        assert paths.rebase_legacy_path(inside) == inside

    def test_explicit_root_and_aliases(self, tmp_path):
        # 纯函数形态：调用方可以显式给根与别名（测试/tools 用，不依赖全局状态）
        base = tmp_path / "新根"
        got = paths.rebase_legacy_path(
            "D:/old/项目/a.txt", root=base, aliases=[Path("D:/old/项目")]
        )
        assert got == base / "a.txt"

    def test_root_equal_to_alias(self, tmp_path):
        base = tmp_path / "新根"
        assert paths.rebase_legacy_path("D:/old/项目", root=base, aliases=[Path("D:/old/项目")]) == base


class TestLegacyRootSources:
    def test_alias_file_is_read(self, new_home):
        assert Path("D:/qq-小糖糖") in paths.legacy_roots()

    def test_missing_alias_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "状态")
        monkeypatch.setattr(paths, "_ALIAS_CACHE", None)
        monkeypatch.delenv("TANGTANG_LEGACY_ROOTS", raising=False)
        assert paths.legacy_roots() == ()

    def test_env_var_is_honored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "无此目录")
        monkeypatch.setattr(paths, "_ALIAS_CACHE", None)
        monkeypatch.setenv("TANGTANG_LEGACY_ROOTS", "E:/临时旧根;F:/另一个旧根")
        roots = paths.legacy_roots()
        assert Path("E:/临时旧根") in roots and Path("F:/另一个旧根") in roots

    def test_corrupt_alias_file_does_not_raise(self, tmp_path, monkeypatch):
        state = tmp_path / "状态"
        state.mkdir()
        (state / paths.LEGACY_ROOTS_FILE_NAME).write_text("{不是 JSON", encoding="utf-8")
        monkeypatch.setattr(paths, "STATE_DIR", state)
        monkeypatch.setattr(paths, "_ALIAS_CACHE", None)
        monkeypatch.delenv("TANGTANG_LEGACY_ROOTS", raising=False)
        assert paths.legacy_roots() == ()


class TestMediaCacheResolvesLegacyRoots:
    def _make_asset(self, root: Path, rel: str) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpg-bytes")
        return p

    def test_legacy_cq_code_still_resolves(self, new_home):
        real = self._make_asset(new_home, "stickers/a.jpg")
        got = media_cache._local_path("file:///D:/qq-小糖糖/stickers/a.jpg")
        assert got == real.resolve()

    def test_outside_project_is_still_rejected(self, new_home):
        # 正向对照：C: 盘系统文件不得因「搬家」被带入项目内
        assert media_cache._local_path("file:///C:/Windows/win.ini") is None

    def test_missing_file_after_rebase_is_rejected(self, new_home):
        # 重定位成功但目标文件不存在——仍然返回 None（不得伪造可用路径）
        assert media_cache._local_path("file:///D:/qq-小糖糖/stickers/不存在.jpg") is None
