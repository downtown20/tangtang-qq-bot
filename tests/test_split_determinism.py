# -*- coding: utf-8 -*-
'''T13 regression: group system split must be pure length decision, no randomness.

2026-10-14 ledger T13. Before the fix, maybe_split_reply in agent/reply_pipeline.py used
a random number in the 40-80 char band to decide whether to split, so the same input
could produce different results on two calls. That is the direct mechanism cause of
unreasonable message density, and it violates the single-brain architecture
(CLAUDE.md lesson #9/#10: the system must not decide for the LLM, least of all by
flipping a coin). The original comment even said length-based decision, not random.

This test is the fix's own positive control:
  - positive: same input repeated N times, byte-identical results;
  - negative: length boundary behaviour preserved (<40 no split, 40-80 no split,
    >80 split only when a break exists).
'''
import inspect
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline


def _pipeline():
    '''Minimal pipeline; maybe_split_reply needs no external state.'''
    from unittest.mock import AsyncMock, MagicMock
    return ReplyPipeline(
        napcat=AsyncMock(), stickers=MagicMock(), store=MagicMock(),
        short_term={}, bot_nicknames=['糖糖'], bot_qq='10000',
    )


def _code_only(src):
    '''Strip comment lines, keep real code only.'''
    return chr(10).join(l for l in src.splitlines() if not l.strip().startswith(chr(35)))


class TestSplitDeterminism:
    '''Split must be deterministic: same input, same result, always.'''

    @pytest.mark.parametrize(
        'text',
        [
            'short reply no split',
            'this is a medium length reply used to test the no-split boundary just over forty chars',
            'this is a longer reply used to test the must-split boundary over eighty chars so it should be split',
            'First sentence done. Second sentence done too, now there is a break to split on.',
        ],
        ids=['lt40', '40-80', 'gt80-no-break', 'gt80-with-break'],
    )
    def test_repeated_calls_are_identical(self, text):
        '''Positive control: randomness gone, 50 repeats must be identical.'''
        p = _pipeline()
        first = p.maybe_split_reply(text)
        assert first, 'split result must not be empty'
        for _ in range(50):
            again = p.maybe_split_reply(text)
            assert again == first, (
                'same input produced different splits - random gate is back (T13): '
                + 'first=%r again=%r' % (first, again))

    def test_no_random_in_split_code(self):
        '''Structural gate: no randomness inside maybe_split_reply code.

        Regression guard - if someone re-adds a coin flip, this goes red.
        '''
        src = inspect.getsource(ReplyPipeline.maybe_split_reply)
        code = _code_only(src)
        hits = re.findall('random', code)
        assert not hits, (
            'maybe_split_reply uses randomness again (T13): %r' % hits)


class TestSplitLengthBoundaries:
    '''Length boundary behaviour (negative control: T13 must not change these).'''

    def test_short_reply_not_split(self):
        '''Under 40 chars: no split.'''
        p = _pipeline()
        text = 'yes' * 3
        assert len(text) < 40
        assert p.maybe_split_reply(text) == [text]

    def test_medium_reply_not_split(self):
        '''40-80 chars: never split now (was a coin flip before T13).'''
        p = _pipeline()
        text = 'this is a medium length reply used to test the no-split boundary'
        assert 40 <= len(text) < 80, len(text)
        for _ in range(20):
            assert p.maybe_split_reply(text) == [text]

    def test_long_reply_split_at_sentence_break(self):
        '''Over 80 chars with a sentence break: split into 2 non-empty parts, no loss.'''
        p = _pipeline()
        text = ('第一句话说完了。这里再说一点别的让它足够长。'
                '再多说几句凑到八十个字符以上。这样才会触发拆分逻辑。'
                '继续补充一些内容确保超过八十个字符的门槛线。'
                '第二句话也跟着说完啦。这样就有断点可以拆分了。')
        assert len(text) > 80
        parts = p.maybe_split_reply(text)
        assert len(parts) == 2
        assert all(parts), 'no empty part after split'
        assert (parts[0] + parts[1]).replace(' ', '') == text.replace(' ', '')

    def test_long_reply_without_break_not_split(self):
        '''Over 80 chars but no break point: no split.'''
        p = _pipeline()
        text = 'x' * 100
        assert len(text) > 80
        assert p.maybe_split_reply(text) == [text]

    def test_cq_code_not_split(self):
        '''Leading CQ code: no split (must not cut CQ code off).'''
        p = _pipeline()
        text = '[CQ:image,file=abc] ' + '内容' * 40
        assert p.maybe_split_reply(text) == [text]

    def test_tail_too_short_not_split(self):
        '''Second part would be too short: no split, avoid pointless bubble.'''
        p = _pipeline()
        text = '前面很长很长很长很长很长很长很长很长很长很长很长。嗯'
        parts = p.maybe_split_reply(text)
        assert parts == [text] or all(len(x) >= 5 for x in parts)


class TestGroupVsPrivateContract:
    '''T13 only touches the group fallback; private path was always single.'''

    def test_private_path_unchanged_by_design(self):
        '''Private path does not go through maybe_split_reply.

        Documentation assertion: T13 scope is the group fallback only.
        '''
        src = inspect.getsource(ReplyPipeline.send)
        marker = 'target_type == ' + chr(34) + 'group' + chr(34)
        assert marker in src, (
            'group/private branch missing in send() - T13 scope assumption broken')
