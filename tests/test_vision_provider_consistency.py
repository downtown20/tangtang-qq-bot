# -*- coding: utf-8 -*-
"""T42 回归：识图判据三处必须一致（2026-10-14）。

缺陷：T1 把识图默认从 local 改成 main，改了 **两处** 判据却漏了 `main.py` ——
`main.py` 仍写着「provider 默认 local 且 != local」，于是**选 main 会被误判成云端**：
一开识图就弹「识图选了云端…改回 local」，而 main 恰恰是推荐值、不需要任何 Key。

同一条判据散在三个文件里，改了两个漏了一个 —— 这是本仓第四次踩
「同一规则的多份拷贝漂移」（前三次：T10 两个发布扫描器 / T12 两处可用性判据 / T25 clean 两份字符类）。

另外 config.yaml 的 `enabled`/`provider` 也曾被改回 `false`/`api`，
所以本文件同时钉住配置侧的当前值。
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _src(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


class TestPredicateConsistency:
    """三处判据必须同口径：默认 main，且只有 api 才需要 Key。"""

    def test_handler_defaults_to_main(self):
        assert 'vision_cfg.get("provider", "main")' in _src('agent/handler.py'), (
            "handler 的 provider 默认值漂移了（应为 main）")

    def test_console_defaults_to_main_and_gates_on_api(self):
        s = _src('运维/糖糖控制台_qt.py')
        assert 'vision.get("provider", "main")) == "api"' in s, (
            "控制台的识图判据漂移了（应为默认 main 且 == api）")

    def test_main_py_defaults_to_main_and_gates_on_api(self):
        """这次修的那一处 —— main.py 曾漏改，导致选 main 弹「改回 local」的错误建议。"""
        s = _src('main.py')
        assert 'vision.get("provider", "main")) == "api"' in s, (
            "main.py 的识图判据又退回旧形状（默认 local / != local）")

    def test_no_stale_local_predicate_anywhere(self):
        """全局闸门：不许再有「拿 local 做比较」的识图判据。"""
        offenders = []
        for rel in ('agent/handler.py', '运维/糖糖控制台_qt.py', 'main.py'):
            for i, line in enumerate(_src(rel).splitlines(), 1):
                if 'vision' not in line.lower():
                    continue
                if re.search(r'provider[^\n]*[!=]=[^\n]*local', line):
                    offenders.append(f'{rel}:{i}')
        assert not offenders, (
            "旧判据（拿 local 比较）复活了：%s" % offenders)


class TestConfigMatchesDesign:
    """config.yaml 必须与 T1 的设计一致：识图开、走 main。"""

    def test_vision_enabled_and_main(self):
        cfg = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
        v = cfg['llm']['vision']
        assert v.get('enabled') is True, (
            "识图被关掉了（enabled=%r）—— 前端的「启用识图」会显示为关" % v.get('enabled'))
        assert str(v.get('provider')) == 'main', (
            "识图方式不是 main（provider=%r）—— 选 api 又没配 Key 会一路失败" % v.get('provider'))

    def test_console_offers_main_choice(self):
        """前端必须把 main 作为可选（且是第一个 = 默认推荐）。"""
        s = _src('运维/糖糖控制台_qt.py')
        m = re.search(r'"llm\.vision\.provider".*?\[(.*?)\]', s)
        assert m, '控制台没有 llm.vision.provider 的 choice 定义'
        options = [x.strip().strip('"\'') for x in m.group(1).split(',')]
        assert options[0] == 'main', (
            "main 不在第一个（=默认推荐位）：%s" % options)
        assert set(options) == {'main', 'local', 'api'}, options
