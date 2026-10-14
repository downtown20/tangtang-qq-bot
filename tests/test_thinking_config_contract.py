"""思考配置契约（2026-09-21）。

## 为什么要有这个

思考是 DeepSeek V4 系列的**默认行为**，而它和正文**共用 max_tokens 这一个预算**。
官方给的默认值：非思考 8K、思考模式 64K、reasoning_effort=max 时 128K。
所以「思考开着、max_tokens 却很小」会得到 **finish_reason=length + 正文为空**——
不是"回复变短"，是**一个字都没有**。2026-08-17 那次「120 次空回复、117 次是
后台提取」就是这个机制，当时的修法是给提取类任务硬编码关思考。

现在把它做成配置（`llm.thinking` / `llm.thinking_effort` / `llm.light_thinking`），
本测试钉住三件事：

1. **auto = 什么都不发**。这是最要紧的一条：官方默认值会随版本调整，
   跟着走比自己钉死一个数安全。auto 必须真的"不发言"。
2. **非法值一律降级成 auto**，不抛异常、不影响启动——配置写错了不该让 bot 起不来。
3. **关思考时不发强度**。`thinking=off` + `reasoning_effort=max` 是自相矛盾的，
   发出去只会让人以为配置生效了。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def tb(thinking, effort=""):
    from agent.handler import _thinking_body
    return _thinking_body(thinking, effort)


# ---------------------------------------------------------------------------
# 1. auto = 什么都不发
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["auto", "", None, "AUTO", "  auto  "])
def test_auto_sends_nothing(mode):
    """auto 必须真的不发言——服务端默认值会变，钉死一个数就落后了。"""
    assert tb(mode) == {}, f"{mode!r} 应该什么都不发"


# ---------------------------------------------------------------------------
# 2. 开关
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["off", "disabled", "false", "0", "no", "OFF"])
def test_off_disables(mode):
    assert tb(mode) == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("mode", ["on", "enabled", "true", "1", "yes", "ON"])
def test_on_enables(mode):
    assert tb(mode) == {"thinking": {"type": "enabled"}}


# ---------------------------------------------------------------------------
# 3. 强度
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("effort", ["low", "high", "max", "LOW", " max "])
def test_effort_sent_when_not_off(effort):
    out = tb("auto", effort)
    assert out == {"reasoning_effort": effort.strip().lower()}
    assert "thinking" not in out          # auto 不发 thinking 字段


def test_effort_ignored_when_off():
    """关思考时发强度是自相矛盾的——发出去会让人以为配置生效了。"""
    assert tb("off", "max") == {"thinking": {"type": "disabled"}}


def test_on_plus_effort():
    assert tb("on", "max") == {"thinking": {"type": "enabled"},
                              "reasoning_effort": "max"}


# ---------------------------------------------------------------------------
# 4. 非法值降级，不炸
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["garbage", "high", "思考", "2", "开启"])
def test_unknown_mode_falls_back_to_auto(bad):
    assert tb(bad) == {}, f"{bad!r} 应该降级成 auto"


def test_whitespace_is_stripped_not_treated_as_unknown():
    """配置文件里手写多一个空格是常事，不该被当成非法值。"""
    assert tb("  enabled  ") == {"thinking": {"type": "enabled"}}
    assert tb(" off ") == {"thinking": {"type": "disabled"}}
    assert tb("auto", " max ") == {"reasoning_effort": "max"}


@pytest.mark.parametrize("bad", ["garbage", "none", "medium", "xhigh", "minimal", "9"])
def test_unknown_effort_dropped(bad):
    """官方认 none/medium/minimal/xhigh，但糖糖只暴露 low/high/max——
    没列进白名单的一律不发，免得行为随服务端映射悄悄变。"""
    assert tb("on", bad) == {"thinking": {"type": "enabled"}}


# ---------------------------------------------------------------------------
# 5. 官方那条 400 的边界：绝不能产生 required / 具名 tool_choice
# ---------------------------------------------------------------------------

def test_thinking_body_never_touches_tool_choice():
    """官方：required 和具名 tool_choice 在思考模式下返回 400。
    本函数只管思考参数，不许碰 tool_choice——碰了就可能踩那条 400。"""
    for mode in ("auto", "on", "off", "garbage"):
        for eff in ("", "low", "high", "max", "garbage"):
            assert "tool_choice" not in tb(mode, eff)


# ---------------------------------------------------------------------------
# 6. 后台任务（轻量调用）的思考参数
# ---------------------------------------------------------------------------

def lte(cfg, explicit=None):
    from agent.handler import _light_thinking_extra
    return _light_thinking_extra(cfg, explicit)


def test_light_auto_means_inherit_not_server_default():
    """**这是最容易写错的一条。**

    `auto` 返回 None = 不附加任何参数。而不附加时 `_call_deepseek` 会照常套用
    它自己的 `cfg["thinking"]`（**主**那套）——所以 auto 的准确含义是
    「跟随主设置」，**不是**「用服务端默认」。

    2026-09-22：这条语义最初被我写反了（注释和 UI 都写成"服务端默认"），
    是主人追问「用中转时怎么开启后台思考」才查出来的。
    """
    assert lte({"light_thinking": "auto"}) is None


def test_light_off_returns_disabled():
    assert lte({"light_thinking": "off"}) == {"thinking": {"type": "disabled"}}


def test_light_on_is_expressible():
    """后台这一项**必须能表达 on**——否则"主对话关着、但想让后台思考"没法配。

    这正是主人问「为什么没有 on」时暴露的缺口：原选项只有 auto/off。
    """
    assert lte({"light_thinking": "on"}) == {"thinking": {"type": "enabled"}}


def test_light_explicit_wins_over_config():
    """提取类任务在代码里写死关思考——那是逐调用的判断，
    不该被一个全局配置反过来覆盖。"""
    cfg = {"light_thinking": "on"}
    assert lte(cfg, {"thinking": {"type": "disabled"}}) == \
        {"thinking": {"type": "disabled"}}


def test_light_effort_passed_through():
    assert lte({"light_thinking": "on", "light_effort": "max"}) == \
        {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}


def test_light_none_cfg_is_safe():
    assert lte(None) is None
