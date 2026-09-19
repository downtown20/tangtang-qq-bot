"""
人格提示词结构测试 — 隐式缓存契约

2026-08-10：system prompt 改为「稳定前缀在前、动态层在后」，
让 DeepSeek 隐式缓存命中固定前缀（输入成本 ¥1 → ¥0.2）。
本测试钉死该契约：
1. 稳定前缀（角色卡+群风格+权力+思考链）跨调用完全不变
2. 动态块（时间/情绪/关系）必须排在思考链之后——不能插进前缀
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.personality import PersonalityEngine, PersonalityConfig

COT_TAIL = "让ta知道你在跟ta说话"  # 思考链最后一句——稳定前缀的结尾标记


class _MockMood:
    """情绪引擎 mock：每次调用返回不同内容（模拟每消息变化的情绪）"""

    def __init__(self):
        self._count = 0

    def build_context(self):
        self._count += 1
        return f"情绪状态测试 #{self._count}"


@pytest.fixture
def eng():
    e = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖", "糖糖"]))
    e.set_mood_engine(_MockMood())
    return e


def _stable_prefix(prompt: str) -> str:
    """稳定前缀 = 角色卡开头 → 思考链最后一句（含）"""
    idx = prompt.find(COT_TAIL)
    assert idx > 0, "系统提示词必须包含思考链"
    return prompt[: idx + len(COT_TAIL)]


def test_stable_prefix_unchanged_across_calls(eng):
    """同一会话内多次调用（情绪/关系变化），稳定前缀必须完全一致——缓存命中基础"""
    p1 = eng.build_system_prompt(
        __import__("agent.personality", fromlist=["Relationship"]).Relationship.FAMILIAR,
        group_vibe="互损互怼", power_structure="--- 权力结构 ---",
    )
    p2 = eng.build_system_prompt(
        __import__("agent.personality", fromlist=["Relationship"]).Relationship.CLOSE,
        group_vibe="互损互怼", power_structure="--- 权力结构 ---",
    )
    assert _stable_prefix(p1) == _stable_prefix(p2)


def test_dynamic_blocks_after_stable_prefix(eng):
    """时间/情绪/关系等动态块必须排在思考链之后——插进前缀会破坏缓存"""
    p = eng.build_system_prompt(
        __import__("agent.personality", fromlist=["Relationship"]).Relationship.STRANGER,
        group_vibe="温馨日常", power_structure="--- 权力结构 ---",
    )
    cot_idx = p.find("## 回复前先想")
    assert p.find("这个群的风格") < cot_idx, "群风格应在思考链之前（稳定前缀）"
    assert p.find("权力结构") < cot_idx, "权力结构应在思考链之前（稳定前缀）"
    # 时间上下文（"现在是…"）在思考链之后
    assert p.find("## 回复前先想") < p.find("现在是"), "时间上下文必须排在思考链之后"
    # 关系层在时间上下文之后（动态区尾部）
    assert p.find("## 关系") > p.find("现在是"), "关系层必须在动态区"


def test_overlay_scenario_keeps_stable_prefix(eng):
    """overlay 场景（心理陪伴）遵守前缀契约：
    sensitivity 紧跟角色卡（身份层），内容跨调用稳定 → 不打断缓存"""
    from agent.personality import Relationship

    class _Overlay:
        is_overlay = True
        sensitivity = "敏感度测试说明"
        tone = ""

    ov = _Overlay()
    p1 = eng.build_system_prompt(Relationship.FAMILIAR, scenario=ov)
    p2 = eng.build_system_prompt(Relationship.CLOSE, scenario=ov)  # 同场景、关系变化
    assert _stable_prefix(p1) == _stable_prefix(p2), "同一场景下前缀必须稳定（sensitivity 属前缀）"


def test_role_card_reload_does_not_expose_partial_cache(monkeypatch):
    """角色卡在 worker 中构建时，读取方继续使用旧快照，不能被慢 I/O 卡住。"""
    eng = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖"]))
    split_started = threading.Event()
    release_split = threading.Event()
    build_done = threading.Event()
    prompt_result = {}
    old_base = eng._cached_base
    original_split = eng._split_minimal

    monkeypatch.setattr(eng, "_load_role_card", lambda: "NEW ROLE CARD")

    def blocked_split(role_card):
        split_started.set()
        assert release_split.wait(2), "测试未释放角色卡重载"
        return original_split(role_card)

    monkeypatch.setattr(eng, "_split_minimal", blocked_split)

    reload_thread = threading.Thread(target=eng.reload_role_card)
    reload_thread.start()
    assert split_started.wait(2), "角色卡重载未进入缓存更新阶段"

    def build_prompt():
        prompt_result["value"] = eng.build_system_prompt(
            __import__("agent.personality", fromlist=["Relationship"]).Relationship.STRANGER,
        )
        build_done.set()

    build_thread = threading.Thread(target=build_prompt)
    build_thread.start()
    assert build_done.wait(0.5), "提示词读取被角色卡慢 I/O 阻塞"
    assert eng._cached_base == old_base, "快照应在完整缓存准备后才交换"
    assert old_base in prompt_result["value"]
    assert "NEW ROLE CARD" not in prompt_result["value"]

    release_split.set()
    reload_thread.join(2)
    build_thread.join(2)
    assert not reload_thread.is_alive()
    assert not build_thread.is_alive()
    assert build_done.is_set()
    assert eng._cached_base == "NEW ROLE CARD"


def test_restore_default_role_refreshes_minimal_cache(monkeypatch):
    """切回糖糖时，替换型场景不能继续沿用上一角色的精简缓存。"""
    eng = PersonalityEngine(PersonalityConfig(name="小糖糖", nicknames=["小糖糖"]))
    eng._cached_minimal = "旧角色精简身份"
    monkeypatch.setattr(eng, "_load_role_card", lambda: "默认糖糖身份")

    assert eng.load_role_file("") is True

    class _Replace:
        is_overlay = False
        role = ""
        tone = ""

    prompt = eng.build_system_prompt(
        __import__("agent.personality", fromlist=["Relationship"]).Relationship.STRANGER,
        scenario=_Replace(),
    )
    assert "默认糖糖身份" in prompt
    assert "旧角色精简身份" not in prompt
