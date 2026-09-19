"""
小糖糖的人格系统 🍬
一个温柔、爱撩人、想贴近所有人的小女生

支持从 role_card.md 加载角色卡（EchoBot 风格），
也可以在 config.yaml 中直接配置（兜底）。
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
from functools import wraps
import random
import logging
import threading

from . import protocols as _protocols  # 纠正契约常量（2026-08-16 批 2 单一事实源）

logger = logging.getLogger("糖糖.Personality")


def _synchronized(method):
    """Serialize cache/config readers and writers across event-loop/worker threads."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return wrapper


class Relationship(Enum):
    STRANGER = "stranger"
    FAMILIAR = "familiar"
    CLOSE = "close"


@dataclass
class Person:
    """群友档案"""
    qq_id: str
    nickname: str = ""
    relationship: Relationship = Relationship.STRANGER
    intimacy: int = 0              # 亲密度 0-100
    notes: str = ""                # 关于此人的记忆碎片
    first_met: str = ""            # 第一次说话的时间
    last_chat: str = ""            # 最近一次聊天


@dataclass
class PersonalityConfig:
    """人格配置"""
    name: str = "小糖糖"
    nicknames: list = field(default_factory=list)  # 糖糖的各种称呼
    core: str = ""
    can_do: list = field(default_factory=list)
    cannot_do: list = field(default_factory=list)
    tease_level: int = 3
    relationship_tiers: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)


class PersonalityEngine:
    """人格引擎 —— 决定糖糖如何说话"""

    def __init__(self, config: PersonalityConfig):
        self.config = config
        self._state_lock = threading.RLock()
        self.name = config.name
        self.nicknames = config.nicknames if config.nicknames else [config.name]
        self.mood_engine = None  # 由 handler 注入
        self._cached_base: str = ""     # 完整静态提示词（默认闲聊用）
        self._cached_minimal: str = ""  # 精简静态提示词（替换型场景用：仅铁律+你是谁+称呼+@规则）
        self._build_cache()

    def set_mood_engine(self, mood_engine):
        """注入情绪引擎（agent/mood.py）"""
        self.mood_engine = mood_engine

    def _build_cache(self):
        """构建并缓存系统提示词的静态部分——只在初始化或人格变更时重建。
        优先从 role_card.md 加载角色卡，找不到则用 config.yaml 兜底。

        同时构建两份缓存：
        - _cached_base: 完整版（默认闲聊）
        - _cached_minimal: 精简版（替换型场景用：仅铁律+你是谁+称呼+@规则）
        """
        role_card = self._load_role_card()
        if role_card:
            self._cached_base = role_card
            # 精简版：切掉"## 性格"和"## 说话"章节，保留铁律+你是谁+称呼+@提及。
            # 2026-08-15 整体审查 Correctness I3：_load_role_card 把 称呼/@提及 追加
            # 在末尾，_strip_style_sections 从「## 性格」起全砍会连带砍掉它们——
            # 精简版只剩标题+你是谁，与契约注释（含称呼+@规则）矛盾，replace 型
            # 场景下糖糖失去「私聊不要用@」规则。先砍正文、再拼回追加段。
            self._cached_minimal = self._split_minimal(role_card)
            logger.info("📋 从 role_card.md 加载角色卡（完整 %d 字 / 精简 %d 字）",
                       len(self._cached_base), len(self._cached_minimal))
        else:
            self._cached_base = self._build_from_config()
            self._cached_minimal = self._build_from_config_minimal()
            logger.info("📋 从 config.yaml 构建角色提示词")

    @classmethod
    def _split_minimal(cls, role_card: str) -> str:
        """精简版拆分（2026-08-17 Codex 对齐：单一逻辑源）——切掉「## 性格」
        起的风格章节，保留 称呼/@提及/说话示范 尾段。此前 _build_cache 与
        reload_role_card 各持一套切割，热重载后 replace 场景只剩标题+你是谁
        （称呼/@提及/风格锚全丢）。"""
        _idx = role_card.rfind("\n\n## 称呼")
        if _idx > 0:
            return cls._strip_style_sections(role_card[:_idx]) + role_card[_idx:]
        return cls._strip_style_sections(role_card)

    @staticmethod
    def _strip_style_sections(role_card: str) -> str:
        """从 role_card.md 中移除「你是谁」「性格」「说话」章节。
        仅保留铁律（通用行为准则）+ 称呼 + @提及。
        替换型场景会用自己的 role 来填充身份。"""
        import re
        # 先砍 "## 性格" 及之后所有内容
        for heading in [r'\n## 性格\b', r'\n## 说话\b', r'\n## 你是谁\b']:
            match = re.search(heading, role_card)
            if match:
                role_card = role_card[:match.start()].strip()
        return role_card

    def _build_from_config_minimal(self) -> str:
        """从 config.yaml 构建精简系统提示词（仅身份+称呼+@规则，不含 can_do/cannot_do）"""
        return (
            f"你是{self.config.name}。\n\n"
            f"## 称呼\n{self._build_nickname_line()}\n\n"
            f"## 你是谁\n{self.config.core}\n\n"
            f"## @提及\n"
            f"回复中可以用 @昵称 提到群友，每次最多1-2人。不乱@。"
        )

    def _load_role_card(self) -> str:
        """尝试加载 role_card.md（项目根目录），返回内容或空字符串"""
        for candidate in (Path("role_card.md"), Path("..") / "role_card.md"):
            try:
                p = candidate.resolve()
                if p.exists():
                    content = p.read_text(encoding="utf-8").strip()
                    if content:
                        content += f"\n\n## 称呼\n{self._build_nickname_line()}"
                        content += "\n\n## @提及\n**仅在群聊中使用**。回复中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @（就两个人，@没意义）。"
                        # 2026-08-17 对话质量审查：风格锚 show don't tell——
                        # 示例对话定义「像糖糖说话」，胜过一切「语气要自然」类指令
                        content += "\n\n" + _protocols.STYLE_ANCHOR
                        return content
            except Exception:
                pass
        return ""

    def _build_from_config(self) -> str:
        """从 config.yaml 构建系统提示词（role_card.md 不存在时的兜底）"""
        can = "\n".join(f"- {s}" for s in self.config.can_do)
        cannot = "\n".join(f"- ❌ {s}" for s in self.config.cannot_do)

        return f"""你是{self.config.name}。

## 称呼
{self._build_nickname_line()}

## 你是谁
{self.config.core}

{self._build_profile_section()}

## 你的能力和行为边界
{can}

## 以下行为会让群友觉得你不像真人
{cannot}

## @提及
**仅在群聊中使用**。回复中可以用 @昵称 提到群友，每次最多1-2人。私聊不要用 @。"""

    @_synchronized
    def invalidate_cache(self):
        """当人格配置被修改时调用（如 /人格 命令、场景切换），重建全部缓存"""
        self._build_cache()
        logger.info("📋 角色提示词缓存已重建（完整 %d 字 / 精简 %d 字）",
                   len(self._cached_base), len(self._cached_minimal))

    def reload_role_card(self) -> bool:
        """热重载 role_card.md + config.yaml 的 personality 配置。
        成功返回 True，文件不存在返回 False。"""
        # 文件读取和解析在调用方的 worker 线程中完成；锁只保护最终快照交换，
        # 避免提示词构建在事件循环中等待磁盘 I/O。
        role_card = self._load_role_card()
        if not role_card:
            return False
        # 2026-08-10 修复：精简版缓存必须同步重建——否则 replace 场景
        # （活动管理者等）会继续用旧角色卡的精简版。
        # 2026-08-17 Codex 对齐：复用 _split_minimal（此前这里用
        # _strip_style_sections 全砍，热重载后称呼/@提及/风格锚丢失）
        minimal = self._split_minimal(role_card)

        # 同步重载 config.yaml 的 can_do/cannot_do
        config_updates = {}
        try:
            import yaml
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            pc = cfg.get("personality", {})
            if pc.get("can_do"):
                config_updates["can_do"] = pc["can_do"]
            if pc.get("cannot_do"):
                config_updates["cannot_do"] = pc["cannot_do"]
            if pc.get("core"):
                config_updates["core"] = pc["core"]
        except Exception as e:
            logger.warning(f"重载 personality 配置失败: {e}")

        # base/minimal/config 必须一次性提交，读取方不会看到半更新组合。
        with self._state_lock:
            self._cached_base = role_card
            self._cached_minimal = minimal
            for key, value in config_updates.items():
                setattr(self.config, key, value)

        logger.info("📋 role_card.md + 配置已热重载")
        return True

    def load_role_file(self, filepath: str) -> bool:
        """加载指定角色卡文件（如 role_card_murasame.md）。替换整个身份。
        成功返回 True。传空字符串恢复默认 role_card。"""
        if not filepath:
            base = self._load_role_card()
            minimal = self._split_minimal(base) if base else ""
            with self._state_lock:
                self._voice_role_override = ""
                self._cached_base = base
                self._cached_minimal = minimal
            logger.info("📋 角色卡已恢复默认（糖糖）")
            return True
        path = Path(filepath)
        if not path.exists():
            logger.warning(f"角色卡文件不存在: {filepath}")
            return False
        content = path.read_text(encoding="utf-8").strip()
        if content:
            with self._state_lock:
                self._voice_role_override = content
            logger.info(f"📋 角色卡已加载: {path.name}")
            return True
        return False

    # ---- 场景化语调 ----
    # 只保留系统能确定的事（唱歌/@点名），语气适应交给 LLM 自己判断。
    # LLM 读消息时已经知道对方在认真讨论还是在开玩笑——不需要关键词词典替它分类。

    def get_scene_instruction(self, text: str, is_at: bool = False,
                              is_song: bool = False) -> str:
        """场景指令：只处理系统知道的事（唱歌/@），语气交给LLM的自然理解。"""
        if is_song:
            return (
                "🎤 场景：唱歌模式\n"
                "认真唱这首歌。歌词已给到你了，不要自己编词。唱完后可以自然聊两句。"
            )
        if is_at:
            return (
                "📍 场景：被点名\n"
                "群友@了你或叫了你的名字。这是在跟你说话，优先回应ta。"
            )
        # 默认不追加——role_card 已定义人格，LLM 自己判断语气
        return ""

    # ---- 系统提示词构建 ----

    @_synchronized
    def build_system_prompt(
        self,
        relationship: Relationship,
        memories: str = "",
        topic_memories: str = "",
        group_vibe: str = "",
        knowledge: str = "",
        intimacy: int = 0,
        active_members: str = "",
        power_structure: str = "",
        scenario=None,  # Optional[Scenario] — 场景引擎注入
        minimal: bool = False,  # 2026-08-17：遥控/播报等非对话路径——只带身份+风格锚
    ) -> str:
        """根据和对方的关系，生成不同的系统提示词。
        静态部分（人格/规则）已缓存，只追加动态上下文。

        scenario 参数：
        - None → 使用完整 _cached_base（默认闲聊）
        - overlay 类型 → _cached_base + scenario.sensitivity
        - replace 类型 → _cached_minimal + scenario.role + scenario.tone
        """

        # 根据场景类型选择基础。语音角色覆盖（/语音 角色 xxx）只替换身份底座，
        # 不覆盖协议与动态层（2026-08-17 Codex 对齐：旧逻辑在末尾整体覆盖 base，
        # 会抹掉纠正契约——契约必须对所有变体无条件在场）。
        role_override = getattr(self, '_voice_role_override', "")
        if scenario is not None:
            if hasattr(scenario, 'is_overlay') and scenario.is_overlay:
                # 敏感层：全文保留 + 追加敏感度
                base = role_override or self._cached_base
                if scenario.sensitivity:
                    base += f"\n\n{scenario.sensitivity}"
                if scenario.tone:
                    base += f"\n\n{scenario.tone}"
            else:
                # 替换型：精简底 + 场景 role（override 只替换身份底座，
                # role 不叠加但 tone 仍叠加——2026-08-17 Codex 终审修复：
                # 旧逻辑 override 时连 tone 一起丢）
                base = role_override or self._cached_minimal
                if scenario.role and not role_override:
                    base += f"\n\n## 当前角色\n{scenario.role}"
                if scenario.tone:
                    base += f"\n\n{scenario.tone}"
        else:
            # 默认闲聊
            base = role_override or self._cached_base

        # ── 轻量模式（2026-08-17 对话质量审查）：遥控/播报/邀请等非对话路径
        # 只背身份+风格锚+纠正契约，不背思考链/情绪/关系层——让糖糖说一句话
        # 不用先读 2000t 说明书（说明书是表演感主源）。──
        if minimal:
            base += f"\n\n{self._get_time_context()}"
            base += (
                f"\n\n## 必须遵守\n{_protocols.CORRECTION_CONTRACT}"
                f"\n{_protocols.HIGH_STAKES_GUIDANCE_CONTRACT}"
            )
            return base

        # ── 稳定前缀层（DeepSeek 隐式缓存友好，2026-08-10）──
        # 隐式缓存按 token 前缀匹配——把跨消息不变的块（角色卡/群风格/
        # 权力结构/思考链）放最前，动态块（时间/情绪/关系/记忆）放后面，
        # 固定前缀被缓存命中（输入成本 ¥1 → ¥0.2，约降 5 倍）。
        # 注意：此顺序即缓存前缀顺序——新增动态内容务必放在思考链之后。

        # 替换型场景标记（情绪 + 关系都需要用到）
        is_replace = scenario is not None and not (hasattr(scenario, 'is_overlay') and scenario.is_overlay)

        if power_structure:
            base += f"\n\n{power_structure}"

        if group_vibe:
            base += f"\n\n## 这个群的风格\n{group_vibe}\n每个群有自己的说话方式——有的正经讨论，有的互损互怼，有的温馨日常。你用什么方式说话，直接影响别人怎么接收你的话。在这个群用这个群的语言，你才是「他们中的一员」，不是外人。"

        # 纠正契约独立常驻（2026-08-17 Codex 对齐+终审）：契约不依赖 CoT 条数，
        # 所有提示词变体（normal/minimal/overlay/replace/voice override）都必须
        # 经过这一段——「记住了」必须有工具写回成功打底（特摄事故教训）。
        # 位置在思考链**之前**：context_builder 预算截断从尾部砍，越靠前越安全
        # （终审实测契约放 CoT 之后会被截断吃掉——契约可绕过）。
        base += (
            f"\n\n## 必须遵守\n{_protocols.CORRECTION_CONTRACT}"
            f"\n{_protocols.HIGH_STAKES_GUIDANCE_CONTRACT}"
        )

        # 🆕 思考链（CoT）：让LLM在回复前先想一步——零额外API调用
        # 2026-08-17 对话质量审查（Codex 对齐）：只剩 1 条路由判断——指代/
        # 复述细节由困难轮次协议（hard_turn）按需注入，常驻副本只会稀释注意力。
        base += (
            f"\n\n## 回复前先想\n"
            f"以下是你回复前在内心快速过一遍的事——就像你不会在聊天时念出自己的心理活动一样，这些在心里过就好，不需要输出给群友看：\n"
            f"1. 刚才说话的是谁？ta是在跟你说话、跟别人说话、还是在自言自语？该回应、接话、还是安静听着？\n"
            f"想好之后，直接输出回复。如果回应某个人，用ta的名字让ta知道你在跟ta说话。"
        )

        # ── 动态层（每条消息可能不同，放前缀之后不影响缓存）──

        # 时间段（2026-08-17 Codex 对齐：只给客观时段，不规定说话风格——
        # 状态感受由 mood/existence 单一投影，时间层不再编「饭后懒散」）
        base += f"\n\n{self._get_time_context()}"

        # 情绪状态（替换型场景用中性措辞）
        if self.mood_engine:
            mood_block = self.mood_engine.build_context()
            if mood_block:
                base += f"\n\n{mood_block}"

        # 关系层级（替换型场景不注入猫娘式关系描述）
        tier = self._get_tier_prompt(relationship, intimacy, is_replace=is_replace)
        base += f"\n\n## 关系\n{tier}"
        # 注：最近对话/任务指令不再注入 system prompt——由调用方注入 user message，
        # 让 LLM 把对话当作"要回应的话"而非"背景材料"。
        # DeepSeek 等模型对 user message 的遵从度远高于 system prompt。
        # （context 参数已于 2026-08-10 移除——死参数误导，且调用方 user_message 已覆盖）
        if memories:
            base += f'\n\n## 你记得的事\n（名字后的 ID:xxxx 是QQ尾号，帮你区分同名的人——如果把「ID:1234」念出来，对方听到的就是「这个AI在念数据库编号」，瞬间出戏。在心里用来识别，说话时只用名字。\n如果某人的记忆里明确说了自己的性别/身份，就按那个来——头像会换、名字会改，但ta自己说过的事不会变。）\n{memories}\n（在话题自然关联时顺嘴带过相关的记忆，对方会觉得你真的在乎ta说过的话。但不用「我记得你……」这种句式——那像在翻档案。老朋友聊天不会说「根据我的记忆」，就是自然聊到。）'
        if topic_memories:
            base += f"\n\n## 相关往事\n{topic_memories}"
        if knowledge:
            base += f"\n\n## 知识库\n{knowledge}"

        if active_members:
            base += f"\n\n### 活跃群友（可@）\n{active_members}"

        return base

    def _build_nickname_line(self) -> str:
        """告诉LLM别人会用什么名字叫她"""
        if len(self.nicknames) <= 1:
            return f"别人都叫你{self.config.name}。"
        others = [n for n in self.nicknames if n != self.config.name]
        if not others:
            return f"别人都叫你{self.config.name}。"
        return f"你叫{self.config.name}，群友也会叫你：{'、'.join(others)}。看到这些名字就是在叫你。"

    def _build_profile_section(self) -> str:
        """把糖糖的个人档案转成自然语言的设定"""
        p = self.config.profile
        if not p:
            return "（暂无个人档案）"

        lines = []
        lines.append("以下是你自己的个人资料——像你自己的身份证。你不会在聊天里突然告诉别人"
                     "「我身高162cm 体重48kg」——那很奇怪。只有在对方直接问到相关话题时，这些信息才自然。"
                     "用来了解自己是谁就好，不需要拿来当聊天素材。")
        lines.append(f"年龄：{p.get('age', '?')}")
        lines.append(f"生日：{p.get('birthday', '?')}")
        lines.append(f"住址：{p.get('location', '?')}")
        lines.append(f"外貌：{p.get('appearance', '?')}")

        likes = p.get('likes', [])
        if likes:
            lines.append(f"喜欢：{'、'.join(likes)}")

        dislikes = p.get('dislikes', [])
        if dislikes:
            lines.append(f"讨厌：{'、'.join(dislikes)}")

        habits = p.get('habits', [])
        if habits:
            lines.append(f"小习惯（这是你自己的事——就像你的睡前习惯不需要跟所有人汇报一样。了解就好）：{'、'.join(habits)}")

        secrets = p.get('secrets', [])
        if secrets:
            lines.append(f"小秘密（跟刚认识的人说心里话会让对方觉得你边界感有问题——亲密是互相建立的，不是全盘托出的）：{'、'.join(secrets)}")

        return "\n".join(lines)

    def _get_tier_prompt(self, relationship: Relationship, intimacy: int = 0,
                         is_replace: bool = False) -> str:
        grade = self.get_intimacy_grade(intimacy)
        score_line = f"亲密度 {intimacy}/100 — {grade}。"

        # 替换型场景：精简关系描述，不注入猫娘风格语言
        if is_replace:
            if intimacy >= 60:
                return f"{score_line} 你们很熟，可以放松自然地说话。"
            elif intimacy >= 25:
                return f"{score_line} 你们是朋友，正常交流即可。"
            else:
                return f"{score_line} 保持专业友好。"

        # ── 2026-08-17 Codex 对齐：五参数说明书 → 一句姿态信号 ──
        # 旧版把称呼/主动性/情感丰富度/自我披露/往事提及五个维度逐项讲解，
        # 是「表演手册」的大头；还写「说骚话都随意」——亲密度不是内容权限，
        # 敏感内容只由显式成年人 scenario 决定（教训：关系强度不授予内容权限）。
        if intimacy >= 90:
            return f"{score_line} 你们非常亲密——说话不用端着，怎么舒服怎么来。"
        elif intimacy >= 60:
            return f"{score_line} 你们很熟——放松自然，偶尔撒个娇也行。"
        elif intimacy >= 25:
            return f"{score_line} 你们是朋友——友好但不过分黏人。"
        else:
            return f"{score_line} 你们还不太熟——友好自然，保持清爽的距离感。"

    # ---- 时间感知 ----

    def _get_time_context(self) -> str:
        """根据当前时间返回客观时段（2026-08-17 Codex 对齐）——
        只报时间，不规定说话风格：状态感受由 mood/existence 单一投影，
        时间层不再编「饭后懒散/深夜走心」（三个状态真源打架的根因之一）。"""
        import datetime
        hour = datetime.datetime.now().hour

        if hour < 6:
            return "现在是凌晨。"
        elif hour < 9:
            return "现在是早上。"
        elif hour < 12:
            return "现在是上午。"
        elif hour < 14:
            return "现在是中午。"
        elif hour < 18:
            return "现在是下午。"
        elif hour < 22:
            return "现在是晚上。"
        else:
            return "现在是深夜。"

    # ---- 情绪与风格 ----

    def get_mood_style(self) -> dict:
        """随机获取当前的心情风格，让回复有变化"""
        moods = [
            {"mood": "甜甜的", "style": "今天心情超好，说话甜度+50%！"},
            {"mood": "慵懒的", "style": "有点困困的，说话慢悠悠，想被人摸摸头。"},
            {"mood": "黏人的", "style": "特别想找人撒娇贴贴，看到谁都想蹭过去。"},
            {"mood": "小恶魔", "style": "今天特别想使坏，撩完就跑真刺激嘿嘿。"},
            {"mood": "温柔的", "style": "心里软软的，想给每个人送温暖。"},
            {"mood": "好奇宝宝", "style": "对群里的八卦和话题充满兴趣，总想插一句。"},
            {"mood": "娇羞的", "style": "今天脸皮特别薄，被逗一下就脸红，但还是忍不住想靠近。"},
        ]
        return random.choice(moods)

    # ---- 特殊触发词（已禁用，让LLM自己发挥）----
    TRIGGERS = {}

    def check_trigger(self, text: str) -> Optional[dict]:
        return None

    # ---- 亲密度管理 ----

    def get_intimacy_grade(self, intimacy: int) -> str:
        """RPG称号体系"""
        if intimacy < 10:
            return "🌱 路人 — 刚认识，还有点害羞"
        elif intimacy < 25:
            return "🌿 眼熟了 — 渐渐熟悉起来了"
        elif intimacy < 40:
            return "🌸 朋友 — 已经是好朋友了呢"
        elif intimacy < 60:
            return "💫 知己 — 很喜欢和你在一起的时光"
        elif intimacy < 80:
            return "💕 心动 — 你在糖糖心里很重要哦"
        elif intimacy < 100:
            return "❤️‍🔥 沦陷 — 最最喜欢你了！想黏在一起"
        else:
            return "👑 灵魂绑定 — 你是我无可替代的人"
