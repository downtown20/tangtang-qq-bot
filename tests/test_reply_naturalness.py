# -*- coding: utf-8 -*-
'''T15 regression: the reply-naturalness rule layer must be present and correct.

2026-10-14 ledger T15 (S5 dimension 1/3). User feedback: unstable reply length and robotic
language. This adds protocols.REPLY_NATURALNESS - four behaviour boundaries the STYLE_ANCHOR
examples do not cover: length (default short, never pad), spoken style (pauses, particles,
no written-language long sentences), anti-template (no AI-speak, no parenthetical stage
directions, no repeated catchphrases), emoji (at most one per sentence).

Injected in _load_role_card right after STYLE_ANCHOR so both 'how to speak' blocks land in
the tail section that _split_minimal preserves - they enter and leave together.
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
    '''The constant itself must exist and carry the four boundaries.'''

    def test_constant_exists(self):
        assert hasattr(protocols, 'REPLY_NATURALNESS')
        assert isinstance(protocols.REPLY_NATURALNESS, str)
        assert len(protocols.REPLY_NATURALNESS) > 300

    def test_has_four_boundaries(self):
        c = protocols.REPLY_NATURALNESS
        assert '长度' in c
        assert '像在打字' in c
        assert '机器味' in c
        assert '表情' in c

    def test_length_rule_is_short_by_default(self):
        '''Dimension 1: default short, never pad (user said 减少).'''
        c = protocols.REPLY_NATURALNESS
        assert '默认一两句说完' in c
        assert '不要为了显得热情而凑长度' in c
        assert '选短的那个' in c

    def test_spoken_style_rules(self):
        '''Dimension 3: pauses, particles, no written-language sentences.'''
        c = protocols.REPLY_NATURALNESS
        assert '省略号' in c
        assert '语气词' in c
        assert '不写书面语长句' in c

    def test_anti_template_rules(self):
        '''Anti-template targets the verified real source, not just AI-speak.'''
        c = protocols.REPLY_NATURALNESS
        assert '作为AI' in c
        assert '很乐意' in c
        # 括号动作是 2026-08-17 取证的真实主源（11.8%），必须点名
        assert '括号写动作' in c
        assert '复读机' in c

    def test_emoji_rule(self):
        c = protocols.REPLY_NATURALNESS
        assert '一句最多一个' in c
        assert '话题本身严肃' in c


class TestInjectionIntoLayers:
    '''Must reach the full prompt and the minimal prompt together with STYLE_ANCHOR.'''

    def test_full_prompt_has_it(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert '回复的分寸' in p
        assert '默认一两句说完' in p

    def test_sits_next_to_style_anchor(self):
        '''Must be adjacent to STYLE_ANCHOR - the two how-to-speak blocks together.'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        i_anchor = p.find('说话示范')
        i_nat = p.find('回复的分寸')
        assert i_anchor > 0 and i_nat > 0
        assert 0 < i_nat - i_anchor < 1200, (
            'REPLY_NATURALNESS is not adjacent to STYLE_ANCHOR - they must enter/leave together')

    def test_minimal_prompt_has_it(self):
        '''Replace-type scenarios also need to speak like a human.'''
        eng = _engine_with_config()
        m = eng._cached_minimal
        assert '回复的分寸' in m
        assert '说话示范' in m

    def test_minimal_still_excludes_rule_layer(self):
        '''But the config.yaml rule layer must NOT be in minimal (T14 contract).'''
        eng = _engine_with_config()
        m = eng._cached_minimal
        assert '能力和行为边界' not in m
        assert '关系档位' not in m
        assert '撩的程度' not in m

    def test_prompt_grew(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert len(p) > 3200, (
            'prompt is only %d chars - the naturalness block is missing again' % len(p))


class TestStability:
    '''New block is static - must not break the implicit-cache prefix contract.'''

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
        assert p.count('回复的分寸') == 1

    def test_hot_reload_keeps_it(self):
        eng = _engine_with_config()
        assert '回复的分寸' in eng._cached_base
        assert eng.reload_role_card() is True
        assert '回复的分寸' in eng._cached_base, 'hot reload dropped the naturalness block'
