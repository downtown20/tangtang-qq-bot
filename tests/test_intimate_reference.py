# -*- coding: utf-8 -*-
"""T2.14 回归：亲密模式范本走独立模块 + pinned 投递（不进背景池）。

背景（为什么要有这个测试）：
  2026-10-16 取证：色色教材 11,529 字，走的是通用背景块框架——
  ① 交付前被 knowledge_block() 的 [:1500] 砍掉 86.3%；
  ② 再作为 知识库=P15（全表最低优先级）进 1200t 公共池；
  ③ 实测 06:21–07:02 的 33 条色色回合，**33/33 被整块淘汰** —— 一个字都没到 LLM。
  而当时**没有任何测试断言这个块过 fit_backgrounds 后还在场**，所以它一直「绿」。

本文件钉住四件事：
  ① 加载侧全读、不截断（独立模块）；
  ② **交付侧**：pinned 的范本在背景池饱和时依然**全文在场**（核心回归闸门）；
  ③ **阳性对照**：把它按旧方式挂成普通背景块 → 同一个判据必须失败（证明闸门会红）；
  ④ 失败关闭：群聊路径不许挂载；归档（_原文/）**永不进检索**（2026-10-23 主人定案）。
"""
import asyncio
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agent import intimate_reference as IR          # noqa: E402
from agent import protocols as P                    # noqa: E402

PKG = ROOT / 'knowledge' / '色色参考'


def _reference_text():
    return IR.load(ROOT / 'knowledge').text


def _tail_probe() -> str:
    """最后一个槽位最后一个文件的结尾——旧 bug 恰好丢这个位置。"""
    slots = [d for d in sorted(PKG.iterdir()) if d.is_dir() and d.name[:1].isdigit()]
    last_file = sorted(slots[-1].glob('*.md'))[-1]
    tail = last_file.read_text(encoding='utf-8').rstrip()
    return tail[-30:].replace(chr(10), ' ').strip()


def test_module_reads_every_slot_file():
    ref = IR.load(ROOT / 'knowledge')
    assert ref.chars > 8000, f'范本只有 {ref.chars} 字，槽位可能没读全'
    for slot in ('00-核心', '10-风格', '20-技法'):
        assert ref.slots.get(slot), f'槽位 {slot} 没有内容'
    assert _tail_probe()[:16] in ref.text, '最后一个文件被截断了'


def test_format_block_keeps_everything_and_adds_its_own_caveat():
    body = _reference_text()
    block = IR.format_block(body)
    assert len(block) > len(body)
    assert block.rstrip().endswith(body.rstrip()[-200:]), 'format_block 截断了正文'
    big = '汉' * 20000
    assert IR.format_block(big).rstrip().endswith('汉' * 200), '两万字的输入被截断了'
    assert IR.format_block('') == ''


def test_delivery_path_has_no_content_scale_slice():
    """AST：交付链路上不许再出现内容量级的切片（旧病是 [:1500]）。"""
    CUT = 100
    targets = [
        (ROOT / 'agent' / 'intimate_reference.py', {'load', 'format_block'}),
        (ROOT / 'agent' / 'protocols.py', {'assemble_user_message'}),
    ]
    offenders = []
    for path, names in targets:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in names:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
                    up = sub.slice.upper
                    if isinstance(up, ast.Constant) and isinstance(up.value, int) and up.value >= CUT:
                        offenders.append(f'{path.name}:{sub.lineno} [:{up.value}]')
    assert not offenders, '交付链路上出现内容量级截断：' + '; '.join(offenders)


def _saturated_backgrounds():
    """构造一个必然饱和的背景池（复刻生产：多个 300~600t 的块争 1200t）。"""
    filler = '这是用来占预算的填充材料。' * 40      # 约 560 字 ≈ 700t
    return [('记忆', filler), ('自忆', filler), ('反思', filler),
            ('窗口', filler), ('画像', filler), ('关系档案', filler)]


def test_pinned_reference_survives_a_saturated_pool():
    """**核心回归闸门**：背景池再怎么挤，pinned 的范本必须全文在场。"""
    body = IR.format_block(_reference_text())
    fitted = P.fit_backgrounds(_saturated_backgrounds())
    msg = P.assemble_user_message(fitted, '在吗？',
                                  pinned=[(P.INTIMATE_REFERENCE_LABEL, body)])
    probe = _tail_probe()[:16]
    assert probe in msg, '范本的结尾不在最终消息里 —— pinned 又被裁了'
    assert body[:200] in msg, '范本的开头不在最终消息里'
    assert msg.rstrip().endswith('在吗？'), '用户原文必须仍在最后'
    assert msg.index(body[:200]) < msg.index('在吗？'), '范本必须在用户原文之前'


def test_old_mounting_would_lose_it():
    """**阳性对照**：按旧方式（普通背景块 + knowledge_block）挂 → 同一个判据必然失败。

    这条保证上面那条闸门是「会红的闸门」而不是装饰：它复刻的正是 2026-10-16 的生产行为。
    """
    body = IR.format_block(_reference_text())
    # 复刻 T2.14 之前的 knowledge_block()：检索用 caveat + [:1500] 封顶 + 标签「知识库」
    old_caveat = ('以下内容是你被问到时可以参考的真实信息。不确定就说「我不确定这个」。' + chr(10))
    old_style = _saturated_backgrounds() + [('知识库', old_caveat + body[:1500])]
    fitted = P.fit_backgrounds(old_style)
    msg = P.assemble_user_message(fitted, '在吗？')
    probe = _tail_probe()[:16]
    assert probe not in msg, (
        '旧挂法居然没丢内容？那说明这个复刻不再等价于生产行为，闸门失效了——请重新取证')


EXPECTED_PUBLIC_API = {'Reference', 'package_dir', 'discover_slot_files', 'load', 'format_block'}
ARCHIVE_DIR = PKG / '_原文'


def _archive_probes() -> dict:
    """每篇归档的指纹：首个非空正文行前 24 字（在原文里精确出现、又足够独特）。"""
    probes = {}
    for f in sorted(ARCHIVE_DIR.glob('*.md')):
        for line in f.read_text(encoding='utf-8').splitlines():
            s = line.strip().lstrip('#').strip()
            if len(s) >= 8 and not s.startswith('>') and '蒸馏' not in s:
                probes[f.name] = s[:24]
                break
    return probes


def test_archive_stays_out_of_the_payload():
    """归档（_原文/）只读、不进常驻注入——2026-10-23 主人定案：**也不进检索**。"""
    text = _reference_text()
    assert 'CG系统说明' not in text, '归档内容泄漏进常驻注入'
    assert '睡奸与醉奸' not in text, '被蒸馏刻意排除的场景出现在注入里'
    probes = _archive_probes()
    assert len(probes) >= 5, '归档指纹不足 5 条（原文被删了？）'
    leaked = [n for n, probe in probes.items() if probe in text]
    assert not leaked, '归档内容进了常驻注入: ' + repr(leaked)


def test_archive_retrieval_is_never_wired():
    """公开接口**冻结**：色色模块里不许长出任何读 _原文/ 的入口。

    2026-10-23 主人定案「_原文 不允许检索」。此前是「待拍板」，所以旧闸门只写了
    not hasattr(IR, 'search_archive')——它只能拦住同一个名字，换个名字就绕过去了。
    这里改成白名单：模块级函数/类多出任何一个就红，逼着新增入口的人先重新拍板。
    """
    src = (ROOT / 'agent' / 'intimate_reference.py').read_text(encoding='utf-8')
    api = {
        node.name for node in ast.parse(src).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    extra = api - EXPECTED_PUBLIC_API
    missing = EXPECTED_PUBLIC_API - api
    assert not extra, (
        '色色模块新增了公开入口 ' + repr(sorted(extra)) + '：_原文/ 不允许检索是主人定案，'
        '新增读归档的入口必须重新拍板并同步本白名单与末尾注释的三处闸门')
    assert not missing, '色色模块少了 ' + repr(sorted(missing)) + '——接口被改名/删除，请同步本闸门'


def test_archive_probe_would_catch_a_leak(tmp_path):
    """**阳性对照**：把归档塞进槽位 → 同一套指纹判据必须命中，否则上面全是假绿。"""
    pkg = tmp_path / '色色参考'
    (pkg / '00-核心').mkdir(parents=True)
    (pkg / '_原文').mkdir()
    sentinel = 'ARCHIVE_LEAK_SENTINEL_甲'
    (pkg / '00-核心' / '正常槽位.md').write_text(sentinel + ' 内容', encoding='utf-8')
    (pkg / '_原文' / '原文.md').write_text('原文内容_乙 ' * 20, encoding='utf-8')
    text = IR.load(tmp_path).text
    assert sentinel in text, '槽位加载器根本没读文件——「零泄漏」断言会是永久假绿'
    assert '原文内容_乙' not in text, '_原文/ 被当成数字槽位读了'


def test_every_knowledge_read_surface_skips_the_archive():
    """七条真实读面全过一遍：加载 / 发现 / 按名解析 / 索引 / 检索 / 读文档工具。

    判据是行为不是文本：拿真实 knowledge/ 目录跑一遍，归档指纹一处都不许出现。
    """
    from agent import knowledge as K
    from agent.handler import MessageHandler

    probes = _archive_probes()
    assert probes, '没取到归档指纹，本闸门会永远绿'
    kdir = ROOT / 'knowledge'
    surfaces = {
        'intimate_reference.load': IR.load(kdir).text,
        'discover_slot_files': '|'.join(str(x) for x in IR.discover_slot_files(PKG)),
        'discover_document_files': '|'.join(str(x) for x in K.discover_document_files(kdir)),
        'resolve_document_path': repr([
            str(K.resolve_document_path(kdir, Path(name).stem)) for name in probes
        ]),
    }
    kb = K.KnowledgeBase(str(kdir))
    surfaces['KnowledgeBase.chunks'] = '|'.join(c.source for c in kb.chunks)
    for query in ('CG系统说明', '睡奸与醉奸', '千恋万花', '中文百合BDSM'):
        surfaces['search:' + query] = kb.search(query)
    for name in list(probes) + [Path(n).stem for n in probes]:
        _ok, content = asyncio.run(MessageHandler._load_document_content(None, name))
        surfaces['read_document:' + name] = str(content)
    leaked = {}
    for surface, text in surfaces.items():
        hit = [name for name, probe in probes.items() if probe in str(text)]
        if hit:
            leaked[surface] = hit
    assert not leaked, '_原文/ 被检索到了: ' + repr(leaked)


def test_sensitive_dirname_is_single_sourced():
    """包名与敏感目录集合**单源**：改包名不能漏改一处，否则 _原文/ 整目录进索引。"""
    from agent import knowledge as K
    assert IR.PACKAGE_DIRNAME in K.SENSITIVE_DIRECTORY_NAMES, (
        'PACKAGE_DIRNAME 不在 SENSITIVE_DIRECTORY_NAMES 里——_原文/ 会随全库索引被检索')
    src = (ROOT / 'agent' / 'knowledge.py').read_text(encoding='utf-8')
    assert '"色色参考"' not in src, (
        'knowledge.py 里又出现了包名字面量：两处各写一份，改包名时必漏一处，'
        '漏的那次就是 _原文/ 静默进检索（应从 intimate_reference.PACKAGE_DIRNAME 取）')



def test_group_path_only_mounts_when_pack_allows_it():
    """群聊范本契约（2026-10-26 变更）：**默认不挂载，按包的 allow_groups 开启**。

    变更原因：主人明确要求「群聊可以注入」。当时的 fail-closed 理由是
    当时的默认「群聊一律不挂」对**自己显式挂了群场景的部署者**太保守，
    所以决定权从代码移到配置（`allow_groups`，默认 False = 与改之前一致）。

    这条测试守的是「默认关」这个性质，不是「永远关」：
      · 群聊路径必须有 allow_groups 守卫，且判据/加载都在守卫内
      · 私聊路径**不许**出现 allow_groups（私聊不受这个开关影响）

    ⚠ 历史：这里先后用过两种弱写法——「标签常量出现 1 次」「pinned.append 只有 1 处」，
    两次都被合法改动打红。教训 #36：别数名字，要守**性质**。
    """
    src = (ROOT / 'agent' / 'handler.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    seen_group = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.get_source_segment(src, node) or ''
        if node.name == 'handle_group_message':
            seen_group = True
            assert 'allow_groups' in body, '群聊挂载缺少 allow_groups 守卫（默认关被破坏）'
            assert 'pinned.append(' in body, '群聊应当有挂载点（受守卫保护）'
            # 守卫必须在挂载之前：先判 allow_groups 再 append
            assert body.index('allow_groups') < body.index('pinned.append('), \
                '群聊挂载写在了 allow_groups 判断之前（等于无条件挂载）'
        elif node.name == 'handle_private_message':
            assert 'allow_groups' not in body, '私聊不该受 allow_groups 影响'
            assert 'pinned.append(' in body, '私聊挂载点丢了'
    assert seen_group, 'handle_group_message 没找到——测试自己失效了'
