# -*- coding: utf-8 -*-
"""T48 回归：聊天手艺协议必须真的进 system prompt，且留在风格尾段。

背景：
  knowledge/恋爱军师/话术/ 那 23 篇（75282 字）里真正服务「回复自然」的只有几篇，
  但它们放在知识库里**几乎不会被用到** —— search_knowledge 跨 10-01~10-14 只被调用 10 次，
  10-07 之后 8 天零调用。而且**说话方式是行为不是知识**：
  查一篇《巧妙接话技巧》不会改变你怎么说话。所以蒸馏进 prompt。

为什么要有这个测试（本仓 T44 的教训）：
  把常量接进 personality 时，生成脚本里列表元素漏了逗号 → Python **隐式字符串拼接**
  把 6 行注释和 content += 拼成了一行 → 后者成了注释、**永不执行**。
  而当时「常量定义得好好的」「pyflakes 干净」「AST 通过」「测试全绿」**四条同时成立**。
  是端到端验证抓到的。所以这里断言的是**组合出来的 prompt 真的含它**。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _system_prompt():
    import yaml
    from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    p = cfg.get('personality') or {}
    engine = PersonalityEngine(PersonalityConfig(
        name=p.get('name', '糖糖'), nicknames=p.get('nicknames', []),
        core=p.get('core', ''), can_do=p.get('can_do', []),
        cannot_do=p.get('cannot_do', []), tease_level=p.get('tease_level', 3),
        relationship_tiers=p.get('relationship_tiers', {}),
        profile=p.get('profile', {}),
    ))
    return engine.build_system_prompt(list(Relationship)[0])


def test_constant_exists_and_is_substantial():
    # 常量要在，且不能是空壳。
    from agent.protocols import CONVERSATION_CRAFT
    assert len(CONVERSATION_CRAFT) >= 800, (
        'CONVERSATION_CRAFT 只有 %d 字，太短了 —— 蒸馏时被削了？' % len(CONVERSATION_CRAFT))


def test_craft_is_actually_injected_into_prompt():
    # **核心**：组合出的 system prompt 真的含它（不是源码里有这行）。
    from agent.protocols import CONVERSATION_CRAFT
    sp = _system_prompt()
    assert CONVERSATION_CRAFT[:24] in sp, (
        'CONVERSATION_CRAFT 没进 system prompt —— 它成了死常量。'
        ' 检查 _load_role_card 里那行是不是被隐式字符串拼接吞成注释了')


def test_craft_sits_in_the_style_tail():
    # 必须与另外四条同层（风格尾段）—— 那是 _split_minimal 保留的区段。
    # 放错地方的话，replace 型场景一精简就把它丢了。
    src = (ROOT / 'agent' / 'personality.py').read_text(encoding='utf-8')
    i = src.find('def _load_role_card')
    assert i > 0, '_load_role_card 不存在了'
    nxt = src.find(chr(10) + '    def ', i + 5)
    body = src[i:nxt if nxt > 0 else len(src)]
    for name in ('STYLE_ANCHOR', 'REPLY_NATURALNESS', 'FOLLOW_UP_PROTOCOL',
                 'KNOWLEDGE_USAGE_BOUNDARY', 'CONVERSATION_CRAFT'):
        assert name in body, (
            '%s 不在 _load_role_card 里 —— 五条必须同进同退，别单独漂移' % name)


def test_no_template_smell_guard():
    # 反模板那条必须在 —— 否则这套内容会反过来伤害「自然」这个目标。
    from agent.protocols import CONVERSATION_CRAFT as C
    assert '不是话术模板' in C, '反模板警告丢了'
    assert '不是在完成一次优秀回复' in C
