# -*- coding: utf-8 -*-
"""T44 回归：外部知识库必须**真的被加载器扫到**，不能只在文件系统里躺着。

为什么要有这个测试（本仓教训 #30/#31 同形）：
  #30「检查器写了但从没生效」—— 正则写错，从上线起就没匹配过任何东西，
      每次都报「零命中」，而**零命中看起来和「没问题」一模一样**。
  #31「按类型开天窗」—— 白名单只列了能想到的类型，剩下的走跳过而不是兜底，
      结果 1392 个文件从未被扫过。

所以：拷贝 43 篇文件进 knowledge/ **不等于**它们会被加载。
本文件断言的是**唯一文档发现器真的看见了它们**，而不是 os.listdir 能列出它们。

写这个测试时第一版把加载函数猜成 load_documents/load_all/scan_documents，
**测试当场红了**（不存在的名字）—— 这正是它该做的：
拒绝凭猜测通过。真实 API 是 discover_document_files / resolve_document_path。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / 'knowledge' / '恋爱军师'
EXPECT_FILES = 44  # README + 知识 20 + 话术 23


def test_package_dir_exists():
    assert PKG.is_dir(), '缺失 knowledge/恋爱军师/'


def test_all_files_present_and_nonempty():
    """44 篇（含 README）必须都在，且都不是空文件。"""
    mds = sorted(p for p in PKG.rglob('*.md') if p.name != 'README.md')
    assert len(mds) >= 43, f'只找到 {len(mds)} 篇，期望 >= 43'
    empties = [str(p.relative_to(PKG)) for p in mds if p.stat().st_size < 200]
    assert not empties, f'这些文件过小，可能是拷贝失败: {empties}'


def test_two_subdirs():
    """知识/ 与 话术/ 两个子目录都要在——保留来源可追溯性。"""
    for zh, least in (('知识', 20), ('话术', 23)):
        d = PKG / zh
        assert d.is_dir(), f'缺失子目录 {zh}/'
        n = len(list(d.glob('*.md')))
        assert n >= least, f'{zh}/ 只有 {n} 篇，期望 >= {least}'


def test_scanner_sees_them_recursively():
    """**关键**：断言唯一文档发现器真的扫到了这批文件。

    不用文件系统断言（listdir 能列出 != 会被加载）——用 discover_document_files 本身。
    它的 docstring 写明「search/read/extract 应共用这个边界」，所以它看见 = 真的可用。
    """
    from agent.knowledge import discover_document_files
    files = discover_document_files(ROOT / 'knowledge')
    mine = [p for p in files if '恋爱军师' in str(p)]
    assert len(mine) >= EXPECT_FILES, (
        f'发现器只扫到 {len(mine)} 个恋爱军师文件，期望 >= {EXPECT_FILES}'
        ' —— 文件在但没被加载，等于没整合')
    names = {p.name for p in mine}
    assert '01-证据分级与内容边界.md' in names, '知识/ 下的文件没被扫到'
    assert '00-导读与使用分级.md' in names, '话术/ 下的文件没被扫到'


def test_not_swallowed_by_sensitive_filter():
    """目录名不能撞上敏感目录过滤器（撞上会让整批静默消失）。"""
    from agent.knowledge import SENSITIVE_DIRECTORY_NAMES
    for d in ('恋爱军师', '知识', '话术'):
        assert d not in SENSITIVE_DIRECTORY_NAMES, (
            f'{d} 被当成敏感目录 —— 整批 43 篇会被静默跳过')


def test_content_is_reachable_by_read_document():
    """能被 resolve_document_path 按名解析 —— read_document 工具走这条路。"""
    from agent.knowledge import resolve_document_path
    p = resolve_document_path(ROOT / 'knowledge', '03-依恋理论与情绪调节')
    assert p is not None, '按文件名解析不到 —— read_document 读不到它'
    assert p.read_text(encoding='utf-8').strip(), '解析到的文件是空的'


def test_readme_records_upstream_provenance():
    """MIT 许可要求保留归属；也为了日后能 diff 上游更新。"""
    t = (PKG / 'README.md').read_text(encoding='utf-8')
    assert 'github.com/shengjidaguai-china/goutoujunshi' in t
    assert 'MIT' in t
    assert '6db7354a4002dc7c448a9c87ffdad8132570c9d3' in t, 'README 里应记录上游 commit SHA'


def test_boundary_is_actually_injected_into_prompt():
    """**关键**：常量可导入 != 它被注入了（本仓教训 #12/#14 同形）。

    实测踩过：第一次插入时脚本里的列表元素漏了逗号，Python 的**隐式字符串拼接**
    把 6 行注释和 content += 拼成了一行 —— 后者变成注释的一部分，永不执行。
    而「常量定义得好好的」「pyflakes 干净」「测试全绿」三条**同时成立**。
    所以这里断言的是**组合出来的 system prompt 真的含它**，不是源码里有这行。
    """
    import io
    import yaml
    from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
    from agent.protocols import KNOWLEDGE_USAGE_BOUNDARY

    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    p = cfg.get('personality') or {}
    engine = PersonalityEngine(PersonalityConfig(
        name=p.get('name', '糖糖'), nicknames=p.get('nicknames', []),
        core=p.get('core', ''), can_do=p.get('can_do', []),
        cannot_do=p.get('cannot_do', []), tease_level=p.get('tease_level', 3),
        relationship_tiers=p.get('relationship_tiers', {}),
        profile=p.get('profile', {}),
    ))
    sp = engine.build_system_prompt(list(Relationship)[0])
    assert KNOWLEDGE_USAGE_BOUNDARY[:26] in sp, (
        'KNOWLEDGE_USAGE_BOUNDARY 没进 system prompt —— 它成了死常量。'
        '检查 _load_role_card 里那行是不是被隐式字符串拼接吞成注释了')


def test_boundary_survives_minimal_split():
    """精简模式（replace 型场景）必须保留它 —— 否则场景一开就失守。"""
    from agent.protocols import KNOWLEDGE_USAGE_BOUNDARY
    t = (ROOT / 'agent' / 'personality.py').read_text(encoding='utf-8')
    i = t.find('def _split_minimal')
    assert i > 0, '_split_minimal 不存在了 —— 请核对精简逻辑再改本测试'
    nxt = t.find(chr(10) + '    def ', i + 5)
    body = t[i:nxt if nxt > 0 else len(t)]
    assert 'KNOWLEDGE_USAGE_BOUNDARY' in body or '风格锚' in body or 'STYLE_ANCHOR' in body, (
        '_split_minimal 既没显式保边界常量，也没保风格尾段的判据 —— 精简模式下会丢')
