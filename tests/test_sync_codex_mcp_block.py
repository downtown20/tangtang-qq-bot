# -*- coding: utf-8 -*-
'''T12 regression: tools/同步codex_mcp.py must not emit dead mcp_servers blocks.

2026-10-14 ledger T12. Before the fix, build_block() only branched on `cfg.get('url')`;
anything without a url fell into the else branch and produced `command = ""` even when
there was no command at all. main() only filtered by `isinstance(cfg, dict)`, so a config
like {'args': ['a']} was written into ~/.codex/config.toml as a dead server while the script
reported success.

A dead config is more expensive than an error: Codex recognises the server and fails to start
it, with nothing in the sync output saying so.
'''
import importlib.util
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TOOL = ROOT / 'tools' / '同步codex_mcp.py'


def _load():
    spec = importlib.util.spec_from_file_location('syncmcp_t12', TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestIsUsable:
    '''The usability predicate must be strict.'''

    @pytest.mark.parametrize('cfg,expected', [
        ({'url': 'http://x/mcp'}, True),
        ({'command': 'npx'}, True),
        ({'command': 'npx', 'args': ['-y', 'f']}, True),
        ({'url': 'http://x/mcp', 'headers': {'A': 'B'}}, True),
        ({}, False),
        ({'args': ['a']}, False),
        ({'env': {'K': 'V'}}, False),
        ({'url': ''}, False),
        ({'url': '   '}, False),
        ({'command': ''}, False),
        ({'command': '  '}, False),
        (None, False),
        ('nope', False),
        ([], False),
    ])
    def test_predicate(self, cfg, expected):
        assert _load().is_usable(cfg) is expected


class TestBuildBlockRejectsDegenerate:
    '''Degenerate configs must raise, not silently produce command = "".'''

    @pytest.mark.parametrize('cfg', [
        {}, 
        {'args': ['a']},
        {'env': {'K': 'V'}},
        {'command': ''},
        {'url': ''},
        None,
    ])
    def test_raises_value_error(self, cfg):
        m = _load()
        with pytest.raises(ValueError):
            m.build_block('weird', cfg)

    def test_never_emits_empty_command(self):
        '''Structural gate: the old dead-block shape must be unreachable.'''
        m = _load()
        for cfg in ({}, {'args': ['a']}, {'env': {'K': 'V'}}):
            try:
                out = m.build_block('x', cfg)
            except ValueError:
                continue
            assert 'command = ""' not in out, (
                'dead block produced for %r: %r' % (cfg, out))


class TestBuildBlockHappyPath:
    '''Valid configs must still render correctly (no regression).'''

    def test_url_block(self):
        out = _load().build_block('srv', {'url': 'http://x/mcp'})
        assert out.startswith('[mcp_servers.srv]')
        assert 'url = "http://x/mcp"' in out
        assert 'command' not in out

    def test_command_block(self):
        out = _load().build_block('srv', {'command': 'npx', 'args': ['-y', 'foo']})
        assert 'command = "npx"' in out
        assert 'args = ["-y", "foo"]' in out
        assert 'url' not in out

    def test_headers_and_env(self):
        out = _load().build_block('srv', {
            'url': 'http://x/mcp', 'headers': {'Authorization': 'Bearer t'},
            'env': {'KEY': 'VAL'},
        })
        assert 'http_headers' in out
        assert 'env' in out


class TestMainContract:
    '''main() must filter unusable entries and surface its result.'''

    def test_main_returns_bool(self):
        import inspect
        sig = inspect.signature(_load().main)
        assert sig.return_annotation is bool

    def test_main_filters_with_is_usable(self):
        '''Structural gate: main must consult is_usable before adding.'''
        import inspect
        src = inspect.getsource(_load().main)
        assert 'is_usable' in src, (
            'main() must filter unusable configs - otherwise dead blocks get written')
        assert 'unusable' in src, 'main() must report which entries were skipped'

    def test_script_exits_with_result(self):
        text = TOOL.read_text(encoding='utf-8')
        assert 'sys.exit(_rc)' in text
        assert 'if not main():' in text

    def test_ast_parses(self):
        ast.parse(TOOL.read_text(encoding='utf-8'))
