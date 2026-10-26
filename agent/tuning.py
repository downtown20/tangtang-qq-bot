# -*- coding: utf-8 -*-
"""可调参数层——把**确实该让人调**的代码常量接到 `config.yaml` 的 `tuning:` 节。

三个「为什么」：

1. **为什么常量留在代码里、配置只做覆盖**：缺省零退化。删掉 `tuning:` 节 = 回到
   出厂值；配置读坏了 = 回到出厂值。反过来（默认值只存在配置里）的话，配置文件
   一丢她就没了出厂状态。
2. **为什么不做热重载**：`config.yaml` 的既有语义就是「保存后重启」（`agent/prompts`
   同理）。再加一套即时生效的语义，同一个量就有了两个生效时机——教训 #38 同族。
   前端提示「重启后生效」即可。
3. **为什么校验失败不抛异常**：参数写错时**糖糖必须还能起来**。越界/非数字/类型不对
   一律回落默认值 + `logger.warning`，界面上标出「这一项没用你的值」。
   起不来比参数错更糟——而她起来的时候主人多半在睡觉。

**唯一事实源是 `TUNING_SPECS`**（本文件）。代码里的
`BACKGROUND_BUDGET_TOKENS = tuning_int("background_budget_tokens", 1100)` 那个
`1100` 是**兜底值**，与台账里的 `default` 由 `tests/test_tuning_contract.py`
用 AST 双向比对——两处都写不算「两个派生点」，因为闸门强制它们相等。

**不开放的**（写清楚，免得下次有人顺手加）：
- `EVIDENCE_RESERVE_TOKENS` / `_RELATIONSHIP_BLOCK_FIXED_TOKENS`：它们是背景预算的
  **派生量**（注释写着「= 背景预算的 33%」）。开放它 = 两个换算点各自漂（#38）。
- `*_SCHEMA_VERSION` / `FEAT_*` / `MANIFEST_VERSION` / `_PROMPT_REV`：数据格式魔数，
  改了就与既有库不兼容。
- `STORE_IO_MAX_CONCURRENCY` / `NUM_CTX` / `TURN_TIMEOUT` / 端口：硬件与部署参数，
  不属于「她的行为」。
- 健康检查阈值（`MIN_EXTRACT_*` 等）：那是**判据**不是参数，调它等于把尺子掰弯。
"""

from __future__ import annotations

import ast
import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

# 测试期是否强制读 config.yaml（默认 False = 一律用出厂值）。
# 理由：**测试不该依赖主人的运行时配置**。否则主人把 knowledge_max_results 从 3
# 调到 5，全量测试就会红——那不是代码坏了，是他动了参数。需要用配置做断言的测试
# 自己 `monkeypatch.setattr(tuning, "_FORCE_CONFIG", True)` 并换掉 _CONFIG_PATH。
_FORCE_CONFIG = False


def _config_allowed() -> bool:
    """pytest 进程内不读用户配置（见 `_FORCE_CONFIG` 的注释）。"""
    if _FORCE_CONFIG:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if "pytest" in sys.modules:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════
# 分组（前端左侧的顺序就是这里的顺序）
# ═══════════════════════════════════════════════════════════════════

GROUPS: Tuple[Tuple[str, str], ...] = (
    ("context", "上下文与背景预算"),
    ("memory", "记忆召回"),
    ("extraction", "记忆提取"),
    ("window", "对话窗口"),
    ("pacing", "消息节奏"),
    ("knowledge", "知识库检索"),
    ("voice", "语音"),
    ("scenario", "场景规则上限"),
)


@dataclass(frozen=True)
class TuningSpec:
    """一项可调参数。

    `module` + `const` 而不是硬写 `file:line`：行号会随着任何一次编辑漂移，手抄进
    台账必然过时（教训 #35 同族）。前端显示的行号由 `locate()` **动态解析**。
    """

    key: str            # config.yaml → tuning.<key>
    const: str          # 代码里的常量名（大写）
    module: str         # 相对项目根的路径，如 "agent/protocols.py"
    default: float      # 出厂值（闸门与代码兜底值比对）
    lo: float
    hi: float
    kind: str           # "int" | "float"
    group: str
    label: str
    desc: str           # 作用：改了会影响她什么


TUNING_SPECS: Tuple[TuningSpec, ...] = (
    # ── 上下文与背景预算 ─────────────────────────────────────────
    TuningSpec(
        "background_budget_tokens", "BACKGROUND_BUDGET_TOKENS",
        "agent/protocols.py", 1100, 400, 4000, "int", "context",
        "每轮背景块总预算",
        "她每一轮能看到的背景材料总量（记忆/关系/知识/画像加起来）。"
        "调大=塞进更多材料，注意力被摊薄；调小=只看最相关的几条。"
        "这是所有背景注入的总闸门，改动影响面最大。",
    ),
    TuningSpec(
        "max_background_block_tokens", "MAX_BACKGROUND_BLOCK_TOKENS",
        "agent/protocols.py", 240, 160, 1000, "int", "context",
        "单个背景块上限",
        "一块材料最多占多少。调大=单块能更完整（长记忆不被截断）；"
        "调小=同样预算能塞更多块，但每块都被切碎。",
    ),
    TuningSpec(
        "profile_max_len", "PROFILE_MAX_LEN",
        "agent/protocols.py", 120, 40, 400, "int", "context",
        "画像注入字数上限",
        "「我认识的你」那段最多多少字。调大=记得更细，但每轮都占预算；"
        "调小=只剩一两句核心印象。",
    ),
    TuningSpec(
        "max_reply_parts", "MAX_REPLY_PARTS",
        "agent/protocols.py", 3, 1, 6, "int", "context",
        "分段条数上限",
        "她一次回复最多分成几条发（仅私聊 [分段] 生效）。"
        "调大=更像人连发；调小=一条说完。",
    ),
    TuningSpec(
        "media_attach_window_s", "MEDIA_ATTACH_WINDOW_S",
        "agent/protocols.py", 120, 30, 900, "int", "context",
        "媒体附件关联窗口（秒）",
        "图片/语音发出后多久内，她还会把它和话题关联起来。"
        "调大=隔很久发的图也能被联想到；调小=只看即时的。",
    ),
    # ── 记忆召回 ────────────────────────────────────────────────
    TuningSpec(
        "memory_recall_default", "MEMORY_RECALL_DEFAULT",
        "agent/protocols.py", 3, 0, 10, "int", "memory",
        "每轮自动浮现的记忆条数",
        "不用她主动查，每轮就摆在她面前的记忆有几条。"
        "调大=她更容易「突然想起」；调到 0 = 只在她主动 search 时才看记忆。",
    ),
    TuningSpec(
        "memory_recall_max", "MEMORY_RECALL_MAX",
        "agent/protocols.py", 20, 5, 50, "int", "memory",
        "单次主动召回上限",
        "她调工具查记忆时最多拿回多少条（护栏，不是预算）。"
        "调大=能一次翻到更多旧事，但容易淹掉当前话题。",
    ),
    TuningSpec(
        "memory_recall_fetch", "MEMORY_RECALL_FETCH",
        "agent/protocols.py", 10, 3, 30, "int", "memory",
        "检索侧取回条数",
        "排序之前先从库里捞多少条候选。调大=精排池更大、结果更准，代价是慢一点。",
    ),
    TuningSpec(
        "uncertain_recall_limit", "UNCERTAIN_RECALL_LIMIT",
        "agent/protocols.py", 5, 1, 20, "int", "memory",
        "模糊路候选数",
        "话题模糊时，SQL 侧先取多少条再精排。",
    ),
    TuningSpec(
        "uncertain_inject_max", "UNCERTAIN_INJECT_MAX",
        "agent/protocols.py", 2, 0, 6, "int", "memory",
        "模糊记忆注入上限",
        "「好像有点像但不确定」的记忆最多注入几条（独立配额，不占可信记忆的额度）。"
        "这条控制她敢不敢联想。",
    ),
    TuningSpec(
        "uncertain_min_importance", "UNCERTAIN_MIN_IMPORTANCE",
        "agent/protocols.py", 6, 1, 10, "int", "memory",
        "模糊档 importance 门槛",
        "importance 低于此值的碎片不参与模糊联想（随手记的多半没用）。",
    ),
    TuningSpec(
        "uncertain_min_confidence", "UNCERTAIN_MIN_CONFIDENCE",
        "agent/protocols.py", 0.7, 0.0, 1.0, "float", "memory",
        "模糊档置信度门槛",
        "模糊记忆的置信度下限。调高=只联想有把握的；调低=更爱猜（容易编）。",
    ),
    # ── 记忆提取 ────────────────────────────────────────────────
    TuningSpec(
        "extraction_immediate_messages", "IMMEDIATE_MESSAGES",
        "agent/extraction_policy.py", 10, 3, 50, "int", "extraction",
        "即时提取的消息条数",
        "攒够多少条消息就立刻做一次记忆提取（群聊口径）。"
        "调大=提取更连贯但更滞后；调小=记得更快、更碎。",
    ),
    TuningSpec(
        "extraction_stale_messages", "STALE_MESSAGES",
        "agent/extraction_policy.py", 3, 1, 20, "int", "extraction",
        "陈旧提取的兜底条数",
        "对话冷下来后，剩多少条未提取的消息触发一次收尾提取。",
    ),
    # ── 对话窗口 ────────────────────────────────────────────────
    TuningSpec(
        "engaged_window", "ENGAGED_WINDOW",
        "agent/conversation_tracker.py", 120, 30, 600, "int", "window",
        "对话窗口（秒）",
        "一次对话结束后，隔多久再说话还算「同一轮对话」。"
        "调大=她更容易接上前文；调小=每句话都是新的开始。",
    ),
    TuningSpec(
        "grace_period", "GRACE_PERIOD",
        "agent/conversation_tracker.py", 10, 0, 120, "int", "window",
        "容差（秒）",
        "窗口刚过期时的宽限时间，避免「卡在边界上」被当成新对话。",
    ),
    TuningSpec(
        "fade_period", "FADE_PERIOD",
        "agent/conversation_tracker.py", 300, 0, 1800, "int", "window",
        "渐变退出期（秒）",
        "窗口过期后这段时间内仍给部分加分（不是立刻归零）。"
        "调大=对话余温更长。",
    ),
    TuningSpec(
        "max_consecutive", "MAX_CONSECUTIVE",
        "agent/conversation_tracker.py", 5, 1, 20, "int", "window",
        "连续主动插话上限",
        "只作用于主动插话的退让判定（should_yield），不影响窗口内逐条回复"
        "（那是契约：系统不做二次门控）。调到 1 = 插一句就让位。",
    ),
    TuningSpec(
        "max_llm_silences", "MAX_LLM_SILENCES",
        "agent/conversation_tracker.py", 2, 1, 10, "int", "window",
        "连续沉默计数上限",
        "她连续几次选择不说话后，窗口进入渐变期（别一直悬着）。",
    ),
    # ── 消息节奏 ────────────────────────────────────────────────
    TuningSpec(
        "batch_window", "BATCH_WINDOW",
        "agent/message_batcher.py", 2.0, 0.3, 10.0, "float", "pacing",
        "消息合批窗口（秒）",
        "对方连发几条时，等多久把它们合成一轮处理。"
        "调大=更少打断、但回得更慢；调小=更快、但可能只回了半句。",
    ),
    TuningSpec(
        "at_debounce", "AT_DEBOUNCE",
        "agent/message_batcher.py", 1.5, 0.0, 10.0, "float", "pacing",
        "@ 消息防抖（秒）",
        "被 @ 之后再等多久才处理（等对方把话说完）。",
    ),
    # ── 知识库检索 ──────────────────────────────────────────────
    TuningSpec(
        "knowledge_max_total_chars", "MAX_TOTAL_CHARS",
        "agent/knowledge.py", 3000, 500, 12000, "int", "knowledge",
        "知识注入总字数上限",
        "一次检索最多把多少字的知识灌进上下文。",
    ),
    TuningSpec(
        "knowledge_max_per_chunk", "MAX_PER_CHUNK",
        "agent/knowledge.py", 1500, 200, 6000, "int", "knowledge",
        "单块知识字数上限",
        "一个知识片段最多截多长。",
    ),
    TuningSpec(
        "knowledge_max_results", "MAX_RESULTS",
        "agent/knowledge.py", 3, 1, 10, "int", "knowledge",
        "返回知识块数",
        "一次检索最多返回几个片段。调大=材料更全，但更容易答非所问。",
    ),
    TuningSpec(
        "knowledge_semantic_threshold", "SEMANTIC_EVIDENCE_THRESHOLD",
        "agent/knowledge.py", 0.58, 0.0, 1.0, "float", "knowledge",
        "语义检索门槛",
        "向量相似度低于此值不算命中。调高=只答有把握的；调低=更爱引用。",
    ),
    TuningSpec(
        "knowledge_semantic_segment_chars", "SEMANTIC_SEGMENT_CHARS",
        "agent/knowledge.py", 450, 100, 1000, "int", "knowledge",
        "语义子段字数",
        "知识库切片长度（与 BGE 512 token 窗口匹配）。改这个会改变索引粒度，"
        "改完需要 /知识库 重载 重建索引才有意义。",
    ),
    # ── 语音 ────────────────────────────────────────────────────
    TuningSpec(
        "voice_speed_min", "VOICE_SPEED_MIN",
        "agent/voice.py", 0.75, 0.5, 1.0, "float", "voice",
        "语速下限",
        "TTS 语速可调范围的下界。",
    ),
    TuningSpec(
        "voice_speed_max", "VOICE_SPEED_MAX",
        "agent/voice.py", 1.25, 1.0, 2.0, "float", "voice",
        "语速上限",
        "TTS 语速可调范围的上界。太大会听不清。",
    ),
    TuningSpec(
        "voice_streaming_char_threshold", "STREAMING_CHAR_THRESHOLD",
        "agent/voice.py", 60, 10, 500, "int", "voice",
        "流式合成阈值（字）",
        "超过多少字才走流式合成（短句直接整段更快）。",
    ),
    # ── 场景规则上限 ────────────────────────────────────────────
    TuningSpec(
        "scenario_max_overlay_chars", "MAX_OVERLAY_CHARS",
        "agent/scenario.py", 2000, 200, 20000, "int", "scenario",
        "叠加型场景字数上限",
        "scenarios/*.yaml 里 overlay 型场景的 tone/behavior 超过这个长度会 warn"
        "（防着一份场景把上下文吃干净）。",
    ),
    TuningSpec(
        "scenario_max_replace_chars", "MAX_REPLACE_CHARS",
        "agent/scenario.py", 500, 100, 8000, "int", "scenario",
        "替换型场景字数上限",
        "replace 型场景（整段替换语气）的字数警戒线。",
    ),
)

_SPEC_BY_KEY: Dict[str, TuningSpec] = {s.key: s for s in TUNING_SPECS}

# key → 值（本进程只解析一次：与 prompt_overrides 一致——改完重启生效）
_cache: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════════════════════════
# 读取与校验
# ═══════════════════════════════════════════════════════════════════

def _read_config_tuning() -> Dict[str, Any]:
    """读 `config.yaml` 的 `tuning:` 节。任何异常都返回空映射（全用出厂值）。"""
    if not _config_allowed():
        return {}
    try:
        import yaml  # 局部导入：tuning 层不该在 import 期强依赖 PyYAML
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001 - 配置坏了也不能让她起不来
        logger.warning("⚙ 调参配置读取失败，全部使用出厂默认值：%s", exc)
        return {}
    if not isinstance(cfg, dict):
        return {}
    raw = cfg.get("tuning")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("⚙ config.yaml 的 tuning 节不是映射（%s），已忽略",
                       type(raw).__name__)
        return {}
    return raw


def _coerce(spec: TuningSpec, raw: Any) -> Tuple[Any, bool, str]:
    """把配置里的原始值收口成合法值 → (value, ok, 说明)。"""
    if isinstance(raw, bool):
        return spec.default, False, "布尔不是数字"
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return spec.default, False, f"不是数字：{raw!r}"
    if not math.isfinite(num):
        return spec.default, False, "不是有限数字"
    if spec.kind == "int":
        if abs(num - round(num)) > 1e-9:
            return spec.default, False, f"必须是整数，收到 {raw!r}"
        num = int(round(num))
    if num < spec.lo or num > spec.hi:
        return spec.default, False, f"超出范围 [{spec.lo}, {spec.hi}]"
    return num, True, ""


def load() -> Dict[str, Any]:
    """解析一次全部可调参数（缓存）。"""
    global _cache
    if _cache is not None:
        return _cache

    raw = _read_config_tuning()
    unknown = sorted(k for k in raw if k not in _SPEC_BY_KEY)
    if unknown:
        logger.warning("⚙ config.yaml 的 tuning 节有 %d 个未登记的键（不会生效，"
                       "检查是否拼错）：%s", len(unknown), ", ".join(unknown))

    resolved: Dict[str, Any] = {}
    for spec in TUNING_SPECS:
        if spec.key not in raw:
            resolved[spec.key] = spec.default
            continue
        val, ok, why = _coerce(spec, raw[spec.key])
        if not ok:
            logger.warning("⚙ 调参 `%s` 的值无效（%s）——回落到默认值 %s",
                           spec.key, why, spec.default)
        resolved[spec.key] = val
    _cache = resolved
    return resolved


def reload() -> Dict[str, Any]:
    """清缓存重读（给测试与「改完重启」之外的显式重载用）。"""
    global _cache
    _cache = None
    return load()


def value(key: str) -> Any:
    """取一项的值。**未登记的 key 是编程错误**，直接 KeyError。"""
    if key not in _SPEC_BY_KEY:
        raise KeyError(f"未登记的调参项：{key}（请加进 TUNING_SPECS）")
    return load()[key]


def _guarded(key: str, fallback: Any) -> Any:
    """代码兜底值入口：台账缺登记/漂移时都要能起来。"""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        logger.warning("⚙ 调参 `%s` 未登记在 TUNING_SPECS——已用代码兜底值 %s",
                       key, fallback)
        return fallback
    if fallback != spec.default:
        logger.warning("⚙ 调参 `%s` 的代码兜底值 %s 与台账默认值 %s 不一致"
                       "——以台账为准（闸门 tests/test_tuning_contract.py 会红）",
                       key, fallback, spec.default)
    return load()[key]


def tuning_int(key: str, fallback: int) -> int:
    """整数型调参项的读取入口（用于模块级常量赋值）。"""
    return int(_guarded(key, fallback))


def tuning_float(key: str, fallback: float) -> float:
    """浮点型调参项的读取入口。"""
    return float(_guarded(key, fallback))


# ═══════════════════════════════════════════════════════════════════
# 源码定位（行号动态解析，不手抄）
# ═══════════════════════════════════════════════════════════════════

_lineno_cache: Dict[str, Dict[str, int]] = {}


def _const_linenos(module: str) -> Dict[str, int]:
    """AST 解析一个模块，返回 {常量名: 行号}。"""
    if module in _lineno_cache:
        return _lineno_cache[module]
    table: Dict[str, int] = {}
    try:
        path = _PROJECT_ROOT / module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                if isinstance(node.targets[0], ast.Name):
                    target = node.targets[0].id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            if target:
                table.setdefault(target, node.lineno)
    except Exception as exc:  # noqa: BLE001 - 定位失败不影响运行
        logger.debug("调参源码定位失败 %s: %s", module, exc)
    _lineno_cache[module] = table
    return table


def locate(spec: TuningSpec) -> str:
    """`agent/protocols.py:445`。行号找不到就退化成 `文件:常量名`（不撒谎）。"""
    lineno = _const_linenos(spec.module).get(spec.const)
    if lineno:
        return f"{spec.module}:{lineno}"
    return f"{spec.module}:{spec.const}"


# ═══════════════════════════════════════════════════════════════════
# 给界面用的载荷
# ═══════════════════════════════════════════════════════════════════

def ui_payload() -> Dict[str, Any]:
    """`GET /api/tuning` 的返回体。"""
    current = load()

    by_group: Dict[str, List[Dict[str, Any]]] = {g: [] for g, _ in GROUPS}
    for spec in TUNING_SPECS:
        val = current.get(spec.key, spec.default)
        by_group.setdefault(spec.group, []).append({
            "key": spec.key,
            "const": spec.const,
            "label": spec.label,
            "desc": spec.desc,
            "kind": spec.kind,
            "value": val,
            "default": spec.default,
            "lo": spec.lo,
            "hi": spec.hi,
            "source": locate(spec),
            "changed": val != spec.default,
        })

    groups = [
        {"key": g, "title": title, "items": by_group.get(g, [])}
        for g, title in GROUPS
    ]
    changed = sum(1 for it in (i for g in groups for i in g["items"]) if it["changed"])
    return {
        "groups": groups,
        "total": len(TUNING_SPECS),
        "changed": changed,
        "config_key": "tuning",
        "note": "改完保存后需要重启糖糖才生效（与 config.yaml 其他项一致）。",
    }


#: 跨项约束。集中写在这里是**校验规则**，不是第二个计算点（教训 #38）：
#: 单块上限比总预算还大时，「总预算」名存实亡（第一块就吃光），但两个值本身
#: 都合法——只有放在一起才矛盾。
_CROSS_CHECKS: Tuple[Tuple[str, str, str, str], ...] = (
    ("background_budget_tokens", "max_background_block_tokens",
     "<==", "「单块上限」不能超过「背景总预算」——否则第一块就吃掉全部预算"),
)


def _cross_check(merged: Dict[str, Any]) -> Tuple[Set[str], List[str]]:
    """→ (冲突的键, 说明)。冲突的键要从待写值里剔掉——**留下来的都是能写的**，
    调用方只看 `clean` 也不会误写一个越界的组合。"""
    bad: Set[str] = set()
    errors: List[str] = []
    for big_key, small_key, op, msg in _CROSS_CHECKS:
        big = merged.get(big_key)
        small = merged.get(small_key)
        if big is None or small is None:
            continue
        conflict = (small > big) if op == "<==" else (small >= big)
        if conflict:
            errors.append(f"{msg}（{small_key}={small} vs {big_key}={big}）")
            bad.update((big_key, small_key))
    return bad, errors


def validate_updates(values: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """校验一批待写入的值 → (合法值, 错误说明)。**不写盘**。

    与 `_coerce` 的区别：这里**不接受回落**——用户手输的错误必须当场告诉他，
    而不是悄悄写了个默认值进去让他在界面上看到「已保存」。
    """
    if not isinstance(values, dict):
        return {}, ["待保存的值必须是对象"]
    clean: Dict[str, Any] = {}
    errors: List[str] = []
    for key, raw in values.items():
        spec = _SPEC_BY_KEY.get(key)
        if spec is None:
            errors.append(f"未登记的调参项：{key}")
            continue
        val, ok, why = _coerce(spec, raw)
        if not ok:
            errors.append(f"{spec.label}（{key}）：{why}")
            continue
        clean[key] = val

    if clean:
        merged = dict(load())
        merged.update(clean)
        bad, cross_errors = _cross_check(merged)
        if cross_errors:
            errors.extend(cross_errors)
            # 冲突的组合一律不放进待写值（留着的都是能写的）
            clean = {k: v for k, v in clean.items() if k not in bad}
    return clean, errors
