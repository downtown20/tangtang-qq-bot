"""T2.10：日记选段**不掷骰子**——她想不想提、提哪一件，由她决定。

背景（2026-10-16 夜，减负轮）
──────────────────────────
`AutonomyMixin._get_diary_fragment` 旧实现在「最近 5 条经验」里 `random.choice` 抽一条，
注入「糖糖突然想起自己最近的一件事：X」。系统替她决定了**想起的是哪一件**——
而这是「她自己的事」，没有任何理由交给随机数（与 T2.6/T2.7/T57 同一个病：系统替她做决定）。

现在改为把候选（最多 3 条、每条 ≤100 字）一起给她，提不提、提哪条由她说；
小时冷却与 `force` 语义不变。
"""

from __future__ import annotations

import ast
import time
import types
from pathlib import Path

from agent.handler_autonomy import AutonomyMixin

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "agent" / "handler_autonomy.py"


def _random_attrs(src: str, fn_name: str) -> set[str]:
    """扫描某个函数体里所有 `random.<attr>` 用法。"""
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    return {n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "random"}


def _fake(experiences=None, last=0.0):
    """只需要这条路径真正会碰到的两个属性。"""
    return types.SimpleNamespace(
        self_state=types.SimpleNamespace(
            self_narrative=types.SimpleNamespace(
                recent_experiences=list(experiences or [])),
        ),
        _last_diary_time=last,
    )


def test_scanner_detects_a_planted_dice():
    """阳性对照（教训 #30）：种一个 random.choice 进去，扫描器必须报出来。"""
    planted = "def _get_diary_fragment(self):\n    return random.choice([1, 2])\n"
    assert _random_attrs(planted, "_get_diary_fragment") == {"choice"}


def test_source_has_no_dice_in_the_diary_fragment():
    used = _random_attrs(SRC.read_text(encoding="utf-8"), "_get_diary_fragment")
    assert used == set(), (
        f"日记选段又掷骰子了：random.{sorted(used)} —— 提哪件事是她的决定，"
        "不是骰子的（见本文件 docstring）")


def test_all_candidates_are_offered_and_none_is_chosen():
    exps = ["甲" * 40, "乙" * 40, "丙" * 40, "丁" * 40]
    out = AutonomyMixin._get_diary_fragment(_fake(exps), "在聊晚饭")
    for e in exps[-3:]:
        assert e[:100] in out, f"候选被系统过滤掉了：{e[:10]}…"
    assert exps[0][:100] not in out, "只该给她最近 3 条，不是全部"
    assert "都不合适就不提" in out, "没把「提不提」的决定权交出去"
    assert "不要编" in out, "缺「材料外不编造」契约（教训 #22）"


def test_empty_when_no_material():
    assert AutonomyMixin._get_diary_fragment(_fake([]), "") == ""


def test_short_material_falls_back_rather_than_giving_up():
    """旧兜底语义**不变**：过滤后为空就退回「最近 3 条里非空的」。

    本轮只动「谁来选」，不动「有没有材料」——最小改动（教训 #3/#5）。
    """
    out = AutonomyMixin._get_diary_fragment(_fake(["太短"]), "")
    assert "太短" in out


def test_hourly_cooldown_and_force_still_work():
    f = _fake(["x" * 40], last=time.time())
    assert AutonomyMixin._get_diary_fragment(f, "") == ""
    out = AutonomyMixin._get_diary_fragment(f, "", force=True)
    assert "x" * 40 in out
    assert abs(f._last_diary_time - time.time()) < 5, "冷却时间必须被推进"


def test_injection_size_is_bounded():
    out = AutonomyMixin._get_diary_fragment(_fake(["汉" * 140] * 3), "")
    assert out.count("汉") <= 300, "注入上界是 3 条 × 100 字（别挤掉别人的材料）"