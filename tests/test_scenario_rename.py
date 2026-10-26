# -*- coding: utf-8 -*-
"""场景卡改名同步的闸门（2026-10-26 第三轮）。

## 防的是什么

`config.yaml` 里 `scenario_targets: {'2524950083': 亲密模式}` 存的是 **display 名**
（`agent/scenario.py:138-150` 用 display 匹配）。改 display 不改这里 → **静默失效**：
场景不再生效，没有任何报错，只有「她怎么不像那个模式了」。

## 阳性对照

- 把 `sync_rename` 的 `if old_key == new_key: return []` 删掉 → `test_noop_when_same`
- 把块范围判断改成全文替换 → `test_other_keys_untouched`（它会误改别处的同名值）
- 把 `display_key` 的 split 去掉 → `test_display_key_strips_emoji`
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import scenario_rename as sr

CONFIG = """# 这份配置的注释是它的一半价值，不许被程序写丢
personality:
  core: 猫娘

scenario_targets:
  '2524950083': 亲密模式
  '999999': 亲密模式

# 这里也有一个「亲密模式」，但它不是挂载点——不能动
note:
  last_used: 亲密模式

voice:
  enabled: true
"""


@pytest.fixture()
def proj(tmp_path):
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


def test_display_key_strips_emoji():
    assert sr.display_key("❤️ 亲密模式") == "亲密模式"
    assert sr.display_key("亲密模式") == "亲密模式"
    assert sr.display_key("🧠 心理陪伴") == "心理陪伴"


def test_rename_updates_mounts(proj):
    hits = sr.sync_rename(proj, "❤️ 亲密模式", "❤️ 深夜档")
    text = (proj / "config.yaml").read_text(encoding="utf-8")
    assert sorted(hits) == ["2524950083", "999999"]
    assert "'2524950083': 深夜档" in text
    assert "'999999': 深夜档" in text


def test_other_keys_untouched(proj):
    """块外的同名值不能动——那是别人的字段，不是挂载点。"""
    sr.sync_rename(proj, "亲密模式", "深夜档")
    text = (proj / "config.yaml").read_text(encoding="utf-8")
    assert "last_used: 亲密模式" in text, "块外的同名值被误改了"


def test_comments_survive(proj):
    """用文本级替换的全部理由：注释不能丢。"""
    sr.sync_rename(proj, "亲密模式", "深夜档")
    text = (proj / "config.yaml").read_text(encoding="utf-8")
    assert "# 这份配置的注释是它的一半价值" in text


def test_noop_when_same(proj):
    assert sr.sync_rename(proj, "亲密模式", "亲密模式") == []
    assert sr.sync_rename(proj, "❤️ 亲密模式", "亲密模式") == []


def test_no_block_returns_empty(tmp_path):
    (tmp_path / "config.yaml").write_text("personality:\n  core: x\n", encoding="utf-8")
    assert sr.sync_rename(tmp_path, "亲密模式", "深夜档") == []


def test_missing_config_isolated(tmp_path):
    assert sr.sync_rename(tmp_path, "亲密模式", "深夜档") == []


def test_unmounted_name_changes_nothing(proj):
    before = (proj / "config.yaml").read_text(encoding="utf-8")
    assert sr.sync_rename(proj, "没人挂的名字", "新名字") == []
    assert (proj / "config.yaml").read_text(encoding="utf-8") == before
