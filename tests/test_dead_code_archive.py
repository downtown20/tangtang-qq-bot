# -*- coding: utf-8 -*-
'''T2 regression: the seven archive deletions must stay deleted, and the private-method gap
in the zero-reference gate must stay visible.

2026-10-14 ledger T2 (S3 stage, first half of '先删后拆'). The T2.1 census found 17 candidates
of which 7 were high-confidence dead. All 7 were re-verified with THREE independent sources
before deletion (text search across 371 files, AST attribute/name access, getattr dispatch,
and quoted-string references) - all four showed zero hits.

Why re-verify: the census predated T13-T18 which edited protocols/personality/reply_pipeline,
and T10 taught that a single text-search symbol name can miss the real thing.

IMPORTANT COVERAGE GAP this file pins: tests/test_integration_contract.py's
CLASS_METHOD_BASELINE gate only scans PUBLIC methods (name must not start with '_').
All 7 deleted symbols were private, so the gate never saw them - nobody would have noticed
if they had rotted further. test_private_method_gap_is_still_open records this.
'''
import ast
import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# 已归档的 7 个符号 —— 不许复活
_ARCHIVED = {
    'agent/handler.py': [
        '_find_last_msg_id',      # 恒返回 None 的死壳，docstring 自证
        '_check_milestones',      # 里程碑日记已禁用，调用方已停用
        '_resolve_sticker_tags',  # ReplyPipeline 转发壳，真实入口 _enrich_reply_async
        '_resolve_at_mentions',   # 同上
        '_maybe_split_reply',     # 同上
        '_send_reply',            # 同上
    ],
    'agent/action_plan.py': [
        '_copy_json',             # 0 引用的纯深拷贝小助手
    ],
}


def _src(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


class TestArchivedSymbolsStayGone:
    '''Structural gate: a deleted dead symbol must not come back silently.'''

    @pytest.mark.parametrize('rel,sym', [
        (f, s) for f, syms in _ARCHIVED.items() for s in syms
    ])
    def test_symbol_has_no_definition(self, rel, sym):
        src = _src(rel)
        pat = re.compile(r'^\s*(async\s+)?def\s+' + re.escape(sym) + r'\b', re.M)
        assert not pat.search(src), (
            '%s::%s was archived in T2 - if it is needed again, re-add it with a caller, '
            'not by reviving the dead copy' % (rel, sym))

    @pytest.mark.parametrize('rel,sym', [
        (f, s) for f, syms in _ARCHIVED.items() for s in syms
    ])
    def test_symbol_has_no_reference_at_all(self, rel, sym):
        '''No stale caller may reference an archived name (would be a NameError/AttributeError).'''
        pat = re.compile(r'\b' + re.escape(sym) + r'\b')
        offenders = []
        # 2026-10-15：DiffSinger / RVC / cosyvoice 三个第三方目录已随翻唱制作链
        # 整体删除，跳过热名单里的这三项一并清掉——它们不可能再出现在 rglob 结果里，
        # 留着只会让「扫描范围」看起来比实际大（教训 #31 的白名单要穷举**该扫的**）。
        skip = ('.trash', 'venv', 'node_modules', 'python310', 'gpt-sovits')
        for p in ROOT.rglob('*.py'):
            s = str(p.relative_to(ROOT)).replace(chr(92), '/')
            if any(x in s for x in skip):
                continue
            if s == 'tests/test_dead_code_archive.py':
                continue
            try:
                t = p.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            if pat.search(t):
                offenders.append(s)
        assert not offenders, (
            'stale references to archived %s::%s: %s' % (rel, sym, offenders))

    def test_both_files_still_import(self):
        for rel in _ARCHIVED:
            p = ROOT / rel
            ast.parse(p.read_text(encoding='utf-8'))


class TestActionPlanStillWorks:
    '''Removing _copy_json must not have taken anything real with it.'''

    def test_remaining_helpers_intact(self):
        src = _src('agent/action_plan.py')
        assert 'def _text(' in src
        assert 'def derive_plan_id(' in src
        assert 'PLAN_SCHEMA_VERSION' in src

    def test_json_import_still_used(self):
        '''_copy_json was one user of json - make sure json is not now an orphan import.'''
        src = _src('agent/action_plan.py')
        if 'import json' in src:
            assert 'json.' in src, 'json import is now unused - remove it too'

    def test_module_imports_cleanly(self):
        import importlib
        mod = importlib.import_module('agent.action_plan')
        assert hasattr(mod, 'derive_plan_id')
        assert not hasattr(mod, '_copy_json'), '_copy_json is back'


class TestPrivateMethodGateGap:
    '''Pin the coverage gap: the zero-reference gate skips private methods.'''

    def test_gate_only_scans_public_methods(self):
        gate = (ROOT / 'tests' / 'test_integration_contract.py').read_text(encoding='utf-8')
        assert 'not node.name.startswith("_")' in gate, (
            'GAP CLOSED: the gate may now cover private methods - update this test')

    def test_all_seven_archived_were_private(self):
        '''That is exactly why the gate never saw them.'''
        for syms in _ARCHIVED.values():
            for s in syms:
                assert s.startswith('_'), '%s is not private - it would have been gated' % s

    def test_gate_baseline_does_not_list_them(self):
        '''Nothing to clean up, but assert it so a future edit does not add them back.'''
        gate = (ROOT / 'tests' / 'test_integration_contract.py').read_text(encoding='utf-8')
        for syms in _ARCHIVED.values():
            for s in syms:
                assert s not in gate, (
                    '%s appears in the integration gate - archived symbols must not be re-listed' % s)
