"""
驱动力引擎动力学测试 — 核心假设验证

2026-08-10 审计发现：释放量远小于积累量 + 触发后释放不匹配 + 负值语义被吞，
导致全部驱动力永久饱和顶格 1.00 → 无稀缺 → 无张力。
本测试钉死修复后的动力学契约：
1. 积累：tick 随时间上涨
2. 周期：触发(≥0.7) → 行动级释放 → 回落 <0.5 → 数小时后再次接近阈值
3. 负值语义：release(负数) = 反向强化（负面反馈）
4. 过冲：释放后短暂低于基线（满足感余波）
5. 匹配：自治循环的 ACTION_RELEASES 覆盖所有会主动发起的 dominant
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _new_system():
    from agent.drives import DriveSystem
    return DriveSystem()


def _tick_hours(ds, hours):
    """模拟真实运行：每 10 分钟 tick 一次（单次 tick 会被 4h cap 截断）。"""
    steps = int(hours * 6)  # 6 次/小时
    for _ in range(steps):
        ds.tick(dt_hours=1 / 6)


# ─────────────────────────────────────────
# 1. 积累
# ─────────────────────────────────────────

def test_tick_accumulates():
    ds = _new_system()
    ds.tick(dt_hours=1.0)
    assert ds.drives["social"].value > 0.0
    assert ds.drives["social"].value == pytest.approx(0.08, abs=0.01)


def test_tick_caps_at_4h():
    ds = _new_system()
    ds.tick(dt_hours=100.0)  # 长离线也不暴涨
    assert all(d.value <= 0.4 + 0.01 for d in ds.drives.values())


# ─────────────────────────────────────────
# 2. 触发 → 释放 → 回落的完整周期
# ─────────────────────────────────────────

def test_trigger_release_cycle():
    """0.7+ 触发 → 行动级释放 → 回落到 0.5 以下 → 数小时后再接近阈值。"""
    ds = _new_system()
    # 积累到触发（信息饥渴 alpha 0.06/h → 12 小时到 0.7+）
    _tick_hours(ds, 13.0)
    info = ds.drives["curiosity_info"]
    assert info.is_urgent, "13 小时后信息饥渴应超过阈值"

    # 模拟自治循环行动级释放（handler_autonomy ACTION_RELEASES 同款数值）
    from agent.handler_autonomy import ACTION_RELEASES
    for name, amount in ACTION_RELEASES["curiosity_info"]:
        ds.release(name, amount)

    assert info.value < 0.5, f"行动级释放后应回落 <0.5，实际 {info.value:.2f}"
    assert info.value > 0.1, f"不应归零——欲望仍在，实际 {info.value:.2f}"

    # 5 小时后应重新接近阈值（0.315 + 0.06*5 = 0.615）
    _tick_hours(ds, 5.0)
    assert info.value >= 0.6, f"5 小时后应重新接近阈值，实际 {info.value:.2f}"
    # 7 小时后应再次触发（0.615 + 0.06*2 = 0.735）
    _tick_hours(ds, 2.0)
    assert info.is_urgent, "7 小时后应重新达到触发阈值——有涨有落的周期"


def test_saturated_release_recovers():
    """饱和度 1.0 → 行动级释放 → 大幅回落（不再 1.00→1.00）。"""
    ds = _new_system()
    ds.drives["social"].value = 1.0  # 模拟历史遗留的饱和态
    for name, amount in [("social", 0.35), ("express", 0.15)]:
        ds.release(name, amount)
    assert ds.drives["social"].value < 0.65, "饱和态释放后必须大幅回落"


# ─────────────────────────────────────────
# 3. 负值语义（负面反馈）
# ─────────────────────────────────────────

def test_negative_release_strengthens():
    """release(负数) = 反向强化——被冷落让回避欲上升（原实现被 beta 吞掉，反向大降）。"""
    ds = _new_system()
    ds.drives["avoid"].value = 0.4
    ds.release("avoid", -0.10)  # 无人回应
    assert ds.drives["avoid"].value == pytest.approx(0.5), "负值释放应反向 +0.10"


def test_negative_release_caps_at_1():
    ds = _new_system()
    ds.drives["avoid"].value = 0.95
    ds.release("avoid", -0.20)  # 被拒绝
    assert ds.drives["avoid"].value == 1.0


# ─────────────────────────────────────────
# 4. 对手过程过冲
# ─────────────────────────────────────────

def test_release_overshoots_below_baseline():
    """释放后短暂低于净释放值——满足感的余波（原来是 +10% 助燃，方向反了）。"""
    ds = _new_system()
    ds.drives["express"].value = 0.8
    ds.release("express", 0.2)
    # 0.8 - 0.2 - 0.02(过冲) = 0.58
    assert ds.drives["express"].value == pytest.approx(0.58, abs=0.01)


def test_release_floor_at_zero():
    ds = _new_system()
    ds.drives["social"].value = 0.1
    ds.release("social", 0.5)
    assert ds.drives["social"].value == 0.0


# ─────────────────────────────────────────
# 5. 释放匹配完整性
# ─────────────────────────────────────────

def test_action_releases_covers_all_initiating_drives():
    """自治循环所有会主动发起的 dominant 都有对应释放路径。"""
    from agent.handler_autonomy import ACTION_RELEASES
    from agent.drives import DriveSystem
    # avoid 主导时不主动发起（走内部消化），不在此列
    for name in ("social", "commitment", "curiosity_info", "curiosity_explore", "express"):
        assert name in ACTION_RELEASES, f"{name} 触发发起后没有释放路径"
        ds = _new_system()
        main = ACTION_RELEASES[name][0]
        assert main[0] == name, f"{name} 的主释放目标应为自身，实际 {main[0]}"
        assert 0.3 <= main[1] <= 0.5, f"{name} 主释放量 {main[1]} 应为行动级(0.3-0.5)"
        for dn, amount in ACTION_RELEASES[name]:
            assert dn in ds.drives, f"{name} 的释放目标 {dn} 不存在"
            assert 0.1 <= amount <= 0.5, f"{name} 的释放量 {amount} 超出范围"


def test_reply_releases_are_meaningful():
    """回复释放量应能压 1-3 小时积累（不是 24 分钟就补回的微调）。"""
    ds = _new_system()
    # 回复一条消息（reply_social 0.12 → 净 0.108，0.08/h 积累可压 ~1.3 小时）
    ds.drives["social"].value = 0.7
    ds.release_by_action("reply_social")
    v_after = ds.drives["social"].value
    assert v_after < 0.6, f"回复后应显著回落，实际 {v_after:.2f}"
    _tick_hours(ds, 1.0)
    assert ds.drives["social"].value < 0.7, "回复释放后 1 小时内不应重新触发"


def test_internal_digest_releases_slightly():
    """内部消化是微量释放（比行动级小），欲望仍在。"""
    ds = _new_system()
    ds.drives["social"].value = 0.8
    ds.release("social", 0.05)
    assert ds.drives["social"].value > 0.7, "内部消化后仍接近阈值——克制不满足欲望"
