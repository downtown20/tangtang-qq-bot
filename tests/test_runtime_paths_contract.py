# -*- coding: utf-8 -*-
'''T55-F 契约闸门：运行时状态与日志的落盘位置只有一个来源。

2026-10-15 根目录规范化把 14 个运行时状态 json 与 15 个 tangtang.log* 收进「状态/」
与「日志/」。这不是风格问题——CLAUDE.md 教训 #15 的原形是「持久化做了一半，重启清零」
（主动私聊冷却全内存态 → 重启 7 次骚扰同一人 7 次）。**漏掉任何一个加载点 = 那份状态
在重启后静默清零**，所以本文件把「位置只有一个来源」钉成机器闸门：

1. agent.paths.state_path() / log_path() 是唯一入口，且目录自动创建；
2. 全仓 .py 里**不允许**再出现裸的根状态文件名字面量（只有交给 helper、文档字符串、
   名字清单、显式基址拼接这几种形态放行）；
3. memory.db / config.yaml / .env **必须留在项目根**（防后人误移）；
4. .stignore **不许把「状态/」整目录忽略**——「状态跟随」清单里那 7 个文件是特意要
   同步的，目录级忽略 = 笔记本上的糖糖丢状态。

教训 #30「检查器必须做阳性对照」落在 TestPositiveControl：真种样本、确认抓到、再删掉。
只测「跑起来没报错」等于没测——它本来就不会报错。
'''
import ast
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent import paths as paths_mod  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
# 1. helper 本身——唯一入口，且目录自动创建
# ═══════════════════════════════════════════════════════════════════

def test_state_dir_and_log_dir_are_named_and_rooted():
    assert paths_mod.STATE_DIR == paths_mod.PROJECT_ROOT / '状态'
    assert paths_mod.LOG_DIR == paths_mod.PROJECT_ROOT / '日志'


def test_state_path_lands_under_state_dir_and_creates_it(tmp_path, monkeypatch):
    target = tmp_path / '状态'
    monkeypatch.setattr(paths_mod, 'STATE_DIR', target)
    monkeypatch.setattr(paths_mod, '_ENSURED_DIRS', set())
    assert not target.exists()
    p = paths_mod.state_path('x.json')
    assert p == target / 'x.json'
    assert target.is_dir(), 'state_path 必须顺手把目录建出来'


def test_state_path_recreates_a_directory_that_vanished(tmp_path, monkeypatch):
    '''目录被人删掉之后不能静默写不进去——这正是教训 #15 的形状。'''
    import shutil
    target = tmp_path / '状态'
    monkeypatch.setattr(paths_mod, 'STATE_DIR', target)
    monkeypatch.setattr(paths_mod, '_ENSURED_DIRS', set())
    paths_mod.state_path('a.json')
    shutil.rmtree(target)
    paths_mod.state_path('b.json')
    assert target.is_dir()


def test_log_path_defaults_to_tangtang_log_under_log_dir(tmp_path, monkeypatch):
    target = tmp_path / '日志'
    monkeypatch.setattr(paths_mod, 'LOG_DIR', target)
    monkeypatch.setattr(paths_mod, '_ENSURED_DIRS', set())
    assert paths_mod.log_path() == target / 'tangtang.log'
    assert paths_mod.log_path('tangtang.log.2026-10-14') == target / 'tangtang.log.2026-10-14'
    assert target.is_dir()


# ═══════════════════════════════════════════════════════════════════
# 2. 真实消费方——各模块的常量必须落进「状态/」
# ═══════════════════════════════════════════════════════════════════

def test_runtime_state_constants_resolve_into_state_dir():
    """各模块的状态常量必须**源码形态**走 state_path()，且文件名正确。

    2026-10-15 事故后口径变更：这些常量的**运行时值在测试里一律是 tmp_path**——
    conftest 的 isolate_runtime_state_files（autouse）会把它们全部 monkeypatch 掉，
    好让「忘记隔离的测试」写不坏生产。既然运行时值被隔离，这里就只能断言源码形态
    ＋文件名。scheduler.TASKS_FILE 从 2026-08 起就是这条先例（见下方注释原文），
    现在把它扩到全部状态常量：
      · 「落点是否在 状态/ 下」由 state_path() 自己的契约测试钉住
        （test_state_path_lands_under_state_dir_and_creates_it）；
      · 「有没有走 state_path()」由这里的源码形态断言钉住；
      · 「测试会不会写生产」由 tests/test_runtime_state_isolation.py 钉住。
    """
    from agent import catch_up, conversation_tracker, daily_report, reflection, scheduler

    src_expectations = [
        ('agent/catch_up.py', 'STATE_FILE = str(state_path(".catch_up_state.json"))'),
        ('agent/catch_up.py', 'SENT_FILE = str(state_path(".catch_up_sent.json"))'),
        ('agent/conversation_tracker.py',
         'STATE_FILE = str(state_path(".conversation_state.json"))'),
        ('agent/daily_report.py', 'STATE_FILE = str(state_path(".daily_report_state.json"))'),
        ('agent/reflection.py', 'STATE_FILE = str(state_path(".reflection_state.json"))'),
        ('agent/scheduler.py', 'TASKS_FILE = str(state_path(".scheduled_tasks.json"))'),
    ]
    for rel, needle in src_expectations:
        src = (_ROOT / rel).read_text(encoding='utf-8')
        assert needle in src, f'{rel} 的状态常量没走 state_path()：{needle}'

    for actual, name in [
        (catch_up.CatchUpManager.STATE_FILE, '.catch_up_state.json'),
        (catch_up.CatchUpManager.SENT_FILE, '.catch_up_sent.json'),
        (conversation_tracker.ConversationTracker.STATE_FILE, '.conversation_state.json'),
        (daily_report.STATE_FILE, '.daily_report_state.json'),
        (reflection.ReflectionEngine.STATE_FILE, '.reflection_state.json'),
        (scheduler.TASKS_FILE, '.scheduled_tasks.json'),
    ]:
        assert Path(actual).name == name, f'{name} 的落点文件名被改动了'


def test_other_state_sites_resolve_into_state_dir():
    '''handler / health_check / main 的那几处——源码形态断言。'''
    from agent import self_state

    # 同 test_runtime_state_constants_resolve_into_state_dir：运行时值在测试里
    # 被 conftest 隔离成 tmp_path，所以这里断言源码形态 + 文件名。
    self_state_src = (_ROOT / 'agent' / 'self_state.py').read_text(encoding='utf-8')
    assert 'STATE_FILE = str(state_path(".tangtang_self.json"))' in self_state_src
    assert Path(self_state.TangTangSelf.STATE_FILE).name == '.tangtang_self.json'

    health_src = Path(_ROOT / 'agent' / 'health_check.py').read_text(encoding='utf-8')
    assert 'state_path(".extraction_state.json")' in health_src, 'health_check 的兜底默认值没走 helper'

    handler_src = (_ROOT / 'agent' / 'handler.py').read_text(encoding='utf-8')
    assert '_EXTRACTION_STATE_FILE = str(state_path(".extraction_state.json"))' in handler_src
    assert 'open(state_path(".fact_extraction_state.json")' in handler_src

    main_src = (_ROOT / 'main.py').read_text(encoding='utf-8')
    assert 'build_log_handlers(str(log_path()), sys.stdout)' in main_src


# ═══════════════════════════════════════════════════════════════════
# 3. AST 全仓扫描——不许再有裸字面量
# ═══════════════════════════════════════════════════════════════════

#: 根目录里**不该再出现**的运行时文件名（迁移前它们都在根上）
RUNTIME_FILE_NAMES = frozenset({
    '.birthday_sent.json', '.catch_up_sent.json', '.catch_up_state.json',
    '.conversation_state.json', '.daily_report_state.json', '.extraction_state.json',
    '.fact_extraction_state.json', '.greeting_state.json', '.mood_state.json',
    '.preferences.json', '.reflection_state.json', '.scheduled_tasks.json',
    '.tangtang_self.json', 'moments.json',
})

#: 日志名
LOG_FILE_NAMES = frozenset({'tangtang.log'})

_TARGETS = RUNTIME_FILE_NAMES | LOG_FILE_NAMES

#: 「基址写在代码里」的锚点——它们与状态名拼在一起就是旧的根相对写法
_ROOT_ANCHORS = frozenset({'PROJECT_ROOT', 'ROOT', 'BASE', 'BASE_DIR'})

#: 放行的形态。**每一个都要有理由**，理由写在下面，不许悄悄放行新形态。
_ALLOWED_CONTEXTS = frozenset({
    # 唯一正确形态：字面量交给 agent.paths 的 helper 解析
    'call:state_path',
    'call:log_path',
    # helper 自己的默认参数名（def log_path(name: str = 'tangtang.log')）
    'default',
    # 文档字符串里提到文件名——是散文，不是路径
    'docstring',
    # 「名字清单」：存名字不存路径，消费方逐项喂给 state_path()
    # （tools/体检.py 的 FOLLOW_STATE_FILES、tools/安装糖糖.py 的打包排除名单）
    'container:FOLLOW_STATE_FILES',
    'container:_EXCLUDE_NAMES',
    # 与**显式基址**拼接（tests 里 tmp_path / 'tangtang.log'）——
    # 基址写在代码里，结果不依赖 CWD，不是「根相对路径」
    'binop:explicit-base',
    # 源码文本断言（'x.json' in src），是字符串比较不是文件系统路径
    'compare',
})

#: 扫描时跳过的目录——第三方/依赖/备份/工具产物，都不是项目源码
_SKIP_DIRS = frozenset({
    '__pycache__', '.trash', '.pytest_cache', '.gitnexus', 'node_modules',
    '_backups', '_归档', 'venv', 'python310',
    '依赖', 'models', 'asr_models', 'assets', 'gpt-sovits', 'SnowLuma',
    'songs', 'stickers', 'stickers_cg', 'stickers_michele', 'stickers_murasame',
    'share_images', 'Imag', 'audio', 'voice_cache', 'generated_images',
    'backups', 'memory_sync',
})

#: 扫描必须真的覆盖到这些顶层目录——否则「零命中」只是「没扫」的同义词（教训 #30）
_MUST_VISIT = ('agent', 'tools', 'tests', 'onebot')


def _docstring_spans(tree):
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                first = body[0].value
                spans.append((first.lineno, first.end_lineno or first.lineno))
    return spans


def _container_name(parent, parents):
    grand = parents.get(parent)
    if isinstance(grand, ast.Assign):
        targets = [getattr(t, 'id', '?') for t in grand.targets]
    elif isinstance(grand, ast.AnnAssign):
        targets = [getattr(grand.target, 'id', '?')]
    else:
        targets = ['?']
    return 'container:' + ','.join(targets)


def _context_of(node, parents, doc_spans):
    parent = parents.get(node)
    if any(a <= node.lineno <= b for a, b in doc_spans):
        return 'docstring'
    if isinstance(parent, ast.Call):
        fn = parent.func
        return 'call:' + str(getattr(fn, 'id', None) or getattr(fn, 'attr', None))
    if isinstance(parent, ast.arguments):
        return 'default'
    if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
        return _container_name(parent, parents)
    if isinstance(parent, ast.BinOp):
        operands = {getattr(parent.left, 'id', None), getattr(parent.right, 'id', None)}
        return 'binop:root-anchor' if operands & _ROOT_ANCHORS else 'binop:explicit-base'
    if isinstance(parent, ast.Compare):
        return 'compare'
    if isinstance(parent, ast.JoinedStr):
        return 'fstring'
    if isinstance(parent, ast.Assign):
        return 'assign:' + ','.join(getattr(t, 'id', '?') for t in parent.targets)
    if isinstance(parent, ast.AnnAssign):
        return 'assign:' + str(getattr(parent.target, 'id', '?'))
    if isinstance(parent, ast.keyword):
        return 'kwarg:' + (parent.arg or '**')
    return 'other:' + type(parent).__name__


def _suffix_context(value, parents, doc_spans, node):
    '''收拾 f-string / 拼接形态：f"{BASE}/.catch_up_state.json"。

    只认「基址 + 分隔符 + 状态名」而且前缀**不是** 状态/ 或 日志/ 的写法——
    正常文档里写「状态/.catch_up_state.json」是允许的。
    '''
    for name in _TARGETS:
        for sep in ('/', chr(92)):
            tail = sep + name
            if not value.endswith(tail):
                continue
            prefix = value[: -len(tail)]
            if prefix.endswith('状态') or prefix.endswith('日志'):
                return ''
            parent = parents.get(node)
            if isinstance(parent, ast.JoinedStr):
                return 'fstring'
            if isinstance(parent, ast.BinOp):
                operands = {getattr(parent.left, 'id', None), getattr(parent.right, 'id', None)}
                return 'binop:root-anchor' if operands & _ROOT_ANCHORS else 'binop:explicit-base'
    return ''


def _violations_in_source(src, relpath):
    '''返回违规描述列表。空列表 = 这段源码干净。'''
    tree = ast.parse(src)
    lines = src.splitlines()
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    doc_spans = _docstring_spans(tree)

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value = node.value
        if value in _TARGETS:
            ctx = _context_of(node, parents, doc_spans)
        else:
            ctx = _suffix_context(value, parents, doc_spans, node)
            if not ctx:
                continue
        if ctx in _ALLOWED_CONTEXTS:
            continue
        line = lines[node.lineno - 1].strip()[:110] if node.lineno <= len(lines) else ''
        found.append(f'{relpath}:{node.lineno} {value!r} 以 [{ctx}] 形态出现 → {line}')
    return found


#: 扫描器自己的家——本文件必须**逐字**写出被禁的名字（它就是这个名单本身），
#: 所以从仓库扫描里豁免。豁免面只有一个文件，由
#: test_exemption_is_narrow_and_load_bearing 钉住（教训 #31：白名单要有边界）。
_SCANNER_SELF = Path(__file__).resolve()


def _iter_project_py():
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if not fn.endswith('.py'):
                continue
            p = Path(dirpath) / fn
            if p == _SCANNER_SELF:
                continue
            yield p


def _rel(p):
    return str(p.relative_to(_ROOT)).replace(chr(92), '/')


def test_exemption_is_narrow_and_load_bearing():
    '''豁免面必须窄到「就是本文件」——否则它就是一个能藏东西的天窗（教训 #31）。

    两条断言：① 本文件确实会命中（说明豁免**在起作用**，不是因为名单没生效）；
    ② 被豁免的集合恰好等于 {本文件}。
    '''
    src = _SCANNER_SELF.read_text(encoding='utf-8')
    assert _violations_in_source(src, _SCANNER_SELF.name), \
        '本文件居然不命中——豁免是假动作，仓库扫描通过的原因另有其他'
    scanned = {p.resolve() for p in _iter_project_py()}
    walked = set()
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        walked |= {Path(dirpath) / fn for fn in filenames if fn.endswith('.py')}
    exempt = {p.resolve() for p in walked} - scanned
    assert exempt == {_SCANNER_SELF}, f'豁免面不止本文件：{exempt}'


def test_repo_has_no_bare_root_relative_state_literals():
    scanned = list(_iter_project_py())
    rels = {p.relative_to(_ROOT).parts[0] for p in scanned}
    missing = [d for d in _MUST_VISIT if d not in rels]
    assert not missing, f'扫描没覆盖到 {missing}——「零命中」不能是「没扫」'
    assert len(scanned) >= 200, f'只扫到 {len(scanned)} 个 .py，扫描器多半坏了'

    violations = []
    for p in scanned:
        try:
            src = p.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        violations.extend(_violations_in_source(src, _rel(p)))
    assert not violations, '出现了裸的根相对状态/日志字面量：\n  ' + '\n  '.join(violations)


# ═══════════════════════════════════════════════════════════════════
# 4. 阳性对照 / 阴性对照——教训 #30
# ═══════════════════════════════════════════════════════════════════

class TestPositiveControl:
    '''检查器必须做阳性对照：种一个真样本进去，看它报不报。

    「跑起来没报错」不是证据——扫描器本来就不会报错（它连正则都没匹配上）。
    '''

    @pytest.mark.parametrize('snippet,name', [
        ('STATE_FILE = ".catch_up_state.json"', '.catch_up_state.json'),
        ('open(".conversation_state.json", "r", encoding="utf-8")', '.conversation_state.json'),
        ('p = Path(".tangtang_self.json")', '.tangtang_self.json'),
        ('LOG_FILE = "tangtang.log"', 'tangtang.log'),
        ('target = BASE / ".extraction_state.json"', '.extraction_state.json'),
        ('target = f"{PROJECT_ROOT}/.scheduled_tasks.json"', '.scheduled_tasks.json'),
    ])
    def test_scanner_catches_planted_shape(self, snippet, name):
        src = "from pathlib import Path\nBASE = Path('.')\nPROJECT_ROOT = Path('.')\n" + snippet + '\n'
        found = _violations_in_source(src, 'planted.py')
        assert found, f'阳性对照失败：种进去的 {snippet!r} 没被抓到'
        assert name in found[0]

    @pytest.mark.parametrize('snippet', [
        'STATE_FILE = str(state_path(".catch_up_state.json"))',
        'p = log_path()',
        "FOLLOW_STATE_FILES = ['.catch_up_state.json']",
        'path = tmp_path / "tangtang.log"',
    ])
    def test_scanner_does_not_flag_correct_forms(self, snippet):
        src = 'from pathlib import Path\n' + snippet + '\n'
        found = _violations_in_source(src, 'clean.py')
        assert not found, f'阴性对照失败：正确写法被误报 {found}'

    def test_real_planted_file_is_caught_end_to_end(self):
        '''真在仓库里种一个文件、跑同一个扫描路径、确认抓到，然后删掉。

        **文件名必须带 pid**：这是全仓扫描的阳性对照，而本仓经常同时跑两条 pytest
        （另一个工作流也在跑 tests/）。固定文件名会让两个进程互相删对方刚种下的样本，
        出现「种进去的文件没被 _iter_project_py() 覆盖」的假红——2026-10-15 实测过一次。
        唯一名之后，两个进程各种各的、各删各的，互不干扰。
        '''
        probe = Path(__file__).parent / ('_t55f_probe_positive_%d.py' % os.getpid())
        probe.write_text(
            '# -*- coding: utf-8 -*-\n'
            'from pathlib import Path\n'
            'CURSOR_FILE = ".reflection_state.json"\n'
            'def load():\n'
            '    return Path(".reflection_state.json").read_text(encoding="utf-8")\n',
            encoding='utf-8',
        )
        try:
            rel = _rel(probe)
            covered = {_rel(p) for p in _iter_project_py()}
            assert rel in covered, '种进去的文件没被 _iter_project_py() 覆盖——阳性对照无效'
            found = _violations_in_source(probe.read_text(encoding='utf-8'), rel)
            assert found, '种了真样本却没抓到——闸门是假的'
            assert '.reflection_state.json' in found[0]
        finally:
            probe.unlink(missing_ok=True)
        assert not probe.exists()


# ═══════════════════════════════════════════════════════════════════
# 5. 迁移完整性 / 不该动的东西 / 两机同步语义
# ═══════════════════════════════════════════════════════════════════

def test_migrated_state_files_are_no_longer_at_root():
    strays = sorted(n for n in RUNTIME_FILE_NAMES if (_ROOT / n).exists())
    assert not strays, ('根目录又出现了状态文件（旧进程写的？跑 '
                        '.trash/2026-10-15-t55f/sweep.py 并回去）：' + str(strays))
    assert not list(_ROOT.glob('tangtang.log*')), '根目录又出现日志文件'


def test_migrated_state_files_exist_under_state_dir():
    missing = sorted(n for n in RUNTIME_FILE_NAMES if not (paths_mod.STATE_DIR / n).exists())
    assert not missing, f'状态/ 里缺文件（迁移丢了数据）：{missing}'


@pytest.mark.parametrize('name', ['memory.db', 'config.yaml', '.env', 'main.py',
                                  'role_card.md', 'requirements.txt', 'CLAUDE.md'])
def test_path_contract_files_stay_at_root(name):
    '''这些动了就失效：SQLite 路径契约、dotenv 默认只读根、控制台固定根路径……'''
    assert (_ROOT / name).exists(), f'{name} 必须留在项目根（路径契约）'


def test_memory_db_path_contract_in_config():
    cfg = (_ROOT / 'config.yaml').read_text(encoding='utf-8')
    assert 'db_path: ./memory.db' in cfg, 'config.yaml 的 db_path 变了——记忆本体不能在根之外'


def test_stignore_does_not_blanket_ignore_state_dir():
    '''「状态/」不能整目录忽略：那 7 个「跟随」文件是特意要同步的。'''
    text = (_ROOT / '.stignore').read_text(encoding='utf-8')
    patterns = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith('//')]
    assert '状态/' not in patterns, (
        '把「状态/」整目录忽略会让笔记本上的糖糖丢掉私聊/群聊窗口、反思游标、偏好——'
        '同步规则要按文件写：状态/.tangtang_self.json 这样')
    for name in ['.tangtang_self.json', '.catch_up_state.json', '.scheduled_tasks.json',
                 '.extraction_state.json', '.mood_state.json']:
        assert f'状态/{name}' in patterns, f'.stignore 缺少「不同步」规则 状态/{name}'
    assert '日志/' in patterns, '.stignore 缺少 日志/ 规则'
    # 根级旧名字保留为「永不复活」守卫——2026-10-15 01:31:14 实测：规则一被删，
    # Syncthing 就把对端索引里的旧副本按原 mtime 拉了回来（syncthing.log 有据）。
    for name in ['.tangtang_self.json', '.catch_up_state.json', '.scheduled_tasks.json',
                 '.extraction_state.json', '.mood_state.json']:
        assert name in patterns, f'.stignore 缺少根级守卫 {name}（Syncthing 会把对端旧副本填回来）'
    for name in ['.conversation_state.json', '.reflection_state.json',
                 '.fact_extraction_state.json', '.birthday_sent.json',
                 '.catch_up_sent.json', '.greeting_state.json', '.preferences.json']:
        assert f'状态/{name}' not in patterns, f'{name} 是「跟随」文件，不该被 .stignore 挡掉'


def test_gitignore_ignores_the_two_new_dirs():
    text = (_ROOT / '.gitignore').read_text(encoding='utf-8')
    patterns = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith('#')]
    assert '状态/' in patterns, '.gitignore 必须以目录级规则忽略 状态/'
    assert '日志/' in patterns, '.gitignore 必须以目录级规则忽略 日志/'
    assert '.catch_up_state.json' not in patterns, '旧路径规则没清理干净'
    assert 'tangtang.log' not in patterns, '旧日志路径规则没清理干净'
