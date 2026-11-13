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
