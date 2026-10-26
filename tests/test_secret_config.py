from pathlib import Path

import yaml


def test_config_keeps_llm_secret_as_environment_reference():
    """配置文件不得把可用的 LLM API key 物化到磁盘。

    契约是「这个字段是 `${VAR}` 引用，**且变量名与 provider 匹配**」——不是
    「必须等于 `${DEEPSEEK_KEY}`」。

    2026-09-22 修：原断言把变量名写死成 deepseek。密钥名是**跟着 provider 走**的
    （`agent/secret_refs.py` 的 `PROVIDER_KEY_ENV` 是单一事实源，控制台写 .env、
    运行期回填都用它），所以开发机一切到 `custom` 它就误报——**报的是"配置错了"，
    实际错的是这条断言**。
    """
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    llm = config.get("llm", {}) or {}
    api_key = str(llm.get("api_key", ""))

    from agent.secret_refs import PROVIDER_KEY_ENV
    provider = str(llm.get("provider", "deepseek") or "deepseek")
    expected = "${" + PROVIDER_KEY_ENV.get(provider,
                                           PROVIDER_KEY_ENV["deepseek"]) + "}"

    assert api_key == expected, (
        f"provider={provider!r} 的密钥引用应为 {expected}，实际是 {api_key!r}。"
        "填真实密钥、或填了个跟 provider 对不上的变量名，都会落到这里")


def test_config_keeps_snowluma_token_as_environment_reference():
    """QQ 接入令牌也必须只从环境变量注入。"""
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    token = str(config.get("napcat", {}).get("access_token", ""))

    assert token == "${SNOWLUMA_TOKEN}"


# ═══════════════════════════════════════════════════════════════════
# 写死的明文 + .env 里又有同名变量 = 必然对不上（2026-09-23 第三次 401）
#
# 事故：前一天把 .env 的 SNOWLUMA_TOKEN 换新，config.yaml 那一格却还留着
# 明文旧值。它同时躲过了两道网——
#   · normalize_secret_fields 的判据是 value == env[VAR]，.env 一换就不成立
#   · unfilled_secrets 的三条判据一条都不命中
# 于是 HTTP 全 401 撑了两天（收得到、发不出）。
# ═══════════════════════════════════════════════════════════════════

def _lit(cfg, env):
    from agent.secret_refs import literal_secret_fields
    return literal_secret_fields(cfg, env)


def test_materialized_literal_with_matching_env_var_is_flagged():
    """**这是那次事故的准确形状**——阳性对照。

    删掉 literal_secret_fields 里 `if not str(env.get(env_name)...): continue`
    之上的任何一行都会让它变红；把整个函数体改成 `return []` 同样变红。
    """
    cfg = {"napcat": {"access_token": "旧token的明文"}}
    assert _lit(cfg, {"SNOWLUMA_TOKEN": "新的token值"}) == [
        ("napcat.access_token", "SNOWLUMA_TOKEN")
    ]


def test_literal_without_env_var_is_not_flagged():
    """「不建 .env，直接把真 Key 写进 config.yaml」是 main._report_credentials
    明写的**选项③**。没有同名 env 变量时它完全合法——不能见明文就报，
    否则就是把「分不清哪些提示与自己有关」这个老毛病又犯一遍。"""
    cfg = {"llm": {"api_key": "sk-直接写死的key"}}
    assert _lit(cfg, {}) == []


def test_env_reference_is_not_flagged():
    cfg = {"napcat": {"access_token": "${SNOWLUMA_TOKEN}"}}
    assert _lit(cfg, {"SNOWLUMA_TOKEN": "x"}) == []


def test_empty_and_blank_are_not_flagged():
    """空值归 unfilled_secrets 管，不该在这里重复报。"""
    for value in ("", "   ", None):
        cfg = {"napcat": {"access_token": value}}
        assert _lit(cfg, {"SNOWLUMA_TOKEN": "x"}) == []


def test_legacy_placeholder_is_not_flagged():
    """出厂中文占位串归 normalize/迁移管，不是「写死的凭据」。"""
    cfg = {"llm": {"api_key": "你的DeepSeek或Claude的API密钥"}}
    assert _lit(cfg, {"DEEPSEEK_KEY": "x"}) == []


def test_literal_detector_covers_all_fixed_paths():
    """四个固定密钥槽位都要被覆盖，漏一个就是留一个静默 401 的口子。"""
    cfg = {
        "llm": {"provider": "custom", "api_key": "明文1",
                "seductive_api_key": "明文2",
                "vision": {"api_key": "明文3"}},
        "napcat": {"access_token": "明文4"},
    }
    env = {"CUSTOM_LLM_KEY": "x", "SEDUCTIVE_KEY": "x",
           "QWEN_KEY": "x", "SNOWLUMA_TOKEN": "x"}
    paths = {p for p, _ in _lit(cfg, env)}
    assert paths == {"llm.api_key", "llm.seductive_api_key",
                     "llm.vision.api_key", "napcat.access_token"}


def test_literal_detector_does_not_rewrite():
    """只报不改——按形状自动改写会把用户自己配的 ${OTHER_KEY} 静默改没
    （secret_refs 里那段血书）。这个断言钉住「它是个纯函数」。"""
    cfg = {"napcat": {"access_token": "明文"}}
    _lit(cfg, {"SNOWLUMA_TOKEN": "x"})
    assert cfg["napcat"]["access_token"] == "明文"


def test_console_restores_snowluma_token_placeholder():
    """控制台保存时必须把 SNOWLUMA_TOKEN 还原成 `${...}` 引用。

    2026-09-20 起这条规则连同它的三个同伴搬进了 `agent/secret_refs.py`
    （单一事实源：控制台 / main.py / 体检 / handler 共用同一张变量名表），
    控制台改为调用 `normalize_secret_fields`。所以这里断言的不再是那行字面量，
    而是**接线本身**——控制台保存路径必须过这个函数。
    """
    root = Path(__file__).resolve().parents[1]
    console = (root / "运维" / "糖糖控制台_qt.py").read_text(encoding="utf-8")

    assert "normalize_secret_fields(data)" in console, (
        "save_config_file 没走密钥归一——保存会把 ${VAR} 物化成明文，"
        "或者让老的占位串留在盘上")
    from agent.secret_refs import FIXED_SECRET_PATHS
    assert (("napcat", "access_token"), "SNOWLUMA_TOKEN") in FIXED_SECRET_PATHS


def test_console_save_rewrites_materialized_secret(tmp_path, monkeypatch):
    """控制台实际保存时，已解析的令牌值仍会被还原为占位符。"""
    import importlib

    console = importlib.import_module("糖糖控制台_qt")
    config_path = tmp_path / "config.yaml"
    backup_path = tmp_path / "config.yaml.bak"
    config_path.write_text("napcat:\n  access_token: old\n", encoding="utf-8")
    monkeypatch.setattr(console, "CONFIG_PATH", config_path)
    monkeypatch.setattr(console, "BACKUP_PATH", backup_path)
    monkeypatch.setenv("SNOWLUMA_TOKEN", "runtime-token")

    data = {"napcat": {"access_token": "runtime-token"}}
    console.save_config_file(data)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["napcat"]["access_token"] == "${SNOWLUMA_TOKEN}"
    assert backup_path.exists()


def test_console_save_preserves_config_sections_not_owned_by_ui(tmp_path, monkeypatch):
    """控制台保存 UI 字段时不得删除尚未接入界面的配置段。"""
    import importlib

    console = importlib.import_module("糖糖控制台_qt")
    config_path = tmp_path / "config.yaml"
    backup_path = tmp_path / "config.yaml.bak"
    config_path.write_text(
        "tasks:\n"
        "  text_action_outbox_enabled: true\n"
        "  media_action_outbox_enabled: false\n"
        "appearance:\n"
        "  font_size: 13\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "CONFIG_PATH", config_path)
    monkeypatch.setattr(console, "BACKUP_PATH", backup_path)

    console.save_config_file({"appearance": {"font_size": 14}})

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["tasks"] == {
        "text_action_outbox_enabled": True,
        "media_action_outbox_enabled": False,
    }
    assert saved["appearance"]["font_size"] == 14


def test_console_save_preserves_task_gates_removed_from_ui(tmp_path, monkeypatch):
    """控制台保存剩余 UI 字段时不得回删 config-only 的任务闸门。"""
    import importlib

    console = importlib.import_module("糖糖控制台_qt")
    config_path = tmp_path / "config.yaml"
    backup_path = tmp_path / "config.yaml.bak"
    config_path.write_text(
        "tasks:\n"
        "  text_action_outbox_enabled: true\n"
        "  media_action_outbox_enabled: true\n"
        "  send_outbox_claims_enabled: true\n"
        "  manual_retry_generation_enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(console, "CONFIG_PATH", config_path)
    monkeypatch.setattr(console, "BACKUP_PATH", backup_path)

    console.save_config_file({"tasks": {
        "text_action_outbox_enabled": False,
        "media_action_outbox_enabled": False,
    }})

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["tasks"] == {
        "text_action_outbox_enabled": False,
        "media_action_outbox_enabled": False,
        "send_outbox_claims_enabled": True,
        "manual_retry_generation_enabled": True,
    }
