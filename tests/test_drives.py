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

# ─────────────────────────────────────────
# 6. 高饱和钝化（2026-10-12 审计 P0-1 回归）
# ─────────────────────────────────────────

def _tick_and_relax(ds, hours):
    """模拟自治循环的真实节奏：每 10 分钟 tick 积累 + natural_decay 松弛。"""
    for _ in range(int(hours * 6)):
        ds.tick(dt_hours=1 / 6)
        ds.natural_decay(1 / 6)


def test_saturation_relaxation_keeps_nobody_pinned_at_top():
    """长时间无行动消费时，没有驱动力能停在 0.85 以上。

    旧口径：衰减 0.01/h < 积累 0.03~0.10/h，净速率恒为正，地板 0.85 又高于
    全部阈值（0.65~0.75）——实测 4/6 停在 0.94~0.99、每 30 分钟注入同一句。
    """
    ds = _new_system()
    for name in ds.drives:
        ds.drives[name].value = 1.0
    _tick_and_relax(ds, 24)
    for name, drive in ds.drives.items():
        assert drive.value < 0.85, f"{name}={drive.value:.3f} 仍然贴顶"


def test_saturation_floor_sits_below_every_urgency_threshold():
    """地板必须低于全部阈值——否则"钝化"永远到她感觉不到的位置。"""
    ds = _new_system()
    assert ds.SATURATION_FLOOR < min(d.threshold for d in ds.drives.values())


def test_saturation_equilibrium_matches_floor_plus_alpha_over_k():
    """平衡点 = 地板 + alpha/k：参数是可推演的，不是试出来的。"""
    ds = _new_system()
    for name in ds.drives:
        ds.drives[name].value = ds.SATURATION_FLOOR
    _tick_and_relax(ds, 48)
    for name, drive in ds.drives.items():
        expected = (
            ds.SATURATION_FLOOR
            + drive.alpha / ds.SATURATION_RELAXATION_PER_HOUR
        )
        assert drive.value == pytest.approx(expected, abs=0.02), name


def test_saturation_relaxation_does_not_mute_drives():
    """钝化不是静音：松弛到平衡后她仍然够得到阈值、仍然会主动。"""
    ds = _new_system()
    for name in ds.drives:
        ds.drives[name].value = ds.SATURATION_FLOOR
    _tick_and_relax(ds, 24)
    urgent = [n for n, d in ds.drives.items() if d.is_urgent]
    assert urgent, "松弛把所有人按在地板上——她会永远不再主动开口"


def test_released_drive_still_climbs_back_over_threshold():
    """行动释放把驱动力打下去之后，仍能按自己的 alpha 重新越过阈值。"""
    ds = _new_system()
    ds.drives["social"].value = 0.3
    _tick_and_relax(ds, 12)
    assert ds.drives["social"].value >= ds.drives["social"].threshold


def test_natural_decay_logs_every_cycle_including_no_op(caplog):
    """2026-10-13 T1.1（审计 G3 观测缺口）：衰减**每周期必留一行**，
    包括「无块可衰减」——否则「衰减没生效」和「衰减生效了但值本来就低于地板」
    在日志里完全同形（RC5）。2026-10-13 那次「M3 无效」的误判正是踩在这里：
    读到的 0.998 是重启时从盘里载入的旧值，衰减还没跑满一个周期。

    同时钉住字段：验收口径是逐项对账 before→after 与 0.5×excess×dt，
    字段一旦被删就没法对账（教训 #30：检查器静默失效）。
    """
    import logging
    ds = _new_system()
    for name in ds.drives:
        ds.drives[name].value = 0.30          # 全部低于地板 → 无块可衰减
    with caplog.at_level(logging.INFO):
        ds.natural_decay(1 / 6)
    assert "自然衰减" in caplog.text, "无块可衰减时不记——两种情况又变得同形"
    assert "均未超地板" in caplog.text, "空周期必须写明是「没有块超地板」"

    caplog.clear()
    ds.drives["commitment"].value = 0.998
    with caplog.at_level(logging.INFO):
        ds.natural_decay(1 / 6)
    text = caplog.text
    assert "0.998→" in text, "衰减日志必须带 before→after（验收要逐项对账）"
    assert "excess=" in text and "α=" in text, (
        "缺少 excess/α 字段——无法与 floor+α/k 对账")
    assert "dt=" in text, "缺少 dt 字段——无法验证 0.5×excess×dt"
    # 数值自洽：Δ 必须等于 −k×excess×dt
    changed = ds.drives["commitment"]
    excess = 0.998 - ds.SATURATION_FLOOR
    expected = 0.998 - ds.SATURATION_RELAXATION_PER_HOUR * excess * (1 / 6)
    assert changed.value == pytest.approx(expected, abs=1e-6), (
        "衰减量与 k×excess×dt 不符——公式被改过")

# ─────────────────────────────────────────
# 7. 注入变化门（2026-10-12 审计 P0-1「每轮注入同一句」）
# ─────────────────────────────────────────

class TestDriveInjectionGate:
    @staticmethod
    def _ds(value=0.8, name="commitment"):
        ds = _new_system()          # 本文件统一懒导入 DriveSystem
        ds.drives[name].value = value
        return ds

    def test_ungated_call_is_backward_compatible(self):
        """自治决策简报不门控——那里驱动力是核心输入，不是背景。"""
        ds = self._ds()
        first = ds.get_drive_context()
        assert first and "内在的状态" in first
        assert ds.get_drive_context() == first

    def test_gated_suppresses_the_same_band_within_cooldown(self):
        ds = self._ds()
        assert ds.get_drive_context(gated=True) != ""
        assert ds.get_drive_context(gated=True) == ""

    def test_band_change_breaks_through_immediately(self, monkeypatch):
        class _T:
            v = 0.0

            @staticmethod
            def monotonic():
                return _T.v

        monkeypatch.setattr("agent.drives.time", _T)
        ds = self._ds(value=0.8)
        assert ds.get_drive_context(gated=True) != ""
        _T.v = 1.0                                  # 远未到 30 分钟
        ds.drives["commitment"].value = 0.95        # 强烈 → 非常强烈
        assert ds.get_drive_context(gated=True) != "", "换档必须立刻放行"

    def test_cooldown_expiry_reinjects(self, monkeypatch):
        class _T:
            v = 0.0

            @staticmethod
            def monotonic():
                return _T.v

        monkeypatch.setattr("agent.drives.time", _T)
        ds = self._ds()
        assert ds.get_drive_context(gated=True) != ""
        assert ds.get_drive_context(gated=True) == ""
        from agent.drives import DriveSystem
        _T.v = DriveSystem.DRIVE_INJECT_COOLDOWN_SECONDS + 1
        assert ds.get_drive_context(gated=True) != "", "冷却到期后应重新注入"

    def test_sentence_varies_with_intensity_and_stays_report_free(self):
        """M3 验收：注入句随强度变化——但**不许**加百分比/清单（那是报告格式，
        2026-08-17 瘦身删的就是它）。"""
        mild = self._ds(value=0.66).get_drive_context()
        mid = self._ds(value=0.80).get_drive_context()
        strong = self._ds(value=0.95).get_drive_context()
        assert mild != mid and mid != strong
        assert "隐约" in mild
        assert "压不住" in strong
        assert "%" not in mid and "%" not in strong

    def test_no_dominant_returns_empty_and_records_nothing(self):
        ds = _new_system()
        assert ds.get_drive_context(gated=True) == ""
        assert ds._last_injection is None
