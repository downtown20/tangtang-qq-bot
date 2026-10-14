# -*- coding: utf-8 -*-
"""T47 回归：色色参考的「文件夹 = 注入槽位」契约。

背景（为什么要有这个测试）：
  旧实现把 5 篇 15749 字拼起来后 `[:6000]` 硬截断，**静默丢掉 62%**，
  丢谁由 sorted(rglob()) 的字典序决定，不是重要性 —— 实测两篇整篇从未进过上下文。
  这是本仓反复踩的形态（教训 #30 「检查器从没生效」/#31「按类型开天窗」）：
  **静默失效看起来和「没问题」一模一样。**

所以本测试钉住三件事：
  ① 槽位结构（数字前缀 = 注入，`_` 前缀 = 归档不注入）
  ② 预算（字数控制在蒸馏阶段，超预算必须可见）
  ③ **绝不截断**（最后一篇的内容必须在返回值里 —— 这一条直接防旧 bug 回归）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / 'knowledge' / '色色参考'
SLOTS = ['00-核心', '10-风格', '20-技法']


def _reader_output():
    """真调一次读取器，拿返回串。"""
    from agent.handler import MessageHandler
    h = object.__new__(MessageHandler)

    class _K:
        knowledge_dir = str(ROOT / 'knowledge')

    h.knowledge = _K()
    return h._seductive_knowledge()


def test_slot_dirs_exist():
    """数字前缀的目录 = 注入槽位。"""
    for s in SLOTS:
        d = PKG / s
        assert d.is_dir(), f'缺失槽位目录 {s}/'
        assert list(d.glob('*.md')), f'{s}/ 里没有 .md —— 槽位是空的'


def test_archive_dir_exists_and_is_not_a_slot():
    """`_原文/` 是蒸馏溯源归档，不注入。"""
    a = PKG / '_原文'
    assert a.is_dir(), '缺失 _原文/ 归档 —— 蒸馏前的原文必须留着可查'
    assert len(list(a.glob('*.md'))) >= 5, '_原文/ 不足 5 篇，原文可能被删了'
    assert not a.name[:1].isdigit(), '_原文 不能是数字前缀（否则会被当槽位注入）'


def test_output_contains_every_slot():
    """每个槽位的内容都要在返回串里 —— 一个都不能被跳过。"""
    out = _reader_output()
    for s in SLOTS:
        files = sorted((PKG / s).glob('*.md'))
        # 用每个文件首个小节的第一个非空行做指纹
        for f in files:
            lines = [l.strip() for l in f.read_text(encoding='utf-8').splitlines()]
            body = [l for l in lines if l and not l.startswith('#')]
            assert body, f'{f.name} 没有正文'
            probe = body[0][:18]
            assert probe in out, (
                f'{f.name} 的内容不在注入结果里 —— 槽位 {s} 被跳过了？')


def test_no_truncation_last_slot_survives():
    """**核心回归闸门**：排在最后的内容必须完整在场。

    旧实现的病就是「排在后面的整篇消失」。这条直接钉住它。
    """
    out = _reader_output()
    last_slot = sorted(PKG.iterdir())
    last_slot = [d for d in last_slot if d.is_dir() and d.name[:1].isdigit()][-1]
    files = sorted(last_slot.glob('*.md'))
    last_file = files[-1]
    tail = last_file.read_text(encoding='utf-8').rstrip()
    # 用文件最末尾的一段做指纹（旧 bug 恰好会丢掉这个位置）
    tail_probe = tail[-40:].replace('\n', ' ').strip()
    assert tail_probe and tail_probe[:20] in out, (
        f'最后一个文件 {last_file.name} 的结尾不在注入结果里 —— **又发生了截断**')


def test_no_hard_truncation_in_source():
    """**源码里不许再有数字切片截断**（旧病是 [:6000]）。

    判据用 **AST 而不是文本匹配**，且只在**内容量级**的切片上报警：
      ① 第一版写成 `'[:6000]' not in body` —— 把我 **docstring 里解释旧行为的句子**
         也算成违规（假红）。文本里提到截断写法 != 代码里真的在截断。
      ② 第二版用 AST 但没设下限 —— 把 `d.name[:1].isdigit()`（取首字符）也报了。
         那合法且常见；真会丢内容的是四位数以上的那些。
    所以判据是：AST 里的 Subscript + 上界 >= 100。
    """
    import ast as _ast
    CUT = 100  # 内容量级切片的下限；低于这个是取首字符之类的特判
    tree = _ast.parse((ROOT / 'agent' / 'handler.py').read_text(encoding='utf-8'))
    fn = None
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            if node.name == '_seductive_knowledge':
                fn = node
                break
    assert fn is not None, '找不到 _seductive_knowledge（改名了？请更新本测试）'
    offenders = []
    for node in _ast.walk(fn):
        if not isinstance(node, _ast.Subscript):
            continue
        sl = node.slice
        if not isinstance(sl, _ast.Slice):
            continue
        upper = sl.upper
        if isinstance(upper, _ast.Constant) and isinstance(upper.value, int):
            if upper.value >= CUT:
                offenders.append(f'第 {node.lineno} 行 [: {upper.value}]')
    assert not offenders, (
        '代码里出现内容量级的切片截断（会静默丢内容）: ' + '; '.join(offenders))

def test_slot_budgets_are_declared_and_respected():
    """预算必须声明；超了要能被发现（不是悄悄超）。"""
    from agent.handler import SEDUCTIVE_SLOT_BUDGETS, SEDUCTIVE_TOTAL_BUDGET
    total = 0
    for s, budget in SEDUCTIVE_SLOT_BUDGETS.items():
        d = PKG / s
        if not d.is_dir():
            continue
        n = sum(len(p.read_text(encoding='utf-8'))
                for p in d.glob('*.md'))
        total += n
        assert n <= budget, (
            f'槽位 {s} 有 {n} 字，超出预算 {budget} —— 请继续蒸馏（不要调大预算）')
    assert total <= SEDUCTIVE_TOTAL_BUDGET, (
        f'注入总量 {total} 字超出总预算 {SEDUCTIVE_TOTAL_BUDGET} —— 请继续蒸馏')


def test_archive_content_is_not_injected():
    """归档里的东西绝不能出现在注入结果里。"""
    out = _reader_output()
    arch = PKG / '_原文'
    # 归档独有、蒸馏稿刻意排除的三处
    for probe in ('CG系统说明', '睡奸与醉奸——趁她熟睡时'):
        assert probe not in out, f'归档内容 {probe!r} 泄漏进了注入结果'


def test_distillation_note_records_exclusions():
    """两条故意排除必须有书面理由 —— 否则后人会当成遗漏。"""
    note = (PKG / '_原文' / '蒸馏说明.md').read_text(encoding='utf-8')
    assert '故意不蒸馏' in note
    assert 'CG系统说明' in note
    assert '搜索触发词' in note
    assert '206' in note, '蒸馏说明里应记录覆盖审计的条目数'
