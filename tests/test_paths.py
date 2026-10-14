"""
agent/paths.py 路径发现（2026-08-15）：
任何电脑复制源码 + 装好依赖即可运行——find_python310 多候选自动发现，
不再依赖 Administrator 用户名或固定盘符。
"""

import sys
from pathlib import Path

from agent import paths


class TestProjectRoot:
    def test_project_root_is_parent_of_agent(self):
        assert paths.PROJECT_ROOT == Path(paths.__file__).resolve().parent.parent


class TestFindPython310:
    def _mk(self, base: Path, rel: str) -> Path:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return p

    def test_env_var_wins(self, tmp_path, monkeypatch):
        explicit = self._mk(tmp_path, "custom/python.exe")
        monkeypatch.setenv("PYTHON310", str(explicit))
        local = tmp_path / "local"
        self._mk(local, "Programs/Python/Python310/python.exe")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        assert paths.find_python310() == explicit

    def test_localappdata_candidate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHON310", raising=False)
        local = tmp_path / "local"
        exe = self._mk(local, "Programs/Python/Python310/python.exe")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "pf"))
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        assert paths.find_python310() == exe

    def test_project_vendored_python(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHON310", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        exe = self._mk(tmp_path, "python310/python.exe")
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        assert paths.find_python310() == exe

    def test_path_which_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHON310", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        exe = self._mk(tmp_path, "somewhere/python3.10.exe")
        monkeypatch.setattr(paths.shutil, "which", lambda _name: str(exe))
        assert paths.find_python310() == exe

    def test_current_interpreter_if_310(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHON310", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(paths.shutil, "which", lambda _name: None)
        monkeypatch.setattr(sys, "version_info", (3, 10, 11))
        monkeypatch.setattr(sys, "executable", str(tmp_path / "current.exe"))
        assert paths.find_python310() == Path(sys.executable)

    def test_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYTHON310", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(paths.shutil, "which", lambda _name: None)
        monkeypatch.setattr(sys, "version_info", (3, 11, 9))
        assert paths.find_python310() is None
