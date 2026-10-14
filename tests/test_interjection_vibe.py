"""
插话氛围感知修复测试（2026-08-15）

之前：handler 在 evaluate 外重新判定（score≥80 覆盖），
日志显示「分数不足 (86/90)」实际按 80 判——排查严重误导。
现在：vibe_bonus 与 threshold_override 传入 evaluate，判定与日志统一。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.interjection import InterjectionEngine


def _engine(thirst=0.1, cooldown=30):
    return InterjectionEngine(thirst=thirst, cooldown_seconds=cooldown)


class TestVibeBonus:
    def test_vibe_bonus_added_to_score_and_reason(self, monkeypatch):
        """氛围加成计入分数且出现在 reason——不再由外部拼接"""
        eng = _engine()
        # 2026-10-12 M3：随机项已删除——真实心境取确定性值，默认状态加成 0
        ok, score, reason = eng.evaluate(
            "有人在吗？", "某人", "g1", intimacy=50, vibe_bonus=10,
            threshold_override=80,
        )
        # 15 基础 + 30 问号 + 15 熟人 + 10 氛围 = 70 < 80
        assert ok is False
        assert score == 70
        assert "氛围感知(+10)" in reason
        assert "70/80" in reason  # 显示真实门槛，不再误导

    def test_threshold_override_hit(self, monkeypatch):
        """冷清群：+10 后达到 80 门槛 → 命中"""
        eng = _engine()
        # 2026-10-12 M3：随机项已删除——真实心境取确定性值，默认状态加成 0
        ok, score, reason = eng.evaluate(
            "今天这个问题有谁知道答案吗？在线等挺急的，谢谢大家了呀，拜托拜托",
            "某人", "g1", intimacy=70,
            vibe_bonus=10, threshold_override=80,
        )
        # 15 + 10 长消息 + 30 问号 + 15 熟人 + 25 亲密 + 10 氛围 = 105 ≥ 80
        assert ok is True
        assert "氛围感知(+10)" in reason

    def test_no_vibe_uses_thirst_threshold(self, monkeypatch):
        """无氛围加成 → 按 thirst 阈值（0.1 → 90），reason 显示真实门槛"""
        eng = _engine()
        # 2026-10-12 M3：随机项已删除——真实心境取确定性值，默认状态加成 0
        ok, score, reason = eng.evaluate(
            "有人在吗？", "某人", "g1", intimacy=50,
        )
        # 15 + 30 问号 + 15 熟人 = 60 < 90
        assert ok is False
        assert "60/90" in reason
        assert "氛围感知" not in reason

    def test_default_params_backward_compatible(self, monkeypatch):
        """旧调用方（不传 vibe/threshold）行为不变"""
        eng = _engine(thirst=0.7)
        # 2026-10-12 M3：随机项已删除——真实心境取确定性值，默认状态加成 0
        ok, score, reason = eng.evaluate(
            "有人在吗？", "某人", "g1", intimacy=70,
        )
        # 15 + 30 + 15 + 25 = 85 ≥ 30（thirst 0.7 → 阈值 30）
        assert ok is True
        assert score == 85

class TestRealMoodBonus:
    """2026-10-12 M3：真实心境替换 random.randint(-15, 15)（审计 P2）。

    改造前日志写「心情影响」而值是随机的——拿随机数冒充她的心情。
    现在按 mood/energy 的真实值算**确定性**加成，量程仍 ±15（不改评分尺度），
    且只调制分数、不接管「回不回」。
    """

    def _score(self, **kw):
        eng = _engine(thirst=0.1)
        return eng.evaluate("有人在吗？", "某人", "g1", intimacy=50, **kw)

    def test_default_state_is_the_zero_point(self):
        """默认状态（心情 70 / 精力 85）加成 0——换掉随机数**不改变基线频率**。"""
        _, base, _ = self._score()
        _, same, reason = self._score(mood=70, energy=85)
        assert same == base
        assert "心境" in reason

    def test_good_state_scores_higher_and_bad_state_lower(self):
        _, good, _ = self._score(mood=100, energy=100)
        _, neutral, _ = self._score(mood=70, energy=85)
        _, bad, _ = self._score(mood=0, energy=0)
        assert bad < neutral < good

    def test_bonus_is_clamped_to_the_old_random_range(self):
        """量程必须与旧 random.randint(-15,15) 一致——只换输入源，不改尺度。"""
        _, good, good_reason = self._score(mood=100, energy=100)
        _, neutral, _ = self._score(mood=70, energy=85)
        _, bad, bad_reason = self._score(mood=0, energy=0)
        assert good - neutral == 15, good_reason
        assert bad - neutral == -15, bad_reason

    def test_reason_carries_the_real_numbers(self):
        _, _, reason = self._score(mood=40, energy=100)
        assert "心情40" in reason and "精力100" in reason

    def test_module_has_no_randomness_left(self):
        """「随机数冒充心情」不许复活——查 AST，不查文本。

        第一版写成对整份源码 `"random" not in src`，结果被**自己的说明注释**误报
        （注释里解释了"替换 random.randint"）——检查器本身也会假阳，
        与教训 #30/#31 同族。改成只看看代码树。
        """
        import ast

        src = (Path(__file__).parent.parent / "agent" / "interjection.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "random" not in imported, "interjection.py 又 import 了 random"
        called = {
            getattr(node.func, "attr", getattr(node.func, "id", ""))
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        assert "randint" not in called, "interjection.py 又在调 randint——它会冒充她的心情"
