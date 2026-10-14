# -*- coding: utf-8 -*-
'''T18 regression + mechanised blind-test proxy (S5 dimension 6 / closeout).

2026-10-14 ledger T18. Two jobs:

1. End-to-end regression that T13-T17 are ALL live in one place - the earlier tasks each have
   their own unit test, but nothing asserted they coexist. This file is the integration net:
   split determinism, two-layer personality, naturalness rules, follow-up protocol, telemetry.

2. A mechanised blind-test PROXY. The user's quality goal mentions a blind-test human
   misjudgement rate, which needs humans. This proxy does not replace that - it is a cheap
   stand-in: judge a curated set of deliberately-mechanical replies against a curated set of
   natural ones with the EXISTING ReplySelfCheck, and assert the mechanical set scores worse.
   It answers one question objectively: does our own quality gate still separate the two?
   If this ever goes green-on-both, the gate has stopped discriminating.
'''
import inspect
import io
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols
from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
from agent.reply_pipeline import ReplyPipeline
from agent.self_check import ReplySelfCheck


# ═══════════════════════════════════════════
# 1. 集成网：T13-T17 必须同时在场
# ═══════════════════════════════════════════


def _engine():
    cfg = yaml.safe_load(io.open('config.yaml', encoding='utf-8'))['personality']
    return PersonalityEngine(PersonalityConfig(
        name='小糖糖', nicknames=['糖糖'],
        can_do=cfg.get('can_do', []),
        cannot_do=cfg.get('cannot_do', []),
        core=cfg.get('core', ''),
        tease_level=cfg.get('tease_level', 3),
        relationship_tiers=cfg.get('relationship_tiers', {}),
        profile=cfg.get('profile', {}),
    ))


def _pipeline():
    from unittest.mock import AsyncMock, MagicMock
    return ReplyPipeline(
        napcat=AsyncMock(), stickers=MagicMock(), store=MagicMock(),
        short_term={}, bot_nicknames=['糖糖'], bot_qq='10000',
    )


class TestAllFiveTasksCoexist:
    '''The S5 fixes must all be live together, not one at a time.'''

    def test_t13_split_is_deterministic(self):
        p = _pipeline()
        text = ('第一句话说完啦。这里再说点别的凑够八十个字以上。'
                '第二句话也说完啦。这样就有断点可以拆分了。'
                '为了稳妥再补一句让它一定超过门槛线。'
                '再多说几个字确保万无一失。'
                '最后再补一句凑数。')
        assert len(text) > 80
        first = p.maybe_split_reply(text)
        for _ in range(30):
            assert p.maybe_split_reply(text) == first
        src = inspect.getsource(ReplyPipeline.maybe_split_reply)
        code = chr(10).join(l for l in src.splitlines() if not l.strip().startswith('#'))
        assert 'random' not in code

    def test_t14_two_layer_personality(self):
        eng = _engine()
        prompt = eng.build_system_prompt(Relationship.STRANGER)
        # 身份层
        assert '刀子嘴豆腐心' in prompt
        # 规则层（T14 之前完全不在场）
        assert '括号写动作' in prompt
        assert '能力和行为边界' in prompt

    def test_t15_naturalness_rules(self):
        eng = _engine()
        prompt = eng.build_system_prompt(Relationship.STRANGER)
        assert '回复的分寸' in prompt
        assert '默认一两句说完' in prompt
        assert '不要为了显得热情而凑长度' in prompt

    def test_t16_follow_up_protocol(self):
        eng = _engine()
        prompt = eng.build_system_prompt(Relationship.STRANGER)
        assert '接住前文' in prompt
        assert '不问收入' in prompt

    def test_t17_telemetry_is_wired(self):
        import importlib.util
        root = Path(__file__).parent.parent
        spec = importlib.util.spec_from_file_location(
            'snap_t18', root / 'tools' / '长期优化快照.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert 'collect_dialog_quality()' in inspect.getsource(m.collect_all)

    def test_all_three_speech_blocks_ordered(self):
        '''STYLE_ANCHOR → REPLY_NATURALNESS → FOLLOW_UP_PROTOCOL, adjacent and ordered.'''
        eng = _engine()
        prompt = eng.build_system_prompt(Relationship.STRANGER)
        i1 = prompt.find('说话示范')
        i2 = prompt.find('回复的分寸')
        i3 = prompt.find('接住前文')
        assert 0 < i1 < i2 < i3

    def test_prompt_budget_sane(self):
        '''Prompt must not have ballooned past a sane ceiling.'''
        eng = _engine()
        prompt = eng.build_system_prompt(Relationship.STRANGER)
        assert 3000 < len(prompt) < 8000, (
            'prompt size %d outside expected band' % len(prompt))


# ═══════════════════════════════════════════
# 2. 机器化盲测代理
# ═══════════════════════════════════════════

# 刻意机器味的样本——每条命中一种已取证的模板特征
_MECHANICAL = [
    '我愣了一下，然后说好呀。',                       # 罐装镜头
    '作为一个人工智能助手，很乐意为你解答这个问题。',      # 助手腔（同时两条）
    '我理解你的感受，这个问题问得很好。',               # 助手腔
    '让我来帮助你解决这个困扰吧。',                     # 助手腔
    '沉默了几秒，我轻轻叹了口气。',                     # 罐装镜头 + 舞台动作
    '（歪头）你在说什么呀～',                          # 括号动作镜头（猫相关词表内）
    '（炸毛）你再说一遍试试。',                         # 括号动作镜头（猫相关词表内）
]

# 自然的口语样本——短、有停顿、无模板特征。
# 注意：刻意不含 4 字以下的极短回复——`ReplySelfCheck._check_blank` 会把它们
# hard-block（< _MIN_REPLY_LENGTH），那是它的既有设计，不是本代理要测的东西。
# 该冲突（风格锚教她说「在的」vs 自检拦极短回复）单独记在
# TestKnownTension::test_style_anchor_short_example_is_flagged。
_NATURAL = [
    '行，想听哪首，我唱给你听',
    '长按右键不放，跟 Java 版点一下不一样',
    '哪里不对？你说，我听着',
    '刚醒……还有点迷糊，等我喝口水',
    '有的吧，你翻翻前面那个',
    '嗯，那就这样定了喵',
]


class TestMechanicalVsNatural:
    '''Does our own quality gate still tell the two apart?'''

    def _scores(self, samples):
        sc = ReplySelfCheck()
        out = []
        for s in samples:
            r = sc.check(s)
            out.append((s, r.score, len(r.warnings), r.blocked))
        return out

    def test_mechanical_set_gets_more_warnings(self):
        mech = self._scores(_MECHANICAL)
        nat = self._scores(_NATURAL)
        mech_warn = sum(w for _, _, w, _ in mech)
        nat_warn = sum(w for _, _, w, _ in nat)
        assert mech_warn > nat_warn, (
            'gate no longer discriminates: mechanical=%d natural=%d warnings'
            % (mech_warn, nat_warn))

    def test_mechanical_set_scores_lower(self):
        mech = self._scores(_MECHANICAL)
        nat = self._scores(_NATURAL)
        mech_avg = sum(s for _, s, _, _ in mech) / len(mech)
        nat_avg = sum(s for _, s, _, _ in nat) / len(nat)
        assert mech_avg < nat_avg, (
            'gate no longer discriminates: mechanical avg=%.1f natural avg=%.1f'
            % (mech_avg, nat_avg))

    def test_anti_tell_catches_each_mechanical_sample(self):
        '''Every curated mechanical sample must trip at least one anti-tell.'''
        sc = ReplySelfCheck()
        for s in _MECHANICAL:
            r = sc.check(s)
            assert any('anti-tell' in w for w in r.warnings), (
                'anti-tell missed a curated mechanical sample: %r (warnings=%r)'
                % (s, r.warnings))

    def test_natural_set_is_not_flagged_as_anti_tell(self):
        '''No natural sample may be flagged - false positives cost trust.'''
        sc = ReplySelfCheck()
        for s in _NATURAL:
            r = sc.check(s)
            assert not any('anti-tell' in w for w in r.warnings), (
                'false positive on a natural sample: %r (warnings=%r)' % (s, r.warnings))

    def test_natural_samples_not_blocked(self):
        sc = ReplySelfCheck()
        for s in _NATURAL:
            r = sc.check(s)
            assert not r.blocked, 'natural sample blocked: %r (%r)' % (s, r.blocks)


class TestKnownTensions:
    '''Real tensions found while building the proxy. Recorded, not hidden.'''

    def test_bracket_catcher_covers_generic_stage_directions(self):
        '''T35（2026-10-14）：**缺口已闭合**——判定侧与采集侧现在指向同一个定义。

        原缺口（T18 记录、本测试原先钉住）：`agent/self_check.py:261` 的判定正则是
        **内联写死**的，词表只有 10 个猫相关词（耳朵/尾巴/猫耳/眼睛/蹭/呼噜/炸毛/歪头/
        小爪/耳朵抖），**漏掉通用舞台说明**（笑了笑）（叹气）（眨眼）；而采集侧
        `handler.py` 又是**另一份** 8 词词表（没有「小爪」「耳朵抖」）——
        **同一个规则的两份拷贝已经漂移**，正是 T10/T12/T25 的同一个形状。

        本任务把定义收到 `ReplySelfCheck.STAGE_DIRECTION_RE` 一处，两边都指向它，并放宽词表。

        实测（近 30 天 882 条私聊回复）：原 10 词词表 **13 条 = 1.47%**；
        新判据 **43 条 = 4.88%** —— **低估 3.3 倍**。

        这是**诊断口径**（只记 warning 不阻断），所以宁宽勿漏：
        放宽的代价只是一条警告，漏报的代价是这类问题继续无人可见（它藏了两个月）。
        '''
        sc = ReplySelfCheck()
        CASES = [
            ('（歪头）你在说什么呀～', '猫相关动作（原有能力，不许退化）'),
            ('（笑了笑）今天天气真好呢。', '通用表情——原缺口'),
            ('（叹气）算了。', '通用动作——原缺口'),
            ('（眨眼）你猜。', '通用动作——原缺口'),
            ('（把脸埋进枕头）都这个点了。', '复合动作（生产真实样本形态）'),
            ('（帽子拉下来盖住半张脸，从缝里瞥你一眼）', '长复合动作（生产真实样本）'),
        ]
        for text, why in CASES:
            r = sc.check(text)
            assert any('括号动作' in w for w in r.warnings), (
                '漏判舞台说明（%s）: %r (warnings=%r)' % (why, text, r.warnings))

    def test_plain_parenthetical_is_not_a_stage_direction(self):
        '''阴性对照：**纯命题式旁白不是舞台说明**——诊断可以宽，但不能宽到这个程度。

        判据是「括号较短 且 含身体部位或表情/动作词」，所以这几条不该命中。
        没有这条对照，放宽词表就会变成「凡括号皆警告」，那个指标立刻失去意义。
        '''
        sc = ReplySelfCheck()
        for text in [
            '（其实我昨天就知道了）真的。',
            '（明天下午三点）我等你。',
            '（三个小时左右吧）差不多。',
        ]:
            r = sc.check(text)
            assert not any('括号动作' in w for w in r.warnings), (
                '把纯旁白误判成舞台说明: %r (warnings=%r)' % (text, r.warnings))

    def test_detector_and_collector_share_one_definition(self):
        '''单一来源闸门：**采集侧不许再自己写一份词表**。

        这正是本任务修掉的东西（两份拷贝漂移，T10/T12/T25 的同一个形状）。
        用 AST/文本双查：采集侧必须出现 `STAGE_DIRECTION_RE`，且不得再内联那份猫词表。
        '''
        from pathlib import Path as _P
        src = (_P(__file__).parent.parent / 'agent' / 'handler.py').read_text(
            encoding='utf-8')
        assert 'STAGE_DIRECTION_RE' in src, (
            '采集侧没指向单一来源——两份拷贝又会各自漂移')
        assert '耳朵|尾巴|猫耳' not in src, (
            '采集侧又内联写了一份词表（应改用 STAGE_DIRECTION_RE）')

    def test_style_anchor_short_example_is_flagged(self):
        '''KNOWN TENSION: STYLE_ANCHOR teaches 「在的」 but the checker hard-blocks it.

        protocols.STYLE_ANCHOR's first example is 「群友：糖糖在吗 / 糖糖：在的」.
        ReplySelfCheck._check_blank hard-blocks any reply shorter than _MIN_REPLY_LENGTH,
        so the very reply the style anchor holds up as the model would be blocked if the
        LLM actually produced it. Recorded as a tension; resolving it needs an owner call
        (raise the floor's exception for direct answers, or drop that example).
        '''
        sc = ReplySelfCheck()
        r = sc.check('在的')
        assert r.blocked, (
            'TENSION RESOLVED: short direct answers are no longer blocked - update this test')
        # 风格锚里确实有这条正面示例
        assert '在的' in protocols.STYLE_ANCHOR


class TestNaturalBlockIsPresentAndStatic:
    '''The prose rules the proxy leans on must actually be shipped.'''

    def test_protocols_ship_all_three_blocks(self):
        for name in ('STYLE_ANCHOR', 'REPLY_NATURALNESS', 'FOLLOW_UP_PROTOCOL'):
            assert hasattr(protocols, name), 'missing protocols.%s' % name

    def test_blocks_are_non_trivial(self):
        for name, floor in (('STYLE_ANCHOR', 150), ('REPLY_NATURALNESS', 300),
                            ('FOLLOW_UP_PROTOCOL', 250)):
            v = getattr(protocols, name)
            assert len(v) >= floor, '%s too short (%d < %d)' % (name, len(v), floor)

    def test_old_uncertainty_discipline_still_present(self):
        '''T16 must complement, not replace, the evidence contracts.'''
        assert '不确定' in protocols.MEMORY_EVIDENCE_CONTRACT
        assert '不要编' in protocols.GROUNDING_CONTRACT
        assert protocols.MEMORY_EVIDENCE_CONTRACT != protocols.FOLLOW_UP_PROTOCOL
