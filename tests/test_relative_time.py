# -*- coding: utf-8 -*-
'''T3.2 regression: relative-time rendering for injected fact timestamps.

2026-10-14 ledger T3.2 (Stage 3). Audit finding: absolute timestamps injected into the
prompt ("2026-09-01 10:01:00") force the LLM to compute the age itself - it does that badly
and ends up describing a three-week-old fact as yesterday's.

Design constraint from the audit: do NOT change event_time semantics - decay, Episode
grouping and the memory golden set all depend on the stored value. So the fix is at RENDER
time only: keep the stored field, add a relative hint next to it.

Tiering matches handler._fmt_quote_time (the quote path) so one event does not read
differently depending on which path surfaced it.
'''
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols

NOW = datetime.datetime(2026, 10, 14, 12, 0, 0)


class TestHumanizeAge:
    '''Each tier, driven by a frozen clock (tier output must not drift with wall time).'''

    @pytest.mark.parametrize('ts,expected', [
        ('2026-10-14 11:59:30', '刚刚'),
        ('2026-10-14 11:01:00', '59分钟前'),
        ('2026-10-14 11:35:00', '25分钟前'),
        ('2026-10-14 11:00:00', '1小时前'),
        ('2026-10-14 09:00:00', '3小时前'),
        ('2026-10-14 06:00:00', '6小时前'),
        ('2026-10-13 12:00:00', '昨天'),
        ('2026-10-12 12:00:00', '2天前'),
        ('2026-10-08 12:00:00', '6天前'),
    ])
    def test_tiers(self, ts, expected):
        assert protocols.humanize_age(ts, now=NOW) == expected

    def test_one_week_and_beyond_is_empty(self):
        '''A week+ is better served by the date than by "43天前" (which reads as nagging).'''
        assert protocols.humanize_age('2026-10-07 12:00:00', now=NOW) == ''
        assert protocols.humanize_age('2026-09-01 10:01:00', now=NOW) == ''

    def test_future_returns_empty(self):
        '''Future timestamps mean clock skew or cross-machine data - do not describe them.'''
        assert protocols.humanize_age('2026-10-15 12:00:00', now=NOW) == ''

    def test_unparseable_returns_empty_not_a_guess(self):
        '''Never invent an approximate age - return empty and let the caller fall back.'''
        for bad in ('', '   ', '不是时间', '2026', '2026-13-45 99:99:99', None):
            assert protocols.humanize_age(bad, now=NOW) == '', bad

    def test_accepts_minute_and_date_precision(self):
        assert protocols.humanize_age('2026-10-14 11:35', now=NOW) == '25分钟前'


class TestFormatFactTime:
    '''The rendered string keeps the absolute time AND adds the relative hint.'''

    def test_recent_gets_both(self):
        out = protocols.format_fact_time('2026-10-14 09:00:00', now=NOW)
        assert out == '2026-10-14 09:00（3小时前）'

    def test_absolute_never_dropped_when_relative_absent(self):
        out = protocols.format_fact_time('2026-09-01 10:01:00', now=NOW)
        assert '2026-09-01' in out, 'absolute time must survive'
        assert '天前' not in out

    def test_empty_becomes_unknown(self):
        assert protocols.format_fact_time('', now=NOW) == '时间未知'
        assert protocols.format_fact_time(None, now=NOW) == '时间未知'

    def test_unparseable_passes_through_unchanged(self):
        '''Better to show a weird string than to show nothing.'''
        assert protocols.format_fact_time('不是时间', now=NOW) == '不是时间'

    def test_relative_result_is_not_truncated(self):
        '''Regression: a caller slicing [:16] would cut the relative hint off.'''
        out = protocols.format_fact_time('2026-10-14 09:00:00', now=NOW)
        assert out[:16] != out, 'relative hint must live beyond the first 16 chars'
        assert out.endswith('）')


class TestStoredFieldUnchanged:
    '''Audit constraint: event_time semantics must NOT change.'''

    def test_helpers_are_pure_renderers(self):
        '''Neither helper may write, mutate or persist anything.'''
        import inspect
        for fn in (protocols.humanize_age, protocols.format_fact_time):
            src = inspect.getsource(fn)
            for forbidden in ('open(', 'write', 'save', 'UPDATE', 'INSERT'):
                assert forbidden not in src, (fn.__name__, forbidden)

    def test_no_datetime_now_leak_without_override(self):
        '''Defaulting to now() must be overridable, otherwise tests cannot freeze time.'''
        a = protocols.humanize_age('2026-10-14 09:00:00', now=NOW)
        b = protocols.humanize_age('2026-10-14 09:00:00', now=NOW)
        assert a == b == '3小时前'


class TestMemoryInjectionUsesHelper:
    '''The two fact-time injection points must actually call the formatter.'''

    def test_memory_py_uses_format_fact_time(self):
        src = (Path(__file__).parent.parent / 'agent' / 'memory.py').read_text(encoding='utf-8')
        assert 'format_fact_time' in src, 'fact time must be rendered, not raw'
        assert 'memory.event_time or memory.timestamp or "时间未知"' not in src, (
            'raw timestamp injection came back - relative hint lost')

    def test_no_truncating_slice_on_rendered_time(self):
        src = (Path(__file__).parent.parent / 'agent' / 'memory.py').read_text(encoding='utf-8')
        assert 'fact_time[:16]' not in src, (
            'slicing the rendered time cuts off the relative hint')
