# -*- coding: utf-8 -*-
'''T21 regression: the before/after quality split must be honest and mechanical.

2026-10-14 ledger T21, motivated by T19's retraction. T19 was about to report
'avg length 172.8 -> 67.4 (-61%)' when the truth was that ALL 16 replies that day fell
in 06:23-06:43, BEFORE the change landed at 08:09. Day-level aggregation read intra-day
variance as the effect of the fix.

The lesson: to claim causality you must prove the change happened AFTER the fix. So this
tool splits reply_metrics at an explicit timestamp and reports BOTH windows separately.
Two properties matter most and are pinned here:
  1. It is MECHANICAL - the caller supplies split_ts, the code does not guess a boundary
     and does not decide whether the result is good.
  2. Delta is only computed when BOTH sides have samples - a missing 'after' window must
     never be silently rendered as a number.
'''
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope='module')
def snap():
    p = ROOT / 'tools' / '长期优化快照.py'
    spec = importlib.util.spec_from_file_location('snap_t21', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestMechanical:
    '''Caller owns the boundary; code does not guess.'''

    def test_split_ts_is_echoed_not_invented(self, snap):
        r = snap.compare_dialog_quality('2099-01-01 00:00', days=7)
        assert r.get('split_ts') == '2099-01-01 00:00'

    def test_late_split_puts_early_data_before(self, snap):
        # days 是**相对 split 的下界**（不是「最近 N 天」）——
        # 所以远未来 split 会把下界一起推到未来、两侧都空。
        # 用一个仍在数据范围内的晚时点，才能验证分区语义。
        r = snap.compare_dialog_quality('2026-10-14 00:00', days=3650)
        assert r.get('after', {}).get('samples', 0) > 0    # 10-14 当天的 16 条
        assert r.get('before', {}).get('samples', 0) > 0   # 更早的

    def test_early_split_puts_everything_after(self, snap):
        r = snap.compare_dialog_quality('2026-08-01 00:00', days=3650)
        assert r.get('before', {}).get('samples', 0) == 0
        assert r.get('after', {}).get('samples', 0) > 0


class TestNoFabricatedDelta:
    '''A missing window must not become a number.'''

    def test_no_delta_when_after_is_empty(self, snap):
        # 最新数据之后切分 = after 窗口为空（真实场景：改动刚落、还没跑起来）
        r = snap.compare_dialog_quality('2026-12-01 00:00', days=3650)
        assert r.get('after', {}).get('samples', 0) == 0
        assert r.get('before', {}).get('samples', 0) > 0
        assert 'delta' not in r, (
            'computed a delta with an empty after-window - that is how T19 went wrong')

    def test_no_delta_when_before_is_empty(self, snap):
        # 最早数据之前切分 = before 窗口为空
        r = snap.compare_dialog_quality('2026-08-01 00:00', days=3650)
        assert r.get('before', {}).get('samples', 0) == 0
        assert r.get('after', {}).get('samples', 0) > 0
        assert 'delta' not in r

    def test_windows_partition_the_data(self, snap):
        '''before + after must equal the total in-window count (no overlap, no loss).'''
        split = '2026-10-10 00:00'
        r = snap.compare_dialog_quality(split, days=3650)
        b = r['before'].get('samples', 0)
        a = r['after'].get('samples', 0)
        assert b > 0 and a > 0, 'need both sides populated for this test'
        conn = sqlite3.connect(f'file:{(ROOT / "memory.db").as_posix()}?mode=ro', uri=True)
        total = conn.execute('SELECT COUNT(*) FROM reply_metrics').fetchone()[0]
        conn.close()
        assert b + a == total, f'{b} + {a} != {total} - windows overlap or drop rows'


class TestObservationOnly:
    '''No thresholds, no verdicts.'''

    def test_no_verdict_in_source(self, snap):
        import inspect
        src = inspect.getsource(snap.compare_dialog_quality)
        for bad in ('0.85', 'should be', 'too long', 'PASS', 'FAIL', '好于', '优于'):
            assert bad not in src, f'verdict logic leaked in: {bad}'
        assert 'raise' not in src

    def test_note_states_observe_only(self, snap):
        r = snap.compare_dialog_quality('2026-10-10 00:00', days=3650)
        assert '只观测' in r.get('note', '')

    def test_read_only_connection(self, snap):
        import inspect
        src = inspect.getsource(snap.compare_dialog_quality)
        assert 'mode=ro' in src, 'must open read-only'
        for w in ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER'):
            assert w not in src.upper(), f'write statement in a read-only collector: {w}'


class TestShape:
    def test_window_shape(self, snap):
        r = snap.compare_dialog_quality('2026-10-10 00:00', days=3650)
        for side in ('before', 'after'):
            w = r[side]
            if not w.get('samples'):
                continue
            for k in ('samples', 'avg', 'median', 'buckets',
                      'over_100_pct', 'meow_pct', 'hard_turn_pct'):
                assert k in w, f'{side} missing {k}'
            assert sum(w['buckets'].values()) == w['samples']

    def test_delta_shape_when_both_sides_present(self, snap):
        r = snap.compare_dialog_quality('2026-10-10 00:00', days=3650)
        if 'delta' in r:
            assert set(r['delta'].keys()) == {'avg_len', 'over_100_pct', 'meow_pct'}

    def test_missing_table_returns_error_not_crash(self, snap, monkeypatch, tmp_path):
        monkeypatch.setattr(snap, 'ROOT', tmp_path)
        r = snap.compare_dialog_quality('2026-10-14 08:09', days=7)
        assert 'error' in r


class TestCliWiring:
    def test_main_has_quality_split_flag(self, snap):
        import inspect
        src = inspect.getsource(snap.main)
        assert '--quality-split' in src
        assert 'compare_dialog_quality(' in src

    def test_cli_says_no_delta_when_one_side_empty(self, snap):
        '''T27: the CLI must print the tool's own refusal reason, not a bare delta.

        Originally this asserted the literal string '某一侧样本为 0' lived in main().
        T27 moved the decision into compare_dialog_quality (which now also refuses on
        SMALL samples, not just empty ones), so main() reads q['delta_skipped'] instead.
        Asserting the literal would have pinned WHERE the string lives rather than WHAT
        the CLI does - so it is rewritten to pin the behaviour: no delta branch without
        the skip branch.'''
        import inspect
        src = inspect.getsource(snap.main)
        assert 'in q' in src and 'delta' in src, 'CLI must branch on delta presence'
        assert 'delta_skipped' in src, (
            'CLI must surface the tool\'s refusal reason when it withholds a delta')
        assert 'delta_noise' in src, (
            'T27: when a delta IS printed it must be shown with its noise level')


class TestSampleSizeGuard:
    '''T27: a delta computed on a handful of samples is a fake effect.

    Live trigger: right after the bot restarted (2026-10-14 09:53) there was exactly
    ONE post-fix sample and the tool still printed '均值 -128.8'. That is the same
    error shape as T19's retracted 'length dropped 61%', except this time it came from
    my own tool. A number a reader would take as a conclusion is worse than no number.
    '''

    def test_min_side_parameter_exists_with_default(self, snap):
        import inspect
        sig = inspect.signature(snap.compare_dialog_quality)
        assert 'min_side' in sig.parameters
        assert sig.parameters['min_side'].default == 10

    def test_empty_side_yields_no_delta(self, snap, monkeypatch, tmp_path):
        '''Pre-existing T21 contract, restated at the T27 boundary.'''
        _stub_db(monkeypatch, snap, tmp_path, before=50, after=0)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert 'delta' not in r
        assert '0' in r['delta_skipped'] or '空' in r['delta_skipped']

    def test_small_side_yields_no_delta(self, snap, monkeypatch, tmp_path):
        '''The T27 finding: 1 sample must NOT produce a delta.'''
        _stub_db(monkeypatch, snap, tmp_path, before=207, after=1)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert 'delta' not in r, 'n=1 must never yield a delta'
        assert '样本不足' in r['delta_skipped']
        assert '207' in r['delta_skipped'] and '1' in r['delta_skipped']

    def test_threshold_is_inclusive(self, snap, monkeypatch, tmp_path):
        '''Exactly min_side on both sides is enough.'''
        _stub_db(monkeypatch, snap, tmp_path, before=10, after=10)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert 'delta' in r

    def test_one_below_threshold_blocks(self, snap, monkeypatch, tmp_path):
        _stub_db(monkeypatch, snap, tmp_path, before=10, after=9)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert 'delta' not in r

    def test_delta_carries_noise_estimate(self, snap, monkeypatch, tmp_path):
        '''A shown delta must come with its noise level so small-n is self-evident.

        T27 self-correction: the first version of this test used the CONSTANT-length
        stub, where the true standard error is 0 - so asserting `noise >= 0` passed even
        when the noise computation was mutated to `0.0`. The mutation stayed green and
        exposed that the assertion could not distinguish real noise from no noise.
        Now the stub carries a known spread and the value is pinned numerically.'''
        _stub_db(monkeypatch, snap, tmp_path, before=10, after=10, spread=True)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert 'delta' in r
        # 造数：before = 80,84,...,116（mean 98, sd 12.11, se 3.83）
        assert abs(r['before']['se'] - 3.83) < 0.05
        assert abs(r['after']['se'] - 3.83) < 0.05
        # 合成噪声 = sqrt(se_a^2 + se_b^2) = 3.83 * sqrt(2) = 5.42
        assert 'delta_noise' in r
        assert abs(r['delta_noise'] - 5.42) < 0.05, (
            'delta_noise must be the composed standard error, not a placeholder')
        assert r['delta_noise'] > 0
        assert 'delta_note' in r and '门槛' in r['delta_note']

    def test_window_reports_standard_error(self, snap, monkeypatch, tmp_path):
        _stub_db(monkeypatch, snap, tmp_path, before=30, after=30)
        r = snap.compare_dialog_quality('2026-10-14 08:09', min_side=10)
        assert r['before']['se'] is not None and r['before']['se'] >= 0
        # 常数列 → 标准差 0 → 标准误 0
        assert r['before']['se'] == 0.0

    def test_single_sample_se_is_none_not_zero(self, snap, monkeypatch, tmp_path):
        '''n=1 的标准误无意义，必须是 None 而不是假装成 0（0 会被读成「无噪声」）。'''
        _stub_db(monkeypatch, snap, tmp_path, before=1, after=0)
        assert snap.compare_dialog_quality('2026-10-14 08:09', min_side=1)['before']['se'] is None

    def test_guard_is_documented_in_source(self, snap):
        import inspect
        doc = inspect.getdoc(snap.compare_dialog_quality)
        assert 'min_side' in doc
        assert 'T27' in doc


def _stub_db(monkeypatch, snap, tmp_path, *, before: int, after: int, spread: bool = False):
    '''Build a temp reply_metrics with `before` rows < split and `after` rows >= split.

    Lengths are constant per side so the expected standard error is exactly 0 and the
    assertions cannot drift with random data.'''
    db = tmp_path / 'memory.db'
    conn = sqlite3.connect(db)
    conn.execute(
        'CREATE TABLE reply_metrics (id INTEGER PRIMARY KEY, ts TEXT, reply_len INTEGER, '
        'meow INTEGER, bracket_action INTEGER, question_end INTEGER, assistant_tell INTEGER)')
    # spread=True 造出已知离散度：80,84,...,116（n=10 → mean 98, sd 12.11, se 3.83）；
    # 否则用常数长度（真值 se = 0，适合只测「有没有差值」的用例）。
    for i in range(before):
        ln = 80 + 4 * (i % 10) if spread else 100
        conn.execute('INSERT INTO reply_metrics (ts, reply_len, meow) VALUES (?,?,?)',
                     ('2026-10-14 07:00:00', ln, 0))
    for i in range(after):
        ln = 80 + 4 * (i % 10) if spread else 60
        conn.execute('INSERT INTO reply_metrics (ts, reply_len, meow) VALUES (?,?,?)',
                     ('2026-10-14 12:00:00', ln, 0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(snap, 'ROOT', tmp_path)
    return db
