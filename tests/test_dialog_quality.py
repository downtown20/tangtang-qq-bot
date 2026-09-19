"""对话质量审查回归（2026-08-17）

用户反馈：「说话不自然、像在表演」「少用点DeepSeek的语言模板」。
根因：每轮消息被塞进 5500-9000t 说明书——信噪比 1:300。
本测试钉住四批修复：
1. 背景块预算淘汰（fit_backgrounds——低优先块让位，引用/协议永不丢）
2. 状态层信号化（mood/存在/叙事/驱动力只在偏离中性时出现，感受口吻）
3. 风格锚（STYLE_ANCHOR show don't tell）+ CoT 瘦身
4. minimal 模式（遥控/播报路径不背全套说明书）+ anti-tell 诊断
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import protocols


class TestBackgroundBudget:
    def test_fit_keeps_quote_and_protocol(self):
        bgs = [("引用", "警告"), ("协议", "理解协议"), ("知识库", "范本" * 500)]
        out = protocols.fit_backgrounds(bgs, budget_tokens=100)
        labels = [l for l, _ in out]
        assert "引用" in labels and "协议" in labels

    def test_priority_selection_not_append_order(self):
        """2026-08-17 Codex 回归：按声明优先级选择而非 append 顺序——
        旧版「记忆在前图片在后」会把当前轮的图片挤掉"""
        bgs = [
            ("记忆", "你" * 200),   # 优先 7
            ("图片", "图" * 200),   # 优先 4
            ("知识库", "啊" * 200),  # 优先 15
        ]
        out = protocols.fit_backgrounds(bgs, budget_tokens=600)
        labels = [l for l, _ in out]
        assert "图片" in labels  # 当前轮证据优先保留
        assert "知识库" not in labels  # 最低优先淘汰

    def test_display_order_restored(self):
        """2026-08-17 Codex 回归：选择后按原始 index 恢复展示顺序——
        协议仍紧贴用户原文"""
        bgs = [("记忆", "你" * 100), ("协议", "理解协议"), ("图片", "图" * 100)]
        out = protocols.fit_backgrounds(bgs, budget_tokens=900)
        labels = [l for l, _ in out]
        assert labels == ["记忆", "协议", "图片"]  # 原始顺序

    def test_oversized_block_truncated(self):
        bgs = [("记忆", "喜欢" * 1000)]
        out = protocols.fit_backgrounds(bgs, budget_tokens=300)
        text = out[0][1]
        assert len(text) < 1000  # 确有截断

    def test_empty_blocks_dropped(self):
        out = protocols.fit_backgrounds([("记忆", "  "), ("窗口", "hi")])
        assert [l for l, _ in out] == ["窗口"]


class TestStateSignalCompression:
    def test_mood_neutral_returns_empty(self):
        """中性状态零注入——「精力正常，正常聊天」是纯噪音"""
        from agent.mood import MoodEngine
        eng = MoodEngine()
        eng._state.energy = 70
        eng._state.mood = 70
        assert eng.build_context() == ""

    def test_mood_tired_injects_feeling_line(self):
        from agent.mood import MoodEngine
        eng = MoodEngine()
        eng._state.energy = 20
        assert "累" in eng.build_context()
        assert "/100" not in eng.build_context()  # 数值不再给 LLM

    def test_existence_neutral_returns_empty(self):
        from agent.self_state import ExistenceState
        assert ExistenceState(energy="medium", mood_tint="neutral").to_context() == ""

    def test_existence_low_injects_feeling_line(self):
        from agent.self_state import ExistenceState
        out = ExistenceState(energy="low", mood_tint="tired").to_context()
        assert "累" in out and "想聊太深" in out

    def test_narrative_drops_resume_lists(self):
        """自我叙事不再带履历清单（每轮复述自己=表演）"""
        from agent.self_state import SelfNarrative
        n = SelfNarrative(summary="我是糖糖。")
        n.recent_experiences = ["昨天唱歌了", "前天写代码", "大前天发呆"]
        n.things_i_learned = ["耐心", "诚实"]
        out = n.to_context()
        assert "唱歌" not in out  # 履历清单已砍
        assert "耐心" not in out  # 只留最后一条领悟
        assert "诚实" in out

    def test_drives_weak_returns_empty(self):
        """驱动力低于 0.6 不注入——报告格式的百分比/次要清单已删"""
        from agent.drives import DriveSystem
        ds = DriveSystem()
        out = ds.get_drive_context(self_state=None)
        assert out == "" or "内在的状态" not in out


class TestStyleAnchorAndMinimal:
    def _engine(self):
        from agent.personality import PersonalityEngine, PersonalityConfig
        return PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖", "糖糖"]))

    def test_style_anchor_in_full_prompt(self):
        from agent.personality import Relationship
        p = self._engine().build_system_prompt(Relationship.STRANGER)
        assert "## 说话示范" in p
        assert "糖糖：「在的」" in p
        # 2026-08-17 Codex 对齐：示例不得教虚构状态（「刚醒」已删）
        assert "刚醒" not in p

    def test_cot_trimmed_to_one_item(self):
        """2026-08-17 Codex 对齐：普通轮 CoT 只剩 1 条路由——
        指代/复述细节归 hard_turn 协议，不常驻复制"""
        from agent.personality import Relationship
        p = self._engine().build_system_prompt(Relationship.STRANGER)
        cot = p[p.find("## 回复前先想"):p.find("想好之后")]
        assert cot.count("1.") == 1
        assert "2." not in cot and "3." not in cot
        # 纠正契约独立常驻（不依赖 CoT 条数）
        assert protocols.CORRECTION_CONTRACT in p
        assert protocols.HIGH_STAKES_GUIDANCE_CONTRACT in p

    def test_minimal_prompt_skips_manual(self):
        """minimal：有身份+风格锚+时间+契约，没有思考链/关系层"""
        from agent.personality import Relationship
        p = self._engine().build_system_prompt(Relationship.FAMILIAR, minimal=True)
        assert "## 说话示范" in p
        assert "## 回复前先想" not in p
        assert "## 关系" not in p
        assert "现在是" in p
        # 2026-08-17 Codex Critical 回归：minimal 也必须带纠正契约
        assert protocols.CORRECTION_CONTRACT in p
        assert protocols.HIGH_STAKES_GUIDANCE_CONTRACT in p

    def test_voice_override_keeps_contract(self):
        """2026-08-17 Codex Critical 回归：语音角色覆盖只换身份底座，
        不抹掉纠正契约（旧逻辑末尾整体覆盖 base）"""
        from agent.personality import Relationship
        eng = self._engine()
        eng._voice_role_override = "你是丛雨。"
        p = eng.build_system_prompt(Relationship.FAMILIAR)
        assert "你是丛雨" in p
        assert protocols.CORRECTION_CONTRACT in p

    def test_reload_keeps_tail_sections(self):
        """2026-08-17 Codex 回归：_split_minimal 单一逻辑源——
        热重载后 replace 场景仍保留 称呼/@提及/说话示范"""
        from agent.personality import PersonalityEngine, PersonalityConfig
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖"]))
        assert eng.reload_role_card() is True
        m = eng._cached_minimal
        assert "称呼" in m and "私聊不要用 @" in m
        assert "说话示范" in m

    def test_time_context_is_objective_only(self):
        """2026-08-17 Codex 对齐：时间层只报时段，不规定说话风格"""
        from unittest.mock import patch
        import datetime as _dt
        from agent.personality import PersonalityEngine, PersonalityConfig
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖"))
        for hour in (0, 8, 11, 13, 17, 21, 23):
            class _Fixed(_dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 8, 17, hour, 0)
            with patch("datetime.datetime", _Fixed):
                ctx = eng._get_time_context()
            assert len(ctx) < 10, f"{hour}点: {ctx}"
            assert "懒散" not in ctx and "走心" not in ctx and "夜猫子" not in ctx


class TestAntiTellDiagnostic:
    def _check(self, reply):
        from agent.self_check import ReplySelfCheck
        sc = ReplySelfCheck()
        r = sc.check(reply)
        return r

    def test_canned_reaction_flagged(self):
        r = self._check("我愣了一下，然后说好呀")
        assert any("anti-tell" in w for w in r.warnings)
        assert not r.blocked  # 只诊断不阻断（观测先行）

    def test_assistant_tone_flagged(self):
        r = self._check("很乐意帮你！这个问题问得很好")
        assert any("anti-tell" in w for w in r.warnings)

    def test_natural_reply_clean(self):
        r = self._check("在的在的喵~刚醒，还有点迷糊呢")
        assert not any("anti-tell" in w for w in r.warnings)


class TestCodexFinalReview:
    """2026-08-17 Codex 终审 Request Changes 的修复回归——
    契约截断豁免 / voice+replace tone / 姿态补读优先级 / 叙事窗口 fail-closed"""

    def test_contract_survives_budget_truncation(self):
        """终审 High：预算截断不可吃掉纠正契约（旧逻辑契约在中间会被砍）"""
        from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
        from agent.context_builder import ContextBuilder
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖"]))
        base = eng.build_system_prompt(Relationship.STRANGER)
        cb = ContextBuilder(self_state=None)
        result = cb.build(
            user_id="1", nickname="x", message="hi",
            system_prompt_base=base, history_messages=[],
            preferences="偏好" * 3000, knowledge="知识" * 3000,
            capability_note="## ⚙️ 当前运行模式\n测试",
        )
        assert protocols.CORRECTION_CONTRACT in result.system_prompt

    def test_voice_override_with_replace_keeps_tone(self):
        """终审 High：voice override + replace 场景——只换身份底座，tone 不丢"""
        from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖"))
        eng._voice_role_override = "你是丛雨。"

        class _Rep:
            is_overlay = False
            role = "活动管理者"
            tone = "专业简洁"
        p = eng.build_system_prompt(Relationship.STRANGER, scenario=_Rep())
        assert "你是丛雨" in p
        assert "专业简洁" in p          # tone 保留
        assert "活动管理者" not in p     # role 与 override 互斥
        assert protocols.CORRECTION_CONTRACT in p

    def test_posture_and_catchup_beat_memories(self):
        """终审 High：姿态（当前轮指令）与补读（用户明确请求）不被旧记忆挤掉"""
        bgs = [
            ("记忆", "你" * 200),
            ("姿态", "接一句就走"),
            ("补读", "好" * 200),
            ("日记", "啊" * 200),
        ]
        out = protocols.fit_backgrounds(bgs, budget_tokens=700)
        labels = [l for l, _ in out]
        assert "姿态" in labels and "补读" in labels

    def test_narrative_window_cases(self):
        """终审 Medium：叙事 3 天窗口 fail-closed——空/非法时间戳不注入"""
        import datetime
        from agent.context_builder import ContextBuilder
        from agent.self_state import TangTangSelf
        ss = TangTangSelf(bot_qq="1")
        ss.self_narrative.summary = "我是糖糖。"
        cb = ContextBuilder(self_state=ss)
        # 无时间戳 → 不注入
        ss.self_narrative.updated_at = ""
        assert cb._get_self_narrative_context() == ""
        # 非法时间戳 → 不注入
        ss.self_narrative.updated_at = "不是时间"
        assert cb._get_self_narrative_context() == ""
        # 3 天内 → 注入
        ss.self_narrative.updated_at = (
            datetime.datetime.now() - datetime.timedelta(days=1)).isoformat()
        assert "我是糖糖" in cb._get_self_narrative_context()
        # 超 3 天 → 不注入
        ss.self_narrative.updated_at = (
            datetime.datetime.now() - datetime.timedelta(days=10)).isoformat()
        assert cb._get_self_narrative_context() == ""
