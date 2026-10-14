"""两条「日志与事实相反」的闸门（2026-09-20 独立复核补）。

## 一、`_check_semantic_ready` —— 健康检查里唯一的模型层检查

在那之前 21 项健康检查**没有一项碰模型层**（`embed_engine` / `reranker` 全仓零引用），
而 `_check_embedding_coverage` 在空库时 `total==0` 直接返回 ok。
于是 BGE 没加载起来时——记忆去重、chat_index 语义搜索、贴图语义兜底、
自忆匹配、知识库 hybrid+RRF+rerank **全部**退化成关键词/时间序——
而 `/状态` 依然报「🟢 全部正常」。

降级链设计得再好，也得有人告诉用户"现在走的是降级链"。

## 二、`_is_full_mode` 要看 `voice.enabled`

判据原先只看 `provider`。而出厂模板是 `enabled: false` + `provider: gpt-sovits`——
于是没装语音、也没打算用语音的用户，启动时照样去拉起一个**根本不存在**的本地
TTS 服务（发布包里 `gpt-sovits/` 只有一份摆放说明），换来一条
「GPT-SoVITS 进程意外退出」或者更含糊的「目录不存在」。
那正是一类"用户既没请求、也无从满足的报错"。
"""
import ast
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
HEALTH = BASE / "agent" / "health_check.py"
HANDLER = BASE / "agent" / "handler.py"


def _health_module():
    """必须是 `agent.health_check`——它有 `from .async_io import ...` 这种相对导入，
    用 spec_from_file_location 单独加载会 ModuleNotFoundError。
    """
    import agent.health_check as mod
    return mod


class _StubEngine:
    def __init__(self, ready, err=""):
        self.ready = ready
        self._last_load_error = err


class _StubHandler:
    def __init__(self, embed_ready, rerank_ready, rerank_err=""):
        self.embed_engine = _StubEngine(embed_ready)
        self.reranker = _StubEngine(rerank_ready, rerank_err)


# ═══════════════════════════════════════════════════════════
# 一、语义模型检查
# ═══════════════════════════════════════════════════════════

def test_semantic_ready_is_registered_at_startup():
    """光定义不算接线——必须在 handler 里注册，否则它一次都不会跑。"""
    src = HANDLER.read_text(encoding="utf-8")
    assert '_check_semantic_ready' in src, "健康检查没引用它"
    assert 'self.health.register("semantic_ready"' in src, "没有注册成一项检查"


@pytest.mark.asyncio
async def test_bge_missing_is_an_error_not_a_quiet_ok():
    mod = _health_module()
    r = await mod._check_semantic_ready(_StubHandler(embed_ready=False, rerank_ready=False))
    assert r["status"] == "error", "BGE 没加载却报 ok——那就是'/状态 全部正常'的来历"
    assert "语义" in r["message"]
    # 必须顺带说清"怎么办"，否则只是换了个姿势抱怨
    assert "安装糖糖" in r["message"] or "没就位" in r["message"]


@pytest.mark.asyncio
async def test_reranker_missing_is_a_warn_with_the_cost():
    mod = _health_module()
    r = await mod._check_semantic_ready(
        _StubHandler(embed_ready=True, rerank_ready=False, rerank_err="显存不足"))
    assert r["status"] == "warn"
    assert "显存不足" in r["message"], "没把真实原因带出来，用户只能猜"
    assert "不影响" in r["message"], "没说明代价，用户会以为整个记忆系统坏了"


@pytest.mark.asyncio
async def test_both_ready_is_ok():
    mod = _health_module()
    r = await mod._check_semantic_ready(_StubHandler(True, True))
    assert r["status"] == "ok"


@pytest.mark.asyncio
async def test_missing_attributes_do_not_crash():
    """轻量测试替身/半初始化实例上没有这些属性——不许把健康检查自己搞崩。"""
    mod = _health_module()

    class _Bare:
        pass

    r = await mod._check_semantic_ready(_Bare())
    assert r["status"] in ("warn", "error")
    assert isinstance(r["message"], str) and r["message"]


@pytest.mark.asyncio
async def test_health_module_has_no_dead_persona_hook_wired():
    """`self_state_context()` 曾是零调用死代码（复核 A-15）。

    这条不要求删它，只钉住"它的存在是已知的"——真接线了要顺手改这里，
    因为它是把内部计数（乃至 owner QQ）送进提示词的那条路。
    """
    src = HEALTH.read_text(encoding="utf-8")
    assert "def self_state_context" in src
    callers = [p for p in (BASE / "agent").glob("*.py")
               if p.name != "health_check.py" and "self_state_context" in p.read_text(
                   encoding="utf-8", errors="replace")]
    assert not callers, (
        f"self_state_context 被接线了（{callers}）——它会把这些内部计数注入提示词，"
        f"包括可能含敏感值的 message。请先确认脱敏，再更新本条断言。")


# ═══════════════════════════════════════════════════════════
# 二、语音模式开关
# ═══════════════════════════════════════════════════════════

def _voice_gate_source() -> str:
    """取出 handler.__init__ 里算 `_is_full_mode` 的那几行（AST，避免命中注释）。"""
    src = HANDLER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "_is_full_mode":
                    return ast.get_source_segment(src, node) or ""
    return ""


def test_full_mode_requires_voice_enabled_not_just_provider():
    """`_is_full_mode` 必须**同时**看 enabled 和 provider。

    只看 provider 的话，出厂模板（`enabled: false` + `provider: gpt-sovits`）
    会让每个没装语音的用户启动时去拉一个不存在的引擎。
    """
    seg = _voice_gate_source()
    assert seg, "找不到 _is_full_mode 的赋值"
    assert "_voice_on" in seg, (
        f"`_is_full_mode` 的判据又只看 provider 了：{seg!r}\n"
        f"出厂模板是 enabled:false + provider:gpt-sovits——"
        f"不看 enabled 就会去拉起一个不存在的本地 TTS 服务")


def test_voice_off_falls_back_to_edge_tts_in_capability_note():
    """能力说明也要跟着关——否则提示词会告诉 LLM「你能发语音」，而实际发不出。

    这不是"日志问题"，是**糖糖会对用户说错话**。
    """
    src = HANDLER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_build_capability_note":
            seg = ast.get_source_segment(src, node) or ""
            if "_voice_on" in seg:
                return
    pytest.fail("_build_capability_note 没有跟着 voice.enabled 走——"
                "语音关着时提示词仍会宣称能发语音")
