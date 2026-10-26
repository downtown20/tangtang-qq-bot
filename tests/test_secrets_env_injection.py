# -*- coding: utf-8 -*-
"""`.env` 密钥写入的行边界与回滚契约（2026-10-27 审查发现 → 修复 → 钉住）。

## 为什么有这份测试

`tools/desktop_bridge.py::api_secrets_save` 最初只挡 `\\n` / `\\r`，
但**读侧** `_load_dotenv` 用的是 `str.splitlines()` —— 而
`\\v \\f \\x1c \\x1d \\x1e \\x85 \\u2028 \\u2029` 这些**在 splitlines 眼里同样是行边界**。
于是往「值」里塞一个 `abc\\u2028EVIL=1`，就能往 `.env` 里**注入一个新的键**
（比如改掉别的服务 token）。审查报告 2026-10-27 的 I1 就是这个。

同一条发现还包括：先落盘、后回读校验，校验失败**不回滚** ——
用户会拿到「点了保存、界面报错、但文件已经被写坏」的 `.env`。

## 钉住什么

1. 九种行边界字符，任何一种都必须在**写盘之前**被拒绝（磁盘不能被污染）
2. 正常值仍能写入；注释与其它键必须保留（最小编辑）
3. 返回值绝不回显明文，只给掩码
4. 写后回读若发现**预期外的键**（= 被注入），必须回滚到写入前的字节
5. 写后回读值不一致时，同样回滚

阳性对照：把 `if len(value.splitlines()) > 1` 改回 `if "\\n" in value or "\\r" in value`，
第 1 条里 `\\v` / `\\u2028` 那几个参数会红。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 每一种都是 str.splitlines() 认的行边界；value 里出现它就等于能多加一行
INJECT_VALUES = [
    ("lf", "abc\nEVIL=1"),
    ("cr", "abc\rEVIL=1"),
    ("vt", "abc\vEVIL=1"),
    ("ff", "abc\fEVIL=1"),
    ("fs", "abc\x1cEVIL=1"),
    ("gs", "abc\x1dEVIL=1"),
    ("rs", "abc\x1eEVIL=1"),
    ("nel", "abc\x85EVIL=1"),
    ("ls", "abc\u2028EVIL=1"),
    ("ps", "abc\u2029EVIL=1"),
]


def _load_bridge():
    path = ROOT / "tools" / "desktop_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_envsec_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    mod = _load_bridge()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm:\n  provider: deepseek\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("# 注释要保留\nSNOWLUMA_TOKEN=old\nOTHER=x\n",
                   encoding="utf-8", newline="")
    monkeypatch.setattr(mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(mod, "ENV_PATH", env)
    return mod


@pytest.mark.parametrize("label,value", INJECT_VALUES, ids=[v[0] for v in INJECT_VALUES])
def test_line_break_injection_is_rejected_before_write(bridge, label, value):
    """任何 splitlines 认的行边界都必须被拒，且**磁盘一个字节都不能动**。"""
    before = bridge.ENV_PATH.read_bytes()
    with pytest.raises(ValueError):
        bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": value})
    assert bridge.ENV_PATH.read_bytes() == before, (
        "%s 注入被拒了，但文件已经被改过 —— 拒绝必须发生在写盘之前" % label)


def test_normal_write_keeps_comments_and_other_keys(bridge):
    """正常值要能写进去，且最小编辑（注释、其它键、掩码、明文不外泄）。"""
    env = bridge.ENV_PATH
    out = bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": "good_value_1234"})
    body = env.read_text(encoding="utf-8")
    assert "SNOWLUMA_TOKEN=good_value_1234" in body
    assert "# 注释要保留" in body, "注释是 .env 的一半价值，不能被吃掉"
    assert "OTHER=x" in body, "其它键不能被吃掉"
    assert out["ok"] is True
    assert "good_value_1234" not in str(out), "返回值不许回显明文"
    assert out["masked"].endswith("1234")


def test_injected_key_triggers_rollback(bridge, monkeypatch):
    """写后回读冒出预期外的键 → 判定为注入 → 必须回滚到写入前的字节。"""
    env = bridge.ENV_PATH
    before = env.read_bytes()
    real = bridge._load_dotenv
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        d = dict(real())
        if calls["n"] >= 2:      # 第 1 次是写入前基线，第 2 次是写后回读
            d["EVIL_INJECTED"] = "1"
        return d

    monkeypatch.setattr(bridge, "_load_dotenv", fake)
    with pytest.raises(RuntimeError):
        bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": "another_value_99"})
    assert env.read_bytes() == before, "检测到注入后必须逐字节回滚"


def test_value_mismatch_triggers_rollback(bridge, monkeypatch):
    """写后回读的值和要写的值不一致 → 同样回滚（不能留个半坏的文件）。"""
    env = bridge.ENV_PATH
    before = env.read_bytes()
    real = bridge._load_dotenv
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        d = dict(real())
        if calls["n"] >= 2:
            d["SNOWLUMA_TOKEN"] = "被别的进程改掉了"
        return d

    monkeypatch.setattr(bridge, "_load_dotenv", fake)
    with pytest.raises(RuntimeError):
        bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": "expected_value_1"})
    assert env.read_bytes() == before


def test_crlf_env_keeps_crlf(bridge):
    """CRLF 的 `.env` 保存后必须仍是 CRLF ——「最小编辑」包括行尾。

    实测过的坑：`ENV_PATH.read_text()` 默认 `newline=None` 是 universal newlines，
    会把 `\\r\\n` 读成 `\\n`，于是 `nl = "\\r\\n" if "\\r\\n" in text else "\\n"`
    永远判成 LF，写回就把整个文件的行尾换掉（3 个 CRLF → 3 个 LF）。
    这是本项目「Windows 文件读写两端 newline=''」那条铁律漏掉的地方。
    """
    env = bridge.ENV_PATH
    env.write_bytes(b"A=1\r\nSNOWLUMA_TOKEN=old\r\nC=3\r\n")

    bridge.api_secrets_save({"name": "SNOWLUMA_TOKEN", "value": "new_val_999"})

    after = env.read_bytes()
    assert after.count(b"\r\n") == 3, "行尾被改了：%r" % after
    assert after.count(b"\n") - after.count(b"\r\n") == 0, "混进了 LF-only 的行：%r" % after
    assert b"SNOWLUMA_TOKEN=new_val_999" in after
    assert b"C=3" in after, "紧随其后的一行被吞掉了"

