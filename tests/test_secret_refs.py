"""`agent/secret_refs.py` 的四条归一规则 + 启动体检（2026-09-20）。

这个模块存在的理由见它自己的 docstring：密钥在项目里有**两条**落点
（config.yaml 存 `${VAR}` 引用、.env 存真值），靠"值恰好是 `${VAR}`"这一个约定
绑在一起。约定一破，控制台照常回显、保存照常成功，而运行期读的是另一份东西——
2026-09-20 那次事故就是这条约定在出厂模板里断了，瞒了很久。

所以下面的测试都围绕**边界**写：哪些情形必须归一、哪些必须**不动**（动了会丢用户的 Key）。
"""
import pytest

from agent.secret_refs import (
    FIXED_SECRET_PATHS, LEGACY_SECRET_LITERALS, PROVIDER_KEY_ENV,
    collapse_unresolved_secrets, normalize_secret_fields, secret_paths,
    unfilled_secrets,
)


def _cfg(provider="deepseek"):
    return {
        "llm": {"provider": provider, "api_key": "",
                "seductive_api_key": "", "vision": {"api_key": ""}},
        "napcat": {"access_token": ""},
    }


def _get(cfg, path):
    for k in path:
        cfg = cfg[k]
    return cfg


# ═══════════════════════════════════════════════════════════
# 归一：四种情形必须动
# ═══════════════════════════════════════════════════════════

def test_materialized_value_returns_to_reference():
    """① 值恰好等于环境变量 → 说明 UI 把 `${VAR}` 物化成了明文，还原。"""
    cfg = _cfg()
    cfg["llm"]["api_key"] = "sk-real-123"
    changed = normalize_secret_fields(cfg, {"DEEPSEEK_KEY": "sk-real-123"})
    assert cfg["llm"]["api_key"] == "${DEEPSEEK_KEY}"
    assert "llm.api_key" in changed


def test_legacy_literal_is_migrated():
    """② 老模板留下的中文占位串 → 换成 `${VAR}`（老用户的迁移路径）。"""
    cfg = _cfg()
    cfg["napcat"]["access_token"] = "请修改为你自己的 access_token"
    normalize_secret_fields(cfg, {})
    assert cfg["napcat"]["access_token"] == "${SNOWLUMA_TOKEN}"


def test_legacy_literal_migrates_even_after_provider_switch():
    """② 的边界：老占位串**不属于任何 provider**。

    原先的判据是 `LEGACY_SECRET_LITERALS.get(value) == env_name`，于是老用户
    把 provider 改成 anthropic 之后这条迁移不触发（老串固定映射 DEEPSEEK_KEY），
    那串中文继续被发给 Anthropic —— 而提示里写着「跑一次控制台会自动改正」，
    控制台走的又是同一处判据，同样不改。（Code Review B-1 缺口 2）
    """
    cfg = _cfg(provider="anthropic")
    cfg["llm"]["api_key"] = "你的DeepSeek或Claude的API密钥"
    normalize_secret_fields(cfg, {})
    assert cfg["llm"]["api_key"] == "${ANTHROPIC_KEY}", "换过 provider 的老配置没被迁移"


def test_provider_switch_rewrites_foreign_reference():
    """③ 指着别的 provider 的 `${..._KEY}` → 跟着 provider 换。

    顺带堵住一条跨商泄漏：provider 换成 anthropic 之后还写着 `${DEEPSEEK_KEY}`，
    等于**把 DeepSeek 的 Key 发给 Anthropic**。
    """
    cfg = _cfg(provider="anthropic")
    cfg["llm"]["api_key"] = "${DEEPSEEK_KEY}"
    normalize_secret_fields(cfg, {"DEEPSEEK_KEY": "sk-deepseek", "ANTHROPIC_KEY": "sk-ant"})
    assert cfg["llm"]["api_key"] == "${ANTHROPIC_KEY}"


@pytest.mark.parametrize("ref", ["${OPENROUTER_KEY}", "${DASHSCOPE_API_KEY}",
                                 "${MY_COMPANY_KEY}", "${SILICONFLOW_KEY}"])
def test_user_supplied_foreign_reference_is_never_clobbered(ref):
    """③ 的**边界**：只改写我们认识的那几个 provider 变量，别的名字一律不碰。

    第一版判据是按**形状**认的（「`${…_KEY}` 且不等于期望值就改」），
    于是用户按自己供应商文档填的 `${OPENROUTER_KEY}`（custom 场景）、
    `${DASHSCOPE_API_KEY}`（阿里官方文档给的名字，vision 场景）
    会被改成 `${CUSTOM_LLM_KEY}` / `${QWEN_KEY}`——而他的 .env 里没有那个新名字，
    解析不到 → 被清空 → 401。**把用户配好的 Key 静默改没了。**
    （Reality Check B-1 实测复现）

    形状不是语义：只有 PROVIDER_KEY_ENV 里那几个互斥槽位才谈得上"跟着 provider 换"。
    """
    cfg = _cfg(provider="custom")
    cfg["llm"]["api_key"] = ref
    normalize_secret_fields(cfg, {"OPENROUTER_KEY": "sk-or"})
    assert cfg["llm"]["api_key"] == ref, f"{ref} 被改写了——用户配好的 Key 会静默失效"


def test_blank_value_gets_reference_back():
    """④ 空串也要还原成 `${VAR}`——最隐蔽的一种。

    `.env.example` 里 `SNOWLUMA_TOKEN=` 是空的 → `_resolve_env_vars` 把它解析成
    `""` → 控制台原样写回 → 引用就这么变成了空字符串。之后用户再往「密钥管理」
    里填 Token，它进了 .env，而 config.yaml 那一格是死的 `""`：回到原病。
    """
    cfg = _cfg()
    normalize_secret_fields(cfg, {"SNOWLUMA_TOKEN": ""})
    assert cfg["napcat"]["access_token"] == "${SNOWLUMA_TOKEN}"
    assert cfg["llm"]["api_key"] == "${DEEPSEEK_KEY}"


# ═══════════════════════════════════════════════════════════
# 必须**不动**的情形（动了会丢用户的 Key）
# ═══════════════════════════════════════════════════════════

def test_user_written_literal_key_is_left_alone():
    """用户直接在 config.yaml 里写了一串真 Key，且 .env 里没有 → **不许动**。

    这里擅自改成 `${VAR}` 会把他的 Key 弄丢（变量没值 → 解析成空）。
    改由启动体检报出来——而这一项它有值，所以什么都不报：用户的选择是能用的。
    """
    cfg = _cfg()
    cfg["llm"]["api_key"] = "sk-用户手写的"
    # 其余三项先归位，免得它们的"空串→${VAR}"规则混进断言
    cfg["llm"]["seductive_api_key"] = "${SEDUCTIVE_KEY}"
    cfg["llm"]["vision"]["api_key"] = "${QWEN_KEY}"
    cfg["napcat"]["access_token"] = "${SNOWLUMA_TOKEN}"
    changed = normalize_secret_fields(cfg, {})
    assert cfg["llm"]["api_key"] == "sk-用户手写的", "把用户手写的 Key 洗掉了"
    assert changed == []


def test_already_correct_reference_is_idempotent():
    cfg = _cfg()
    for path, env in secret_paths(cfg):
        _set = cfg
        for k in path[:-1]:
            _set = _set[k]
        _set[path[-1]] = "${" + env + "}"
    assert normalize_secret_fields(cfg, {}) == []
    assert normalize_secret_fields(cfg, {"DEEPSEEK_KEY": "x"}) == []


def test_missing_field_is_not_created():
    """配置里没有这个字段就不要凭空造——`_put` 不建父级。"""
    cfg = {"llm": {"provider": "deepseek"}}
    normalize_secret_fields(cfg, {})
    assert "api_key" not in cfg["llm"]


# ═══════════════════════════════════════════════════════════
# 清空未解析的 `${VAR}`——绝不让它漏进 Authorization 头
# ═══════════════════════════════════════════════════════════

def test_unresolved_reference_is_collapsed_to_empty():
    """`_resolve_env_vars` 解析不到时会把原串还回来。

    那串 `${DEEPSEEK_KEY}` 一旦漏进 HTTP 头就是
    `Authorization: Bearer ${DEEPSEEK_KEY}`——401 的报错里看不出是这回事。
    """
    cfg = _cfg(provider="anthropic")
    cfg["llm"]["api_key"] = "${ANTHROPIC_KEY}"
    collapsed = collapse_unresolved_secrets(cfg)
    assert cfg["llm"]["api_key"] == ""
    assert "llm.api_key" in collapsed


def test_collapse_leaves_resolved_and_literal_values_alone():
    cfg = _cfg()
    cfg["llm"]["api_key"] = "sk-真值"
    cfg["napcat"]["access_token"] = ""
    assert collapse_unresolved_secrets(cfg) == []
    assert cfg["llm"]["api_key"] == "sk-真值"


# ═══════════════════════════════════════════════════════════
# 体检：区分「变量没设」/「值是空的」/「还是占位文字」
# ═══════════════════════════════════════════════════════════

def test_unfilled_reports_reason_precisely():
    """三种成因要说三句不同的话——用户要做的事不一样。"""
    cfg = _cfg()
    cfg["llm"]["api_key"] = "${DEEPSEEK_KEY}"     # 变量名对，但 .env 里没有
    cfg["napcat"]["access_token"] = ""            # 引用被物化成了空
    got = {p: r for p, _, r in unfilled_secrets(cfg, {"SNOWLUMA_TOKEN": ""})}
    assert ".env 里没有 DEEPSEEK_KEY 这个变量" == got["llm.api_key"]
    assert "值是空的" in got["napcat.access_token"]


def test_unfilled_flags_legacy_literal():
    cfg = _cfg()
    cfg["llm"]["api_key"] = "你的DeepSeek或Claude的API密钥"
    kinds = {p: r for p, _, r in unfilled_secrets(cfg, {})}
    assert "厂模板" in kinds["llm.api_key"] or "占位文字" in kinds["llm.api_key"]


def test_unfilled_is_quiet_when_every_resolvable_secret_has_a_value():
    """全部解析成功时，`unfilled_secrets` 一条都不报。

    注意这里喂的是**已解析**的配置（`main._resolve_env_vars` 的输出）——
    这是它的契约，也是它能把「变量没设」和「值本来就是空的」分开的原因。
    """
    cfg = _cfg(provider="openai")
    cfg["llm"]["api_key"] = "sk-real"
    cfg["llm"]["seductive_api_key"] = "sk-sed"
    cfg["llm"]["vision"]["api_key"] = "sk-qwen"
    cfg["napcat"]["access_token"] = "tok"
    assert unfilled_secrets(cfg, {}) == []


def test_optional_secrets_are_not_reported_by_the_logging_layer(monkeypatch):
    """**真正的契约在报告层**：可选密钥空着时，启动日志一个字都不该说。

    这一条直接对应主人 2026-09-20 的反馈「日志非常乱」——三个可选字段
    （亲密模式 Key / 识图 Key / SnowLuma Token）本来就允许为空，
    对它们无差别地打「未填」，就又把日志变回了噪音。
    """
    import logging

    from main import _report_credentials

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("糖糖")
    cap = _Cap()
    log.addHandler(cap)
    try:
        cfg = {"llm": {"provider": "openai", "api_key": "sk-real",
                       "vision": {"enabled": True, "provider": "local", "api_key": ""}},
               "napcat": {"access_token": ""}}
        missing = unfilled_secrets(cfg, {})
        assert missing, "前置条件：这几项确实是空的"
        _report_credentials(missing, cfg)
    finally:
        log.removeHandler(cap)
    assert lines == [], f"可选密钥空着却报了警：{lines}"


def test_missing_llm_key_is_reported_loudly(monkeypatch):
    """反过来：真正挡住功能的那个必须报，而且要说清怎么填。"""
    import logging

    from main import _report_credentials

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("糖糖")
    cap = _Cap()
    log.addHandler(cap)
    try:
        cfg = {"llm": {"provider": "anthropic", "api_key": "",
                       "vision": {"enabled": False, "provider": "local", "api_key": ""}},
               "napcat": {"access_token": ""}}
        _report_credentials(unfilled_secrets(cfg, {}), cfg)
    finally:
        log.removeHandler(cap)
    joined = "\n".join(lines)
    assert "LLM API Key" in joined
    assert "密钥管理" in joined and "ANTHROPIC_KEY" in joined, (
        f"报错了但没说怎么填：{joined}")


def test_upgraded_install_is_fixed_in_memory_without_touching_disk(monkeypatch):
    """老用户升级的路径：老占位串 → 归一 → **解析器能认**。

    ⚠ 这条必须自己清掉环境变量。第一版没清，于是单独跑绿、全量跑红——
    因为开发机的 `.env` 里有 `DEEPSEEK_KEY`，别的测试调用 `load_dotenv()` 之后
    它就进了 `os.environ`，`_resolve_env_vars` 于是真的解析出了值。
    一个**取决于测试执行顺序**的断言，等于没有断言。
    """
    import yaml
    from main import _resolve_env_vars
    monkeypatch.delenv("DEEPSEEK_KEY", raising=False)
    monkeypatch.delenv("SNOWLUMA_TOKEN", raising=False)
    raw = yaml.safe_load("""
llm:
  provider: deepseek
  api_key: 你的DeepSeek或Claude的API密钥
  vision:
    api_key: 你的视觉API密钥（不用识图可留空）
napcat:
  access_token: 请修改为你自己的 access_token
""")
    normalize_secret_fields(raw, {"DEEPSEEK_KEY": "sk-from-dotenv"})
    resolved = _resolve_env_vars(raw)
    # 环境里没值 → 解析器原样返回 `${VAR}`，这正是"归一到位了"的指纹
    assert resolved["llm"]["api_key"] == "${DEEPSEEK_KEY}"
    assert resolved["napcat"]["access_token"] == "${SNOWLUMA_TOKEN}"
    # 而环境里有值时必须解析出来——这才是 .env 机制真的接上了
    monkeypatch.setenv("DEEPSEEK_KEY", "sk-from-dotenv")
    assert _resolve_env_vars(raw)["llm"]["api_key"] == "sk-from-dotenv"


def test_legacy_literals_never_reach_the_runtime(monkeypatch):
    """老配置 + .env 有值 → 运行期必须拿到 .env 里的那一份（内存迁移即生效）。"""
    import os as _os
    import yaml
    from main import _resolve_env_vars
    monkeypatch.setenv("DEEPSEEK_KEY", "sk-from-dotenv")
    raw = yaml.safe_load("llm:\n  provider: deepseek\n  api_key: 你的DeepSeek或Claude的API密钥\n")
    normalize_secret_fields(raw, _os.environ)
    resolved = _resolve_env_vars(raw)
    assert resolved["llm"]["api_key"] == "sk-from-dotenv", (
        "老配置的内存迁移没生效——用户的 Key 还是到不了运行期")


@pytest.mark.parametrize("name", sorted(PROVIDER_KEY_ENV))
def test_every_provider_has_a_distinct_variable(name):
    assert PROVIDER_KEY_ENV[name].endswith("_KEY")
    assert len(set(PROVIDER_KEY_ENV.values())) == len(PROVIDER_KEY_ENV)


def test_fixed_paths_are_the_three_non_provider_secrets():
    assert dict(FIXED_SECRET_PATHS) == {
        ("llm", "seductive_api_key"): "SEDUCTIVE_KEY",
        ("llm", "vision", "api_key"): "QWEN_KEY",
        ("napcat", "access_token"): "SNOWLUMA_TOKEN",
    }


def test_legacy_literals_are_all_known_placeholders():
    """每个老占位串都得能被 `unfilled_secrets` 认出来（迁移表与体检表不许分家）。"""
    for literal in LEGACY_SECRET_LITERALS:
        cfg = _cfg()
        cfg["llm"]["api_key"] = literal
        assert unfilled_secrets(cfg, {}), f"{literal!r} 没被体检认出来"
