# -*- coding: utf-8 -*-
'''T4.9 regression: the drives daily-series sampler must be stable, CSV-shaped, and wired.

2026-10-14 ledger T4.9 (S4). T4.1's acceptance criterion is 'every day at least one drive below
0.85 and not always social/avoid'. A single-point snapshot cannot prove 'every day' - it needs
a time series. But the series should NOT cost a dedicated 7-day soak; it should accumulate
during normal operation. So the snapshot tool gained --drives-series: each run appends one
CSV row of the six drives.

Design constraints pinned here:
  - OBSERVE ONLY: no thresholds, no verdicts (user said not to set numeric targets).
  - STABLE COLUMNS: ts + the six drive keys, header written exactly once. Column drift would
    make the series incomparable across time, which is the whole point of having it.
  - READ-ONLY on .tangtang_self.json; only the CSV under the snapshot dir is written.
'''
import csv
import importlib.util
import io
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

EXPECT_KEYS = ['social', 'commitment', 'curiosity_info', 'express',
               'curiosity_explore', 'avoid']


@pytest.fixture(scope='module')
def snap():
    p = ROOT / 'tools' / '长期优化快照.py'
    spec = importlib.util.spec_from_file_location('snap_t49', p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def csv_guard(snap, tmp_path, monkeypatch):
    '''Redirect the CSV into tmp_path — tests never touch the live accumulated data.

    2026-10-15 found by a full-suite run: this fixture used to backup/restore the
    REAL docs/开发规划/长期优化/快照/drives-series.csv — a file that anything on this
    machine may be appending to (its mtime moved during a full-suite window, 01:57:21).
    So "append once -> exactly two lines" was a race: another writer adds a row and the
    assertion sees three. Standalone runs (<1s) almost never hit it; the full suite
    (13min) hits it repeatedly -- the tell was "green alone, red in the suite".
    Isolating to tests/_tmp/ decouples the test from live data and removes the need for
    backup/restore entirely (nothing safe to write through is left).

    ⚠ 归属更正（2026-10-15 Lead 取证）：写入者是 tools/长期优化快照.py 的
    --drives-series（唯一写点：_DRIVES_SERIES + append_drives_series()，:507/:525），
    而 agent/、config.yaml、定时任务里**没有任何自动化调用它**——即**不是「在跑的 bot
    的快照任务」**，是有人手动跑过那个 CLI。原注释把根因记成 bot 任务，会让下一个接手
    的人按虚构前提做设计（教训 #35「文档说的和代码做的分家」）。
    '''
    # 落点必须仍**在仓库内**：append_drives_series() 的返回值里有
    # _DRIVES_SERIES.relative_to(ROOT)（tools/长期优化快照.py:544），
    # 指到 %TEMP% 会 ValueError。所以用 tests/_tmp/<唯一名>/ 而不是 tmp_path 本身。
    d = ROOT / 'tests' / '_tmp' / tmp_path.name
    d.mkdir(parents=True, exist_ok=True)
    p = d / 'drives-series.csv'
    monkeypatch.setattr(snap, '_DRIVES_SERIES', p)
    yield p
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.parent.rmdir()            # 父目录空了就一起收掉（非空/被占用就算了）
    except OSError:
        pass


class TestConstants:
    def test_drive_keys_are_the_six(self, snap):
        assert snap._DRIVE_KEYS == EXPECT_KEYS

    def test_series_path_under_snapshot_dir(self, snap):
        assert snap._DRIVES_SERIES.name == 'drives-series.csv'
        assert snap._DRIVES_SERIES.parent == snap.OUT_DIR


class TestReadDrives:
    def test_reads_six_values(self, snap):
        d = snap._read_drives()
        assert d, 'drives file should be readable in this repo'
        for k in EXPECT_KEYS:
            assert k in d, 'missing drive: %s' % k
            assert isinstance(d[k], (int, float))

    def test_values_in_unit_range(self, snap):
        d = snap._read_drives()
        for k in EXPECT_KEYS:
            assert 0.0 <= float(d[k]) <= 1.0, '%s out of range: %s' % (k, d[k])


class TestAppendShape:
    def test_first_append_writes_header_once(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        r = snap.append_drives_series()
        assert 'error' not in r, r
        lines = csv_guard.read_text(encoding='utf-8').strip().split(chr(10))
        assert len(lines) == 2, lines
        assert lines[0].split(',') == ['ts'] + EXPECT_KEYS

    def test_second_append_does_not_repeat_header(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        snap.append_drives_series()
        snap.append_drives_series()
        lines = csv_guard.read_text(encoding='utf-8').strip().split(chr(10))
        assert len(lines) == 3, lines
        assert sum(1 for l in lines if l.startswith('ts,')) == 1, 'header repeated'

    def test_every_row_has_same_column_count(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        for _ in range(3):
            snap.append_drives_series()
        rows = list(csv.reader(io.StringIO(csv_guard.read_text(encoding='utf-8'))))
        widths = {len(r) for r in rows if r}
        assert widths == {7}, widths

    def test_appended_row_matches_live_drives(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        live = snap._read_drives()
        snap.append_drives_series()
        rows = list(csv.reader(io.StringIO(csv_guard.read_text(encoding='utf-8'))))
        last = dict(zip(rows[0], rows[-1]))
        for k in EXPECT_KEYS:
            assert float(last[k]) == float(live[k]), k

    def test_return_payload_shape(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        r = snap.append_drives_series()
        assert set(r.keys()) == {'ts', 'values', 'file'}
        assert set(r['values'].keys()) == set(EXPECT_KEYS)


class TestColumnCheck:
    '''Positive-control driven: a corrupted column MUST be reported.'''

    HEADER = 'ts,social,commitment,curiosity_info,express,curiosity_explore,avoid'

    def _write(self, p, text):
        p.write_text(text, encoding='utf-8')

    def test_good_file_passes(self, snap, csv_guard):
        self._write(csv_guard, self.HEADER + chr(10) + '2026-10-14 00:00:00,0.1,0.2,0.3,0.4,0.5,0.6' + chr(10))
        assert snap.check_drives_series() == []

    def test_header_only_is_valid(self, snap, csv_guard):
        self._write(csv_guard, self.HEADER + chr(10))
        assert snap.check_drives_series() == []

    def test_missing_column_detected(self, snap, csv_guard):
        self._write(csv_guard, 'ts,social,commitment' + chr(10) + '1,2,3' + chr(10))
        assert snap.check_drives_series(), 'short header must be reported'

    def test_renamed_column_detected(self, snap, csv_guard):
        self._write(csv_guard, self.HEADER.replace('avoid', 'avoidance') + chr(10))
        assert snap.check_drives_series(), 'renamed column must be reported'

    def test_ragged_row_detected(self, snap, csv_guard):
        self._write(csv_guard,
                    self.HEADER + chr(10)
                    + '2026-10-14 00:00:00,0.1,0.2,0.3,0.4,0.5,0.6' + chr(10)
                    + '2026-10-14 00:01:00,0.1,0.2' + chr(10))
        assert snap.check_drives_series(), 'ragged row must be reported'

    def test_empty_file_detected(self, snap, csv_guard):
        self._write(csv_guard, '')
        assert snap.check_drives_series(), 'empty file must be reported'

    def test_missing_file_detected(self, snap, csv_guard):
        if csv_guard.exists():
            csv_guard.unlink()
        assert snap.check_drives_series(), 'missing file must be reported'


class TestWiring:
    '''The sampler must actually be reachable from main().'''

    def test_main_has_drives_series_flag(self, snap):
        import inspect
        src = inspect.getsource(snap.main)
        assert '--drives-series' in src
        assert 'append_drives_series()' in src

    def test_main_has_check_series_flag(self, snap):
        import inspect
        src = inspect.getsource(snap.main)
        assert '--check-series' in src
        assert 'check_drives_series()' in src

    def test_no_threshold_in_series_code(self, snap):
        '''Observe only - no 0.85 checking, no verdicts.'''
        import inspect
        src = inspect.getsource(snap.append_drives_series)
        assert '0.85' not in src, 'T4.9 must not enforce the T4.1 threshold'
        assert 'raise' not in src

    def test_series_code_does_not_write_self_json(self, snap):
        '''Only the CSV is written; .tangtang_self.json stays read-only.'''
        import inspect
        src = inspect.getsource(snap.append_drives_series) + inspect.getsource(snap._read_drives)
        # 只应写 CSV；.tangtang_self.json 只读
        assert 'read_text' in src, 'self json must be read, not written'
        assert 'self' in src or '_tangtang_self' in src or '.tangtang_self.json' in src
