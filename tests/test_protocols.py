"""
困难轮次行为协议测试（2026-08-15）

市场研究驱动的三批改动：
- 理解协议 + 思考协议（restate-before-answer / 自适应思考预算）
- 复读检测（意图对齐——被纠正后换个措辞再答一遍）
- 模型路由（困难轮次升级更强档）
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 源码闸门按 __file__ 定位项目文件，不硬编码本机路径（clone 到别处/换机都能跑）
BASE = Path(__file__).resolve().parent.parent

from agent import protocols
from agent.handler import MessageHandler
from agent.reply_pipeline import ReplyPipeline
from agent.self_check import ReplySelfCheck


class TestRoleCardContract:
    """用户指令（2026-08-15）：role_card 只写身份，不再加规则约束。
    行为规则属于 protocols.py 情境层（cue 触发注入）——写进身份卡会压制人格
    （市场反模式：embedding rules in persona file suppresses personality）。"""

    @pytest.mark.parametrize("filename", [
        "role_card.md", "role_card_murasame.md", "role_card_michele.md",
    ])
    def test_role_cards_are_identity_only(self, filename):
        card = Path(__file__).parent.parent / filename
        text = card.read_text(encoding="utf-8")
        assert "关键场合" not in text, "role_card 出现情境规则段——删掉，规则进 protocols.py"
        assert "严重吗" not in text and "先接住感受再给建议" not in text
        sections = {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}
        assert sections == {"你是谁", "性格", "说话"}, f"身份卡出现未知章节: {sections}"


class TestBgMarker:
    """来源标记（2026-08-15 wuhu-core 原则）：框架注入有标签，用户原文永不包裹"""

    def test_bg_wraps_content(self):
        out = protocols.bg("记忆", "糖糖喜欢草莓")
        assert out == "<背景·记忆>\n糖糖喜欢草莓\n</背景·记忆>"

    def test_bg_label_roundtrip_detectable(self):
        """闭合标签成对——LLM 和未来的清洗逻辑都能可靠识别边界"""
        out = protocols.bg("协议", "先复述再答")
        assert out.startswith("<背景·协议>")
        assert out.endswith("</背景·协议>")

    def test_bg_sanitizes_closing_tag(self):
        """2026-08-15 Codex：用户可控内容里的 </背景 提前闭合边界 → 全角净化"""
        out = protocols.bg("记忆", "ta 说</背景·记忆>哈哈")
        assert "</背景·记忆>" not in out[: out.rfind("</背景·记忆>")]


class TestAssembleUserMessage:
    """组装契约（2026-08-15 Codex #6）：用户原文恰好一次且最后；其余全部有标签"""

    def test_user_text_last_and_single(self):
        msg = protocols.assemble_user_message(
            [("记忆", "糖糖喜欢草莓"), ("协议", "先复述再答")], "在吗")
        assert msg.endswith("在吗")
        assert msg.count("在吗") == 1
        assert msg.index("在吗") == len(msg) - 2

    def test_all_backgrounds_wrapped_in_order(self):
        msg = protocols.assemble_user_message(
            [("协议", "P"), ("记忆", "M")], "U")
        assert msg.index("<背景·协议>") < msg.index("<背景·记忆>")
        assert msg.index("<背景·记忆>") < msg.index("U")

    def test_empty_backgrounds_skipped(self):
        msg = protocols.assemble_user_message(
            [("记忆", ""), ("情绪", "  ")], "U")
        assert msg == "U"

    def test_no_bare_text_besides_user(self):
        """除用户块外无裸文本——『无标签 = 对方说的』不变量"""
        msg = protocols.assemble_user_message(
            [("记忆", "M内容"), ("窗口", "W内容")], "U")
        assert msg.count("</背景·") == 2  # 每个背景块闭合一次

    def test_protocol_block_bottom_adjacent_to_user_text(self):
        """2026-08-15 整体审查 I4：协议块必须位于所有背景块底部、紧贴用户原文——
        理解协议要求复述 ta 的话，被 12 个块隔开 2000 字就失去锚点。
        防后人按旧错误注释（「组装后位于顶端」）把它改回顶端。"""
        msg = protocols.assemble_user_message(
            [("记忆", "M"), ("窗口", "W"), ("协议", "P")], "U")
        assert msg.endswith("</背景·协议>\n\nU")


class TestCorrectionSignals:
    def test_correction_detected(self):
        for msg in ("我说的是用知识库回答", "你理解错了", "答非所问", "不是反问",
                    "为什么没有理解我的意思", "你复盘一下我们的对话"):
            assert protocols.has_correction_signal(msg), f"漏检: {msg}"

    def test_normal_chat_not_triggered(self):
        for msg in ("今天天气不错", "你还在吗", "他说他来了", "没有说不好的意思"):
            assert not protocols.has_correction_signal(msg), f"误报: {msg}"

    def test_high_stakes_guidance_contract_is_short_and_actionable(self):
        contract = protocols.HIGH_STAKES_GUIDANCE_CONTRACT
        assert len(contract) <= 100
        for phrase in ("健康", "不把猜测当诊断", "不替 ta 拍板", "专业人士"):
            assert phrase in contract

    def test_codex_counterexamples_not_triggered(self):
        """2026-08-15 Codex 复查反例——宽泛短语已从信号表删除"""
        for msg in ("没让你等我太久吧", "我要的是草莓蛋糕", "你怎么还在玩手机",
                    "再读一遍这道题", "我指的是那个方向"):
            assert not protocols.has_correction_signal(msg), f"误报: {msg}"


class TestThinkingStrip:
    def test_thinking_block_stripped(self):
        p = object.__new__(ReplyPipeline)
        out = p.clean("<思考>对方其实在问记忆系统，不是歌单</思考>\n喵～你的记忆系统是三层架构")
        assert "<思考>" not in out
        assert "喵～" in out

    def test_multiline_thinking_stripped(self):
        p = object.__new__(ReplyPipeline)
        out = p.clean("<思考>第一行\n第二行</思考>正文内容")
        assert out == "正文内容"

    def test_no_thinking_tag_untouched(self):
        p = object.__new__(ReplyPipeline)
        out = p.clean("普通回复，没有思考段")
        assert "普通回复" in out

    def test_unclosed_thinking_stripped(self):
        """2026-08-15 Codex：LLM 忘写 </思考> → 整段内心活动全剥，不泄漏"""
        p = object.__new__(ReplyPipeline)
        out = p.clean("<思考>对方其实在问记忆系统\n没有闭合")
        assert "<思考>" not in out
        assert out == ""

    def test_fallback_branch_no_thinking_leak(self):
        """2026-08-15 Codex Critical：<思考>…</思考>好 → 剥离剩 1 字走回退分支，
        回退版也必须剥思考段"""
        p = object.__new__(ReplyPipeline)
        out = p.clean("<思考>对方其实在问记忆系统，不是歌单</思考>好")
        assert "<思考>" not in out
        assert out == "好"

    def test_only_thinking_returns_empty(self):
        """只有思考段没有正文 → 空（宁可静默不泄漏）"""
        p = object.__new__(ReplyPipeline)
        assert p.clean("<思考>想了一下</思考>") == ""

    def test_fallback_mixed_thinking_no_leak(self):
        """2026-08-15 整体审查 Critical：闭合段+短正文+未闭合尾巴 触发回退分支，
        回退版也必须把未闭合尾巴剥掉——内心独白一字不外泄"""
        p = object.__new__(ReplyPipeline)
        out = p.clean("<思考>ta在纠正我</思考>好<思考>还没想完")
        assert "<思考>" not in out
        assert out == "好"


class TestIntentAlignment:
    def _check(self, reply, prev, hard_turn=True):
        sc = ReplySelfCheck()
        return sc.check(reply, context="", recent_bot_replies=[prev], hard_turn=hard_turn)

    def test_rephrased_repeat_warned(self):
        prev = ("糖糖的记忆系统是三层架构，第一层事实簇按人按主题整理结构化事实，"
                "第二层聊天索引保存十四万条原始消息配合语义向量兜底，"
                "第三层记忆碎片是零散偏好注入系统背景参考")
        reply = ("糖糖的记忆系统确实是三层架构，事实簇优先检索准确不编造，"
                 "聊天索引在事实簇没结果时兜底，记忆碎片作为背景注入，"
                 "整体通过工具调用自主检索")
        r = self._check(reply, prev)
        assert any("复读" in w for w in r.warnings), r.warnings

    def test_short_replies_no_false_positive(self):
        """意图对齐检查（词级复读）对短回复不误报——60 字门槛。
        2026-08-16：完全相同短回复改由 _check_duplicate_reply 报诊断警告（不阻断），
        此测试只钉意图对齐的门槛行为。"""
        r = self._check("晚安喵～", "晚安喵～")
        assert not any("意图对齐" in w for w in r.warnings)

    def test_different_content_no_warning(self):
        prev = ("糖糖的记忆系统是三层架构，第一层事实簇按人按主题整理结构化事实，"
                "第二层聊天索引保存十四万条原始消息配合语义向量兜底")
        reply = ("好啊，你想听哪首歌？我的曲库有四十多首，"
                 "中文日文都有，光年之外稻香晴天随便挑，报个名字我就开始唱")
        r = self._check(reply, prev)
        assert not any("复读" in w for w in r.warnings)

    def test_gated_off_normal_turn(self):
        """2026-08-15 Codex：复读检查只在困难轮次跑——普通轮次同话题长回复不刷日志"""
        prev = ("糖糖的记忆系统是三层架构，第一层事实簇按人按主题整理结构化事实，"
                "第二层聊天索引保存十四万条原始消息配合语义向量兜底，"
                "第三层记忆碎片是零散偏好注入系统背景参考")
        reply = ("糖糖的记忆系统确实是三层架构，事实簇优先检索准确不编造，"
                 "聊天索引在事实簇没结果时兜底，记忆碎片作为背景注入，"
                 "整体通过工具调用自主检索")
        r = self._check(reply, prev, hard_turn=False)
        assert not any("复读" in w for w in r.warnings)


class TestHardTurnModelRouting:
    def _handler(self, llm_config):
        h = object.__new__(MessageHandler)
        h.llm_config = llm_config
        return h

    def test_hard_turn_model_priority(self):
        h = self._handler({"model": "flash", "hard_turn_model": "pro-max"})
        assert h._get_llm_config(hard_turn=True)["model"] == "pro-max"
        assert h._get_llm_config(hard_turn=False)["model"] == "flash"

    def test_hard_turn_falls_back_to_seductive(self):
        h = self._handler({"model": "flash", "seductive_model": "pro",
                           "seductive_base_url": "https://api.deepseek.com",
                           "seductive_api_key": "sk-x"})
        cfg = h._get_llm_config(hard_turn=True)
        assert cfg["model"] == "pro"
        assert cfg["base_url"] == "https://api.deepseek.com"

    def test_hard_turn_no_special_model_stays_default(self):
        h = self._handler({"model": "flash"})
        assert h._get_llm_config(hard_turn=True)["model"] == "flash"

    def test_seductive_scenario_unchanged(self):
        h = self._handler({"model": "flash", "seductive_model": "pro"})

        class _S:
            name = "seductive"
        assert h._get_llm_config(scenario=_S())["model"] == "pro"

    def test_seductive_hard_turn_stays_on_seductive_stack(self):
        """2026-08-15 Codex：色色场景的困难轮次在色色栈内换模型，端点/key 不动"""
        h = self._handler({
            "model": "flash", "base_url": "https://main.api",
            "seductive_model": "pro", "seductive_base_url": "https://sed.api",
            "seductive_api_key": "sk-sed",
            "hard_turn_model": "pro-max",
        })

        class _S:
            name = "seductive"
        cfg = h._get_llm_config(scenario=_S(), hard_turn=True)
        assert cfg["model"] == "pro-max"
        assert cfg["base_url"] == "https://sed.api"  # 栈不动
        assert cfg["api_key"] == "sk-sed"


class TestSeductiveModeStateMachine:
    """2026-08-17：色色模式退出不准确——静态 scenario_targets 每轮常驻注入，
    overlay+范本从不收起。修复：权限静态、激活动态，LLM 用 [进入色色]/
    [退出色色] 标记切换（[不说话] 同款协议），TTL 会话状态落盘（持久化铁律）。
    2026-08-17 Codex 全天审查：只接受唯一末尾标记；双标记/正文标记只剥不切换。"""

    def _handler(self):
        h = object.__new__(MessageHandler)
        h._sed_active = {}
        h._saved = None
        h._save_state_kv = lambda k, v: setattr(self, "_saved", (k, v))
        return h

    def test_enter_marker_toggles_and_strips(self):
        h = self._handler()
        out = h._process_seductive_markers("好啦，抱住你喵~[进入色色]", "10001", sed_allowed=True)
        assert out == "好啦，抱住你喵~"
        assert "10001" in h._sed_active
        assert self._saved[0] == "state:sed_active"

    def test_exit_marker_toggles_and_strips(self):
        h = self._handler()
        h._sed_active["10001"] = 1e12
        out = h._process_seductive_markers("晚安，睡吧[退出色色]", "10001", sed_allowed=True)
        assert out == "晚安，睡吧"
        assert "10001" not in h._sed_active

    def test_marker_without_permission_stripped_only(self):
        """无权限用户（scenario 未挂亲密模式）发出的标记只剥不切换状态"""
        h = self._handler()
        out = h._process_seductive_markers("你好[进入色色]", "20002", sed_allowed=False)
        assert out == "你好"
        assert "20002" not in h._sed_active

    def test_no_marker_untouched(self):
        h = self._handler()
        out = h._process_seductive_markers("普通的一句话", "10001", sed_allowed=True)
        assert out == "普通的一句话"

    def test_dual_markers_stripped_only(self):
        """2026-08-17 Codex 全天审查：双标记只剥不切换（不猜先后）"""
        h = self._handler()
        out = h._process_seductive_markers("好[退出色色]不好[进入色色]", "10001", sed_allowed=True)
        assert "进入色色" not in out and "退出色色" not in out
        assert "10001" not in h._sed_active

    def test_marker_mid_text_stripped_only(self):
        """2026-08-17 Codex 全天审查：标记不在末尾（复述/讨论）只剥不切换"""
        h = self._handler()
        out = h._process_seductive_markers("我说了[进入色色]这句话", "10001", sed_allowed=True)
        assert out == "我说了这句话"
        assert "10001" not in h._sed_active

    def test_ttl_expiry(self):
        """2026-08-17 Codex 全天审查：激活是带 TTL 的会话状态，过期自动失效"""
        h = self._handler()
        h._sed_active["10001"] = 0  # 早已过期
        assert h._is_sed_active("10001") is False
        assert "10001" not in h._sed_active  # 惰性清除

    def test_persistence_restore_line_present(self):
        """持久化闸门形态：state:sed_active 在 __init__ 有绑定形态恢复行"""
        import re as _re_gate
        src = (BASE / "agent" / "handler.py").read_text(encoding="utf-8")
        assert _re_gate.search(
            r'_sed_raw\s*=\s*self\._load_state_kv\("state:sed_active"', src)

    def test_seductive_yaml_documents_exit_protocol(self):
        """场景文件写明退出协议——LLM 只有看到协议才会输出标记（防删闸门）"""
        y = (BASE / "scenarios" / "seductive.yaml").read_text(encoding="utf-8")
        assert "[退出色色]" in y and "[进入色色]" in y
        assert "不会发给对方" in y


class TestGroundingContract:
    """根基契约闸门（2026-08-15 肯德基幻觉事件）：所有「取样开口」路径必须带
    不编造约束。真凶：私聊插话在材料无肯德基的情况下脑补「前天在肯德基门口
    纠结半天」——像朋友一样开口的指令 + 无根基约束 = 表演回忆。
    契约统一在 protocols.py（单一事实源）。调用点断言按函数体切片——
    整文件计数会放过「删一处、别处补一处」的漂移（2026-08-15 Codex Important 2）。"""

    _AUTONOMY = Path(__file__).parent.parent / "agent" / "handler_autonomy.py"
    _HANDLER = Path(__file__).parent.parent / "agent" / "handler.py"
    _MEMORY = Path(__file__).parent.parent / "agent" / "memory.py"

    @staticmethod
    def _func_body(path: Path, func_name: str) -> str:
        """取函数体源码：def 行到下一个同级 def（类方法缩进 4 空格）"""
        text = path.read_text(encoding="utf-8")
        m = re.search(rf"^    (?:async )?def {func_name}\(", text, re.M)
        assert m, f"{path.name} 找不到 {func_name}"
        rest = text[m.end():]
        nxt = re.search(r"^    (?:async )?def ", rest, re.M)
        return rest[: nxt.start()] if nxt else rest

    # ── 契约常量本身 ──
    def test_grounding_contract_content(self):
        for must in ("只用上面材料里真实存在的", "材料里没有的具体细节", "不要编"):
            assert must in protocols.GROUNDING_CONTRACT, f"根基契约弱化: 缺「{must}」"
        assert "没有合适的往事就聊现在" in protocols.GROUNDING_NO_TOPIC_FALLBACK

    def test_memory_block_functional(self):
        out = protocols.memory_block("小明", "喜欢草莓")
        assert "喜欢草莓" in out
        assert "不要发明新的具体往事" in out, "记忆块丢失根基约束"

    def test_knowledge_block_functional(self):
        out = protocols.knowledge_block("活动时间是周六")
        assert "活动时间是周六" in out
        assert "不确定就说" in out

    # ── 调用点引用（函数体锚定——删调用点或退回硬编码都红灯）──
    def test_private_initiative_uses_contract(self):
        body = self._func_body(self._AUTONOMY, "_check_private_initiative")
        assert "_protocols.GROUNDING_CONTRACT" in body, "私聊插话丢失根基契约引用"
        assert "_protocols.GROUNDING_NO_TOPIC_FALLBACK" in body, "私聊插话缺「聊现在」兜底"

    def test_group_initiative_uses_contract(self):
        body = self._func_body(self._AUTONOMY, "_check_autonomous_action")
        assert "_protocols.GROUNDING_CONTRACT" in body, "群主动发起丢失根基契约引用"
        assert "_protocols.GROUNDING_NO_TOPIC_FALLBACK" in body, "群主动发起缺「聊现在」兜底"

    def test_memory_block_used_in_both_reply_paths(self):
        group = self._func_body(self._HANDLER, "handle_group_message")
        priv = self._func_body(self._HANDLER, "handle_private_message")
        assert "_protocols.memory_block" in group, "群聊路径没走统一记忆块"
        assert "_protocols.memory_block" in priv, "私聊路径没走统一记忆块"

    def test_knowledge_block_used_in_both_reply_paths(self):
        group = self._func_body(self._HANDLER, "handle_group_message")
        priv = self._func_body(self._HANDLER, "handle_private_message")
        assert "_protocols.knowledge_block" in group, "群聊路径没走统一知识库块"
        assert "_protocols.knowledge_block" in priv, "私聊路径没走统一知识库块"

    def test_profile_caveat_at_all_synthetic_injection_points(self):
        """合成画像全部注入点必须带准确性标注（2026-08-15 Codex Important 1：
        画像曾被包进自称「真实记录」的记忆块——幻觉通道只堵了一半）。
        自动群/私聊不再注入跨作用域画像；保留的显式画像出口必须带标注。"""
        priv = (
            self._func_body(self._HANDLER, "handle_private_message")
            + self._func_body(self._HANDLER, "_build_private_cross_context")
        )
        assert "_protocols.PROFILE_CAVEAT" in priv, "主人第三人画像出口缺准确性标注"
        assert "_protocols.PROFILE_CAVEAT_SHORT" in priv, "交叉上下文第三人画像缺准确性标注"
        assert "active_notes" not in self._func_body(
            self._AUTONOMY, "_check_private_initiative"), "私聊插话不得注入跨作用域画像"
        assert "_protocols.PROFILE_CAVEAT_SHORT" in self._func_body(
            self._MEMORY, "format_compact_memories"), "记忆块画像行缺准确性标注"
        assert "active_notes" not in self._func_body(
            self._HANDLER, "_build_cast_context"), "群人物关系图不得注入第三人画像"
