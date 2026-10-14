# -*- coding: utf-8 -*-
'''T24 regression: /人格 重载 must report its real scope.

2026-10-14. The command used to answer:

    ✅ 人格已从 role_card.md 重新加载～

That reads as 'everything is now live'. It is not. The reload re-reads two
DATA files (role_card.md, config.yaml). Python will not re-read an imported
module - there is no importlib.reload anywhere in this project - so changes to
protocols.py or personality.py (the prompt text and the prompt assembly code)
still need a process restart.

An operator who edits the prompt, runs /人格 重载, sees a green check, and then
measures behaviour would be measuring the OLD prompt while believing it was the
new one. That is the T10/T11/T12 family: the reported success is broader than
the actual effect.

This file pins the honest report and the reasoning that produces it.
'''
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler_commands import CommandRouter

_SRC = (Path(__file__).parent.parent / 'agent' / 'handler_commands.py').read_text(
    encoding='utf-8', errors='ignore')
_TREE = ast.parse(_SRC)


class _Personality:
    def __init__(self, ok=True):
        self._ok = ok
        self.reloaded = 0
        self.invalidated = 0
        self.config = SimpleNamespace(core='测试人设')

    def reload_role_card(self):
        self.reloaded += 1
        return self._ok

    def invalidate_cache(self):
        self.invalidated += 1


def _router(ok=True):
    p = _Personality(ok=ok)
    return CommandRouter(SimpleNamespace(personality=p)), p


def _run(ok=True):
    import asyncio
    router, p = _router(ok=ok)
    return asyncio.run(router._cmd_personality('owner', '重载')), p


class TestHonestReport:
    def test_success_message_names_what_was_reloaded(self):
        msg, _p = _run()
        assert 'role_card.md' in msg
        assert 'config.yaml' in msg

    def test_success_message_warns_code_changes_need_restart(self):
        '''The load-bearing assertion: the operator must be told the boundary.'''
        msg, _p = _run()
        assert '重启' in msg, (
            'the report hides the fact that code-level prompt changes need a restart')
        assert 'protocols.py' in msg and 'personality.py' in msg, (
            'the warning must name the files whose changes are NOT picked up')

    def test_old_unqualified_message_is_gone(self):
        '''The old text implied full application with no caveat.

        Uses AST, not text search: the T24 comment itself quotes the old message,
        so a substring check would match the explanation instead of the code.
        This is the same self-reference trap recorded in T2.2 - a checker matching
        its own documentation. AST ignores comments, so it sees only real strings.
        '''
        strings = [n.value for n in ast.walk(_TREE)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert '人格已从 role_card.md 重新加载' not in strings, (
            'the unqualified success message came back as a live string')

    def test_failure_path_unchanged(self):
        msg, p = _run(ok=False)
        assert 'role_card.md 不存在' in msg
        assert p.reloaded == 1
        assert p.invalidated == 0, 'must not invalidate the cache when the reload failed'

    def test_success_still_invalidates_cache(self):
        _msg, p = _run(ok=True)
        assert p.reloaded == 1
        assert p.invalidated >= 1, (
            'the prompt cache must be rebuilt after a successful reload')


class TestNoModuleReload:
    '''The premise of the warning: this project never reloads code at runtime.'''

    def test_no_importlib_reload_in_production_code(self):
        '''AST again, for the same reason: the T24 comment mentions importlib.reload
        while explaining that it does not exist anywhere.'''
        root = Path(__file__).parent.parent
        offenders = []
        for d in ('agent', 'onebot'):
            for p in (root / d).rglob('*.py'):
                try:
                    tree = ast.parse(p.read_text(encoding='utf-8', errors='ignore'))
                except SyntaxError:
                    continue
                # 精确指向 `importlib.reload(...)`——不是任何 `.reload()`。
                # 现场：`agent/handler.py:13123` 有 `manager.reload()`（场景重载），
                # 那是另一件事；用宽判据会把它误报成模块重载（我第一版就是这么错的）。
                for n in ast.walk(tree):
                    if (isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Attribute)
                            and n.func.attr == 'reload'
                            and isinstance(n.func.value, ast.Name)
                            and n.func.value.id == 'importlib'):
                        offenders.append(str(p.relative_to(root)))
                        break
        assert not offenders, (
            'a code reload appeared - the /人格 重载 warning may now be overstated:'
            ' %s' % offenders)
