"""`tools/上传release附件.py` 的契约测试。

这个工具是从 2026-10-28 一次发版事故里长出来的：`gh release upload` 传 748 MB
时 18~50 分钟没有任何进展、`curl --data-binary @file` 把整个文件读进内存后
IO 写恒为 0、Windows schannel 还因为代理阻断 OCSP 报 `CRYPT_E_NO_REVOCATION_CHECK`。
最后用 `http.client` 分块 `send()` 才跑通（747 MB / 2.6 分钟）。

所以这里钉的是**三条当时踩过的前提**，不是工具的全部行为：
  1. Authorization 头不能重复 `Bearer`（那次 747 MB 传完收到 401 的直接原因）
  2. 分块函数不许一次性返回整个文件（内存恒定的唯一保证）
  3. 块大小不许悄悄调大（4 MB 是实测过的）
"""

import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

# 中文文件名没法直接 import（与 安装糖糖.py / 准备发布.py 同一套路）
_spec = importlib.util.spec_from_file_location(
    "upload_release", BASE / "tools" / "上传release附件.py")
_up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_up)


class TestAuthHeader:
    """2026-10-28 的 401：header 文件里是 `Authorization: Bearer <token>`，
    split 出 `Bearer <token>` 之后又拼了一次 `"Bearer " + TOK` →
    `Bearer Bearer gho_...`，**747 MB 传完才被拒**。"""

    def test_bare_token_gets_one_bearer(self):
        assert _up.auth_header("gho_abc123") == "Bearer gho_abc123"

    def test_prefixed_token_does_not_double_bearer(self):
        """阳性对照：输入已经带前缀时，输出必须仍然只有一个 `Bearer`。"""
        assert _up.auth_header("Bearer gho_abc123") == "Bearer gho_abc123"
        assert _up.auth_header("bearer gho_abc123") == "Bearer gho_abc123"
        assert "Bearer Bearer" not in _up.auth_header("Bearer gho_abc123")

    def test_strips_surrounding_whitespace(self):
        assert _up.auth_header("  gho_abc123\n") == "Bearer gho_abc123"

    def test_empty_raises(self):
        with pytest.raises(RuntimeError):
            _up.auth_header("   ")
        with pytest.raises(RuntimeError):
            _up.auth_header("Bearer ")


class TestChunking:
    def test_iter_chunks_never_returns_whole_file(self, tmp_path):
        """分块函数不许一次性返回整个文件——这是「内存与体积无关」的唯一保证。"""
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * (_up.CHUNK * 2 + 123))
        sizes = [len(c) for c in _up.iter_chunks(f)]
        assert sizes == [_up.CHUNK, _up.CHUNK, 123]
        assert max(sizes) <= _up.CHUNK

    def test_small_file_yields_one_chunk(self, tmp_path):
        f = tmp_path / "small.bin"
        f.write_bytes(b"y" * 1000)
        assert [len(c) for c in _up.iter_chunks(f)] == [1000]

    def test_empty_file_yields_nothing(self, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert list(_up.iter_chunks(f)) == []

    def test_chunk_size_not_silently_raised(self):
        """4 MB 是实测过的：5.5 MB/s 下 4 MB 块的分块开销可忽略。
        调大它换不来速度，只会让「内存恒定」这条承诺变松。"""
        assert _up.CHUNK <= 4 * 1024 * 1024


class TestRepoSlug:
    def test_parses_https_remote(self):
        slug = _up.repo_slug()
        assert "/" in slug and not slug.endswith(".git")
        assert slug == "downtown20/tangtang-qq-bot"


class TestDefaultsAgreeWithPacker:
    """默认的 tag / 包名从 打包发布版本.py 读——两处写死必然漂移（教训 #35）。"""

    def test_tag_and_pkg_name_come_from_packer(self, tmp_path, monkeypatch):
        import re
        packer = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
        tag = re.search(r'^TAG = "([^"]+)"', packer, re.M).group(1)
        pkg = re.search(r'^PKG_NAME = f"([^"]+)"', packer, re.M).group(1)
        assert pkg.replace("{TAG}", tag).startswith("tangtang-v")
        # 工具里不硬编码版本号：源码里不该出现具体 tag 字面量
        src = (BASE / "tools" / "上传release附件.py").read_text(encoding="utf-8")
        assert tag not in src, "上传工具里硬编码了版本号，应该从打包脚本读"
