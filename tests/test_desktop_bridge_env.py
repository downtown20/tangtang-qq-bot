# -*- coding: utf-8 -*-
"""桌面端「连接密钥 / 首启自举」的契约（2026-10-26）。

## 为什么有这份测试

旧 Qt 控制台的「密钥管理」是用户填 LLM Key / SnowLuma Token 的唯一入口。
2026-10-26 它移入 `运维/`、桌面端成为主入口，于是桥新增了 `/api/secrets` 与
`/api/secrets/save`。**密钥写入是安全敏感面**，所以这里不只测「能写」，
还钉死四条边界（都是主人明确要求的安全测试）：

1. 白名单外的键写入 → 必须被拒绝（阳性：`ARBITRARY_KEY`）；
2. 读接口绝不回显明文（阳性：写一个假 token，断言完整串不在响应里）；
3. 写入后**磁盘上确实有值**（证明不是假写）；
4. 写前备份文件被创建；且是最小编辑（注释与其它键必须保留）。

另外两条防「桥起不来」：
5. 缺 `config.yaml` 但有 `config.example.yaml` → 自举成功，`/api/config`
   返回 `configured=true` / `bootstrapped=true`；
6. 两者都缺 → 返回 `{}` 而不是抛异常（桥仍可启动，前端显示「未配置」）。

阳性对照（教训 #30/#37）：把 `_secret_env_names()` 的白名单检查删掉，第 1 条会红；
把 `_mask_env_value` 改成原样返回，第 2 条会红。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_bridge():
    """每次 exec 一份新模块：模块级常量（CONFIG_PATH 等）可被 monkeypatch。"""
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n  provider: deepseek\nbot:\n  qq_id: '1'\n", encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text(
        "llm:\n  provider: deepseek\n", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(mod, "_example_bootstrapped", False)
    return mod


# ── 安全边界 1：白名单外拒绝 ──────────────────────────────────

def test_secret_whitelist_rejects_unknown_key(bridge):
    with pytest.raises(ValueError) as ei:
        bridge.api_secrets_save({"name": "ARBITRARY_KEY", "value": "x"})
    assert "不允许" in str(ei.value)
    # 拒绝时不许留下任何写入痕迹
    if bridge.ENV_PATH.exists():
        assert "ARBITRARY_KEY" not in bridge.ENV_PATH.read_text(encoding="utf-8")


def test_secret_whitelist_rejects_newline_injection(bridge):
    """值里带换行 = 可以注入一个新键——必须拒绝。"""
    with pytest.raises(ValueError):
        bridge.api_secrets_save(
            {"name": "SNOWLUMA_TOKEN", "value": "ok\nARBITRARY_KEY=evil"})


def test_secret_whitelist_rejects_placeholder(bridge):
    with pytest.raises(ValueError):
        bridge.api_secrets_save(
            {"name": "SNOWLUMA_TOKEN", "value": "${SNOWLUMA_TOKEN}"})


# ── 安全边界 2：读接口不回显明文 ──────────────────────────────

def test_read_never_echoes_plaintext(bridge):
    fake = "sk-test-abcdef123456"
    bridge.ENV_PATH.write_text(
        f"SNOWLUMA_TOKEN={fake}\nDEEPSEEK_KEY={fake}\n", encoding="utf-8")
    data = bridge.api_secrets()
    blob = json.dumps(data, ensure_ascii=False)
    assert fake not in blob, "读接口把明文回显给前端了"
    entry = next(e for e in data["entries"] if e["name"] == "SNOWLUMA_TOKEN")
    assert entry["configured"] is True
    assert entry["masked"].endswith("3456")
    assert len(entry["masked"]) < len(fake)


# ── 安全边界 3 + 4：真写盘、备份、最小编辑 ────────────────────

def test_write_lands_on_disk_with_backup_and_preserves_other_lines(bridge):
    bridge.ENV_PATH.write_text(
        "# 注释是价值的一半\nDEEPSEEK_KEY=old-value\nOTHER=keep\n",
        encoding="utf-8")
    res = bridge.api_secrets_save(
        {"name": "SNOWLUMA_TOKEN", "value": "tok-123456"})

    text = bridge.ENV_PATH.read_text(encoding="utf-8")
    assert "# 注释是价值的一半" in text, "最小编辑不该丢注释"
    assert "OTHER=keep" in text, "最小编辑不该动其它键"
    assert "DEEPSEEK_KEY=old-value" in text
    assert "SNOWLUMA_TOKEN=tok-123456" in text
    assert res["configured"] is True
    assert res["masked"].endswith("3456")

    backups = sorted(bridge.ENV_PATH.parent.glob(".env.bak_*"))
    assert backups, "写 .env 前必须备份"
    assert "SNOWLUMA_TOKEN" not in backups[0].read_text(encoding="utf-8")

    # 覆盖同键：旧值被替换，不产生第二行
    bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": "new-999999"})
    text2 = bridge.ENV_PATH.read_text(encoding="utf-8")
    assert text2.count("SNOWLUMA_TOKEN=") == 1
    assert "new-999999" in text2
    assert "tok-123456" not in text2


# ── provider 决定变量名：单一事实源 ────────────────────────────

def test_provider_env_name_follows_provider(bridge):
    names = bridge._secret_env_names()
    assert "DEEPSEEK_KEY" in names
    bridge.CONFIG_PATH.write_text("llm:\n  provider: custom\n", encoding="utf-8")
    assert "CUSTOM_LLM_KEY" in bridge._secret_env_names()


# ── 首启自举 ──────────────────────────────────────────────────

def test_bootstrap_from_example(bridge):
    bridge.CONFIG_PATH.unlink()
    raw = bridge._load_raw_config()
    assert bridge.CONFIG_PATH.is_file(), "缺 config.yaml 时桥没有从模板自举"
    assert raw["llm"]["provider"] == "deepseek"
    cfg = bridge.api_config()
    assert cfg["configured"] is True
    assert cfg["bootstrapped"] is True
    assert cfg["example_available"] is True


def test_missing_example_returns_empty_not_crash(bridge):
    bridge.CONFIG_PATH.unlink()
    (bridge.ROOT / "config.example.yaml").unlink()
    assert bridge._load_raw_config() == {}
    assert bridge.api_config()["configured"] is False


def test_bootstrap_never_overwrites_existing_config(bridge):
    before = bridge.CONFIG_PATH.read_text(encoding="utf-8")
    assert bridge._bootstrap_config() is False
    assert bridge.CONFIG_PATH.read_text(encoding="utf-8") == before


# ── 连接自检序列化 / 路由登记 ─────────────────────────────────

def test_connect_check_serializes_results(bridge, monkeypatch):
    status = type("S", (), {"value": "ok"})()
    item = type("R", (), {
        "key": "k", "title": "t", "status": status,
        "detail": "d", "next_step": "n",
    })()

    class FakeMod:
        @staticmethod
        def check_all(root):
            assert Path(root) == bridge.ROOT
            return [item]

    monkeypatch.setattr(bridge, "_load_conn_check_module", lambda: FakeMod)
    out = bridge.api_connect_check()
    assert out["results"] == [{
        "key": "k", "title": "t", "status": "ok",
        "detail": "d", "next_step": "n"}]


def test_new_routes_registered(bridge):
    for r in ("/api/secrets", "/api/snowluma/status", "/api/connect-check"):
        assert r in bridge.ROUTES_GET, f"{r} 没登记"
    for r in ("/api/secrets/save", "/api/snowluma/start"):
        assert r in bridge.ROUTES_POST, f"{r} 没登记"
    assert "/api/config" in bridge.ROUTES_GET


# ── SnowLuma 状态/启动的错误路径 ──────────────────────────────

def test_snowluma_status_not_installed(bridge, monkeypatch):
    # 探端口必须注入——否则测试结果取决于本机 SnowLuma 是否恰好开着
    monkeypatch.setattr(bridge, "_port_open", lambda port, timeout=0.4: False)
    st = bridge.api_snowluma_status()
    assert st["installed"] is False
    assert st["running"] is False
    assert st["webui_url"].startswith("http://127.0.0.1:")


def test_snowluma_start_without_install_raises(bridge):
    with pytest.raises(ValueError) as ei:
        bridge.api_snowluma_start()
    assert "SnowLuma" in str(ei.value)


def test_snowluma_start_finds_launcher(bridge, monkeypatch):
    """有 launcher.bat 时走 Popen——用假的 Popen 避免真的起进程。"""
    d = bridge.ROOT / "SnowLuma" / "SnowLuma-v1.0.0-win-x64"
    d.mkdir(parents=True)
    (d / "launcher.bat").write_text("@echo off\n", encoding="utf-8")

    calls = {}

    class FakePopen:
        pid = 1234

        def __init__(self, args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs

    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(bridge, "_port_open", lambda port, timeout=0.4: False)
    out = bridge.api_snowluma_start()
    assert out["ok"] is True
    assert out["pid"] == 1234
    assert calls["args"] == [str(d / "launcher.bat")]
    assert calls["kwargs"]["cwd"] == str(d)
