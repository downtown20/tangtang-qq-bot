"""2026-10-25 自查闭环 第 1 轮 ——「她不知道自己在哪儿」的两个定位缺陷

## 缺陷 A（S-1）：历史消息里的 @ 是裸数字，她不认识——包括指向她自己的那个

取证链：
1. `chat_log#300159`（群 30001，2026-10-24 23:03:09）存的是
   `……😢@QQ10002`，raw 是 `[CQ:at,qq=10002]`；
   `people` 表里 10002 的昵称**就是「小糖糖」**（她自己）。
2. 同一回合 `turn_traces#41` 她的 reasoning 原话：
   「大狗在@QQ10002，不是我（我是40007）」
   —— 把自己的号推给别人，把 40007（people 表里是「绝望的小奈203」）
   认成自己。
3. 会客厅直接问她「你自己的 QQ 号是多少」，她答：
   「我自己的号？这我还真没记住。」

根因：buffer 是 LLM 历史消息的**直接来源**，而 buffer 只存
`normalize_image_placeholder(text)` 的结果，`@QQ<号>` 原样入 buffer。
当前消息在 `_build_user_message` 里会被 `_nick_for_qq` 解析成 `@小糖糖`，
历史却不解析 —— 同一批对话两种形态并存。

## 缺陷 B（T-1）：默认上下文只有「现在是深夜」，没有日期/星期/时分

取证：`personality._get_time_context()` 旧版只返回一个时段词。
后果见 `turn_traces#25`（2026-10-24 16:11，实为**周六下午**）：她自己在
reasoning 里写「时间：周五上午」——星期和时段两处都错。而同一场景只要真正
调 `get_time` 就完全答对（`turn_traces#59`：「25号星期日」）。
所以缺的不是能力，是默认上下文里没有锚点。

同时钉住「单一口径」：prompt 的时间文案和 `get_time` 工具必须走同一份
格式化逻辑，否则会出现「prompt 说凌晨、工具说深夜」的两套词表。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HANDLER = ROOT / "agent" / "handler.py"
PERSONALITY = ROOT / "agent" / "personality.py"


@pytest.fixture
def memory(store):
    from agent.memory import MemorySystem
    return MemorySystem(store=store)


# ═══════════════════════════════════════════════════════════════════
# 缺陷 A：@ 数字必须在入 buffer 前解析成昵称
# ═══════════════════════════════════════════════════════════════════

def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path: Path, name: str):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"没找到函数 {name}（改名了？闸门要跟着改）")


def _names_assigned_from_at_qq_sub(func) -> set:
    """函数体里，哪些变量是被「@QQ 正则的 re.sub」赋值的。"""
    parsed = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "sub"):
            continue
        owner = call.func.value
        if not (isinstance(owner, ast.Name) and owner.id == "re"):
            continue
        if not call.args:
            continue
        pattern = call.args[0]
        if isinstance(pattern, ast.Constant) and "@QQ" in str(pattern.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parsed.add(target.id)
    return parsed


def _buffer_arg_is_parsed(func, buffer_attr: str = "add_to_buffer") -> bool:
    """`add_to_buffer(...)` 的**第 4 个位置参数**（消息正文）必须来自上面的 re.sub。"""
    parsed = _names_assigned_from_at_qq_sub(func)
    if not parsed:
        return False
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == buffer_attr):
            continue
        if len(node.args) < 4:
            continue
        arg = node.args[3]
        if isinstance(arg, ast.Name) and arg.id in parsed:
            return True
    return False


class TestAtQQParsedBeforeBuffer:
    def test_group_buffer_parses_qq_mention(self):
        """群聊：入 buffer 的正文必须先解析 @QQ<号>（缺陷 A 的群聊入口）"""
        assert _buffer_arg_is_parsed(_func(HANDLER, "handle_group_message")), (
            "handle_group_message 里 add_to_buffer 的正文没有经过 @QQ 解析——"
            "历史会重新变成一串她不认识的数字，turn_traces#41 那类"
            "「我是对方的号」的自指错误会复发"
        )

    def test_private_buffer_parses_qq_mention(self):
        """私聊：同一批对话在两种上下文里不能变形（缺陷 A 的私聊入口）"""
        assert _buffer_arg_is_parsed(_func(HANDLER, "handle_private_message")), (
            "handle_private_message 里 add_to_buffer 的正文没有经过 @QQ 解析"
        )

    def test_current_message_still_parses(self):
        """反向闸门：当前消息的解析（2026-08-16 那个修复）不能被这次改动顶掉。

        `handle_group_message` 里应有**两个**被 `@QQ` 正则赋值的变量：
        一个给 buffer（历史来源），一个给 `user_text`（本轮当前消息）。
        只剩一个说明有一侧的解析被删了。
        """
        parsed = _names_assigned_from_at_qq_sub(_func(HANDLER, "handle_group_message"))
        assert len(parsed) >= 2, (
            f"handle_group_message 里被 @QQ 正则赋值的变量只有 {sorted(parsed)}——"
            "当前消息和历史必须各自解析一次，少一边就会出现"
            "「同一批对话两种形态」或「@对象丢失」"
        )


class TestSelfQQResolvesToName:
    def _handler(self, memory, bot_qq: str = "10002"):
        from agent.handler import MessageHandler
        h = object.__new__(MessageHandler)
        h.memory = memory
        h.bot_qq = bot_qq
        return h

    def test_self_qq_resolves_to_own_nickname(self, memory):
        """她自己的号必须解析成她的名字——不是一串数字（缺陷 A 的身份侧）"""
        h = self._handler(memory)
        memory.store.get_or_create_person("10002", "小糖糖")
        assert h._nick_for_qq("10002", "30001") == "小糖糖"

    def test_other_qq_resolves_to_their_nickname(self, memory):
        """别人也一样——「绝望的小奈203」不能在她的历史里退化成一个号"""
        h = self._handler(memory)
        memory.store.get_or_create_person("40007", "绝望的小奈203")
        assert h._nick_for_qq("40007", "30001") == "绝望的小奈203"

    def test_unknown_qq_keeps_old_fallback(self, memory):
        """没建档的号仍按号码原样返回——本次改动不比原来更差"""
        h = self._handler(memory)
        assert h._nick_for_qq("1234567", "30001") == "1234567"


# ═══════════════════════════════════════════════════════════════════
# 缺陷 B：时间上下文必须带日期/星期/时分，且与 get_time 工具同源
# ═══════════════════════════════════════════════════════════════════

class TestTimeContextSelfLocation:
    def test_prompt_time_reuses_single_formatter(self):
        """prompt 的时间文案必须复用 sensory.get_current_time（单一口径）"""
        func = _func(PERSONALITY, "_get_time_context")
        imports_shared = False
        for node in ast.walk(func):
            if isinstance(node, ast.ImportFrom) and node.module == "sensory":
                if any(alias.name == "get_current_time" for alias in node.names):
                    imports_shared = True
        assert imports_shared, (
            "_get_time_context 又自己写了一套时段词表——prompt 说「凌晨」、"
            "工具说「深夜」的两套口径会让同一天的两个说法打架"
        )

    def test_sensory_formatter_accepts_injected_now(self):
        """格式化函数必须可注入时钟——否则测试只能读真实时间（假绿）"""
        from agent.sensory import get_current_time
        import inspect
        sig = inspect.signature(get_current_time)
        assert "now" in sig.parameters, (
            "get_current_time 没有 now 参数：patch('datetime.datetime') 对"
            "模块级 `from datetime import datetime` 绑定无效，测试会一直读真实时间"
        )

    def test_formatter_output_has_date_weekday_and_clock(self):
        """给定时刻的输出必须含年月日 + 星期 + 时分（缺陷 B 的判据）"""
        import datetime as _dt
        from agent.sensory import get_current_time
        out = get_current_time(_dt.datetime(2026, 10, 24, 16, 11))
        assert "2026年10月24日" in out
        assert "星期六" in out
        assert "16点11分" in out

    def test_formatter_never_carries_style_words(self):
        """反向闸门：2026-08-17 定的「只报时间不规定风格」不能被这次改动推翻"""
        import datetime as _dt
        from agent.sensory import get_current_time
        for hour in (0, 8, 11, 13, 17, 21, 23):
            out = get_current_time(_dt.datetime(2026, 8, 17, hour, 0))
            for banned in ("懒散", "走心", "夜猫子", "该睡了", "乖"):
                assert banned not in out, f"{hour}点带入了风格词：{out}"


# ═══════════════════════════════════════════════════════════════════
# 缺陷 B-2：时间块曾**从来没进过她的上下文**（截断吃掉）
# 缺陷 B-3：尺子坏了——`estimate_tokens` 把真实 token 高估 2.36 倍，
#           导致 44% 的 system prompt 被误砍（不只是时间块，「## 关系」
#           「说话示范」整段都在被砍的那一侧）
#
# B-3 的取证（2026-10-25，同一模型 API 实测）：
#   真实 system prompt = 3000 字符 / 中文 2340 / 其他 660
#   `usage.prompt_tokens` = **1926**（含 user「在吗」）
#   旧公式 `中文×1.8 + 其他×0.5` = **4542** → 高估 2.36 倍
#   ⇒ 预算 3000「估算 tokens」实际只装得下约 1271 真实 tokens
#
# 修完两处之后，正确状态是：**真实 prompt 根本不该触发截断**，
# 尾部段（「## 关系」「说话示范」「## 此刻」）必须全部在场。
# ═══════════════════════════════════════════════════════════════════

class TestPromptBudgetKeepsWholeManual:
    @staticmethod
    def _build_closed():
        from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
        from agent.context_builder import ContextBuilder
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖"))
        base = eng.build_system_prompt(Relationship.CLOSE)
        cb = ContextBuilder(self_state=None)
        limit = cb.BUDGET_CORE + cb.BUDGET_RELEVANT + cb.BUDGET_SUPPLEMENTARY
        res = cb.build(
            user_id="10001", nickname="主人", message="在吗",
            system_prompt_base=base, history_messages=[],
        )
        return cb, limit, res

    def test_real_prompt_fits_within_budget(self):
        """真实 system prompt 不该超预算——「说明书完整送达」的第一条判据"""
        from agent.personality import PersonalityEngine, PersonalityConfig, Relationship
        from agent.context_builder import estimate_tokens
        eng = PersonalityEngine(PersonalityConfig(name="小糖糖"))
        base = eng.build_system_prompt(Relationship.CLOSE)
        _cb, limit, _res = self._build_closed()
        assert estimate_tokens(base) <= limit, (
            f"prompt 估 {estimate_tokens(base)} > 预算 {limit}——尺子又被改回去了？"
            "（旧系数 1.8 会把它估成 4542，于是砍掉 44% 的内容）"
        )

    def test_tail_sections_all_survive(self):
        """判据：渲染后尾部各段必须全在——不只是时间块，是整本说明书"""
        _cb, _limit, res = self._build_closed()
        sp = res.system_prompt
        missing = [seg for seg in (
            "## 你是谁", "## 性格", "说话示范", "## 回复前先想", "## 关系", "## 此刻",
        ) if seg not in sp]
        assert not missing, (
            f"这些段被截断吃掉了：{missing}——她的说明书又只剩一半"
        )

    def test_estimate_tokens_matches_measured_usage(self):
        """尺子校准回归：估算必须贴着 API 实测值（旧系数会红）

        样本与实测值都写死在这里，避免「改了系数却没人发现」
        ——这正是 B-3 能存在这么久的原因：没有任何测试拿真实 token 校过。
        """
        from agent.context_builder import estimate_tokens
        sample = (
            "今天下午我去图书馆借了两本书，一本是关于认知心理学的，"
            "另一本讲的是城市规划和公共交通的关系。回来的路上下了小雨，"
            "我在便利店买了把伞，顺手带了一杯热豆浆。晚上做饭的时候发现"
            "冰箱里的鸡蛋只剩两个了，明天得记得补。"
        ) * 2
        measured = 133  # API usage.prompt_tokens，2026-10-25 实测
        est = estimate_tokens(sample)
        ratio = est / measured
        assert 0.85 <= ratio <= 1.40, (
            f"估算 {est} vs 实测 {measured} = {ratio:.2f} 倍。旧系数（中文 1.8）"
            f"会算出 {int(sum(1 for c in sample if '一' <= c <= '鿿') * 1.8 + (len(sample) - sum(1 for c in sample if '一' <= c <= '鿿')) * 0.5)}"
            "（2.7 倍），预算全部虚高，内容被误砍"
        )

    def test_exempt_sections_survive_forced_truncation(self):
        """强制截断路径：人工构造超长 base，白名单里的段必须活下来

        不依赖「真实 prompt 恰好超预算」这个易变前提——那条前提在未来任何
        一次 prompt 瘦身之后都会失效，届时闸门会静默变成测空气。
        """
        from agent.context_builder import ContextBuilder, estimate_tokens
        filler = "## 废话段\n" + ("这是一段用来撑爆预算的填充文本，没有任何实际含义。" * 400)
        base = filler + (
            "\n\n## 必须遵守\n契约正文在此。"
            "\n\n## 此刻\n现在是2026年10月25日 星期日 深夜1点09分"
            "\n\n## 关系\n亲密度 0/100。"
        )
        cb = ContextBuilder(self_state=None)
        limit = cb.BUDGET_CORE + cb.BUDGET_RELEVANT + cb.BUDGET_SUPPLEMENTARY
        assert estimate_tokens(base) > limit, "构造的 base 没超预算，测不到截断路径"
        res = cb.build(user_id="1", nickname="测试", message="在吗",
                       system_prompt_base=base, history_messages=[])
        sp = res.system_prompt
        assert "## 必须遵守" in sp, "契约被截断吃掉了"
        assert "## 此刻" in sp, "时间块被截断吃掉了——turn_traces#25 那类自编时间会复发"
        assert len(sp) < len(base), "没有真的截断，测试在测空气"

    def test_time_block_format_in_final_prompt(self):
        """「## 此刻」段必须带真实日期/星期/时分"""
        import re
        _cb, _limit, res = self._build_closed()
        sp = res.system_prompt
        assert "## 此刻" in sp
        m = re.search(r"## 此刻\n(现在是\d{4}年\d{1,2}月\d{1,2}日 星期[一二三四五六日] \S+\d{1,2}点\d{2}分)", sp)
        assert m, f"「## 此刻」段格式不对：{sp[sp.find('## 此刻'):][:120]}"

    def test_exempt_whitelist_contains_this_moment(self):
        """源码闸门：白名单里必须有「## 此刻」——别人重构时不会想当然删掉"""
        src = (ROOT / "agent" / "context_builder.py").read_text(encoding="utf-8")
        assert '"## 此刻"' in src or "'## 此刻'" in src, (
            "context_builder 的不可裁白名单里没有「## 此刻」，"
            "时间块会重新被尾部截断吃掉"
        )
