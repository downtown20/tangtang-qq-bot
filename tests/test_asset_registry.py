# -*- coding: utf-8 -*-
"""注册表与改名同步的闸门（2026-10-26 第三轮 P2）。"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import asset_edit  # noqa: E402


@pytest.fixture()
def root(tmp_path, monkeypatch):
    (tmp_path / "role_card.md").write_text("# 小糖糖\n", encoding="utf-8")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "psychology.yaml").write_text(
        "name: psychology\ntype: overlay\ndisplay: 心理陪伴\ndescription: 通用\n",
        encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "thinking.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "knowledge" / "恋爱军师").mkdir(parents=True)
    (tmp_path / "knowledge" / "恋爱军师" / "a.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "knowledge" / "色色参考").mkdir(parents=True)
    (tmp_path / "knowledge" / "色色参考" / "01_开场.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "_backups").mkdir()
    monkeypatch.setattr(asset_edit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(asset_edit, "BACKUP_ROOT", tmp_path / "_backups")
    return tmp_path


def _write_registry(root, items):
    (root / "assets.yaml").write_text(
        yaml.safe_dump({"assets": items}, allow_unicode=True), encoding="utf-8")


class TestRegistry:
    def test_absent_means_defaults(self, root):
        """没有 assets.yaml 时照常工作——它是可选增强，不是必需依赖。"""
        out = asset_edit.list_assets()
        labels = {s["key"]: s["label"] for g in out["groups"] for s in g["items"]}
        assert labels["knowledge_scenario"] == "场景触发"

    def test_label_override(self, root):
        _write_registry(root, [{"key": "knowledge_scenario", "label": "深夜档语料"}])
        out = asset_edit.list_assets()
        labels = {s["key"]: s["label"] for g in out["groups"] for s in g["items"]}
        assert labels["knowledge_scenario"] == "深夜档语料"
        # 其余没写的照旧
        assert labels["prompt"] == "协议条文"

    def test_root_override_keeps_matching(self, root):
        """改 root 之后 `_spec_for` 也要跟着改——否则界面能列出来但点进去说「不在白名单」。"""
        (root / "knowledge" / "深夜档").mkdir(parents=True)
        (root / "knowledge" / "深夜档" / "01.md").write_text("x\n", encoding="utf-8")
        _write_registry(root, [{"key": "knowledge_scenario", "root": "knowledge/深夜档"}])
        assert asset_edit._spec_for("knowledge/深夜档/01.md")["key"] == "knowledge_scenario"

    def test_broken_registry_falls_back(self, root):
        """注册表写坏了不能让整页打不开。"""
        (root / "assets.yaml").write_text("assets: [ {{{ 坏的\n", encoding="utf-8")
        out = asset_edit.list_assets()
        assert out["groups"], "注册表坏了应该退回默认值，而不是抛异常"

    def test_mechanism_fields_are_rejected(self, root):
        """kind / injection 属于机制，不许由数据改——改了界面和真实行为就分叉。"""
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.write_registry_entry("knowledge_scenario", injection="forced")
        assert ei.value.code == "not_allowed"

    def test_unknown_key_rejected(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.write_registry_entry("不存在的类", label="x")
        assert ei.value.code == "not_found"

    def test_write_registry_entry_roundtrip(self, root):
        asset_edit.write_registry_entry("knowledge_retrieval", label="资料库")
        reg = asset_edit.load_registry()
        assert reg["knowledge_retrieval"]["label"] == "资料库"
        # 文件头要写明它可选、能改什么——下一个接手的人不用翻代码
        text = (root / "assets.yaml").read_text(encoding="utf-8")
        assert "可选" in text and "label" in text
