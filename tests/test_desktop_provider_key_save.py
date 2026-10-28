# -*- coding: utf-8 -*-
"""新建 provider 组的 Key 必须能存进 .env（2026-10-28 主人报的 bug）。

现象：在 AI 模型页新建一个 provider、粘 Key 回车 → 输入框被清空、界面仍显示「没填」。
根因：`.env` 写入白名单只认 `PROVIDER_KEY_ENV` 那 5 个固定 provider，
新建组自动生成的 `${PMVRO2C5Y_KEY}` 不在里面 → 400 拒绝；前端又先把输入框清空了。
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_CFG = """\
llm:
  providers:
  - id: custom
    label: 自建中转
    api: openai-completions
    base_url: https://example.test/v1
    api_key: ${CUSTOM_LLM_KEY}
  - id: pmvro2c5y
    label: DeepSeek 官方
    api: openai-completions
    base_url: https://api.deepseek.com/v1
    api_key: ${PMVRO2C5Y_KEY}
image_tagging:
  model_ref: ''
"""


def _load_bridge():
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_key_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(BASE_CFG, encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("# 保留我的注释\nCUSTOM_LLM_KEY=sk-keep-me\n", encoding="utf-8")
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "ENV_PATH", env)
    return mod, cfg, env


def test_new_provider_env_name_is_allowed(tmp_path, monkeypatch):
    """核心：新建组自动生成的变量名必须进白名单，否则 Key 永远存不进去。"""
    mod, _, _ = _fixture(tmp_path, monkeypatch)
    names = mod._secret_env_names()
    assert "PMVRO2C5Y_KEY" in names, (
        f"新建 provider 的 env 名不在白名单里：{sorted(names)}")
    assert "CUSTOM_LLM_KEY" in names, "固定 provider 的键也不能丢"


def test_save_key_for_new_provider_succeeds(tmp_path, monkeypatch):
    """端到端：写成功 + 读回来 configured=True（界面据此显示「已填」）。"""
    mod, _, env = _fixture(tmp_path, monkeypatch)

    out = mod.api_secrets_save({"name": "PMVRO2C5Y_KEY", "value": "sk-deepseek-probe"})
    assert out.get("ok") is True

    text = env.read_text(encoding="utf-8")
    assert "PMVRO2C5Y_KEY=sk-deepseek-probe" in text
    assert "# 保留我的注释" in text, "写 Key 不许把 .env 里其它行/注释冲掉"
    assert "CUSTOM_LLM_KEY=sk-keep-me" in text

    after = mod.api_secrets()
    entry = next(e for e in after["entries"] if e["name"] == "PMVRO2C5Y_KEY")
    assert entry["configured"] is True, (
        "写进 .env 了但读回来还是「未配置」—— 界面会一直显示「没填」")
    assert entry["masked"].endswith("robe"), "掩码要露末四位给人确认"


def test_whitelist_is_still_a_whitelist(tmp_path, monkeypatch):
    """放行范围仍然是白名单：配置里没引用的任意键必须被拒。"""
    import pytest

    mod, _, _ = _fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        mod.api_secrets_save({"name": "TOTALLY_RANDOM_KEY", "value": "x"})


def test_referenced_names_only_from_providers(tmp_path, monkeypatch):
    """白名单只吃 `llm.providers[].api_key` 上的 `${VAR}`，不扫别处。"""
    mod, cfg, _ = _fixture(tmp_path, monkeypatch)
    cfg.write_text(
        BASE_CFG.replace(
            "    api_key: ${PMVRO2C5Y_KEY}",
            "    api_key: ${PMVRO2C5Y_KEY}\n"
            "llm_extra:\n  sneaky: ${SNEAKY_KEY}\n"),
        encoding="utf-8")
    names = mod._secret_env_names()
    assert "PMVRO2C5Y_KEY" in names
    assert "SNEAKY_KEY" not in names, "别处出现的 ${VAR} 不该被放行"
