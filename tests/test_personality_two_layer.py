# -*- coding: utf-8 -*-
'''T14 regression: role_card identity layer and config.yaml rule layer must both land in the prompt.

2026-10-14 ledger T14. Before the fix, _build_cache was either/or: if role_card.md existed it
was used alone, and config.yaml (can_do 20 entries / cannot_do 6 entries / core) was only a
fallback when role_card.md was missing. Runtime probe measured the live personality prompt at
918 chars - the 26 rules that specifically fight robotic language never made it into production.

This test pins the two-layer contract:
  - identity layer (role_card.md) still present;
  - rule layer (config.yaml can_do/cannot_do/core/tiers/tease) also present;
  - minimal (replace-scenario) cache stays lean and carries no rule layer;
  - the stable prefix stays static across calls (implicit cache contract).
'''
import io
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.personality import PersonalityEngine, PersonalityConfig, Relationship


def _engine_with_config():
    '''Engine whose config mirrors the real config.yaml personality section.'''
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


class TestTwoLayerComposition:
    '''Identity layer and rule layer must both reach the prompt.'''

    def test_identity_layer_present(self):
        '''role_card.md identity content survives.'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert '刀子嘴豆腐心' in p
        assert '说话示范' in p

    def test_rule_layer_present(self):
        '''config.yaml rules reach the prompt (the T14 core).'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        # can_do / cannot_do / core 三类规则的标志性内容
        assert '用猫娘的方式说话' in p
        assert '括号写动作' in p
        assert '叫别人「糖糖」' in p
        assert '湖南郴州' in p
        assert '能力和行为边界' in p
        assert '不像真人' in p

    def test_prompt_grew_beyond_role_card_only(self):
        '''Guard: prompt must be far larger than the 918-char role-card-only baseline.'''
        eng = _engine_with_config()
        base = eng.build_system_prompt(Relationship.STRANGER)
        role_only = eng._load_role_card()
        assert len(base) > len(role_only) + 1000, (
            'prompt is barely larger than role_card alone - the rule layer is missing again'
            ' (base=%d role_card=%d)' % (len(base), len(role_only)))

    def test_tiers_and_tease_present(self):
        '''relationship_tiers and tease_level also land.'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert '关系档位' in p
        assert '撩的程度' in p

    def test_empty_profile_is_not_injected(self):
        '''All-empty profile must not inject a placeholder section.'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        # config.yaml 的 profile.likes/dislikes/habits/secrets 全为空
        assert '个人档案' not in p

    def test_nonempty_profile_is_injected(self):
        '''Non-empty profile does inject.'''
        eng = _engine_with_config()
        eng.config.profile = {
            'age': '18岁', 'birthday': '6月1日', 'location': '湖南郴州',
            'likes': ['被摸头', '打游戏'], 'dislikes': [], 'habits': [], 'secrets': [],
        }
        eng.invalidate_cache()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert '个人档案' in p
        assert '被摸头' in p

    def test_reload_keeps_rule_layer(self):
        '''Hot reload must not drop the rule layer again (same path as _build_cache).'''
        eng = _engine_with_config()
        before = eng._cached_base
        assert '括号写动作' in before
        assert eng.reload_role_card() is True
        after = eng._cached_base
        assert '括号写动作' in after, 'hot reload dropped the config.yaml rule layer'
        assert '刀子嘴豆腐心' in after


class TestMinimalCacheStaysLean:
    '''Replace-type scenarios must not inherit the rule layer.'''

    def test_minimal_has_no_rule_layer(self):
        eng = _engine_with_config()
        m = eng._cached_minimal
        assert '能力和行为边界' not in m
        assert '括号写动作' in m   # T15：自然度规则与说话示范同在精简尾段
        assert '关系档位' not in m
        assert '撩的程度' not in m

class TestStablePrefixStillStatic:
    '''Appended rule layer must not break the implicit-cache prefix contract.'''

    def test_prefix_identical_across_relationships(self):
        eng = _engine_with_config()
        cot = '让ta知道你在跟ta说话'
        p1 = eng.build_system_prompt(Relationship.STRANGER)
        p2 = eng.build_system_prompt(Relationship.CLOSE)
        i1 = p1.find(cot)
        i2 = p2.find(cot)
        assert i1 > 0 and i2 > 0
        assert p1[:i1 + len(cot)] == p2[:i2 + len(cot)]

    def test_rule_layer_is_static_not_per_call(self):
        '''Rule layer content must not vary between two calls.'''
        eng = _engine_with_config()
        p1 = eng.build_system_prompt(Relationship.FAMILIAR)
        p2 = eng.build_system_prompt(Relationship.FAMILIAR)
        assert p1.count('括号写动作') == p2.count('括号写动作') == 2


class TestNoDuplicateIdentity:
    '''Identity must not be stated twice (would be noise and drift).'''

    def test_core_appears_once(self):
        '''core (完整设定) is injected once, not duplicated with role_card.'''
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        # 「湖南郴州」出现在 core 里；role_card 没有它 → 应只出现 1 次
        assert p.count('湖南郴州') == 1

    def test_sections_not_duplicated(self):
        eng = _engine_with_config()
        p = eng.build_system_prompt(Relationship.STRANGER)
        assert p.count('## 你的能力和行为边界') == 1
        assert p.count('## 以下行为会让群友觉得你不像真人') == 1
        assert p.count('## 说话示范') == 1
