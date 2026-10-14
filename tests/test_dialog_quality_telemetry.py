# -*- coding: utf-8 -*-
'''T17 regression: dialog-quality telemetry must stay read-only, wired, and goal-free.

2026-10-14 ledger T17 (S5 dimension 6). The user's quality goal is to improve blind-test
human-misjudgement rate, satisfaction and effective-information ratio, and explicitly said
NOT to set numeric targets. So this collector only OBSERVES - no thresholds, no verdicts.

Background: reply_metrics + store.record_reply_metric() were built on 2026-08-17 and wired at
the send-commit point (handler.py:13402-13403) with the table comment calling itself the
'blind-test / A-B observation basis' - but it had NO reader until now (CLAUDE.md lesson
#12/#14: writing infra is not wiring it). T17 attaches a reader instead of building a second
collector.

Why not tangtang.log: the log only carries send receipts (no reply body), so counting
template-tells from it yields an all-zero false reading (lesson #36: a tool's 0 is a fact
about the tool, not about the code).
'''
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _snap_module():
    '''Load the snapshot tool by path (it is not an importable package).'''
    p = ROOT / 'tools' / '长期优化快照.py'
    spec = importlib.util.spec_from_file_location('snap_t17', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope='module')
def snap():
    return _snap_module()


class TestCollectorExists:
    '''The collector must exist and be wired into collect_all.'''

    def test_has_collect_dialog_quality(self, snap):
        assert hasattr(snap, 'collect_dialog_quality')
        assert callable(snap.collect_dialog_quality)

    def test_wired_into_collect_all(self, snap):
        '''Structural gate: snapshot must include the dialog_quality key.'''
        import inspect
        src = inspect.getsource(snap.collect_all)
        assert 'collect_dialog_quality()' in src, (
            'collect_dialog_quality is not called from collect_all - telemetry would be orphaned again')
        assert 'dialog_quality' in src

    def test_buckets_cover_full_range(self, snap):
        '''Last bucket must be open-ended so no reply falls through.'''
        assert snap._LEN_BUCKETS[-1][0] is None
        labels = [label for _, label in snap._LEN_BUCKETS]
        assert len(labels) == len(set(labels)), 'bucket labels must be unique'


class TestReadOnly:
    '''Must never write to memory.db.'''

    def test_opens_db_read_only(self, snap):
        '''Structural gate: the DB URI must be mode=ro.'''
        import inspect
        src = inspect.getsource(snap.collect_dialog_quality)
        assert 'mode=ro' in src, 'dialog quality collector must open memory.db read-only'

    def test_no_write_sql(self, snap):
        import inspect
        src = inspect.getsource(snap.collect_dialog_quality).upper()
        for bad in ('INSERT', 'UPDATE ', 'DELETE', 'DROP', 'ALTER'):
            assert bad not in src, 'collector must not contain %s' % bad


class TestNoGoalValues:
    '''User explicitly said: no numeric targets. The collector must not enforce any.'''

    def test_does_not_assert_thresholds(self, snap):
        '''Structural gate: no comparison against a hard-coded target inside the collector.'''
        import inspect
        src = inspect.getsource(snap.collect_dialog_quality)
        assert 'raise' not in src, 'collector must not raise on quality values'
        assert 'assert ' not in src, 'collector must not assert on quality values'

    def test_result_declares_observation_only(self, snap):
        r = snap.collect_dialog_quality(30)
        assert 'error' not in r, r
        assert 'note' in r and '只观测' in r['note']


class TestResultShape:
    '''Shape of the reading must stay stable so it can be ratcheted over time.'''

    @staticmethod
    @pytest.fixture(scope='class')
    def reading(snap):
        return snap.collect_dialog_quality(3650)

    def test_top_level_keys(self, reading):
        for k in ('window_days', 'samples', 'reply_len', 'feature_hits',
                  'feature_pct', 'by_type'):
            assert k in reading, 'missing key: %s' % k

    def test_reply_len_shape(self, reading):
        rl = reading['reply_len']
        for k in ('avg', 'median', 'min', 'max', 'buckets'):
            assert k in rl, 'missing reply_len.%s' % k
        assert sum(rl['buckets'].values()) == reading['samples'], (
            'bucket counts must sum to samples')

    def test_feature_keys(self, reading):
        for k in ('bracket_action', 'assistant_tell', 'meow', 'question_end', 'hard_turn'):
            assert k in reading['feature_hits'], 'missing feature: %s' % k
            assert k in reading['feature_pct']

    def test_pct_matches_hits(self, reading):
        n = reading['samples']
        for k, v in reading['feature_hits'].items():
            expect = round(100.0 * v / n, 2)
            assert abs(reading['feature_pct'][k] - expect) < 0.01, k

    def test_by_type_splits_group_and_private(self, reading):
        if reading['samples'] == 0:
            pytest.skip('no samples in window')
        assert reading['by_type'], 'by_type must not be empty when samples exist'
        for t, b in reading['by_type'].items():
            assert b['n'] > 0
            assert 'avg_len' in b


class TestWindowSemantics:
    '''Window filtering must actually narrow the sample.'''

    def test_narrow_window_is_subset(self, snap):
        wide = snap.collect_dialog_quality(3650)
        narrow = snap.collect_dialog_quality(30)
        if wide.get('samples', 0) == 0:
            pytest.skip('no data')
        assert narrow['samples'] <= wide['samples'], (
            'a 30-day window cannot contain more samples than a 10-year window')

    def test_pct_bounds(self, snap):
        r = snap.collect_dialog_quality(3650)
        for k, v in r['feature_pct'].items():
            assert 0.0 <= v <= 100.0, '%s out of range: %s' % (k, v)


class TestPositiveControl:
    '''Positive control: the bucket classifier must actually place values.'''

    def test_bucket_placement_is_deterministic(self, snap):
        '''Re-derive bucket placement from the constant and check boundaries.'''
        def place(v):
            for cap, label in snap._LEN_BUCKETS:
                if cap is None or v <= cap:
                    return label
            return None
        assert place(1) == '<=30'
        assert place(30) == '<=30'
        assert place(31) == '31-60'
        assert place(100000) == '>250'
        assert place(0) == '<=30'
