# -*- coding: utf-8 -*-
'''T2.1 supplement regression: the vulture whitelist must stay SCOPED and EFFECTIVE.

Why this test exists (2026-10-14):
  The T2.1 census measured vulture's false-positive rate at 92.9% (222 of 239 production
  hits were alive-but-unreferenced-by-static-analysis). Unfiltered, that makes vulture
  useless as a ratchet - too noisy to spot a real regression in.

  The fix is vulture_whitelist.py, which declares ONLY the structurally-certain- alive
  categories from the census record (skill registration, Qt callbacks, instance
  attributes, rollback imports).

  The trap, measured: whitelisting ALL 315 names makes vulture permanently report 0 -
  which is indistinguishable from turning the gate off (lesson #30 variant: a checker
  that can never fire is worse than no checker, because it still reads as green).
  So this file pins BOTH directions: the whitelist must suppress the known-false
  categories AND must still let real dead code through.
'''
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WHITELIST = ROOT / 'tools' / 'vulture_whitelist.py'   # 2026-10-15 T55：随诊断脚本移入 tools/


def _agent_uses_qt():
    '''`agent/` 里还有没有 Qt 子类（PySide6 已于 2026-11-14 整体下线）。

    只有还存在 GUI 框架时，「框架回调名静态分析到不了 → 必须白名单」这条
    才成立。判据看的是**源码里的框架导入**，不是「Qt 曾在这」的记忆。
    '''
    pat = re.compile(r'^\s*(?:from|import)\s+(?:PySide6|PyQt5|PyQt6|tkinter)\b', re.M)
    return any(pat.search(p.read_text(encoding='utf-8', errors='ignore'))
               for p in (ROOT / 'agent').glob('*.py'))


def _vulture(*extra, confidence='60'):
    args = [sys.executable, '-m', 'vulture', 'agent/']
    args += [str(p) for p in extra]
    args += ['--min-confidence', confidence]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding='utf-8', errors='ignore', cwd=str(ROOT))
    return [l.strip() for l in r.stdout.splitlines() if l.strip().startswith('agent')]


class TestWhitelistScope:
    '''The whitelist declares only the four structurally-certain-alive categories.'''

    def test_whitelist_exists(self):
        assert WHITELIST.exists(), 'vulture_whitelist.py missing - vulture is unfiltered again'

    QT = {'paintEvent', 'mousePressEvent', 'mouseMoveEvent', 'mouseReleaseEvent',
          'sizeHint', 'wheelEvent', 'closeEvent', 'keyPressEvent', 'enterEvent',
          'leaveEvent', 'resizeEvent', 'showEvent', 'hideEvent', 'dropEvent'}

    def test_declares_only_known_categories(self):
        '''Every declared name must be verifiable against its own definition line.

        Note the first version of this test guessed the category from the NAME
        (e.g. 'leading underscore means instance attribute'). That was wrong:
        `active_conversations`, `total_tools`, `image_gen` are instance attributes
        WITHOUT an underscore. A name is not evidence of its kind - the definition
        line is. So this checks each name against the source.
        '''
        text = WHITELIST.read_text(encoding='utf-8')
        decls = re.findall(r'^(agent\.[\w.]+)\.(\w+)\s*(?:#.*)?$', text, re.M)
        assert decls, 'whitelist declares nothing'
        src = ROOT / 'agent'
        bad = []
        for mod, name in decls:
            fn = src / (mod.split('.', 1)[1] + '.py')
            if not fn.exists():
                bad.append('%s (module missing)' % mod)
                continue
            body = fn.read_text(encoding='utf-8', errors='ignore')
            is_skill = name.endswith('_skill')
            is_qt = name in self.QT
            is_attr = re.search(r'self\.' + re.escape(name) + r'\s*(:[^=]+)?=', body)
            # A name can legitimately be declared as a module-level binding: an import
            # alias, a class definition, or a re-export used only as a base class /
            # annotation. What makes it whitelistable is that the NAME IS BOUND in that
            # very module - not which binding form it uses.
            esc = re.escape(name)
            bound = re.search(
                # a class definition
                r'^class\s+' + esc + r'\b'
                # an import that mentions the name anywhere in the imported list
                # (catches `from x import A, Property, B` and `import x as _alias`)
                r'|^\s*(?:from\s+\S+\s+)?import\s+[^#\n]*\b' + esc + r'\b(?!\s*import)'
                # a module-level assignment / annotated assignment
                r'|^' + esc + r'\s*(?::[^=]+)?=',
                body, re.M)
            if not (is_skill or is_qt or is_attr or bound):
                bad.append('%s.%s (not bound in that module)' % (mod, name))
        assert not bad, 'whitelist entries not backed by their source: %s' % bad

    def test_covers_all_categories(self):
        text = WHITELIST.read_text(encoding='utf-8')
        decls = re.findall(r'^agent\.[\w.]+\.(\w+)\s*(?:#.*)?$', text, re.M)
        assert any(n.endswith('_skill') for n in decls), 'no skill-registration entry'
        # Qt 回调类于 2026-11-14 退休：PySide6 图形控制台整体下线，`agent/` 里
        # 已无任何 Qt 子类，也就不存在「框架会调、静态分析到不了」的回调方法。
        # 刻意写成**条件断言**而不是删掉——哪天 agent/ 重新引入 Qt（或别的 GUI
        # 框架），这条要求得自己回来。整条断言删掉的话，闸门会被「下线」顺手带成
        # 永久哑巴，那是教训 #30 的变体。
        if _agent_uses_qt():
            assert any(n in self.QT for n in decls), 'no Qt-callback entry'
        assert len(decls) >= 20, 'whitelist suspiciously small (%d)' % len(decls)

    def test_is_far_smaller_than_the_hit_set(self):
        '''Guards against the measured trap: whitelisting everything = gate off.'''
        bare = _vulture()
        declared = len(re.findall(r'^agent\.[\w.]+\.(\w+)$',
                                 WHITELIST.read_text(encoding='utf-8'), re.M))
        assert len(bare) > 100, 'baseline shrank unexpectedly (%d) - re-measure' % len(bare)
        assert declared * 8 < len(bare), (
            'whitelist declares %d of %d hits - too many; whitelisting nearly everything'
            ' makes vulture report ~0, which is the same as no gate (lesson #30)'
            % (declared, len(bare)))


class TestWhitelistEffect:
    '''It must actually suppress, and must actually still catch.'''

    @pytest.fixture(scope='module')
    def bare(self):
        return _vulture()

    @pytest.fixture(scope='module')
    def filtered(self):
        return _vulture(WHITELIST)

    def test_suppresses_something(self, bare, filtered):
        assert len(filtered) < len(bare), 'whitelist suppresses nothing'

    def test_still_reports_a_meaningful_remainder(self, filtered):
        '''If it ever reports 0, the gate has silently become a no-op.'''
        assert len(filtered) > 0, (
            'vulture now reports zero under the whitelist - the gate is effectively off')

    def test_planted_dead_function_is_still_caught(self):
        '''The positive control: real dead code must survive the whitelist.'''
        # 2026-10-15 T55：样本名必须唯一。原先用固定名 '_zz_planted_probe.py'，
        # 两条 pytest 并发跑时各自 unlink 掉对方刚种下的样本 → 假红「阳性对照无效」。
        # 同一缺陷本轮出现三次（工作流 F 的 test_runtime_paths_contract 也是这个模式，
        # 它改成 pid 唯一名后并发复跑 31/31 通过）——这里沿用同一 remedy。
        planted = ROOT / 'agent' / ('_zz_planted_probe_%d.py' % os.getpid())
        before = _vulture(WHITELIST)
        planted.write_text('def _zz_planted_probe_fn():' + chr(10)
                           + '    return 7' + chr(10), encoding='utf-8')
        try:
            after = _vulture(WHITELIST)
            # 只认**本次**种下的那个样本：vulture 扫的是整个 agent/，并发跑测时
            # 别家的探针会扰动全局计数——那是共享状态，不是这个测试该管的事。
            hits = [l for l in after if os.path.basename(planted).removesuffix('.py') in l]
            assert hits, (
                'a planted dead function was NOT caught under the whitelist -'
                ' the whitelist is over-suppressing (lesson #30)')
        finally:
            planted.unlink(missing_ok=True)
        assert not [l for l in _vulture(WHITELIST) if os.path.basename(planted).removesuffix('.py') in l]


class TestSnapshotWiring:
    '''The ratchet must consume the whitelist, not the raw output.'''

    def test_snapshot_passes_whitelist_to_vulture(self):
        src = (ROOT / 'tools' / '长期优化快照.py').read_text(encoding='utf-8')
        i = src.find('def collect_vulture')
        assert i > 0
        body = src[i:i + 1200]
        assert 'vulture_whitelist.py' in body, (
            'collect_vulture no longer references the whitelist - the ratchet number'
            ' would jump back to the unfiltered set')

    def test_snapshot_number_matches_filtered_run(self):
        '''The persisted count must equal the whitelist-filtered run at 90 confidence.'''
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_snap_for_test', str(ROOT / 'tools' / '长期优化快照.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        got = mod.collect_vulture()['total']
        want = len(_vulture(WHITELIST, confidence='90'))
        assert got == want, 'snapshot says %d, direct run says %d' % (got, want)
