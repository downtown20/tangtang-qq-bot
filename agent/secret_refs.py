"""密钥引用（``${VAR}``）的单一事实源 ⚠ 纯标准库，tools/ 与图形控制台可直接 import。

## 为什么单独一个模块

这个项目里，密钥有**两条**落点，靠**一个**约定绑在一起：

    配置文件 (config.yaml)          .env（真值，不进 git）
    llm.api_key: ${DEEPSEEK_KEY}  →  DEEPSEEK_KEY=sk-xxxx

约定是「字段值**恰好**是 ``${VAR}``」——`main._resolve_env_vars` 的正则
（``^\\$\\{([A-Za-z_][A-Za-z0-9_]*)\\}$``）只认整串完全匹配，一个多余字符都不行。

2026-09-20 之前，这条约定在出厂模板里是**断**的：`tools/准备发布.py` 的
`scrub_cfg()` 把开发机 config.yaml 里正确的 ``${DEEPSEEK_KEY}`` 主动改写成了
「你的DeepSeek或Claude的API密钥」这类中文字面量。于是：

  · 控制台「🔑 密钥管理」把用户填的 Key 写进 .env（它只写 .env）
  · 运行期读 config.yaml 拿到的是那串中文 → .env 从创建起就没人读过
  · **而界面上一切正常**：保存成功、字段回显正确、提示「已保存到 config.yaml」

唯一的例外是 provider=deepseek——`agent/handler.py` 当时有一行
``if _provider == "deepseek"`` 的特判把 `DEEPSEEK_KEY` 捞了回来。
所以真实症状是「默认配置能用、换成 openai/xai/claude 就 401」，
比「整个不能跑」更难查。

## 这个模块负责什么

  · `PROVIDER_KEY_ENV`      provider → 环境变量名（控制台写 .env、运行期回填、体检核对，共用）
  · `FIXED_SECRET_PATHS`    不随 provider 变的三个密钥字段
  · `LEGACY_SECRET_LITERALS` 老模板留下的中文占位串——**迁移用**，认出来换成 ``${VAR}``
  · `normalize_secret_fields()` 把字段值归一成 ``${VAR}``（幂等，纯函数）
  · `unfilled_secrets()`    启动前校验：哪些密钥还是空的/没解析/还是占位串

改这里的任何一张表，都要同时看 `tests/test_config_example_contract.py`——
它钉住「出厂模板的密钥字段必须是 ``${VAR}``」与「模板/控制台/运行期三处键名一致」。
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Optional

# provider → 该 provider 用哪个环境变量名。
# 三处共用：控制台「密钥管理」写 .env、agent/handler.py 回填 llm.api_key、
# tools/准备发布.py 生成 .env.example。改一处必须改全。
PROVIDER_KEY_ENV: dict[str, str] = {
    "deepseek":  "DEEPSEEK_KEY",
    "xai":       "XAI_KEY",
    "anthropic": "ANTHROPIC_KEY",
    "openai":    "OPENAI_KEY",
    "custom":    "CUSTOM_LLM_KEY",
}

DEFAULT_ENV_NAME = PROVIDER_KEY_ENV["deepseek"]

# 不随 provider 变的密钥字段： (配置路径, 环境变量名)
FIXED_SECRET_PATHS: list[tuple[tuple, str]] = [
    (("llm", "seductive_api_key"), "SEDUCTIVE_KEY"),
    (("llm", "vision", "api_key"), "QWEN_KEY"),
    (("napcat", "access_token"), "SNOWLUMA_TOKEN"),
]

# 出厂模板在 2026-09-20 之前写死的中文占位串——**只用于识别与迁移**。
# 新模板不会再产生它们（见 tools/准备发布.py scrub_cfg），但老用户的
# config.yaml 里躺着，光改模板不迁移，他们的症状一模一样。
LEGACY_SECRET_LITERALS: dict[str, str] = {
    "你的DeepSeek或Claude的API密钥": "DEEPSEEK_KEY",
    "你的视觉API密钥（不用识图可留空）": "QWEN_KEY",
    "请修改为你自己的 access_token": "SNOWLUMA_TOKEN",
    "请填写你的access_token": "SNOWLUMA_TOKEN",       # 更早的写法
}

# 每个密钥字段的「属于什么功能 / 不填会怎样」——启动校验与日志共用。
# 键是点号路径，值是 (人话名称, 不填的后果)。
SECRET_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "llm.api_key": (
        "LLM API Key（糖糖的「大脑」）",
        "没有它糖糖一句话也说不出——每次回复都会 401",
    ),
    "llm.seductive_api_key": (
        "亲密模式专用 Key",
        "可选。不填就用上面那个 Key，不影响正常聊天",
    ),
    "llm.vision.api_key": (
        "识图 Key（云端千问 VL 用）",
        "只有「识图方式」选云端时才需要；选本地 MiniCPM-V 不用填",
    ),
    "napcat.access_token": (
        "SnowLuma 授权 Token",
        "只有 SnowLuma 面板里设了 Token 才需要——两边不一样就接不上，两边都空则正常",
    ),
}


def secret_paths(data: Any) -> list[tuple[tuple, str]]:
    """(配置路径, 环境变量名) 清单。`llm.api_key` 跟着 provider 走，其余固定。"""
    provider = str(((data or {}).get("llm") or {}).get("provider") or "deepseek")
    return [(("llm", "api_key"), PROVIDER_KEY_ENV.get(provider, DEFAULT_ENV_NAME))] \
        + list(FIXED_SECRET_PATHS)


def _dig(data: Any, path: tuple):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _put(data: Any, path: tuple, value: Any) -> bool:
    node = data
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    if not isinstance(node, dict):
        return False
    node[path[-1]] = value
    return True


def normalize_secret_fields(data: Any,
                            environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """把密钥字段的值归一成 ``${VAR}``，返回被改动的点号路径（幂等）。

    只动这四种情形：
      ① 值恰好等于对应环境变量的值——UI 保存把 ``${VAR}`` 物化成了明文
      ② 值是出厂模板早期的中文占位串（`LEGACY_SECRET_LITERALS`）
      ③ 值是**我们认识的那几个** provider 变量之一（`PROVIDER_KEY_ENV.values()`）——
         provider 换人了，同一个槽位的引用得跟着换。**不按形状认**：用户自己写的
         `${OPENROUTER_KEY}` / `${DASHSCOPE_API_KEY}` 一律不碰（按形状认会把它们
         改成我们的名字，而 .env 里没有那个新名字 → 静默把 Key 弄丢，实测过）。
      ④ 值是空串——**最隐蔽的一种**：`.env.example` 里 `SNOWLUMA_TOKEN=` 是空的，
         `_resolve_env_vars` 把 ``${SNOWLUMA_TOKEN}`` 解析成 `""`，控制台原样写回，
         引用就这么变成了空字符串。之后用户再往「密钥管理」里填 Token，
         它进了 .env，而 config.yaml 那一格是死的 `""` —— 回到原病。
         恢复成 ``${VAR}`` 不改变语义（空变量本来就解析成空），只把机制接回去。

         ⚠ 这条有一个已知的副作用，写在这里免得下次有人当成 bug 修：
         用户若**刻意**把 `llm.api_key` 清成 `""`（想禁用 LLM），而我们又
         normalize 回 ``${DEEPSEEK_KEY}``，而他的 `.env` 里还留着旧 Key——
         那个 Key 会"复活"。想真的禁用请**整行删掉**（字段不存在时本函数不动它，
         见下一条）。取舍理由：让 ``${VAR}`` 机制静默断掉（本次事故）比
         "清空键反而没清掉"更常见也更难查。真要彻底避免，得比对磁盘上那份原始
         配置——调用方（`save_config_file`）手里有 `existing`，但那属于更大的改动。

    其余一律不动。特别是「用户手写一串真 Key 而 .env 里没有」——
    那是他的选择，这里擅自改成 ``${VAR}`` 会把 Key 弄丢。
    那种情况交给 `unfilled_secrets()` 在启动时报出来。
    """
    env = os.environ if environ is None else environ
    changed: list[str] = []
    for path, env_name in secret_paths(data):
        found, value = _dig(data, path)
        if not found:
            continue
        want = "${" + env_name + "}"
        if value == want:
            continue
        env_val = env.get(env_name, "")
        is_materialized = bool(env_val) and value == env_val
        # ⚠ 只判「值是不是出厂占位串」，**不判它映射到哪个变量**。
        # 原先写的是 `LEGACY_SECRET_LITERALS.get(value) == env_name`，于是老用户在
        # config.yaml 里改过 provider 之后（老占位串固定映射 DEEPSEEK_KEY，
        # 而 env_name 已经变成 ANTHROPIC_KEY）**这条迁移不触发**，
        # 那串中文继续被当成 API Key 发给新供应商——而提示里还写着
        # 「跑一次控制台会自动改正」，控制台走的又是同一处判据，同样不改。
        # 出厂占位串不属于任何 provider，它只是一个"这里还没填"的记号，
        # 一律归一成当前 provider 的 ${VAR}。
        is_legacy = isinstance(value, str) and value in LEGACY_SECRET_LITERALS
        # ③ 只在它指向**我们已知的另一个 provider 变量**时才改写。
        #
        # ⚠ 判据不能按形状来。第一版写的是「`${…_KEY}` 且不等于期望值就改」，
        #   于是用户自己填的 `${OPENROUTER_KEY}`、`${DASHSCOPE_API_KEY}`
        #   （阿里官方文档给的就是这个名字）会被改成 `${CUSTOM_LLM_KEY}` /
        #   `${QWEN_KEY}`——而他的 .env 里没有那个新名字 → 解析不到 → 被清空 →
        #   401。**把用户配好的 Key 静默改没了**，比不改还糟。（Reality Check B-1 实测）
        #
        #   现在只动 PROVIDER_KEY_ENV 里那几个互斥的槽位：它们本来就是"同一个位置、
        #   换个供应商换个名字"，跟着 provider 走是正确的；别的名字一律不碰。
        _known_llm_vars = set(PROVIDER_KEY_ENV.values())
        _ref_name = (value[2:-1] if isinstance(value, str)
                     and value.startswith("${") and value.endswith("}") else "")
        is_other_ref = bool(_ref_name) and _ref_name in _known_llm_vars and value != want
        is_blank = value is None or (isinstance(value, str) and value.strip() == "")
        if is_materialized or is_legacy or is_other_ref or is_blank:
            if _put(data, path, want):
                changed.append(".".join(path))
    return changed


def collapse_unresolved_secrets(data: Any) -> list[str]:
    """把「仍是 ``${VAR}`` 原样」的密钥字段清空，返回被清空的点号路径。

    只在**解析之后**调用。`_resolve_env_vars` 在环境变量不存在时会把原串
    还回来——那串 ``${DEEPSEEK_KEY}`` 一旦漏进 HTTP 头，就是
    `Authorization: Bearer ${DEEPSEEK_KEY}`，401 的报错里看不出是这回事。
    清空反而让下游的「没配 Key」分支能正常工作（识图会自动退回本地后端等）。

    注意：**清空不等于放过**。调用方必须同时把 `unfilled_secrets()` 的结果报给用户，
    否则就成了「静默把配置吃掉」。
    """
    collapsed: list[str] = []
    for path, env_name in secret_paths(data):
        found, value = _dig(data, path)
        if found and isinstance(value, str) and value.strip() == "${" + env_name + "}":
            if _put(data, path, ""):
                collapsed.append(".".join(path))
    return collapsed


def unfilled_secrets(cfg: Any,
                     environ: Optional[Mapping[str, str]] = None) -> list[tuple[str, str, str]]:
    """体检**已解析后**的配置：返回 ``[(点号路径, 变量名, 该怎么说)]``。

    必须喂 `main._resolve_env_vars()` 的输出——只有解析过，才能把
    「变量没设」和「引用写对了但值是空的」区分开。

    判据（三条都算「没填好」）：
      · 空值 / 全空白
      · 值**仍是** ``${VAR}`` 原样——说明变量名对了但 .env 里没有它
        （`_resolve_env_vars` 解析不到时原样返回，这条是它的指纹）
      · 值命中老模板的中文占位串——迁移没跑到的老配置
    """
    env = os.environ if environ is None else environ
    out: list[tuple[str, str, str]] = []
    for path, env_name in secret_paths(cfg):
        found, value = _dig(cfg, path)
        if not found:
            continue
        text = "" if value is None else str(value)
        dotted = ".".join(path)
        if text.strip() in ("", "${" + env_name + "}"):
            # 两种成因给出不同的话——「.env 里没这个变量」和
            # 「config.yaml 那格是空的」要做的事不一样（前者去填 .env，
            # 后者要让控制台把 ${...} 引用写回去），说混了等于没说。
            if text.strip():
                reason = f".env 里没有 {env_name} 这个变量"
            elif env_name not in env:
                reason = f".env 里没有 {env_name} 这个变量"
            elif not str(env.get(env_name) or "").strip():
                reason = f".env 里有 {env_name}，但值是空的"
            else:
                reason = f"config.yaml 的 {dotted} 是空的（.env 里 {env_name} 有值也读不到）"
            out.append((dotted, env_name, reason))
        elif text in LEGACY_SECRET_LITERALS:
            out.append((dotted, env_name,
                        "还是出厂模板里的占位文字（老配置，跑一次控制台会自动改正）"))
    return out


def literal_secret_fields(cfg: Any,
                          environ: Optional[Mapping[str, str]] = None,
                          ) -> list[tuple[str, str]]:
    """返回 ``[(点号路径, 环境变量名)]``：**该字段写死了明文，而 .env 里又有同名变量**。

    ## 为什么判据必须是「明文 **且** env 里有同名变量」

    「写死明文」本身**不是**错误——`main._report_credentials` 的选项③明写着
    「不建 .env，直接把真 Key 写进 config.yaml」。所以不能见明文就报。

    真正的病灶是两者**同时存在**：用户在用 ``${VAR}`` 模式，某一格却留着一枚
    写死的旧值。它永远不会跟着 .env 更新，而且——

    ## 它躲过了现有的两道网（2026-09-23 第三次 401 事故）

      · ``normalize_secret_fields`` 判「明文」的判据是 ``value == env[VAR]``。
        **.env 一换**，盘上那枚旧值就不再等于 env 的值，于是不再被认作明文，
        永远不会被恢复成 ``${VAR}``。而这个检测器恰好在「换 Token」——
        也就是它最该生效的那一刻——失灵。
      · ``unfilled_secrets`` 的三条判据（空 / 仍是 ``${VAR}`` / 老中文占位串）
        一条都不命中 → **静默放行**。

    结果：HTTP 全 401 撑了两天，契约测试 ``test_secret_config.py`` 是红的，
    却没有任何东西在生产里跑它。所以这里补上「启动时喊一声」。

    ## 只报不改

    不做自动改写：按形状改写会踩 ``normalize_secret_fields`` 里那段血书——
    把用户自己配的 ``${DASHSCOPE_API_KEY}`` 这类静默改没，比不改更糟。
    发现异常只报警，改不改由人决定。

    ⚠ 必须喂**解析前**的原始配置（``_resolve_env_vars`` 之后 ``${VAR}`` 已变成值，
    两者再也分不开）；并应在 ``normalize_secret_fields`` **之后**调用——
    那一步能自愈的（老占位串/空值/值恰好等于当前 env 值）都已经改好了。
    """
    env = os.environ if environ is None else environ
    out: list[tuple[str, str]] = []
    for path, env_name in secret_paths(cfg):
        if not str(env.get(env_name) or "").strip():
            continue                      # 没在用 ${VAR} 模式 → 选项③ 的合法填法
        found, value = _dig(cfg, path)
        if not found or not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text.startswith("${") or text in LEGACY_SECRET_LITERALS:
            continue                      # 空 / 引用 / 老占位串：另外两条判据管
        out.append((".".join(path), env_name))
    return out
