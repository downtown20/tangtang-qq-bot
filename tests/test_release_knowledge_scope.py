# -*- coding: utf-8 -*-
"""T69 回归：发布快照里 knowledge/ 的范围——**发蒸馏稿，不发原文**。

主人 2026-10-23 定案（原话：「发布包不要把原文发出去就行了，把蒸馏的发出去」）。
这条定案之前的状态是**名存实亡**的守卫，取证见 docs/开发规划/长期优化/记录/T69-发布包剔除原文.md：

  · 2026-09-05 的裁决写的是「只发软色情 2 篇（丛雨对话范例/恋爱感氛围）」，
    而 tools/准备发布.py 的 KNOWLEDGE_IGNORE 是按**三个文件名**写死的；
  · T47（槽位化）/ T2.14（独立模块）改了目录结构并蒸馏改名之后，那三个名字
    **只存在于 _原文/ 里**，槽位侧的新名字一个都不在名单里；
  · 实测旧规则下快照会带 11 篇（槽位 9 + _原文 2 + 蒸馏说明）——
    「BDSM/官能技法不进公开」这条守卫早就不挡任何东西了。

修法不是再补几个文件名，而是按**前缀**判（与 agent/intimate_reference.py 的
ARCHIVE_PREFIX_BLOCK 同一条语义）：数字前缀 = 蒸馏槽位要发，下划线前缀 = 归档不发。

本文件钉四件事：
  ① 归档（_原文/，含蒸馏说明）一个文件都不进发布物；
  ② 蒸馏槽位 9 篇一篇不少（别为了「不发原文」把整个色色参考一起砍掉）；
  ③ **阳性对照**：同一套模拟器在不加忽略规则时必须能看见归档（否则①是假绿）；
  ④ 反向闸门：下划线前缀规则不许静默误伤——knowledge/ 下除归档目录外不该有下划线条目。
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
PREP_PATH = ROOT / 'tools' / '准备发布.py'
KNOWLEDGE = ROOT / 'knowledge'
PACKAGE = KNOWLEDGE / '色色参考'
ARCHIVE = PACKAGE / '_原文'
SLOTS = ('00-核心', '10-风格', '20-技法')


def _prep_module():
    """按路径加载构建脚本，拿它的真实 KNOWLEDGE_IGNORE（不复制一份规则）。"""
    spec = importlib.util.spec_from_file_location('prep_release_t69', PREP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_files(ignore, src=KNOWLEDGE):
    """复刻 shutil.copytree 的 ignore 语义，返回会进快照的相对路径集合。

    不真的建快照：发布脚本会往项目上级目录写东西，测试不许有那种副作用。
    """
    shipped = set()

    def walk(directory, prefix=''):
        names = sorted(p.name for p in directory.iterdir())
        ignored = set(ignore(str(directory), names))
        for name in names:
            path = directory / name
            rel = prefix + '/' + name if prefix else name
            if name in ignored:
                continue
            if path.is_dir():
                walk(path, rel)
            else:
                shipped.add(rel)

    walk(src)
    return shipped


def _release_ignore():
    """与 main() 里完全一致的合并口径（IGNORE_ALWAYS + KNOWLEDGE_IGNORE）。"""
    prep = _prep_module()

    def merged(directory, names):
        return {n for fn in (prep.IGNORE_ALWAYS, prep.KNOWLEDGE_IGNORE)
                for n in fn(directory, names)}

    return merged


def _archive_files():
    return {PACKAGE.name + '/_原文/' + p.name for p in sorted(ARCHIVE.glob('*.md'))}


def test_archive_never_ships():
    """原文（_原文/ 整目录）一个文件都不许进发布物。"""
    shipped = _snapshot_files(_release_ignore())
    leaked = [f for f in sorted(shipped)
              if '/_原文/' in f or f.endswith('/_原文')]
    assert not leaked, '发布物里出现了蒸馏前的原文: ' + repr(leaked)


def test_distillation_note_never_ships():
    """蒸馏说明.md（记录刻意排除项的那篇）也在归档里，同样不许发。"""
    shipped = _snapshot_files(_release_ignore())
    note = PACKAGE.name + '/_原文/蒸馏说明.md'
    assert not (note in shipped), '发布物带出了蒸馏说明（它把刻意排除的内容名称写入正文）'


def test_distilled_slots_all_ship():
    """蒸馏稿一篇都不能少——「不发原文」不等于「不发色色参考」。"""
    shipped = _snapshot_files(_release_ignore())
    missing = []
    for slot in SLOTS:
        for p in sorted((PACKAGE / slot).glob('*.md')):
            rel = PACKAGE.name + '/' + slot + '/' + p.name
            if rel not in shipped:
                missing.append(rel)
    assert not missing, '蒸馏稿被排除在发布物之外: ' + repr(missing)
    assert len([f for f in shipped if f.startswith(PACKAGE.name + '/')]) >= 9, (
        '发布物里的色色参考不足 9 篇——槽位可能整块被漏掉')


def test_simulator_would_catch_a_leak():
    """**阳性对照**：同一套模拟器在不加忽略时必须看得见归档，否则上面全是假绿。"""
    archive_files = _archive_files()
    assert archive_files, '归档目录里没有 .md —— 本闸门无从谈起（目录被搬走或清空了？）'
    naive = _snapshot_files(lambda directory, names: set())
    unseen = sorted(archive_files - naive)
    assert not unseen, (
        '模拟器看不见这些归档文件，所以「发布物里没有它」证明不了任何事: ' + repr(unseen))
    shipped = _snapshot_files(_release_ignore())
    assert not (archive_files & shipped), '忽略规则没挡住归档'


def test_no_other_underscore_resources_are_silently_dropped():
    """反向闸门：下划线前缀规则不许静默误伤别的知识库资源。

    规则是「_* 一律不发」，好处是不用再逐个猜文件名；代价是**将来有人把该发的
    资料命名成 _xxx 就会被静默剔除**（用户拿到的包里少了东西，没人会知道）。
    这条测试把「下划线条目」的清单钉住：出现新的就红，逼着改名或收窄规则。
    """
    allowed = {PACKAGE.name + '/_原文'}

    def _rel(path):
        # Windows 上 relative_to 给的是反斜杠；判据统一成 POSIX 分隔符。
        return str(path.relative_to(KNOWLEDGE)).replace('\\', '/')

    unexpected = [_rel(p) for p in KNOWLEDGE.rglob('*')
                  if p.name.startswith('_') and _rel(p) not in allowed]
    assert not unexpected, (
        '这些下划线前缀条目会被 "_*" 规则静默剔除，发布包里不会有它们: ' + repr(unexpected) +
        ' —— 若它们本该发布，请改名；若确实该剔除，把它们加进本测试的 allowed 并写明理由。')
