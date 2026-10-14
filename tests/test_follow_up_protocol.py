# -*- coding: utf-8 -*-
'''T16 regression: the follow-up / prior-context protocol must be present and correct.

2026-10-14 ledger T16 (S5 dimension 4). User feedback: context memory should improve toward
long-range linking of prior context, proactive follow-up questions, and admitting uncertainty.

MEMORY_EVIDENCE_CONTRACT and GROUNDING_CONTRACT already forbid inventing details when
unsure. This constant is the other face of the same coin: when she DOES remember, she must
actually use it - carry the thread forward and ask something specific.

Boundary from the user: do not actively solicit privacy. So follow-up questions only target
things the other person raised themselves; never income / address / family / health details.
'''
import io
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols
from agent.personality import PersonalityEngine, PersonalityConfig, Relationship


def _engine_with_config():
    cfg = yaml.safe_load(io.open('config.yaml', encoding='utf-8'))['personality']
    return PersonalityEngine(PersonalityConfig(
        name='X', nicknames=['A'],
        can_do=cfg.get('can_do', []),
        cannot_do=cfg.get('cannot_do', []),
        core=cfg.get('core', ''),
        tease_level=cfg.get('tease_level', 3),
        relationship_tiers=cfg.get('relationship_tiers', {}),
        profile=cfg.get('profile', {}),
    ))


class TestConstantShape:
    '''The constant must exist and carry all four sections.'''

    def test_constant_exists(self):
        assert hasattr(protocols, 'FOLLOW_UP_PROTOCOL')
        assert isinstance(protocols.FOLLOW_UP_PROTOCOL, str)
        assert len(protocols.FOLLOW_UP_PROTOCOL) > 250

    def test_four_sections(self):
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '接住前文' in c
        assert '该追问的时候' in c
        assert '不该追问的时候' in c
        assert '边界' in c

    def test_carries_prior_context(self):
        '''Dimension 4a: link prior context.'''
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '你上次说' in c
        assert '一句话就够' in c
        assert '不要复述整段' in c

    def test_admits_uncertainty(self):
        '''Dimension 4c: admit not remembering instead of inventing.'''
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '不记得就直接说不记得' in c
        assert '这个我真忘了' in c
        assert '编的会被拆穿' in c

    def test_proactive_follow_up_is_specific(self):
        '''Dimension 4b: ask something specific, not an open-ended punt.'''
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '问一个具体的问题' in c
        assert '问那件事本身' in c
        assert '还有吗' in c
        assert '然后呢' in c
        assert '把球踢回去' in c

    def test_knows_when_not_to_push(self):
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '明显不想聊' in c
        assert '别连珠炮' in c
        assert '沉默和一句「我在」也是回应' in c

    def test_privacy_boundary(self):
        '''User boundary: do not actively solicit privacy.'''
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '不问收入' in c
        assert '不问住址' in c
        assert '不问家里的事' in c
        assert '不问健康和身体的细节' in c
        assert '对方没提的，不要挖' in c

    def test_no_new_retrieval_mechanism(self):
        '''Must reuse injected material, not invent a new search path.'''
        c = protocols.FOLLOW_UP_PROTOCOL
        assert '上面的材料里' in c
        assert '材料里没有的细节不补' in c


class TestInjectionIntoLayers:
    '''Must reach both the full prompt and the minimal prompt.'''

    def test_full_prompt_has_it(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert '接住前文' in p
        assert '不问收入' in p

    def test_minimal_prompt_has_it(self):
        '''Replace-type scenarios also need prior-context discipline.'''
        eng = _engine_with_config()
        m = eng._cached_minimal
        assert '接住前文' in m
        assert '不问收入' in m

    def test_sits_with_other_speech_blocks(self):
        '''All three how-to-speak blocks must be adjacent (enter/leave together).'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        i_anchor = p.find('说话示范')
        i_nat = p.find('回复的分寸')
        i_follow = p.find('接住前文')
        assert i_anchor > 0 and i_nat > 0 and i_follow > 0
        assert i_anchor < i_nat < i_follow, 'three blocks must be in order'
        assert i_follow - i_anchor < 3000, 'blocks drifted apart'

    def test_minimal_still_excludes_rule_layer(self):
        '''T14 contract intact: config.yaml rule layer stays out of minimal.'''
        eng = _engine_with_config()
        m = eng._cached_minimal
        assert '能力和行为边界' not in m
        assert '关系档位' not in m
        assert '撩的程度' not in m

    def test_prompt_grew(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert len(p) > 3600, (
            'prompt is only %d chars - the follow-up block is missing again' % len(p))


class TestStability:
    '''Static block - must not break the implicit-cache prefix contract.'''

    def test_prefix_identical_across_relationships(self):
        eng = _engine_with_config()
        cot = '让ta知道你在跟ta说话'
        p1 = eng.build_system_prompt(Relationship.STRANGER)
        p2 = eng.build_system_prompt(Relationship.CLOSE)
        i1 = p1.find(cot)
        i2 = p2.find(cot)
        assert i1 > 0 and i2 > 0
        assert p1[:i1 + len(cot)] == p2[:i2 + len(cot)]

    def test_block_not_duplicated(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.FAMILIAR)
        assert p.count('接住前文') == 1

    def test_hot_reload_keeps_it(self):
        eng = _engine_with_config()
        assert '接住前文' in eng._cached_base
        assert eng.reload_role_card() is True
        assert '接住前文' in eng._cached_base, 'hot reload dropped the follow-up block'


class TestComplementsNotDuplicates:
    '''Must complement MEMORY_EVIDENCE_CONTRACT, not restate it.'''

    def test_both_contracts_present(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        # 既有「不确定不编」纪律
        assert '不确定' in p
        # 新增「记得要用起来」
        assert '接住前文' in p

    def test_not_verbatim_duplicate_of_evidence_contract(self):
        c = protocols.FOLLOW_UP_PROTOCOL
        ev = protocols.MEMORY_EVIDENCE_CONTRACT
        assert c != ev
        assert '证据优先级' not in c, 'should not restate the evidence contract'
