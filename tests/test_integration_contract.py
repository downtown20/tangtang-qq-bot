"""
接线审计测试（2026-08-15）——把 CLAUDE.md 教训从散文变成红灯。

背景：教训 #14「引擎在转，离合器没接上」三次复发（Reranker/驱动力/情绪标签），
散文清单拦不住。本测试机械断言三件事：

A. 零调用扫描：agent/ 模块的公共函数必须有生产调用方——
   调用（AST，别名感知）/ 属性访问 / 字符串引用 / @register_skill 注册都算接线。
   例外必须登记 ALLOWLIST 并写明理由（新增死代码 → 红灯）。
B. 管线契约：关键入口点必须在指定文件被调用（如驱动力投影进两条回复路径）。
C. 判定单一来源：handler.py 的插话判定只允许来自 interjection.evaluate()——
   2026-08-15 运行当天就抓出 80/65 两处 evaluate 外重判并已修复。

已知边界（文档化）：
- 纯互相调用的死模块不在此覆盖内（模块入口函数互相调用但整模块无人用）——
  需要端到端验证补位。
- 属性访问引用偏宽松（.close 会算 close() 的接线）——宁可漏报不可误报。
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
BASE = Path(__file__).parent.parent

# 生产调用方范围：agent/ napcat/ tools/ + 根目录 main.py 与控制台。
# tests/ 不算——测试调用不构成生产接线（test-only 函数 = 生产死代码）。
PROD_FILES = sorted(
    [BASE / "main.py", BASE / "糖糖控制台_qt.py"]
    + [p for d in ("agent", "napcat", "tools") for p in (BASE / d).rglob("*.py")]
)


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# key 统一为相对路径（正斜杠），如 "agent/handler.py"
_parsed = {}
for _p in PROD_FILES:
    _t = _parse(_p)
    if _t is not None:
        _parsed[str(_p.relative_to(BASE)).replace("\\", "/")] = _t


def _global_aliases() -> dict[str, str]:
    """全局别名表：from x import f as g / target = func 绑定 → 规范名。

    跨文件生效——handler.py 里 self._parse_natural_schedule = parse_natural_schedule，
    handler_commands.py 的 self.handler._parse_natural_schedule(...) 据此解析。
    （AST 静态扫描的第一课：别名绑定会藏调用点。）"""
    m: dict[str, str] = {}
    for t in _parsed.values():
        for node in ast.walk(t):
            if isinstance(node, ast.ImportFrom) and node.names:
                for a in node.names:
                    if a.asname:
                        m[a.asname] = a.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                for tg in node.targets:
                    if isinstance(tg, ast.Name):
                        m[tg.id] = node.value.id
                    elif isinstance(tg, ast.Attribute):
                        m[tg.attr] = node.value.id
    return m


_ALIASES = _global_aliases()


def _canon(name: str) -> str:
    return _ALIASES.get(name, name)


def _references() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """(调用, 属性访问, 字符串引用) 按规范名聚合 → 引用文件集合"""
    calls: dict[str, set[str]] = {}
    attrs: dict[str, set[str]] = {}
    strs: dict[str, set[str]] = {}
    for f, t in _parsed.items():
        for node in ast.walk(t):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.setdefault(_canon(node.func.id), set()).add(f)
                elif isinstance(node.func, ast.Attribute):
                    calls.setdefault(_canon(node.func.attr), set()).add(f)
            elif isinstance(node, ast.Attribute):
                attrs.setdefault(_canon(node.attr), set()).add(f)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                strs.setdefault(_canon(node.value), set()).add(f)
    return calls, attrs, strs


_CALLS, _ATTRS, _STRS = _references()


def _skill_registered() -> set[tuple[str, str]]:
    """(注册名, 文件)——@register_skill 装饰即接线（按名字分发，无静态调用点）"""
    reg: set[tuple[str, str]] = set()
    for f, t in _parsed.items():
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                        fn = dec.func
                        nm = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                        if nm == "register_skill":
                            reg.add((dec.args[0].value, f))
    return reg


_REG = _skill_registered()

# 例外登记——每个条目必须写明理由。只允许「标准工具函数保留待接线」类，
# 不允许「暂时没空接」。登记的函数被删除时 test_allowlist_entries_still_exist 会红灯。
ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent/text_utils.py", "word_match"):
        "CLAUDE.md 七章规定的标准工具函数——新功能中文匹配接线时调用（word_match_any/word_match_score 已按反模式#6 归档）",
    ("agent/platform_receipts.py", "validate_platform_receipt"):
        "ADR-006 纯验证入口——平台回执适配器尚未接入生产路由，feature-off 期间保留契约和回归测试",
}


def _agent_public_functions():
    for f in sorted(_parsed):
        if not f.startswith("agent/"):
            continue
        t = _parsed[f]
        for node in t.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                yield f, node.lineno, node.name


def _walk_with_parent(node, parent=None):
    yield node, parent
    for child in ast.iter_child_nodes(node):
        yield from _walk_with_parent(child, node)


def _target_names(node) -> list[str]:
    """赋值目标扁平化——will_interject, score, reason = ... 的目标是 Tuple，要展开"""
    names = []
    for tg in node.targets:
        if isinstance(tg, ast.Name):
            names.append(tg.id)
        elif isinstance(tg, ast.Attribute):
            names.append(tg.attr)
        elif isinstance(tg, (ast.Tuple, ast.List)):
            for el in tg.elts:
                if isinstance(el, ast.Name):
                    names.append(el.id)
                elif isinstance(el, ast.Attribute):
                    names.append(el.attr)
    return names


def _contains_call_attr(node, attr: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == attr:
            return True
    return False


HANDLER = "agent/handler.py"
AUTONOMY = "agent/handler_autonomy.py"


class TestZeroCallerGate:
    """A. 零调用扫描——新写 infra 不接业务 → 红灯"""

    def test_all_public_functions_are_wired(self):
        dead = []
        for f, lineno, name in _agent_public_functions():
            if (name, f) in _REG:
                continue  # 技能注册即接线
            refs = _CALLS.get(name, set()) | _ATTRS.get(name, set()) | _STRS.get(name, set())
            if not refs and (f, name) not in ALLOWLIST:
                dead.append(f"{f}:{lineno} :: {name}")
        assert not dead, (
            "接线审计（CLAUDE.md 教训#14）：以下 agent/ 公共函数无任何生产调用方。\n"
            "处理：接线（谁该调用它）或归档（删除，反模式#6）；\n"
            "确认是标准工具函数则在 tests/test_integration_contract.py 的 ALLOWLIST 登记理由。\n"
            + "\n".join(dead))

    def test_allowlist_entries_still_exist(self):
        """ALLOWLIST 条目过期即失效——函数删了，登记也必须删"""
        existing = {(f, n) for f, _, n in _agent_public_functions()}
        stale = [f"{f} :: {n}" for (f, n) in ALLOWLIST if (f, n) not in existing]
        assert not stale, "ALLOWLIST 登记了不存在的函数——函数已归档，删除对应登记：\n" + "\n".join(stale)


# (函数/方法名, 必须被调用的文件列表, 为什么这条契约存在)
PIPELINE_CONTRACTS = [
    ("get_drive_context", ["agent/context_builder.py"],
     "驱动力投影必须经 context_builder 模板注入 system prompt（2026-08-15 整体审查："
     "handler 直追加会与模板双注入——context_builder 是唯一注入点）"),
    ("get_recent_dialogue", ["agent/handler_autonomy.py"],
     "私聊主动说话必须有最近对话上下文（2026-08-15 补全）"),
    ("evaluate", [HANDLER],
     "插话评分必须接在群消息处理路径"),
    ("get_available_styles_for_prompt", [HANDLER],
     "情绪标签列表必须进入 LLM 工具描述（2026-08-15 接线，此前零调用）"),
    ("close_cosy_engine", ["main.py"],
     "优雅退出必须释放 CosyVoice 引擎（2026-08-15 接线，此前零调用）"),
    # 2026-08-16 范式转换（教训 #24）：parse_natural_schedule 已删除——
    # 自然语言定时意图一律 LLM 工具；此契约条目随删除移除
    ("get_method_type", [HANDLER],
     "工具方法类型查询必须接入（经 import 别名 _skill_method_type）"),
    ("record_interjection", [HANDLER],
     "插话命中必须记账（配额/冷却数据源）"),
    ("release_by_action", [HANDLER],
     "插话结果必须释放驱动力（initiated/avoided）"),
]


class TestPipelineContract:
    """B. 管线契约——关键入口点必须在指定文件被调用"""

    @pytest.mark.parametrize(
        "name,files,why", PIPELINE_CONTRACTS, ids=[c[0] for c in PIPELINE_CONTRACTS])
    def test_entry_point_wired(self, name, files, why):
        callers = _CALLS.get(name, set()) | _ATTRS.get(name, set()) | _STRS.get(name, set())
        hit = [f for f in files if f in callers]
        assert hit, f"管线契约破裂：{name} 不在 {files} 中被调用——{why}"


class TestVoiceEmotionTagContract:
    """D. 语音情绪标签约束——send_voice 工具描述必须含「最多一个+白名单」约束。
    （2026-08-24 批D：LLM 标白名单外词/标多个 → 全部兜底 normal，情绪表达失效；
    约束被删或改松 → 红灯）"""

    def test_send_voice_description_constrains_emotion_tag(self):
        # 批D 白名单约束（2026-08-24 深夜改版：主通道升级为必填枚举参数，
        # 文本标签协议保留给语音模式路径——白名单约束必须仍在描述里）
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "只能用列表里的词" in src, (
            "send_voice 文本标签路径的白名单约束被删——语音模式下 LLM 会自创标签，"
            "白名单外词全部兜底 normal，情绪表达失效")

    def test_send_voice_description_clarifies_tag_vs_topic(self):
        # 2026-08-24 晚：LLM 把话题里的情绪误标成说话语气——
        # 「有人难过的时候」被标「难过」用 sad 声线，与温柔陪伴内容打架。
        # 语义澄清「语气≠话题情绪」必须留在描述里。
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "语气不是话题情绪" in src, (
            "send_voice 工具描述的话题/语气语义澄清被删——"
            "LLM 会重新把话题里的情绪词（「有人难过的时候」）误标成语音情绪标签")

    def test_send_voice_requires_emotion_enum(self):
        # 2026-08-24 深夜：「不写用默认语气」给了 LLM 跳过情绪决策的许可——
        # 全部语音兜底 normal 音色「局促统一」（主人实测反馈）。emotion 升级为
        # 必填枚举参数：每条语音 LLM 都必须决策语气，且白名单枚举防自创词。
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert '"required": ["text", "emotion", "speed", "pause"]' in src, (
            "send_voice 的 text/emotion/speed/pause 不再是必填——LLM 会跳过内容或韵律决策，"
            "语音重新退化为统一的默认语速或默认停顿")
        assert 'sorted(EMOTION_SPEED.keys())' in src, (
            "send_voice 的 emotion 枚举不再来自 EMOTION_SPEED 单一来源——"
            "枚举与音色映射表可能分叉")

    def test_send_voice_carries_exact_spoken_text(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert "语音里只说 text 的内容" in src, (
            "send_voice 没有明确区分语音正文与工具后的自然语言回复——"
            "用户要求只说一句时，LLM 容易擅自扩写")
        assert src.count('turn_actions.get("voice_text") or reply') >= 2, (
            "LLM 明确选择的语音正文没有同时接到群聊和私聊发送路径")

    def test_voice_success_log_does_not_claim_planned_gpt_speaker(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert '语音已发送 ({voice_desc})' not in src, (
            "发送成功日志仍把计划的 GPT-SoVITS 音色当成实际音色；"
            "降级 Edge-TTS 时会产生误导性取证记录")

    def test_voice_prosody_reaches_group_and_private_send_paths(self):
        src = Path(HANDLER).read_text(encoding="utf-8")
        assert src.count('speed=turn_actions.get("voice_speed", 1.0)') >= 2, (
            "LLM 选择的语速没有同时接到群聊和私聊发送路径")
        assert src.count('pause=turn_actions.get("voice_pause", "自然")') >= 2, (
            "LLM 选择的停顿风格没有同时接到群聊和私聊发送路径")

    def test_voice_emotion_chain_has_no_keyword_fallback(self):
        # 2026-08-24 晚：真凶取证——「有人难过的时候」被 classify_emotions
        # 子串匹配标「难过」用 sad 声线（日志 97→97字 = LLM 未标签，关键词层
        # 抢答）。话题情绪 vs 说话语气是语义区分，关键词工程修不好——
        # 决策链只能是 LLM 标签 → 显式参数 → mood → normal。
        # 用 AST 查真实代码（注释/字符串不算），防「好心修复」加回。
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        hits = [
            (cls.name, m) for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            and m.name == "_send_voice_reply"
        ]
        assert hits, "找不到 _send_voice_reply 方法"
        for _, method in hits:
            refs = [
                n.id for n in ast.walk(method)
                if isinstance(n, ast.Name) and n.id == "classify_emotions"
            ]
            assert refs == [], (
                "语音情绪决策链重新出现关键词兜底（classify_emotions 子串匹配）——"
                "话题里的情绪词会被误标成说话语气，sad 声线配温柔内容（2026-08-24 事故复现）")


class TestSingleSourceThreshold:
    """C. 判定单一来源——插话判定只允许一处"""

    def test_will_interject_only_from_evaluate_or_yield(self):
        """will_interject 赋值只能来自 evaluate() 或退让约束（should_yield）——
        不允许第三判定点（2026-08-15 曾抓出 80/65 两处 evaluate 外重判）"""
        t = _parsed[HANDLER]
        violations = []
        saw_evaluate = False
        for node, parent in _walk_with_parent(t):
            if not isinstance(node, ast.Assign):
                continue
            names = _target_names(node)
            if "will_interject" not in names:
                continue
            # 来源 1：evaluate() 调用（判定唯一来源）
            if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "evaluate"):
                saw_evaluate = True
                continue
            # 来源 2：退让约束——if should_yield(...): will_interject = False（系统约束，非判定）
            if (isinstance(node.value, ast.Constant) and node.value.value is False
                    and isinstance(parent, ast.If) and _contains_call_attr(parent.test, "should_yield")):
                continue
            violations.append(node.lineno)
        assert saw_evaluate, "handler.py 的插话判定没有接 evaluate()——管线断了"
        assert not violations, (
            f"handler.py 出现 evaluate 外的 will_interject 赋值（第二判定点）: 行 {violations}\n"
            "门槛必须通过 threshold_override 传入 evaluate——判定与日志只能有一处")


class TestConversationDecisionContract:
    """群聊窗口只负责把消息交给 LLM，是否开口由 LLM 单一决策。"""

    @staticmethod
    def _group_handler_source() -> str:
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_group_message":
                return ast.get_source_segment(src, node) or ""
        raise AssertionError("找不到 handle_group_message")

    def test_active_window_has_no_system_content_gate(self):
        """窗口内不再按等待状态、长度、标点或连续次数拦截消息。"""
        method = self._group_handler_source()
        window = method.split("# 🆕 对话窗口连续性", 1)[1].split("# 插话引擎", 1)[0]
        assert "should_reply = True" in window
        assert "is_waiting_for_reply" not in window
        assert "len(text.strip())" not in window
        assert "should_yield" not in window
        assert "has_other_at" not in window

    def test_active_window_bypasses_group_batch_merge(self):
        """窗口消息逐条交给 LLM，不被 2 秒批处理合并成另一条消息。"""
        method = self._group_handler_source()
        batching = method.split("# 🆕 R1-6: 防抖/批处理", 1)[1].split("# 🍬 自主节律", 1)[0]
        assert "is_engaged(user_id, group_id)" in batching
        assert 'not msg.get("_is_pending")' in batching

    def test_routed_turn_has_no_random_lazy_veto(self):
        """进入回复管线后不能再由系统随机决定“懒得回”。"""
        method = self._group_handler_source()
        routed = method.split("if not should_reply:", 1)[1].split("# 打字节奏", 1)[0]
        assert "lazy_chance" not in routed
        assert "random.random()" not in routed

    def test_voice_only_paths_do_not_return_before_common_finalizer(self):
        """群聊和私聊语音发送后都必须继续执行聊天记录/窗口状态收尾。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        group = self._group_handler_source()
        group_voice = group.split("# 🎤 语音发送", 1)[1].split("# 引用回复", 1)[0]
        assert "return" not in group_voice
        assert "self._busy = False" not in group_voice

        tree = ast.parse(src)
        private = next(
            ast.get_source_segment(src, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_private_message"
        )
        private_voice = private.split("# 🎤 语音发送", 1)[1].split("# 私聊中移除", 1)[0]
        assert "return" not in private_voice
        assert "self._busy = False" not in private_voice

    def test_private_text_path_initializes_self_memory_action_id(self):
        """纯文字私聊不能在自我记忆收尾引用未赋值的语音 action id。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        private = next(
            ast.get_source_segment(src, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_private_message"
        )
        voice = private.split("# 🎤 语音发送", 1)[1].split("# 私聊中移除", 1)[0]
        assert '_self_memory_action_id = ""' in voice

    def test_group_persistent_voice_mode_never_sends_text_twice(self):
        """语音路径自身已负责文字降级；群持久语音模式不得再走普通文字发送。"""
        group = self._group_handler_source()
        voice = group.split("# 🎤 语音发送", 1)[1].split("# 引用回复", 1)[0]
        after_send = voice.split("ok = await self._send_voice_reply", 1)[1]
        assert "if not _in_voice_mode:" not in after_send
        assert "_voice_only = True" in after_send

    def test_autonomy_send_paths_must_enrich(self):
        """handler_autonomy.py 里每个含 _checked_send 的函数必须先调 _enrich_reply
        且发送后调 log_chat——2026-08-16 抓出主动路径直发 LLM 原文（[贴图:开心]
        原样泄露）且主动说过的话不进聊天史（记忆提取看不到=失忆感）。
        正常回复路径（handler.py）enrich+log_chat 后发，主动路径漏了——此闸门钉住。"""
        t = _parsed[AUTONOMY]
        assert t is not None, "handler_autonomy.py 解析失败"
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
                names = {getattr(c, "id", getattr(c, "attr", "")) for c in calls}
                attributes = {
                    n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
                }
                if "_checked_send" in names:
                    assert "_enrich_reply" in names, (
                        f"{node.name} 直接 _checked_send 但未调 _enrich_reply——"
                        f"[贴图:xx]/<思考> 会原样泄露（2026-08-16 事故）")
                    # 自治路径的聊天史回写经过取消安全 Store helper，
                    # 因而 log_chat 作为函数对象出现在 Attribute 而非 Call.func。
                    assert "log_chat" in names or "log_chat" in attributes, (
                        f"{node.name} 主动发送但未调 log_chat——"
                        f"糖糖主动说的话不进聊天史（2026-08-16 流程审计）")

    # 2026-08-16 发送链路全审计：LLM 文本直发点清单——每个函数必须调 _enrich_reply。
    # 新增加 LLM 直发路径时追加到本表（否则闸门不知道它的存在）。
    LLM_SEND_FUNCS = [
        (HANDLER, "handle_group_increase"),    # 入群欢迎
        (HANDLER, "handle_friend_request"),    # 好友申请打招呼
        (HANDLER, "_delayed_group_say"),       # group_say_later 延迟发送
        (HANDLER, "_execute_natural_action"),  # pm/群发言 LLM 生成
        ("agent/handler_commands.py", "_generate_and_send"),  # /说
        # /传话现在走统一 send_actions helper，非 LLM 生成路径；
        # 其清洗/回执契约由 tests/test_send_message.py 覆盖。
    ]

    @pytest.mark.parametrize("file,func", LLM_SEND_FUNCS)
    def test_llm_send_paths_must_enrich(self, file, func):
        """LLM 生成的文本发往用户前必须过 _enrich_reply（清洗+贴图解析）——
        2026-08-16 全链路审计抓出 6 处直发点（欢迎/打招呼/延迟发送/遥控发言），
        [贴图:xx] 标签原样泄露。此表钉住每一个已知 LLM 直发函数。"""
        t = _parsed[file]
        assert t is not None, f"{file} 解析失败"
        for node in ast.walk(t):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                calls = [n.func for n in ast.walk(node) if isinstance(n, ast.Call)]
                names = {getattr(c, "id", getattr(c, "attr", "")) for c in calls}
                assert names & {"_enrich_reply", "_enrich_reply_async"}, (
                    f"{file}:{func} 发送 LLM 文本但未调同步/异步 enrich——"
                    f"标签/思考段会原样泄露给用户")
                break
        else:
            raise AssertionError(f"{file} 中找不到函数 {func}（改名后请同步本表）")

    def test_sing_markers_are_captured_before_group_and_private_reply_cleaning(self):
        """[SING:] 是媒体动作，不得在 ReplyPipeline 清洗后才解析。"""
        src = Path(HANDLER).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for name in ("handle_group_message", "handle_private_message"):
            method = next(
                ast.get_source_segment(src, node) or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name
            )
            marker_parse = method.index("self._parse_sing_tag(sing_marker_reply)")
            enrich = method.index("await self._enrich_reply_async(reply")
            assert marker_parse < enrich, f"{name} 在清洗后才解析 [SING:]"
            assert "await self._send_singing_actions(" in method, (
                f"{name} 未将唱歌交给 ActionPlan child receipt"
            )
            assert "await self._send_singing_action(" not in method
            assert "await self._send_singing_reply(" not in method

    PERSIST_STATE_KEYS = [
        "state:quiet_groups", "state:voice_blocked", "state:voice_mode",
        "state:care_due", "state:pending_pm", "state:poke_tracker",
        "state:like_tracker", "state:auto_pending", "state:auto_cold",
        "state:no_friend_until", "state:sed_active",
    ]

    def test_runtime_state_persistence_wired(self):
        """2026-08-16 主人规矩：这类功能都持久化——静默/开关/冷却/风控/
        草稿重启不丢（主动私聊冷却重启骚扰事故的教训推广）。
        闸门：每个 key 必须有「绑定形态」的恢复行
        `self._X = self._load_state_kv("state:key", ...)`——仅出现 key 字符串
        不够（写穿行也含 key；2026-08-16 Codex I5：_like_tracker 恢复行存在
        但被 __init__ 后置初始化覆盖成空，旧闸门全绿没拦住）。"""
        import re as _re_gate
        text = (BASE / "agent" / "handler.py").read_text(encoding="utf-8")
        for k in self.PERSIST_STATE_KEYS:
            # 三种合法形态：self._X = [set(]self._load_state_kv(...)（直接绑定）、
            # napcat 属性恢复（no_friend_until 住在 napcat 层）、
            # 两行绑定（2026-08-17 sed_active：raw 读取行 + 类型转换绑定行）
            m = _re_gate.search(
                rf'self\._(\w+)\s*=\s*(?:set\(|)?self\._load_state_kv\("{k}"', text)
            napcat_m = _re_gate.search(
                rf'self\._load_state_kv\("{k}".*?self\.napcat\._(\w+)\s*=', text, _re_gate.S)
            two_line_m = _re_gate.search(
                rf'_(\w+)\s*=\s*self\._load_state_kv\("{k}"[^\n]*\n\s*self\._(\w+)', text)
            assert m or napcat_m or two_line_m, f"状态 {k} 无绑定形态恢复行（self._X = self._load_state_kv）——重启会失忆"

    def test_opinion_invite_contract(self):
        """2026-08-16 Codex：征集邀请文案契约——
        1. LLM 邀请发送前必须过 _enrich（贴图/思考段不得字面泄露）
        2. 邀请 prompt 必须含「禁止提到主人」——征集是糖糖自己想问"""
        text = (BASE / "agent" / "opinion.py").read_text(encoding="utf-8")
        assert "self._enrich(reply" in text, "邀请文案未过 enrich——标签会泄露"
        assert "禁止提到「主人让我来」" in text, "邀请 prompt 丢失主人保密契约"
        # 导出目录必须在知识库扫描范围外（意见含私聊内容与 QQ 号）
        assert 'Path("data") / "意见收集"' in text or "data/意见收集" in text, (
            "意见文档导出目录落入知识库范围——群友可经 search_knowledge 检索")

    def test_state_kv_round_trip(self, tmp_path):
        """_save_state_kv/_load_state_kv 往返——JSON 序列化不丢类型"""
        from agent.handler import MessageHandler
        from agent.store import Store
        h = object.__new__(MessageHandler)
        h.memory = type("M", (), {})()
        h.memory.store = Store(str(tmp_path / "kv.db"))
        h._save_state_kv("state:test", {"a": [1, 2], "b": "x"})
        assert h._load_state_kv("state:test", None) == {"a": [1, 2], "b": "x"}
        assert h._load_state_kv("state:missing", {"d": 1}) == {"d": 1}

    def test_no_hardcoded_score_comparisons(self):
        """handler.py 不允许对插话 score 做整数比较——
        2026-08-15 抓出「threshold==65 and score>=65」渐变补判已移入 evaluate。
        （情绪 score 的浮点比较 0.35/0.65 是另一领域，不在此列）"""
        t = _parsed[HANDLER]
        bad = []
        for node in ast.walk(t):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "score"):
                int_consts = [c for c in node.comparators
                              if isinstance(c, ast.Constant) and isinstance(c.value, int)]
                if int_consts:
                    bad.append(node.lineno)
        assert not bad, f"handler.py 出现 score 整数比较（插话第二门槛）: 行 {bad}"
