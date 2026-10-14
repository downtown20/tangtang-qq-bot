# -*- coding: utf-8 -*-
'''T3.1 regression: observer on a scheduled task + absence alert.

2026-10-14 ledger T3.1 (Stage 3 / M5). Audit requirement, verbatim:
  wire tools/runtime_observer.py --once into the existing scheduler;
  stopping the observer for >=2 cycles must produce a real alert;
  and >=6 of the 23 'observer/health-query' methods must gain production call sites
  so CLASS_METHOD_BASELINE shrinks (the ratchet tightens).

Design decision pinned here: the observer is NOT run inside the event loop. It carries a
blocking time.sleep sampling loop (--interval-seconds default 300, --duration-hours default 2),
so running it in-process would hold the loop. Instead the observer records a heartbeat when it
finishes, and an in-process health check reads that heartbeat and raises the alert when the
observer has been absent >=2 cycles.

The observer had ZERO production callers before this (CLAUDE.md lesson #14: who writes it /
who reads it / end to end). A tool whose job is noticing that things stopped, and which itself
stops silently, is the exact failure it exists to catch.
'''
import ast
import asyncio
import io
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agent import health_check as hc

OBS_PATH = ROOT / 'tools' / 'runtime_observer.py'


class _FakeStore:
    def __init__(self, value):
        self._value = value

    def kv_get(self, key):
        return self._value


class _FakeHandler:
    def __init__(self, value):
        self.memory = type('_M', (), {'store': _FakeStore(value)})()


def _observer_const(name):
    '''Read a module-level constant from the observer without importing it (importing it
    runs dataclass decoration that needs a registered module; a plain AST read is enough
    for a string constant and keeps this test side-effect free).'''
    tree = ast.parse(OBS_PATH.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if getattr(tgt, 'id', '') == name:
                    return node.value.value
    raise AssertionError('constant %s not found in observer' % name)


class TestHeartbeatKeyContract:
    def test_key_matches_on_both_sides(self):
        '''The observer writes the key; health_check reads it. Drift = silent blind spot.'''
        assert _observer_const('OBSERVER_HEARTBEAT_KEY') == hc.OBSERVER_HEARTBEAT_KEY

    def test_absence_threshold_is_two_cycles(self):
        assert hc.OBSERVER_ABSENCE_CYCLES == 2

    def test_cycle_length_is_sane(self):
        '''One cycle must be much longer than the observer's own 5-minute snapshot interval,
        otherwise a normal run would look absent.'''
        assert hc.OBSERVER_MAX_AGE_SECONDS >= 1800.0


class TestAbsenceDetection:
    def _check(self, value):
        return asyncio.run(hc._check_observer_absence(_FakeHandler(value)))

    def test_recent_run_is_ok(self):
        r = self._check(str(time.time()))
        assert r['status'] == 'ok'

    def test_one_cycle_late_is_still_ok(self):
        r = self._check(str(time.time() - hc.OBSERVER_MAX_AGE_SECONDS))
        assert r['status'] == 'ok'

    def test_two_cycles_late_is_an_alert(self):
        '''THE acceptance criterion: absent >=2 cycles must produce a real alert.'''
        r = self._check(str(time.time() - hc.OBSERVER_ABSENCE_CYCLES * hc.OBSERVER_MAX_AGE_SECONDS - 60))
        assert r['status'] == 'error', r
        assert '缺席' in r['message']

    def test_never_run_in_warmup_is_not_an_alert(self):
        '''Cold start: not yet run is expected, not a fault.'''
        r = self._check(None)
        assert r['status'] == 'ok'

    def test_unparseable_timestamp_warns(self):
        r = self._check('not-a-number')
        assert r['status'] == 'warn'

    def test_check_never_raises_on_store_failure(self):
        class _Boom:
            def __init__(self):
                self.memory = type('_M', (), {'store': self})()

            def kv_get(self, key):
                raise RuntimeError('db down')

        r = asyncio.run(hc._check_observer_absence(_Boom()))
        assert r['status'] == 'warn'


class TestPipelineBacklogConsumesQueries:
    '''The second acceptance criterion: >=6 health-query methods gain production callers.'''

    WIRED = [
        'get_decision_run_health',
        'get_inbound_event_health',
        'get_proactive_event_health',
        'get_action_receipt_mailbox_health',
        'count_quarantined_chat_logs',
        'count_memories_today',
        'get_persisted_unprocessed_backlog',
    ]

    def test_source_calls_at_least_six(self):
        src = io.open(ROOT / 'agent' / 'health_check.py', encoding='utf-8').read()
        i = src.find('async def _check_pipeline_backlog')
        assert i > 0
        body = src[i:i + 3000]
        called = [n for n in self.WIRED if ('store.' + n + '(') in body]
        assert len(called) >= 6, 'only wired %d: %s' % (len(called), called)

    def test_every_wired_name_exists_on_store(self):
        '''A production call site to a method that does not exist is a NameError waiting.'''
        tree = ast.parse((ROOT / 'agent' / 'store.py').read_text(encoding='utf-8'))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
        missing = [n for n in self.WIRED if n not in names]
        assert not missing, 'not on Store: %s' % missing

    def test_check_runs_against_real_store(self):
        '''End to end: the check must actually execute and return a shaped result.'''
        from agent.store import Store

        class _H:
            def __init__(self):
                self.memory = type('_M', (), {'store': Store(str(ROOT / 'memory.db'))})()

        r = asyncio.run(hc._check_pipeline_backlog(_H()))
        assert r['status'] in ('ok', 'warn'), r
        assert 'message' in r and r['message']


class TestObserverRecordsHeartbeat:
    def test_observer_defines_record_heartbeat(self):
        tree = ast.parse(OBS_PATH.read_text(encoding='utf-8'))
        defs = {n.name for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert 'record_heartbeat' in defs

    def test_main_calls_record_heartbeat(self):
        src = OBS_PATH.read_text(encoding='utf-8')
        i = src.find('def main()')
        assert i > 0
        assert 'record_heartbeat(args.db)' in src[i:], (
            'the observer must record a heartbeat from main(), else absence is undetectable')

    def test_heartbeat_is_written_only_after_report(self):
        '''A heartbeat written before the report would mask a broken observer.

        Careful: `main()` contains TWO `_write_outputs` calls -- an in-loop
        checkpoint (only when >=2 snapshots exist) and the final one after the
        sampling loop. The invariant is about the FINAL write: the heartbeat must
        not be reachable before the run has produced a report. So we compare
        against the LAST `_write_outputs`, not the first.
        (This test was originally written against the FIRST one and a mutation
        that moved the heartbeat ahead of the final write stayed green -- the
        probe caught the assertion pointing at the wrong call site.)
        '''
        src = OBS_PATH.read_text(encoding='utf-8')
        i = src.find('def main()')
        body = src[i:]
        i_beat = body.find('record_heartbeat(args.db)')
        i_final_report = body.rfind('_write_outputs(')
        assert i_beat > 0, 'observer must record a heartbeat'
        assert 0 < i_final_report < i_beat, (
            'heartbeat must come after the FINAL report write')

    def test_heartbeat_uses_kv_store_table(self):
        '''Must match the real schema (kv_store with key/value/updated), not a guessed one.'''
        src = OBS_PATH.read_text(encoding='utf-8')
        assert 'kv_store' in src
        assert 'INSERT INTO kv(' not in src, 'guessed table name would fail at runtime'

    def test_heartbeat_never_raises(self):
        '''Heartbeat failure must not crash the observation run.'''
        src = OBS_PATH.read_text(encoding='utf-8')
        i = src.find('def record_heartbeat')
        j = src.find('def _write_outputs', i)
        body = src[i:j]
        assert 'return False' in body
        assert 'raise' not in body.replace('# raise', '')


class TestChecksAreRegistered:
    def test_both_checks_registered_in_handler(self):
        src = io.open(ROOT / 'agent' / 'handler.py', encoding='utf-8', errors='ignore').read()
        assert 'observer_absence' in src
        assert 'pipeline_backlog' in src
        assert 'from .health_check import' in src

    def test_registered_names_match_functions(self):
        src = io.open(ROOT / 'agent' / 'handler.py', encoding='utf-8', errors='ignore').read()
        assert '_check_observer_absence' in src
        assert '_check_pipeline_backlog' in src
