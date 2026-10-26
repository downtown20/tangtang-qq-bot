# -*- coding: utf-8 -*-
"""测试不得写生产 状态/ —— 阳性对照闸门（2026-10-15 事故）。

## 事故

`tests/test_reflection_io.py` 走真实 `engine.maybe_reflect()`，而
`ReflectionEngine.STATE_FILE` 是**类属性**、import 时由
`state_path(".reflection_state.json")` 算死，指向生产
`状态/.reflection_state.json`。于是 pytest 把生产反思游标写成
「2026-10-15 13:05」，而 `agent/reflection.py:120` 的 20 小时门正是拿它
判断「该不该反思」——生产反思被推迟一整天。

实证链：① 该文件 mtime = 2026-10-15 13:05:06，正落在 pytest 跑批窗口内；
② 全量日志里 `🧘 开始反思整合` 最后一次出现在 **2026-10-12 22:32:08**，
   13:05 前后没有任何反思日志（成功反思必然先打这行）→ 那次写入不是反思。

同族的落点还有 TangTangSelf / ConversationTracker / CatchUpManager /
daily_report / scheduler —— 全都是「模块常量或类属性」，谁漏写
monkeypatch，谁就写生产。

## 本文件的作用

conftest 的 `isolate_runtime_state_files`（autouse）把这些落点默认改到
tmp_path。本文件是它的**阳性对照**（教训 #30：只测「跑起来没报错」等于没测）：

1. 先证明「本来确实会写生产」——默认落点就在 状态/ 下；
2. 再证明「隔离真的生效」——运行时落点全部不在 状态/ 下，而在系统临时目录里；
3. 最后跑一遍真实写入路径——游标真的落在了隔离目录里。

文件名与常量的对应关系由 tests/test_runtime_paths_contract.py 的源码形态断言
负责（本文件不重复登记文件名清单，避免在容器里出现裸状态名）。
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agent.paths import STATE_DIR, state_path


#: 与 conftest.isolate_runtime_state_files 一一对应（这里只登记「常量名」）
_ISOLATED_CONSTANTS = (
    "scheduler.TASKS_FILE",
    "reflection.ReflectionEngine.STATE_FILE",
    "self_state.TangTangSelf.STATE_FILE",
    "conversation_tracker.ConversationTracker.STATE_FILE",
    "catch_up.CatchUpManager.STATE_FILE",
    "catch_up.CatchUpManager.SENT_FILE",
    "daily_report.STATE_FILE",
)


def _resolve(dotted: str):
    """'reflection.ReflectionEngine.STATE_FILE' -> 运行时字符串值。"""
    from agent import catch_up, conversation_tracker, daily_report
    from agent import reflection, scheduler, self_state

    mods = {
        "scheduler": scheduler,
        "reflection": reflection,
        "self_state": self_state,
        "conversation_tracker": conversation_tracker,
        "catch_up": catch_up,
        "daily_report": daily_report,
    }
    parts = dotted.split(".")
    obj = mods[parts[0]]
    for p in parts[1:]:
        obj = getattr(obj, p)
    return obj


def test_default_paths_would_have_hit_production():
    """阳性对照之一：证明「本来就在生产」——否则下面那条断言是恒真的假绿。"""
    assert state_path(".reflection_state.json").resolve().parent == STATE_DIR.resolve()
    assert state_path(".tangtang_self.json").resolve().parent == STATE_DIR.resolve()


def test_isolation_redirects_every_state_constant():
    """阳性对照之二：隔离生效——所有状态常量都不再指向 状态/，而是临时目录。"""
    prod = STATE_DIR.resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    for dotted in _ISOLATED_CONSTANTS:
        actual = Path(str(_resolve(dotted))).resolve()
        assert actual.parent != prod, (
            f"{dotted} 仍指向生产状态目录 {prod}——"
            "conftest.isolate_runtime_state_files 漏了这个落点"
        )
        assert tmp_root in actual.parents, (
            f"{dotted} 没落到系统临时目录下（{actual}）——隔离用的不是 tmp_path"
        )


def test_reflection_cursor_write_lands_in_isolated_path():
    """阳性对照之三：真跑一遍写入路径，游标落在隔离目录里。"""
    from agent.reflection import ReflectionEngine

    engine = ReflectionEngine(
        llm_call=lambda *_a, **_k: None,
        self_state=SimpleNamespace(),
        store=SimpleNamespace(),
        min_interactions_before_reflect=1,
    )
    target = Path(engine.STATE_FILE).resolve()
    assert target.parent != STATE_DIR.resolve(), "游标落点没被隔离"
    if target.exists():
        target.unlink()

    engine._save_cursor(datetime(2026, 10, 15, 13, 5))

    assert target.exists(), "隔离落点应该被写出来"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "last_reflection": "2026-10-15 13:05"
    }
