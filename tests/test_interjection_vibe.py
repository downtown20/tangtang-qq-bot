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
        monkeypatch.setattr("agent.interjection.random.randint", lambda a, b: 0)  # 去掉随机
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
        monkeypatch.setattr("agent.interjection.random.randint", lambda a, b: 0)
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
        monkeypatch.setattr("agent.interjection.random.randint", lambda a, b: 0)
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
        monkeypatch.setattr("agent.interjection.random.randint", lambda a, b: 0)
        ok, score, reason = eng.evaluate(
            "有人在吗？", "某人", "g1", intimacy=70,
        )
        # 15 + 30 + 15 + 25 = 85 ≥ 30（thirst 0.7 → 阈值 30）
        assert ok is True
        assert score == 85
