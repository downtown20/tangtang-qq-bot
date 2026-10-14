# -*- coding: utf-8 -*-
"""T30 regression: the pipeline-backlog check must not contradict stale_extraction.

2026-10-14. Two checks read the same extraction backlog:

  _check_stale_extraction  (pre-existing) reads get_extraction_backlog_snapshot,
      which carries the eligible/deferred split. It prints '到期0，等待584，state=normal'
      and returns status OK when eligible_messages == 0.
  _check_pipeline_backlog  (added in T3.1) read get_persisted_unprocessed_backlog --
      a RAW count with no split -- and returned warn whenever backlog_total > 0.

So the same 579 produced 'ok' from one line and 'warn' from the other in the same
startup log. Same shape as T10 (two release scanners) and T25 (two character classes):
one rule, two implementations, drifting apart with no test comparing them.

Fix: separate READINGS from JUDGEMENTS. A raw level (backlog, quarantine pile) is a
reading; only things this check can call wrong independent of any baseline go into warn.
"""
import ast
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.health_check import _check_pipeline_backlog


class _Store:
    def __init__(self, *, backlog=None, quarantined=0, unknown=0, corrupt=0,
                 receipt_open=0, memories_today=0):
        self._backlog = backlog or []
        self._quarantined = quarantined
        self._unknown = unknown
        self._corrupt = corrupt
        self._receipt_open = receipt_open
        self._memories_today = memories_today

    def get_decision_run_health(self):
        return {'unknown': self._unknown, 'corrupt': self._corrupt}

    def get_inbound_event_health(self):
        return {}

    def get_proactive_event_health(self):
        return {}

    def get_action_receipt_mailbox_health(self):
        return {'open': self._receipt_open}

    def count_quarantined_chat_logs(self):
        return self._quarantined

    def count_memories_today(self):
        return self._memories_today

    def get_persisted_unprocessed_backlog(self):
        return self._backlog


class _Mem:
    def __init__(self, store):
        self.store = store


class _Handler:
    def __init__(self, store):
        self.memory = _Mem(store)


def _run(store):
    return asyncio.run(_check_pipeline_backlog(_Handler(store)))


# The real reading on 2026-10-14 was 584 messages across 173 users (avg 3.4);
# 579 was the number printed minutes earlier. Build a fixture that sums to 579
# EXACTLY -- the first version was 173x3 = 519 and the two assertions below
# caught the misnamed constant, not a code defect.
BACKLOG_579 = [
    ('q%d' % i, 4 if i < 60 else 3, '2026-09-25 00:08:10')
    for i in range(173)
]


class TestReadingsDoNotAlarm:
    def test_backlog_alone_is_not_a_warning(self):
        '''The exact T30 regression: 579 pending must read as ok, not warn.

        stale_extraction owns the 'is it actionable' judgement (it has the
        eligible/deferred split); this check cannot make it from a raw count.
        '''
        r = _run(_Store(backlog=BACKLOG_579))
        assert r['status'] == 'ok', r

    def test_quarantine_pile_alone_is_not_a_warning(self):
        '''14065 rows all came from ONE batch on 2026-08-28; 0 new in 7 days.

        A frozen pile would warn on every single startup forever.
        '''
        r = _run(_Store(quarantined=14065))
        assert r['status'] == 'ok', r

    def test_both_readings_together_still_ok(self):
        r = _run(_Store(backlog=BACKLOG_579, quarantined=14065))
        assert r['status'] == 'ok', r

    def test_readings_stay_visible(self):
        '''Separating them from the verdict must not HIDE them.'''
        r = _run(_Store(backlog=BACKLOG_579, quarantined=14065))
        assert '读数' in r['message']
        assert '579' in r['message']
        assert '14065' in r['message']

    def test_backlog_message_names_its_owner_check(self):
        '''A reader must be able to find who judges 'due' when it is not us.'''
        r = _run(_Store(backlog=BACKLOG_579))
        assert 'stale_extraction' in r['message']


class TestJudgementsStillAlarm:
    def test_unknown_decision_runs_warn(self):
        '''Stuck rows in the state machine: wrong regardless of any baseline.'''
        r = _run(_Store(unknown=3))
        assert r['status'] == 'warn', r
        assert '未知决策回合 3' in r['message']

    def test_corrupt_decision_runs_warn(self):
        r = _run(_Store(corrupt=2))
        assert r['status'] == 'warn', r

    def test_open_receipt_mailbox_warns(self):
        r = _run(_Store(receipt_open=2))
        assert r['status'] == 'warn', r
        assert '回执信箱未结 2' in r['message']

    def test_warning_keeps_readings_too(self):
        '''When it does warn, the readings must still ride along for context.'''
        r = _run(_Store(unknown=1, backlog=BACKLOG_579))
        assert r['status'] == 'warn'
        assert '579' in r['message']

    def test_all_clean_is_ok(self):
        r = _run(_Store())
        assert r['status'] == 'ok', r


class TestNoRegressionInSource:
    '''Structural guard: the raw backlog must never feed the warn trigger again.'''

    def _fn_source(self):
        src = (Path(__file__).parent.parent / 'agent' / 'health_check.py').read_text(
            encoding='utf-8')
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == '_check_pipeline_backlog'):
                return src, node, ast
        raise AssertionError('_check_pipeline_backlog not found')

    def test_backlog_total_is_not_appended_to_parts(self):
        '''`parts` is the warn trigger; `context` is the reading list.

        AST-based on purpose: a text search here would match THIS test's own
        docstring and comment (the T2.2 self-reference trap, hit twice already).
        '''
        src, fn, _ = self._fn_source()
        offenders = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr != 'append':
                    continue
                base = node.func.value
                if not (isinstance(base, ast.Name) and base.id == 'parts'):
                    continue
                for a in node.args:
                    for sub in ast.walk(a):
                        if isinstance(sub, ast.Name) and sub.id in (
                                'backlog_total', 'quarantined'):
                            offenders.append(
                                (node.lineno, getattr(sub, 'id', '?')))
        assert not offenders, (
            'raw level fed the warn trigger again at %r -- that is the T30 bug'
            % (offenders,))

    def test_both_lists_exist(self):
        src, fn, _ = self._fn_source()
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert 'parts' in names and 'context' in names

    def test_source_parses(self):
        self._fn_source()
