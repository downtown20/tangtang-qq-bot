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
        旧版「记忆在前图片在后」会把当前轮的图片挤掉。

        2026-10-25：`estimate_tokens` 换成实测系数（中文 1.8→0.72）后，同样
        600 的预算能装下原本装不下的块，这条测试的前提（预算不够三块）失效。
        **判据没变，是尺子变了**——把预算按新尺子重算到「够两块、不够三块」
        的区间（每块 200 汉字≈144t + 包装 24t，两块 336t，留 64t 给第三块
        不足 120t 的最低门槛）。
        """
        bgs = [
            ("记忆", "你" * 200),   # 优先 7
            ("图片", "图" * 200),   # 优先 4
            ("知识库", "啊" * 200),  # 优先 15
        ]
        out = protocols.fit_backgrounds(bgs, budget_tokens=400)
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

    def test_memory_block_survives_default_budget(self):
        """2026-10-25 第六轮：默认预算下「记忆」块必须**完整**进池。

        实测证据（`日志/tangtang.log`，2026-10-25）：35 条「背景块淘汰/裁剪」
        记录里 **27 条「记忆」块被淘汰（77%）**，94% 的回合预算打满。
        她的 reasoning 原话：
        「the background says memory is empty, need to search」
        ——于是每轮都得现查 `search_facts`，体感不是「她记得我」，是「她翻了档案」。

        **块价按日志实测，不按直觉构造**：关系档案 ~240t（单块上限处封顶）、
        窗口原始可到 ~216t、协议 ~30t、缺口 ~70t、记忆 ~215t——这些加起来
        已经接近 720，所以 720 那档出现过「记忆在但被裁到 35t」：
        `🧺 保留flex=3 淘汰=1（自忆） 裁=1（记忆 156→35t）| 预算 683/720`。
        **有块没内容，等于没给**——所以断言落在「内容完整」而不是「块存在」。
        """
        bgs = [
            ("关系档案", "档" * 333),   # 240t，被单块上限封顶
            ("窗口", "窗" * 300),       # 216t（日志里见过 864→240t 的原始量级）
            ("协议", "协" * 42),        # 30t，保护块
            ("记忆", "记" * 300),       # 215t ← 要看的就是它
            ("反思", "反" * 200),       # 144t，与记忆同为 P7
            ("自忆", "自" * 333),       # 240t，最低优先
        ]
        out = protocols.fit_backgrounds(bgs)   # 用默认预算
        labels = [l for l, _ in out]
        assert "记忆" in labels, (
            f"「记忆」块又被淘汰了：{labels}——她又会变成「每轮现查档案」")
        mem_content = dict(out)["记忆"]
        assert len(mem_content) >= 250, (
            f"记忆块只剩 {len(mem_content)} 字（原文 300 字）——被裁得太狠，"
            "她要的还是「几条残缺记忆」，不是「她记得我」")

    def test_default_budget_covers_memory_but_not_unbounded(self):
        """预算要「够装记忆」，但也不能大到把背景池重新放大成材料堆

        上限判据：900 的估算预算 ≈ 833 真实 tokens——约为 system prompt
        （≈2100 真实 tokens）的三分之一，仍在「少而重要」的量级内。
        2026-08-17 反对的是「一次塞十几个块导致材料综述/演讲腔」，
        不是「4 个有用的块装得下」。
        """
        assert 600 <= protocols.BACKGROUND_BUDGET_TOKENS <= 1100, (
            f"背景预算 {protocols.BACKGROUND_BUDGET_TOKENS} 超出「够装记忆但不放大成材料堆」"
            "的区间，需要重新评估 2026-08-17 那条『材料综述/演讲腔』的反面约束")

    def test_current_turn_evidence_survives_protected_overflow(self):
        """2026-10-13 T1.3（审计 P1-3）：保护块吃满预算时，当前轮直接证据
        （提及/图片/文件）不得被静默饿死。

        复现：两个保护块（引用/协议，优先级≤1 且允许 overflow）各自就超过总预算，
        旧代码 (budget - used) 变负 → flex 循环第一轮 break → 提及块静默消失。
        """
        bgs = [
            ("引用", "引" * 400),       # 保护块，优先级 0，允许 overflow
            ("协议", "协" * 400),       # 保护块，优先级 1，允许 overflow
            ("提及", "@小明 说了这句"),  # 当前轮直接证据，优先级 2
            ("记忆", "你" * 300),        # 旧材料，优先级 7
        ]
        out = protocols.fit_backgrounds(bgs, budget_tokens=200)
        labels = [l for l, _ in out]
        assert "提及" in labels, (
            "保护块吃满预算后当前轮证据被静默饿死——她又看不到对方 @ 了谁")
        assert "记忆" not in labels, (
            "保底配额只保证当前轮直接证据，不该把旧材料一起捞回来")
        # 2026-10-16 T59/T57：系统注入的「缺口」告知永远在最前（它是对「材料不全」
        # 的说明，不是材料本身）——顺序契约检查的对象是**材料块**。
        material = [l for l in labels if l != "缺口"]
        assert material == [l for l, _ in bgs if l in material], (
            "保底改变了展示顺序——协议紧贴用户原文的契约不因保底而破")

    def test_evicted_blocks_are_logged_with_reserve_marker(self, caplog):
        """2026-10-13 T1.3：淘汰明细必须进日志——P1-3 的失败形态是「静默饿死」。
        动了证据保底时还要标注，否则日志里只看到「淘汰」看不出正是 P1-3 现场。"""
        import logging
        bgs = [("引用", "引" * 400), ("协议", "协" * 400),
               ("提及", "@小明 说了这句"), ("记忆", "你" * 300)]
        with caplog.at_level(logging.INFO):
            protocols.fit_backgrounds(bgs, budget_tokens=200)
        text = caplog.text
        assert "背景块淘汰" in text, "淘汰没有留痕——静默饿死回来了"
        assert "记忆" in text, "淘汰明细里必须指明被淘汰的是哪一块"
        assert "证据保底" in text, "动了证据保底却不标注——P1-3 现场无法从日志识别"

    def test_truncated_block_is_logged(self, caplog):
        """2026-10-15 T56（C5 观测盲区）：**裁剪**必须留痕。

        原先只记淘汰不记裁剪——全仓 40 万行日志里一条裁剪记录都没有，于是
        「反编造契约行到底有没有进上下文」只能靠复刻实验倒推（P1-3 的「静默」
        以另一种形式重演）。"""
        import logging
        bgs = [("关系档案", "长" * 2000)]
        with caplog.at_level(logging.INFO):
            out = protocols.fit_backgrounds(bgs, budget_tokens=600)
        archive = next(text for label, text in out if label == "关系档案")
        text = caplog.text
        assert "背景块淘汰" in text, "裁剪没有留痕——「静默裁掉」回来了"
        assert "裁=1" in text and "关系档案" in text, "必须指明被裁的是哪一块"
        assert "→" in text, "必须给出裁前→裁后 token，否则看不出被裁掉多少"

    def test_relationship_block_keeps_contract_after_truncation(self):
        """2026-10-15 T56（C3）：关系档案的反编造契约行必须在**块首**。

        复现的事故形态：契约原先在块第 2 行，而 truncate_bg_block 只裁尾部——
        预算一紧，先丢的恰好是唯一约束材料的那一行，留下一段没有任何说明的叙事
        复述（压力越大，注入越像事实本身）。这里用**真块**加小预算强制截断。"""
        from agent.handler import _build_relationship_block
        body = "\n".join(
            f"{i}. 第{i}条回忆，写长一点好把这一块顶到预算之上。"
            for i in range(1, 40)
        )
        block = _build_relationship_block("某人", body)
        out = protocols.fit_backgrounds([("关系档案", block)], budget_tokens=300)
        assert out, "块被整块淘汰了——本测试要的是「截断之后契约还在」"
        # 按标签取，不按位置：2026-10-16 T59/T57 起，真有裁剪时会多一块
        # 系统注入的「缺口」告知（在最前），位置索引不再等于「第一块材料」。
        text = next(t for label, t in out if label == "关系档案")
        assert len(text) < len(block), "前提：这一块确实被裁了"
        assert text.split("\n")[0].startswith("（这是基于真实聊天记录整理的关系档案。"), (
            "契约行不在块首——尾部截断会先把它切掉")
        assert "不要编造上面没有的信息" in text, "截断后契约行丢了"


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
        """2026-08-17 Codex 对齐：时间层只报时段，不规定说话风格。

        2026-10-25 自查升级：只报「现在是深夜」不够——她因此不知道「明天」
        是哪一天（`turn_traces#25` 自编「时间：周五上午」，星期和时段都错）。
        现在必须同时给出**日期/星期/时分**，但仍然只报事实、不带风格：
        长度可控 + 三个风格词缺席这两类断言保持不变。
        """
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
            assert "2026年8月17日" in ctx, f"{hour}点: {ctx}"
            assert "星期一" in ctx, f"{hour}点: {ctx}"
            assert f"{hour}点00分" in ctx, f"{hour}点: {ctx}"
            assert len(ctx) < 60, f"{hour}点: {ctx}"
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
