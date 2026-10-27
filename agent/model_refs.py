# -*- coding: utf-8 -*-
"""模型档位的「用途」标记与引用解析（2026-10-27）。

## 为什么需要它

主人的原话是「在『AI模型』统一添加聊天 AI 模型和识图 AI 模型还有本地 AI 模型
三种，然后在对应位置选择添加的模型即可」。但那三个**不在同一维度**：

    · 聊天 / 识图 —— 是**用途**
    · 本地        —— 是**部署方式**（云端走 OpenAI 兼容 HTTP，本地走 Ollama）

按用途分三类，就必然要为同一个部署重复建档（同一个 qwen-vl 既是识图又是打标），
而且「本地」这一类会把「本地识图」和「本地聊天」混成一件事——后者当前不支持。

所以档位是两个维度：

    { id, label, kinds: ["chat"|"vision"|"tag"], provider, base_url, model, api_key, ... }

`kinds` 可多选。老档位没有这个键 → 按 `["chat"]` 处理（它们本来就是聊天档）。

## 这里只做三件事

1. `kinds_of()`           —— 档位的用途列表（含向后兼容）
2. `profiles_with_kind()` —— 给下拉框用：某用途有哪些档位可选
3. `resolve_model_ref()`  —— **唯一**把「档位 id」翻成
   `{provider, base_url, model, api_key}` 的地方

识图（`llm.vision.model_ref`）和打标（`image_tagging.model_ref`）都调它。
如果各自去翻 `llm.models` 数组，加字段时必然漏一处（教训 #38：同一个量
只许一个换算点；2026-10-26 的 `allow_groups` 事故就是这种形状）。
"""
from __future__ import annotations

#: 用途取值。加新用途要同时想清楚「谁消费它」（教训 #33）
KINDS: tuple[str, ...] = ("chat", "vision", "tag")

KIND_LABELS: dict[str, str] = {
    "chat": "聊天",
    "vision": "识图",
    "tag": "打标",
}

#: 没有 `kinds` 键的老档位按什么算——它们都是聊天档（2026-10-27 之前只有聊天）
DEFAULT_KINDS: tuple[str, ...] = ("chat",)


def _llm_of(config) -> dict:
    """取出 `llm` 段，容忍两种入参。

    完整 config（`{"llm": {...}}`）和 `llm` 段本身（`MessageHandler.llm_config`）
    在项目里都真实存在：`image_tagger` / `vision_router` 传前者，
    `handler._get_llm_config` 传后者。容错只在这里做一处，调用方不用记形状。
    """
    if not isinstance(config, dict):
        return {}
    inner = config.get("llm")
    if isinstance(inner, dict):
        return inner
    return config


def _raw_profiles(config) -> list[dict]:
    """`llm.models[]` 里的档位对象（脏数据直接跳过，不抛）。"""
    profiles = _llm_of(config).get("models")
    if not isinstance(profiles, list):
        return []
    return [p for p in profiles if isinstance(p, dict)]


#: provider 组的协议取值（对应 DSH Harness `providers.<id>.api`）。
#: 现在只实现 OpenAI 兼容一种——列出来是为了让界面显示协议，而不是让用户
#: 以为「协议」可以随便填（填了不支持的，请求会静默走同一个实现）。
API_PROTOCOLS: dict[str, str] = {
    "openai-completions": "OpenAI 兼容",
}


def raw_providers(config) -> list[dict]:
    """`llm.providers[]` —— **组级**连接表（2026-10-28 引入，对齐 DSH Harness）。

    为什么要有这一层：`base_url` / `api_key` 是**整组共享**的属性，原来每个档位
    各存一份（教训 #54 的反模式：组级属性存成条目级副本，改哪份生效靠遍历顺序）。
    现在组表是唯一源，档位只写 `provider: <组id>` 引用。

    容忍手写成映射的配置（`providers: {custom: {...}}`），因为 YAML 里两种都自然。
    """
    llm = _llm_of(config)
    raw = llm.get("providers")
    if isinstance(raw, dict):
        out: list[dict] = []
        for key, val in raw.items():
            if isinstance(val, dict):
                item = dict(val)
                item.setdefault("id", str(key))
                out.append(item)
        return out
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def provider_by_id(config, key) -> dict | None:
    """按 id 取一个 provider 组；没有就 None。"""
    want = str(key or "").strip()
    if not want:
        return None
    for item in raw_providers(config):
        if str(item.get("id") or "").strip() == want:
            return item
    return None


def provider_conn(config, profile) -> dict:
    """档位实际使用的连接信息（组级优先，**逐字段**回退）。

    **唯一换算点**（教训 #38）：`handler._get_llm_config` /
    `vision_router` / `image_tagger` / 桌面端 bridge 都调它，谁也不许自己翻
    `llm.providers`——否则加一个字段就漏一处。

    回退顺序（逐字段，不是整条记录）：
      1. `llm.providers[id == profile.provider]`  —— 新结构（组表）
      2. `profile` 自己的 `base_url` / `api_key`  —— 旧结构（每档一份副本）
      3. `llm.base_url` / `llm.api_key`           —— 更旧（只有单模型的时代）

    为什么逐字段：迁移期必然出现「组表建好了但 key 还没搬进去」的中间状态，
    整条回退会让档位里本来可用的 key 一夜失效。
    """
    prof = profile if isinstance(profile, dict) else {}
    llm = _llm_of(config)
    key = str(prof.get("provider") or "").strip()
    group = provider_by_id(config, key) or {}

    def _pick(name: str) -> str:
        for src in (group, prof, llm):
            if not isinstance(src, dict):
                continue
            val = src.get(name)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    return {
        "provider": key or str(group.get("id") or "").strip(),
        "label": str(group.get("label") or group.get("id") or key),
        "api": str(group.get("api") or "openai-completions"),
        "base_url": _pick("base_url"),
        "api_key": _pick("api_key"),
    }


def virtual_providers(config) -> list[dict]:
    """组表的「完整视图」：真组 + 从档位遗留字段推导出来的虚拟组。

    老配置里没有 `llm.providers`，连接信息散落在每个档位里。界面不能因此空白
    （那等于要用户重填一遍地址和 key），所以这里分两层兜底：

      1. 按 `provider` 把档位里的连接归并成虚拟组；
      2. 连档位都没有的老配置（纯扁平 `llm.*`），把扁平值挂到它声明的 provider 上。

    `derived: True` = 这一组是**推出来的**，不是用户显式存的组表——界面据此说明
    「保存后会写进组表」。`api_key` 原样返回（可能是 `${VAR}`），掩码是界面层的事。
    """
    llm = _llm_of(config)
    groups = [dict(p) for p in raw_providers(config)]
    real_ids = {str(g.get("id") or "").strip() for g in groups}

    def _ensure(pid: str) -> dict:
        target = next(
            (g for g in groups if str(g.get("id") or "").strip() == pid), None)
        if target is None:
            target = {
                "id": pid,
                "label": pid,
                "api": "openai-completions",
                "base_url": "",
                "api_key": "",
                "derived": True,
            }
            groups.append(target)
        return target

    for prof in _raw_profiles(config):
        pid = str(prof.get("provider") or "").strip()
        if not pid:
            continue
        target = _ensure(pid)
        for name in ("base_url", "api_key"):
            if str(target.get(name) or "").strip():
                continue
            val = str(prof.get(name) or "").strip()
            if val:
                target[name] = val

    if not groups:
        pid = str(llm.get("provider") or "custom").strip() or "custom"
        _ensure(pid)

    # 最后一层：从扁平 llm.* 补齐推导出来的组（真组不动——它是用户的显式声明）
    for group in groups:
        if str(group.get("id") or "").strip() in real_ids:
            continue
        for name in ("base_url", "api_key"):
            if str(group.get(name) or "").strip():
                continue
            val = str(llm.get(name) or "").strip()
            if val:
                group[name] = val
    return groups


def kinds_of(profile) -> list[str]:
    """一个档位的用途列表；没有/写坏时退化成 `["chat"]`。"""
    raw = profile.get("kinds") if isinstance(profile, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return list(DEFAULT_KINDS)
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        if key in KINDS and key not in out:
            out.append(key)
    return out or list(DEFAULT_KINDS)


def profiles_with_kind(config, kind: str) -> list[dict]:
    """某用途下可选的档位（保持 config 里的顺序，界面下拉直接用）。"""
    want = str(kind or "").strip()
    if want not in KINDS:
        return []
    return [
        {
            "id": str(p.get("id") or "").strip(),
            "label": str(p.get("label") or p.get("id") or ""),
            "provider": str(p.get("provider") or "").strip(),
            "model": str(p.get("model") or "").strip(),
        }
        for p in _raw_profiles(config)
        if str(p.get("id") or "").strip() and want in kinds_of(p)
    ]


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def input_of(profile) -> list[str]:
    """这个模型能吃什么：`["text"]` 或 `["text", "image"]`。

    优先读显式的 `input` 字段（与 DSH Harness 的
    `llm-pi-ai.providers.<id>.models[].input` 同名同义）；没写时从 `kinds` 退推——
    带 `vision` 就当能看图，这样老的 `kinds: [vision]` 不改配置也能识别成多模态。
    """
    raw = profile.get("input") if isinstance(profile, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            key = str(item or "").strip().lower()
            if key in ("text", "image") and key not in out:
                out.append(key)
        if out:
            return out
    # 没写 `input` 时再看 `kinds`；两个都没写 = **未知**（返回空表）。
    # 为什么不用默认的 `["chat"]` 去推：那会把「没标注的老档位」一律当成
    # 不能看图（2026-10-28 实测：老档位一选就报「不能看图」，属于误拦）。
    kinds = profile.get("kinds") if isinstance(profile, dict) else None
    if not kinds:
        return []
    return ["text", "image"] if "vision" in kinds_of(profile) else ["text"]


def all_profiles(config) -> list[dict]:
    """所有档位的摘要（给「选档位」下拉用）。**不含 api_key。**

    为什么列出全部而不是只列「用途匹配」的：2026-10-27 之前的档位都没有
    `kinds` 键（都算聊天档），只列 tag 档会让用户一个都选不到。
    用途在这里是**推荐信息**，门禁由使用方自己判（打标看 provider_ready）。

    `input` / `context_window` 与 DSH Harness 的 `provider.models[]` 同名，
    界面拿它们显示「可看图」和 `256K` 徽标（2026-10-28 起云端档位也有徽标了）。
    """
    return [
        {
            "id": str(p.get("id") or "").strip(),
            "label": str(p.get("label") or p.get("id") or ""),
            "provider": str(p.get("provider") or "").strip(),
            "provider_label": provider_conn(config, p)["label"],
            "model": str(p.get("model") or "").strip(),
            "kinds": kinds_of(p),
            "input": input_of(p),
            "context_window": _int_or(p.get("context_window"), 0),
        }
        for p in _raw_profiles(config)
        if str(p.get("id") or "").strip()
    ]


def resolve_model_ref(config, ref_id) -> dict | None:
    """按档位 id 取出它引用的连接信息；找不到返回 None。

    **返回值里的 `api_key` 是原样**（可能是 `${VAR}` 引用）——解析引用是
    使用方的事（`agent/secret_refs.py` / `image_tagger._resolve_secret`）。
    这里不做二次加工，否则「引用 vs 字面量」的分辨点会散到两个地方。
    """
    ref = str(ref_id or "").strip()
    if not ref:
        return None
    for profile in _raw_profiles(config):
        if str(profile.get("id") or "").strip() != ref:
            continue
        # 连接信息走组表（2026-10-28）：档位只存引用，base_url/api_key 在
        # `llm.providers` 里一份。provider_conn 负责逐字段回退到老结构。
        conn = provider_conn(config, profile)
        return {
            "id": ref,
            "label": str(profile.get("label") or ref),
            "kinds": kinds_of(profile),
            "input": input_of(profile),
            "context_window": _int_or(profile.get("context_window"), 0),
            "provider": conn["provider"],
            "provider_label": conn["label"],
            "api": conn["api"],
            "base_url": conn["base_url"],
            "model": str(profile.get("model") or "").strip(),
            "api_key": conn["api_key"],
        }
    return None
