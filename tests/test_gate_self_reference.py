# -*- coding: utf-8 -*-
'''T2.2 regression: the zero-call gate must not cite its own baseline as evidence.

Discovered 2026-10-14 while investigating the private-method coverage gap.

The defect, stated plainly:
  `CLASS_METHOD_BASELINE` is a dict of 72 `('file', 'method')` string pairs.
  `_references()` collects STRING CONSTANTS as evidence that a name is referenced.
  So if `tests/` were ever added to the scanned set, every baselined name would be
  found 'referenced' - inside the baseline itself. All 72 entries would look wired,
  and the 'baseline has gone stale' alarm could never fire.

  That alarm is the only thing keeping the baseline honest. Losing it silently is
  lesson #30's shape: a checker fooled by its own declaration.

  It is latent today only because `_parsed` excludes `tests/`. This file makes the
  protection explicit and permanent, so widening the scan later cannot re-open it.
'''
import ast
import importlib.util
from pathlib import Path

# 用文件路径加载，不用 `from tests import ...`——环境里存在一个同名的
# site-packages `tests` 包，会抢先解析（实测 ImportError）。
_SPEC = importlib.util.spec_from_file_location(
    '_tic_for_selfref', str(Path(__file__).parent / 'test_integration_contract.py'))
tic = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tic)


class TestSelfReferenceGuard:
    '''The guard exists, is applied, and actually protects the baseline.'''

    def test_guard_declares_this_file(self):
        assert hasattr(tic, '_SELF_REFERENCE_GUARD')
        assert 'tests/test_integration_contract.py' in tic._SELF_REFERENCE_GUARD, (
            'the gate must exclude its own file from string evidence')

    def test_string_evidence_from_guarded_file_is_ignored(self):
        '''A baselined name appearing as a string ONLY in the gate file is not evidence.'''
        planted = 'agent/__guard_probe__.py'
        src = {
            planted: ast.parse('class C:' + chr(10) + '    def scanned_name(self): pass' + chr(10)),
            'tests/test_integration_contract.py': ast.parse(
                'X = ("scanned_name",)' + chr(10)),
        }
        calls, attrs, strs = tic._references(src)
        assert 'scanned_name' not in strs, (
            'the gate cited its own text as evidence - the self-reference bug is back')

    def test_ast_evidence_from_guarded_file_still_counts(self):
        '''The guard must block STRINGS only - a real call in the gate file still counts.'''
        src = {
            'agent/__guard_probe2__.py': ast.parse(
                'class C:' + chr(10) + '    def scanned_two(self): pass' + chr(10)),
            'tests/test_integration_contract.py': ast.parse(
                'C().scanned_two()' + chr(10)),
        }
        _calls, attrs, _strs = tic._references(src)
        assert 'scanned_two' in attrs, (
            'guard is too broad - it blocked real attribute evidence')

    def test_baseline_entries_would_be_protected(self):
        '''Concretely: every baselined name must be absent from strs if the gate file
        were scanned. This is the exact failure the guard prevents.'''
        names = {name for _f, name in tic.CLASS_METHOD_BASELINE}
        assert names, 'baseline is empty - nothing to protect'
        leaked = sorted(n for n in names if n in tic._STRS)
        assert not leaked, (
            'these baselined names are cited as string evidence (self-reference): %s'
            % leaked)
