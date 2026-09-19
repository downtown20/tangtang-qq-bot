from pathlib import Path

import yaml


def test_config_keeps_llm_secret_as_environment_reference():
    """配置文件不得把可用的 LLM API key 物化到磁盘。"""
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    api_key = str(config.get("llm", {}).get("api_key", ""))

    assert api_key == "${DEEPSEEK_KEY}"


def test_config_keeps_snowluma_token_as_environment_reference():
    """QQ 接入令牌也必须只从环境变量注入。"""
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    token = str(config.get("napcat", {}).get("access_token", ""))

    assert token == "${SNOWLUMA_TOKEN}"


def test_console_restores_snowluma_token_placeholder():
    root = Path(__file__).resolve().parents[1]
    console = (root / "糖糖控制台_qt.py").read_text(encoding="utf-8")

    assert '(("napcat", "access_token"), "SNOWLUMA_TOKEN")' in console


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
