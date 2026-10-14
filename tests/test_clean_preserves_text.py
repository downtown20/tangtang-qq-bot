# -*- coding: utf-8 -*-
'''T25 regression: clean() must not delete legitimate characters.

2026-10-14. Found while auditing the cleaning pipeline for causes of
'语言机械'.

The defect: the invisible-character class at reply_pipeline.py:189 contained a
STRAY literal hyphen. `[\u200b-\u200f-\u202f\u2060-\u206f]` - the second '-'
after a completed range is not a range operator, it is a class member. So the
main cleaning path deleted every ASCII hyphen from every outgoing reply.

Measured effect (real clean() calls, before the fix):
    '2026-10-14'            -> '20261014'
    'https://x.com/a-b'     -> 'https://x.com/ab'
    'e-mail'                -> 'email'
    '(^▽^-)'                -> '(^▽^)'
    '大概 3-5 分钟就好'       -> '大概 35 分钟就好'   <- SEMANTIC change

The last one is why this matters beyond cosmetics: a range became a quantity.

Second defect, same class as T10: the fallback branch at :212 had a DIFFERENT
class - it had no stray hyphen (so hyphens survived a fallback) but was missing
U+202F (so a narrow no-break space survived it). Two copies of one rule that had
drifted apart, with no test comparing them.
'''
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.reply_pipeline import ReplyPipeline


class _Stub:
    '''clean() does not touch self; an empty object is enough.'''


_SRC_PATH = Path(__file__).parent.parent / 'agent' / 'reply_pipeline.py'
_SRC = _SRC_PATH.read_text(encoding='utf-8', errors='ignore')


def clean(text):
    return ReplyPipeline.clean(_Stub(), text)


def _invisible_classes():
    '''Every character class in clean() that targets control/invisible chars.'''
    found = []
    for node in ast.walk(ast.parse(_SRC)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'sub'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            pat = node.args[0].value
            if pat.startswith('[') and 'x7f' in pat:
                found.append((node.lineno, pat))
    return found


class TestLegitimateTextSurvives:
    '''The load-bearing behaviour: nothing a person would type gets eaten.'''

    def test_hyphen_survives_plainly(self):
        assert clean('嗯-这样啊') == '嗯-这样啊'

    def test_date_survives(self):
        assert clean('2026-10-14 考试') == '2026-10-14 考试'

    def test_url_survives(self):
        assert clean('https://example.com/a-b') == 'https://example.com/a-b'

    def test_english_word_survives(self):
        assert clean('e-mail 我发了') == 'e-mail 我发了'

    def test_kaomoji_survives(self):
        assert clean('好呀 (^▽^-) 等我') == '好呀 (^▽^-) 等我'

    def test_range_keeps_its_meaning(self):
        '''The worst case before the fix: 3-5 became 35.'''
        out = clean('大概 3-5 分钟就好')
        assert '3-5' in out, ('the range collapsed into a quantity: %r' % out)
        assert '35' not in out

    def test_common_punctuation_and_particles_survive(self):
        for t in ('好哦~', '嗯，好的。', '喵～', '“引号”', '——破折', '·点'):
            assert clean(t) == t, 'clean() altered %r' % t


class TestInvisibleStillRemoved:
    '''The rule must still do its real job.'''

    def test_known_invisibles_are_deleted(self):
        for cp in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
                   0x202F, 0x2060, 0x2065, 0x206F, 0xFEFF):
            assert clean('a' + chr(cp) + 'b') == 'ab', (
                'U+%04X survived the cleaning class' % cp)

    def test_control_chars_are_deleted(self):
        for cp in (0x00, 0x07, 0x0B, 0x0C, 0x1B, 0x7F):
            assert clean('a' + chr(cp) + 'b') == 'ab', (
                'control U+%04X survived' % cp)

    def test_ordinary_space_is_not_control(self):
        assert clean('a b') == 'a b', 'a plain space must never be treated as a control char'


class TestTwoCopiesAgree:
    '''T10's lesson: two copies of one rule drift apart silently.'''

    def test_main_and_fallback_classes_are_byte_identical(self):
        classes = _invisible_classes()
        assert len(classes) == 2, (
            'expected the main path and the fallback path, found %d' % len(classes))
        (ln_a, a), (ln_b, b) = classes[:2]
        assert a == b, (
            'the two invisible-char classes have drifted apart (lines %d and %d):'
            ' %r vs %r' % (ln_a, ln_b, a, b))

    def test_class_does_not_match_hyphen(self):
        '''Pins the invariant directly: the class must not consume an ASCII hyphen.

        Asking the regex itself is far more reliable than trying to reason about
        whether a '-' inside the class is a range operator or a stray literal.
        '''
        import re as _re
        for ln, pat in _invisible_classes():
            assert _re.match(pat, '-') is None, (
                'the invisible-char class at line %d deletes ASCII hyphens: %r'
                % (ln, pat))


class TestKaomojiSurvive:
    '''T26: the italic strip must not eat kaomoji.

    The code comment promises kaomoji are preserved ('但保留颜文字（如 (*^▽^*)）').
    That promise held only for kaomoji whose inner text has no word character.
    `_` counts as \\w, so '(*^_^*)' - one of the most common kaomoji - lost its
    asterisks and came out as '(^_^)'. Requiring two CONSECUTIVE word/CJK chars
    keeps the promise while still stripping real markdown italics.

    Tested inside a longer sentence on purpose: a very short standalone reply
    triggers the 'washed too hard' fallback, which would mask the damage.
    '''

    KAOMOJI = (
        '(*^_^*)',
        '(*^▽^*)',
        '(๑•̀ㅂ•́)و✧',
        '(*≧▽≦)',
        '(≧▽≦*)',
        '( ˘•ω•˘ )',
        'ヾ(≧▽≦*)o',
    )

    def test_kaomoji_survive_inside_a_sentence(self):
        for k in self.KAOMOJI:
            sentence = '好耶 ' + k + ' 开心'
            out = clean(sentence)
            assert k in out, (
                'kaomoji %r was damaged: %r -> %r' % (k, sentence, out))

    def test_markdown_italic_is_still_stripped(self):
        '''The rule must still do its job.'''
        assert clean('这是 *重点* 内容') == '这是 重点 内容'
        assert clean('*abc*') == 'abc'

    def test_asterisk_pair_around_punctuation_is_not_italic(self):
        '''^_^ is not a word - it is a face.'''
        assert clean('哈哈 (*^_^*) 好可爱') == '哈哈 (*^_^*) 好可爱'

    def test_bold_still_stripped(self):
        out = clean('这是 **粗体** 文字')
        assert '**' not in out, 'markdown bold leaked: %r' % out
