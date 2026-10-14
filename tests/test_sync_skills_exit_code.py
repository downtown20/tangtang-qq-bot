# -*- coding: utf-8 -*-
'''T11 regression: tools/同步skills.py must surface robocopy failure as a non-zero exit code.

2026-10-14 ledger T11. Before the fix, _robocopy() correctly returned False on failure
(robocopy exit code >= 8), and main() even printed '[OK]'/'[失败]' from it - but main()
implicitly returned None and the script always exited 0. Automation/CI could not see the
failure at all; only a human reading stdout would notice.

This is the same family as tools/准备发布.py's lesson: a red line must not depend on a human
reading console output.
'''
import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TOOL = ROOT / 'tools' / '同步skills.py'


def _load():
    spec = importlib.util.spec_from_file_location('syncskills_t11', TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestExitCodeContract:
    '''The tool must turn failure into a non-zero exit code.'''

    def test_main_returns_bool(self):
        m = _load()
        import inspect
        sig = inspect.signature(m.main)
        assert sig.return_annotation is bool, (
            'main() must declare -> bool so its result can become an exit code')

    def test_main_source_returns_bool_literals(self):
        '''Structural gate: main() must actually return True/False.'''
        import inspect
        src = inspect.getsource(_load().main)
        assert 'return True' in src and 'return False' in src, (
            'main() must return True/False, not fall through to None')

    def test_script_calls_sys_exit_with_result(self):
        '''Structural gate: the __main__ block must propagate the result.'''
        text = TOOL.read_text(encoding='utf-8')
        assert 'sys.exit(_rc)' in text, (
            'the script must exit with the result code - otherwise failures stay invisible')
        assert 'if not main():' in text

    def test_ast_parses(self):
        ast.parse(TOOL.read_text(encoding='utf-8'))


class TestRobocopyVerdict:
    '''_robocopy's own verdict must stay correct (that part was never broken).'''

    def test_robocopy_returns_false_on_bad_destination(self):
        m = _load()
        src = ROOT / 'tests'
        dst = Path('Z:/nonexistent_t11_probe')
        assert m._robocopy(src, dst) is False, (
            'robocopy to an unreachable destination must be reported as failure')

    def test_robocopy_returns_true_on_real_copy(self, tmp_path):
        m = _load()
        src = tmp_path / 'src'
        dst = tmp_path / 'dst'
        src.mkdir()
        (src / 'a.txt').write_text('hello', encoding='utf-8')
        assert m._robocopy(src, dst) is True, 'a real copy must be reported as success'
        assert (dst / 'a.txt').is_file(), 'the file must actually land'


class TestEndToEndExitCode:
    '''Positive control: run the real script with a bad mirror and read the exit code.'''

    def test_normal_run_exits_zero(self):
        r = subprocess.run(
            [sys.executable, '-X', 'utf8', str(TOOL)],
            capture_output=True, text=True, encoding='utf-8', errors='ignore',
            cwd=str(ROOT), timeout=180,
        )
        assert r.returncode == 0, (
            'a healthy sync must exit 0; got %d\n%s' % (r.returncode, (r.stdout or '')[-500:]))

    def test_bad_destination_exits_nonzero(self):
        '''Mutation-style end-to-end: patch MIRRORS so one target is unreachable.'''
        orig = TOOL.read_bytes()
        text = orig.decode('utf-8')
        # 从文件动态取锚点（不凭记忆拼引号形式）
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines)
                   if ln.startswith('    (Path.home()') and '.claude' in ln)
        old = lines[idx]
        new = old.split(',')[0] + ", Path('Z:/nonexistent_t11_e2e')),"
        try:
            TOOL.write_bytes(text.replace(old, new, 1).encode('utf-8'))
            r = subprocess.run(
                [sys.executable, '-X', 'utf8', str(TOOL)],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                cwd=str(ROOT), timeout=180,
            )
            assert r.returncode != 0, (
                'a failed mirror MUST NOT exit 0 - that was the T11 defect')
            assert '镜像失败' in (r.stdout or ''), 'failure must be reported in stdout too'
        finally:
            TOOL.write_bytes(orig)
        assert TOOL.read_bytes() == orig, 'tool must be restored byte-exact'
