# -*- coding: utf-8 -*-
"""可编辑资产层的闸门（2026-10-26 第 49 轮 B1）。

## 这组用例防的是什么

主人要「参数完全暴露，自由度拉满」。**自由度最大的风险是实现者自己**：
一个写错的路径检查、一次忘记备份、一个漏掉的 `..` 过滤，就能让人从界面里
改到 `memory.db`、或者把她的协议文本改成空串导致她答非所问一整夜。

所以这组测试的重点**不是「能读能写」**（那是最容易的部分），而是：

| 方向 | 断言 | 防的是 |
|---|---|---|
| 白名单 | `..` / 绝对路径 / `agent/protocols.py` / `memory.db` 全部拒绝 | 通用文件管理器 = 一把能删库的刀（教训 #31 白名单） |
| 符号链接 | resolve 后跳出白名单根 → 拒绝 | 白名单被软链绕过 |
| 后缀 | `scenarios/` 只收 `.yaml`，`prompts/` 只收 `.md` | 把 py 塞进场景目录再让加载器去 import |
| 备份 | **先备份再校验**：校验失败的写入也留下原文件 | 改坏了想回滚，发现没备份 |
| 校验 | 坏 YAML 拒绝写入，**磁盘内容不变**，错误带行号 | 半截写入 / 静默截断 |
| 默认值 | `reset` 只对 `prompts/` 有效 | 以为能一键恢复人格卡，结果清空了 |

## 阳性对照（教训 #30/#37：闸门必须能红）

- 把 `_spec_for` 的白名单判定改成「只要不报错就放行」→ `test_rejects_paths_outside_whitelist` 红；
- 把 `write_asset` 的备份挪到校验之后 → `test_backup_happens_before_validation` 红；
- 把 YAML 校验删掉 → `test_bad_yaml_is_rejected_and_disk_unchanged` 红；
- 把 `reset_asset` 的 `spec["key"] != "prompt"` 判断删掉 → `test_reset_only_for_prompts` 红。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import asset_edit  # noqa: E402


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """造一棵最小的假项目树，并把模块的根指向它。

    **绝不能用真实项目根**：`write_asset` 会真的写文件，测一次就把人格卡改了。
    """
    (tmp_path / "role_card.md").write_text("# 小糖糖\n\n## 你是谁\n\n猫娘\n", encoding="utf-8")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios" / "psychology.yaml").write_text(
        "name: psychology\ntype: overlay\ndisplay: 心理陪伴\ndescription: 通用\n",
        encoding="utf-8")
    (tmp_path / "scenarios" / "evil.py").write_text("print('nope')\n", encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "thinking.md").write_text("内置思考协议的覆盖版\n", encoding="utf-8")
    (tmp_path / "knowledge" / "恋爱军师").mkdir(parents=True)
    (tmp_path / "knowledge" / "恋爱军师" / "知识.md").write_text("军师知识\n", encoding="utf-8")
    (tmp_path / "knowledge" / "色色参考").mkdir(parents=True)
    (tmp_path / "knowledge" / "色色参考" / "01_开场.md").write_text("范本\n", encoding="utf-8")
    # 白名单外的诱饵：存在、但是绝对不能碰
    (tmp_path / "memory.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "protocols.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "_backups").mkdir()

    monkeypatch.setattr(asset_edit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(asset_edit, "BACKUP_ROOT", tmp_path / "_backups")
    return tmp_path


# ── 白名单 ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../etc/passwd",
    "scenarios/../../memory.db",
    "memory.db",
    "agent/protocols.py",
    "config.yaml",
    "状态/不该可编辑.json",
    "/abs/path/role_card.md",
    "C:/Windows/system.ini",
    "~/secret",
])
def test_rejects_paths_outside_whitelist(root, bad):
    """白名单外的一切都必须拒绝，且**磁盘上什么都没发生**。"""
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.read_asset(bad)
    assert ei.value.code in ("bad_path", "forbidden"), ei.value.code
    assert (root / "memory.db").read_bytes().startswith(b"SQLite")


def test_rejects_wrong_extension(root):
    """`scenarios/` 只收 `.yaml`——不然能往场景目录塞一个 py，等加载器去 import。"""
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.read_asset("scenarios/evil.py")
    assert ei.value.code == "bad_ext"


def test_allows_whitelisted_paths(root):
    for rel in ("role_card.md", "scenarios/psychology.yaml",
                "prompts/thinking.md", "knowledge/恋爱军师/知识.md",
                "knowledge/色色参考/01_开场.md"):
        got = asset_edit.read_asset(rel)
        assert got["path"] == rel
        assert got["content"].strip()


def test_symlink_escape_is_rejected(root):
    """白名单根里放一个指向外面的软链——resolve 之后必须发现并拒绝。"""
    outside = root.parent / "outside_secret.md"
    outside.write_text("外面的秘密\n", encoding="utf-8")
    link = root / "prompts" / "escape.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.read_asset("prompts/escape.md")
    assert ei.value.code in ("forbidden", "not_found")


# ── 备份 ──────────────────────────────────────────────────────

def test_write_creates_backup_with_old_content(root):
    rel = "prompts/thinking.md"
    old = asset_edit.read_asset(rel)["content"]
    res = asset_edit.write_asset(rel, "新的思考协议\n")
    assert res["ok"] and res["backup"], "写入必须留下备份路径"
    backup_file = root / "_backups" / res["backup"]
    assert backup_file.is_file(), "备份文件不在预期位置"
    assert backup_file.read_text(encoding="utf-8") == old, "备份的不是改前的内容"
    assert asset_edit.read_asset(rel)["content"] == "新的思考协议\n"


def test_backup_happens_before_validation(root):
    """**顺序铁律**：坏内容也要留备份——否则「改坏了想回滚」时发现没东西可回。"""
    rel = "scenarios/psychology.yaml"
    before = (root / rel).read_text(encoding="utf-8")
    with pytest.raises(asset_edit.AssetError):
        asset_edit.write_asset(rel, "name: [未闭合\n")   # 坏 YAML
    backups = asset_edit.list_backups(rel)
    assert backups, "校验失败也必须留下备份"
    restored = (root / "_backups" / backups[0]["path"]).read_text(encoding="utf-8")
    assert restored == before


def test_bad_yaml_is_rejected_and_disk_unchanged(root):
    rel = "scenarios/psychology.yaml"
    before = (root / rel).read_text(encoding="utf-8")
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.write_asset(rel, "name: 心理\ntype: [\n")
    assert ei.value.code == "bad_yaml"
    assert "行" in str(ei.value), f"错误信息要带行号：{ei.value}"
    assert (root / rel).read_text(encoding="utf-8") == before, "校验失败不许动磁盘"


def test_yaml_top_level_must_be_mapping(root):
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.write_asset("scenarios/psychology.yaml", "just a string\n")
    assert ei.value.code == "bad_yaml"


def test_too_large_text_is_rejected(root):
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.write_asset("prompts/thinking.md", "长" * (asset_edit.MAX_TEXT_BYTES + 10))
    assert ei.value.code == "too_large"


def test_empty_markdown_is_allowed_with_warning(root):
    """清空 md 是合法操作（相当于临时停用），但要提醒。"""
    res = asset_edit.write_asset("prompts/thinking.md", "")
    assert res["ok"] and res["warnings"], "清空应当只是警告，不是拒绝"


# ── 恢复与默认值 ──────────────────────────────────────────────

def test_restore_from_backup(root):
    rel = "prompts/thinking.md"
    old = asset_edit.read_asset(rel)["content"]
    res = asset_edit.write_asset(rel, "改过的内容\n")
    asset_edit.restore_asset(rel, res["backup"])
    assert asset_edit.read_asset(rel)["content"] == old


def test_restore_itself_creates_a_backup(root):
    """恢复也要可撤销——恢复错了还能回来。"""
    rel = "prompts/thinking.md"
    res = asset_edit.write_asset(rel, "被恢复掉的内容\n")
    n_before = len(asset_edit.list_backups(rel))
    asset_edit.restore_asset(rel, res["backup"])
    assert len(asset_edit.list_backups(rel)) > n_before


def test_reset_only_for_prompts(root):
    """`reset` 只对协议文本有意义（默认值在代码里）；其他资产不许假装能重置。"""
    out = asset_edit.reset_asset("prompts/thinking.md")
    assert out["deleted"] is True
    assert not (root / "prompts" / "thinking.md").exists()
    assert out["apply_hint"]

    for rel in ("role_card.md", "scenarios/psychology.yaml"):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.reset_asset(rel)
        assert ei.value.code == "not_resettable"
        assert (root / rel).is_file(), "拒绝重置时不许删除原文件"


def test_reset_backs_up_before_delete(root):
    rel = "prompts/thinking.md"
    old = asset_edit.read_asset(rel)["content"]
    asset_edit.reset_asset(rel)
    backups = asset_edit.list_backups(rel)
    assert backups
    assert (root / "_backups" / backups[0]["path"]).read_text(encoding="utf-8") == old


# ── 清单与元信息 ──────────────────────────────────────────────

def test_list_assets_has_all_whitelisted_roots(root):
    out = asset_edit.list_assets()
    keys = [spec["key"] for g in out["groups"] for spec in g["items"]]
    assert set(keys) == {s["key"] for s in asset_edit.ASSET_SPECS}
    paths = [f["path"] for g in out["groups"] for spec in g["items"]
             for f in spec["files"]]
    assert "scenarios/evil.py" not in paths, "非允许后缀不该出现在清单里"
    assert "role_card.md" in paths
    assert "knowledge/恋爱军师/知识.md" in paths


def test_every_spec_has_a_reason_and_apply_hint(root):
    """白名单每一条都要能回答「为什么它可写」「改完怎么生效」。"""
    for spec in asset_edit.ASSET_SPECS:
        assert spec["desc"].strip(), f"{spec['key']} 没有说明"
        assert spec["group"].strip() and spec["label"].strip()
        assert spec["exts"], f"{spec['key']} 没有后缀约束（等于全都放行）"


def test_apply_hint_always_tells_how_to_take_effect(root):
    """存完不等于生效——每一类都要明说怎么让改动起作用。"""
    for rel in ("role_card.md", "scenarios/psychology.yaml",
                "prompts/thinking.md", "knowledge/恋爱军师/知识.md"):
        assert asset_edit.read_asset(rel)["apply_hint"].strip()


def test_main_file_cannot_be_created_via_write(root):
    """`role_card.md` 被删掉后不许通过 write 重建——那是「重置」的语义，不是「编辑」。"""
    (root / "role_card.md").unlink()
    with pytest.raises(asset_edit.AssetError) as ei:
        asset_edit.write_asset("role_card.md", "重建一份\n")
    assert ei.value.code == "not_found"


# ══════════════════════════════════════════════════════════════
# 结构管理（第 49 轮 B5）—— 新建 / 删除 篇目与目录
#
# 主人要 C：「还要能新建/删除/改结构（槽位、分类目录）」。
# 这一批比「改文字」危险得多：改坏了还有原文在编辑器里，**删掉就没了**。
# 所以重点全在「拒绝」上：
#
# | 操作 | 必须拒绝 | 为什么 |
# |---|---|---|
# | 新建篇目 | 已存在 / 上级目录不存在 / 单文件资产 | 覆盖 = 静默丢内容；凭空建目录树 = 满地空壳 |
# | 删除篇目 | 主文件（`role_card.md`） | 删了她可能起不来 |
# | 新建目录 | 名字不带数字或下划线（亲密范本）/ 平铺类（prompts、scenarios） | **目录名就是语义**：不带数字前缀的目录不进注入，界面上「加好了」而她永远读不到 |
# | 删除目录 | 非空目录 / 白名单根 | `rmtree` 一次删 23 篇，「误操作面」最大的操作 |
#
# 阳性对照：把 `create_dir` 的正则校验删掉 → `test_intimate_rejects_plain_dir_name` 红；
# 把 `delete_dir` 的非空检查删掉 → `test_nonempty_dir_is_rejected` 红；
# 把 `delete_asset` 的备份挪走 → `test_delete_backs_up_before_unlink` 红。
# （三条都进了 `变异_资产结构管理.json`。）
# ══════════════════════════════════════════════════════════════


class TestCreateFile:
    def test_creates_under_whitelisted_dir(self, root):
        out = asset_edit.create_asset("knowledge/恋爱军师/新篇.md", "新写的\n")
        assert out["ok"] and out["created"]
        assert (root / "knowledge/恋爱军师/新篇.md").read_text(encoding="utf-8") == "新写的\n"

    def test_rejects_existing_file(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_asset("knowledge/恋爱军师/知识.md", "覆盖你\n")
        assert ei.value.code == "exists"
        # 内容不能被改动
        assert (root / "knowledge/恋爱军师/知识.md").read_text(encoding="utf-8") == "军师知识\n"

    def test_rejects_missing_parent(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_asset("knowledge/恋爱军师/不存在/新篇.md", "x")
        assert ei.value.code == "not_found"

    def test_rejects_single_file_spec(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_asset("role_card.md", "重建一份\n")
        assert ei.value.code in ("not_allowed", "exists")

    def test_rejects_outside_whitelist(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_asset("agent/evil.py", "print(1)\n")
        assert ei.value.code == "forbidden"
        assert not (root / "agent/evil.py").exists()

    def test_rejects_wrong_extension(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_asset("prompts/evil.py", "print(1)\n")
        assert ei.value.code == "bad_ext"


class TestDeleteFile:
    def test_backs_up_before_unlink(self, root):
        rel = "knowledge/恋爱军师/知识.md"
        asset_edit.delete_asset(rel)
        assert not (root / rel).exists()
        backups = asset_edit.list_backups(rel)
        assert backups, "删除必须留备份，否则删错就没了"
        assert (root / "_backups" / backups[0]["path"]).read_text(encoding="utf-8") == "军师知识\n"

    def test_deleted_file_can_be_restored(self, root):
        rel = "knowledge/恋爱军师/知识.md"
        asset_edit.delete_asset(rel)
        backups = asset_edit.list_backups(rel)
        asset_edit.restore_asset(rel, backups[0]["path"])
        assert (root / rel).read_text(encoding="utf-8") == "军师知识\n"

    def test_rejects_missing(self, root):
        # 用**允许删除**的那一类测「不存在」：prompts 现在会先被
        # 「这一类不允许删除」挡下（not_allowed），测不到 not_found 这条路径。
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_asset("knowledge/恋爱军师/nope.md")
        assert ei.value.code == "not_found"

    def test_main_file_cannot_be_deleted(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_asset("role_card.md")
        assert ei.value.code == "not_allowed"
        assert (root / "role_card.md").exists()


class TestCreateDir:
    def test_slot_dir_in_intimate(self, root):
        out = asset_edit.create_dir("knowledge/色色参考/30-新槽")
        assert out["kind"] == "slot"
        assert out["in_injection"] is True
        assert (root / "knowledge/色色参考/30-新槽").is_dir()
        # 新槽位没有单独字数预算，必须提醒
        assert any("预算" in w for w in out["warnings"])

    def test_archive_dir_in_intimate(self, root):
        out = asset_edit.create_dir("knowledge/色色参考/_草稿")
        assert out["kind"] == "archive"
        assert out["in_injection"] is False
        assert any("不会被注入" in w for w in out["warnings"]), out["warnings"]

    def test_category_dir_in_junshi(self, root):
        out = asset_edit.create_dir("knowledge/恋爱军师/方法")
        assert out["kind"] == "category"
        assert (root / "knowledge/恋爱军师/方法").is_dir()

    def test_intimate_rejects_plain_dir_name(self, root):
        """「新目录」不带数字也不带下划线 —— 她会读不到，必须当场拒绝。"""
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_dir("knowledge/色色参考/新目录")
        assert ei.value.code == "bad_name"
        assert not (root / "knowledge/色色参考/新目录").exists()

    def test_flat_spec_cannot_mkdir(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_dir("prompts/子目录")
        assert ei.value.code == "not_allowed"

    def test_existing_dir_rejected(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_dir("knowledge/恋爱军师")
        assert ei.value.code == "exists"

    def test_missing_parent_rejected(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_dir("knowledge/恋爱军师/不存在/子")
        assert ei.value.code == "not_found"

    def test_outside_whitelist_rejected(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.create_dir("agent/hack")
        assert ei.value.code == "forbidden"


class TestDeleteDir:
    def test_empty_dir_is_removed(self, root):
        (root / "knowledge/色色参考/40-空槽").mkdir()
        out = asset_edit.delete_dir("knowledge/色色参考/40-空槽")
        assert out["deleted"] is True
        assert not (root / "knowledge/色色参考/40-空槽").exists()

    def test_nonempty_dir_is_rejected(self, root):
        (root / "knowledge/色色参考/_草稿").mkdir()
        (root / "knowledge/色色参考/_草稿/一.md").write_text("x", encoding="utf-8")
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_dir("knowledge/色色参考/_草稿")
        assert ei.value.code == "not_empty"
        assert (root / "knowledge/色色参考/_草稿/一.md").exists(), "绝不能递归删除"

    def test_root_cannot_be_deleted(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_dir("knowledge")
        assert ei.value.code == "not_allowed"

    def test_missing_dir_rejected(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_dir("knowledge/色色参考/99-没建过")
        assert ei.value.code == "not_found"


class TestDirListing:
    def test_empty_slot_dir_is_listed(self, root):
        """空槽位必须列出来——否则刚建的目录界面上就消失了。"""
        (root / "knowledge/色色参考/30-新槽").mkdir()
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"]
                if s["key"] == "knowledge_scenario"][0]
        paths = [d["path"] for d in spec["dirs"]]
        assert "knowledge/色色参考/30-新槽" in paths
        d = [x for x in spec["dirs"] if x["path"].endswith("30-新槽")][0]
        assert d["empty"] is True
        assert d["in_injection"] is True

    def test_injection_semantics_are_reported(self, root):
        (root / "knowledge/色色参考/10-风格").mkdir()
        (root / "knowledge/色色参考/_原文").mkdir()
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"]
                if s["key"] == "knowledge_scenario"][0]
        by_name = {d["name"]: d for d in spec["dirs"]}
        assert by_name["10-风格"]["in_injection"] is True
        assert by_name["_原文"]["in_injection"] is False
        assert by_name["_原文"]["kind"] == "archive"

    def test_flat_specs_report_no_mkdir(self, root):
        out = asset_edit.list_assets()
        for g in out["groups"]:
            for s in g["items"]:
                if not s["can_mkdir"]:
                    assert s["dirs"] == []
                if s["can_mkdir"]:
                    assert s["dir_rule"], f"{s['key']} 能建目录却没有命名规则说明"


# ══════════════════════════════════════════════════════════════
# 知识分层 + 场景卡片（2026-10-26 返工，主人验收第 1、4 条）
#
# 第 4 条的批评是范式错：界面按「恋爱军师 / 色色参考」这种**内容名**分组，
# 还把 44 篇原文当成了「她读的东西」摊出来——而她真正读到的是蒸馏后的两条协议
# 与数字前缀槽位。改法是按**注入方式**分组，文案里不出现具体内容名。
# ══════════════════════════════════════════════════════════════

class TestKnowledgeLayering:
    def test_injection_values_are_known(self):
        """`injection` 只有四种取值——多一个就是没人认识的标注。

        `scenario` 是 2026-10-26 补的：主人纠正「这一包是在我的亲密模式下才触发」，
        它既不是 forced（每轮都在）也不是 retrieval（查了才有），必须单独一类。
        """
        for spec in asset_edit.ASSET_SPECS:
            assert spec.get("injection") in (
                "forced", "scenario", "retrieval", "none"), spec["key"]

    def test_every_group_has_desc_or_is_simple(self):
        """「必要背景知识」必须带整体说明——否则第三种进入方式（蒸馏）看起来是漏的。"""
        out = asset_edit.list_assets()
        groups = {g["group"]: g for g in out["groups"]}
        assert groups["必要背景知识"]["group_desc"], "缺整体说明"
        assert "蒸馏" in groups["必要背景知识"]["group_desc"]

    def test_exclude_does_not_swallow_own_root(self):
        """`exclude` 不能把 spec 自己的 root 排除掉——那会让它的文件一个都认不出来。"""
        for spec in asset_edit.ASSET_SPECS:
            for ex in spec.get("exclude", ()):
                assert not (spec["root"] == ex or spec["root"].startswith(ex + "/")), (
                    f"{spec['key']} 的 exclude 覆盖了自己的 root")

    def test_pinned_pack_files_are_not_listed_twice(self):
        """常驻直投的文件不能再出现在「检索命中」里——同一批文件两个归属，
        界面上会出现两份，改一处另一处显示不同步。"""
        out = asset_edit.list_assets()
        by_key = {s["key"]: s for g in out["groups"] for s in g["items"]}
        pinned = {f["path"] for f in by_key["knowledge_scenario"]["files"]}
        retrieval = {f["path"] for f in by_key["knowledge_retrieval"]["files"]}
        assert not (pinned & retrieval), pinned & retrieval

    def test_spec_for_assigns_intimate_paths_to_pinned(self):
        """`knowledge/色色参考/x.md` 必须归「常驻直投」，不能落到「检索命中」。"""
        spec = asset_edit._spec_for("knowledge/色色参考/00-核心/写作铁律.md")
        assert spec["key"] == "knowledge_scenario"

    def test_spec_for_assigns_other_knowledge_to_retrieval(self):
        spec = asset_edit._spec_for("knowledge/恋爱军师/01-证据分级与内容边界.md")
        assert spec["key"] == "knowledge_retrieval"

    def test_ui_copy_has_no_content_specific_names(self):
        """界面文案（分组 / 名称 / 说明）里**不许出现具体内容名**。

        这是主人第 4 条的原话：「用『狗头军师』太具体了，应该把这部分说成
        必要背景知识……她未必全是色色内容」。文案一旦写死内容名，
        换一套语料就要改代码，通用范式就成了假的。
        """
        specs_by_group: dict[str, list[str]] = {}
        for spec in asset_edit.ASSET_SPECS:
            specs_by_group.setdefault(spec["group"], []).append(spec["root"])
        for spec in asset_edit.ASSET_SPECS:
            tail = spec["root"].rstrip("/").split("/")[-1]
            if not tail.endswith((".md", ".yaml", ".txt")):
                # 目录型 root 的末段就是内容名（色色参考 / 恋爱军师 / prompts）
                if tail in ("prompts", "scenarios", "knowledge"):
                    continue
                for field in ("label", "group", "desc"):
                    assert tail not in spec[field], (
                        f"{spec['key']} 的 {field} 里出现了具体内容名「{tail}」；"
                        "文案要通用，内容名只放 root")


class TestScenarioCards:
    def test_cards_carry_display_and_description(self, root):
        (root / "scenarios").mkdir(exist_ok=True)
        (root / "scenarios/card.yaml").write_text(
            "name: demo\ntype: replace\ndisplay: \"🧪 测试卡\"\n"
            "description: \"这张卡在测试里\"\n", encoding="utf-8")
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"]
                if s["key"] == "scenario"][0]
        card = spec["cards"][0]
        # 只给文件名的话，使用者看到的是 card.yaml，而她在群里挂的是「🧪 测试卡」
        assert card["display"] == "🧪 测试卡"
        assert card["description"] == "这张卡在测试里"
        assert card["type"] == "replace"
        assert card["name"] == "demo"
        assert card["path"] == "scenarios/card.yaml"

    def test_broken_yaml_card_is_still_listed(self, root):
        """yaml 写坏的那张卡**更**必须显示出来——那正是要改的那一张。"""
        (root / "scenarios").mkdir(exist_ok=True)
        (root / "scenarios/bad.yaml").write_text(
            "name: [unclosed\n", encoding="utf-8")
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"]
                if s["key"] == "scenario"][0]
        cards = [c for c in spec["cards"] if c["path"] == "scenarios/bad.yaml"]
        assert cards, "解析失败的卡片消失了"
        assert cards[0]["warning"], "解析失败必须带 warning"
        assert cards[0]["display"] == "bad", "display 要回落到文件名，不能空"

    def test_card_missing_display_falls_back_to_name(self, root):
        (root / "scenarios").mkdir(exist_ok=True)
        (root / "scenarios/min.yaml").write_text(
            "name: minimal\n", encoding="utf-8")
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"]
                if s["key"] == "scenario"][0]
        card = [c for c in spec["cards"] if c["path"] == "scenarios/min.yaml"][0]
        assert card["display"] == "minimal"
        assert card["type"] == "overlay"


# ══════════════════════════════════════════════════════════════
# 身份卡不止一张（2026-10-26 第二轮验收第 1 条）
#
# 主人：「角色卡共有三张，你漏了 role_card_murasame.md、role_card_michele.md」。
# 根因是白名单里写死了 `role_card.md` 一个路径——glob 型资产就是为这件事加的。
# ══════════════════════════════════════════════════════════════

class TestRoleCards:
    def _make_extra(self, root):
        (root / "role_card_murasame.md").write_text(
            "# 丛雨（ムラサメ）\n\n## 你是谁\n\n刀魂少女\n", encoding="utf-8")
        (root / "role_card_michele.md").write_text(
            "# 米雪儿·李\n\n## 你是谁\n\n搜查官\n", encoding="utf-8")

    def test_all_role_cards_are_listed(self, root):
        self._make_extra(root)
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"] if s["key"] == "roles"][0]
        names = sorted(f["path"] for f in spec["files"])
        assert names == ["role_card.md", "role_card_michele.md", "role_card_murasame.md"]

    def test_role_label_comes_from_first_heading(self, root):
        """卡片名从文件首行读，**不维护一张「文件名 → 人名」的映射表**。

        映射表换一张卡就要改代码，改名之后界面还显示旧名字。
        """
        self._make_extra(root)
        out = asset_edit.list_assets()
        spec = [s for g in out["groups"] for s in g["items"] if s["key"] == "roles"][0]
        by_path = {f["path"]: f for f in spec["files"]}
        assert by_path["role_card.md"]["label"] == "小糖糖"
        assert by_path["role_card_murasame.md"]["label"] == "丛雨（ムラサメ）"
        assert by_path["role_card_michele.md"]["label"] == "米雪儿·李"
        assert by_path["role_card.md"]["is_default"] is True
        assert by_path["role_card_michele.md"]["is_default"] is False

    def test_extra_role_cards_are_editable(self, root):
        self._make_extra(root)
        content = asset_edit.read_asset("role_card_murasame.md")["content"]
        assert "刀魂少女" in content

    def test_role_card_cannot_be_deleted(self, root):
        """API 层必须和界面层同一个判据：界面不给删，API 也不能删。"""
        self._make_extra(root)
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.delete_asset("role_card_murasame.md")
        assert ei.value.code == "not_allowed"
        assert (root / "role_card_murasame.md").exists()

    def test_write_does_not_create_new_role_card(self, root):
        """保存 ≠ 新建：写一个不存在的角色卡必须拒绝（新建走 create_asset）。"""
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.write_asset("role_card_new.md", "# 新卡\n")
        assert ei.value.code == "not_found"
        assert not (root / "role_card_new.md").exists()

    def test_role_card_can_be_created_via_create_asset(self, root):
        out = asset_edit.create_asset("role_card_new.md", "")
        assert out["ok"] is True
        text = (root / "role_card_new.md").read_text(encoding="utf-8")
        # 骨架要提醒「只写身份不加规则」——那是 role_card 的唯一约束
        assert "# role_card_new" in text
        assert "只写身份" in text


# ══════════════════════════════════════════════════════════════
# 场景触发通道的语义不能回退（2026-10-26 主人纠正）
#
# 我第一版把 `knowledge/色色参考` 这一包写成「常驻直投 / 每轮都在」，
# 主人当场纠正：「这会在我的『亲密模式』下才触发」。
# 代码依据：agent/handler.py:5502-5509 —— `private_scenario.name == "seductive"`
# 且 `_is_sed_active(user_id)` 才 append 进 pinned；群聊永不挂载。
#
# 这类错误最坏的地方不是文案不准，而是**让人以为改这里会影响每句话**。
# ══════════════════════════════════════════════════════════════

def test_scenario_pack_is_not_described_as_always_on():
    """场景触发的一包，desc 里不许出现「每轮都在 / 常驻」这类说法。"""
    out = asset_edit.list_assets()
    spec = [s for g in out["groups"] for s in g["items"] if s["key"] == "knowledge_scenario"][0]
    assert spec["injection"] == "scenario", spec["injection"]
    for bad in ("每轮都在", "常驻"):
        assert bad not in spec["desc"], (bad, spec["desc"])
    # 必须说清「什么时候才在」，否则界面等于什么都没说
    assert "激活" in spec["desc"], spec["desc"]


def test_every_spec_declares_an_injection_mode():
    """每一类资产都必须回答「她什么时候用得上」——空着等于没回答。"""
    out = asset_edit.list_assets()
    for g in out["groups"]:
        for spec in g["items"]:
            assert spec["injection"] in ("forced", "scenario", "retrieval", "none"), spec["key"]
            assert spec.get("triggered_by") is not None, spec["key"]


def test_group_desc_says_scenario_not_always_on():
    desc = asset_edit.GROUP_DESC["必要背景知识"]
    assert "场景触发" in desc, desc
    assert "每轮都在" in desc, "①蒸馏进提示词那一路是每轮都在，这句要保留"
    # 「每轮都在」只能用于第一种进入方式，不能挂在场景触发上
    assert desc.index("每轮都在") < desc.index("场景触发"), desc


class TestScenarioRenameWiring:
    """**接线**测试：`sync_rename` 单测绿不等于它被调用了（教训 #14）。

    `agent/scenario.py:138-150` 用 display 名匹配场景，而 `scenario_targets`
    存的就是那个名字——所以改 display 不同步挂载点 = 场景静默失效。
    """

    def _config(self, root, body: str):
        (root / "config.yaml").write_text(body, encoding="utf-8")

    def test_rename_display_syncs_mounts(self, root):
        self._config(root, "scenario_targets:\n  '10001': 心理陪伴\n")
        out = asset_edit.write_asset(
            "scenarios/psychology.yaml",
            "name: psychology\ntype: overlay\ndisplay: 深度陪伴\ndescription: 通用\n",
        )
        assert out["renamed_mounts"] == ["10001"], out
        text = (root / "config.yaml").read_text(encoding="utf-8")
        assert "'10001': 深度陪伴" in text

    def test_unchanged_display_does_not_touch_config(self, root):
        self._config(root, "scenario_targets:\n  '1': 心理陪伴\n")
        out = asset_edit.write_asset(
            "scenarios/psychology.yaml",
            "name: psychology\ntype: overlay\ndisplay: 心理陪伴\ndescription: 改的是描述\n",
        )
        assert out["renamed_mounts"] == []
        assert (root / "config.yaml").read_text(encoding="utf-8").count("心理陪伴") == 1

    def test_non_scenario_asset_never_touches_config(self, root):
        """改协议/知识时绝不能顺手改配置——那是另一个资产的事。"""
        self._config(root, "scenario_targets:\n  '1': 心理陪伴\n")
        before = (root / "config.yaml").read_text(encoding="utf-8")
        asset_edit.write_asset("prompts/thinking.md", "新的思考协议\n")
        assert (root / "config.yaml").read_text(encoding="utf-8") == before


class TestRenameAsset:
    """换文件名（主人 2026-10-26 第四条要求支持）。

    查证后的关键结论：`agent/scenario.py:94-96` **强制要求 yaml 有显式 `name:`
    字段**，文件名不参与任何匹配。所以改名只是文件操作——不用动
    `scenario_targets`（按 display）也不用动 `scenario_reference.trigger_scenario`
    （按 name）。这个结论写进测试，防止以后有人「顺手」把 name 改成从 stem 推导。
    """

    def _config(self, root, body: str):
        (root / "config.yaml").write_text(body, encoding="utf-8")

    def test_rename_scenario_keeps_config_untouched(self, root):
        self._config(root, "scenario_targets:\n  '1': 心理陪伴\n")
        before = (root / "config.yaml").read_text(encoding="utf-8")
        out = asset_edit.rename_asset("scenarios/psychology.yaml", "陪伴")
        assert out["new_path"] == "scenarios/陪伴.yaml"
        assert (root / "scenarios" / "陪伴.yaml").is_file()
        assert not (root / "scenarios" / "psychology.yaml").exists()
        # 配置一动不动：name/display 都在内容里，文件名跟它们无关
        assert (root / "config.yaml").read_text(encoding="utf-8") == before
        assert "不受影响" in out["note"]

    def test_rename_backs_up_original(self, root):
        out = asset_edit.rename_asset("scenarios/psychology.yaml", "陪伴")
        assert out["backup"]
        backed = asset_edit.BACKUP_ROOT / out["backup"]
        assert backed.is_file(), "改名前的原件必须留一份"

    def test_rename_rejects_path_separator(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_asset("scenarios/psychology.yaml", "sub/新名")
        assert ei.value.code == "forbidden"

    def test_rename_rejects_extension_change(self, root):
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_asset("scenarios/psychology.yaml", "陪伴.md")
        assert ei.value.code == "bad_extension"

    def test_rename_appends_original_extension(self, root):
        out = asset_edit.rename_asset("scenarios/psychology.yaml", "陪伴")
        assert out["new_path"].endswith(".yaml")

    def test_rename_rejects_existing_target(self, root):
        (root / "scenarios" / "已有.yaml").write_text("name: x\n", encoding="utf-8")
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_asset("scenarios/psychology.yaml", "已有")
        assert ei.value.code == "exists"
        # 原文件还在（不能因为目标冲突就丢东西）
        assert (root / "scenarios" / "psychology.yaml").is_file()

    def test_rename_to_same_name_is_noop(self, root):
        out = asset_edit.rename_asset("scenarios/psychology.yaml", "psychology.yaml")
        assert out.get("unchanged") is True

    def test_rename_knowledge_file(self, root):
        out = asset_edit.rename_asset("knowledge/恋爱军师/知识.md", "军师笔记")
        assert out["new_path"] == "knowledge/恋爱军师/军师笔记.md"


def test_rename_entry_is_double_click_not_a_button():
    """改名入口是**双击文件名**，不是按钮（主人 2026-10-26）。

    为什么值得一条闸门：按钮和双击是两个入口，加回按钮就会变成两个，
    而「双击改名」这个交互没人会在测试里主动验（前端没有 e2e）。
    """
    from pathlib import Path
    src = (Path(__file__).parent.parent / "desktop" / "src" / "pages" / "Assets.tsx") \
        .read_text(encoding="utf-8")
    assert "onDoubleClick" in src, "双击改名没了"
    assert "beginRename" in src, "改名辅助函数没了"
    # 不该再有那个写死的「名」按钮
    assert ">\n          名\n        </button>" not in src, "「名」按钮又回来了"


class TestRenameDir:
    """槽位目录改名（主人 2026-10-26：「00-核心、10-风格，编号后的文字也需要可以改名」）。"""

    def _slot(self, root, name="00-核心"):
        d = root / "knowledge" / "恋爱军师" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.md").write_text("内容\n", encoding="utf-8")
        return d

    def test_rename_keeps_numeric_prefix(self, root):
        self._slot(root)
        out = asset_edit.rename_dir("knowledge/恋爱军师/00-核心", "开场")
        assert out["new_path"] == "knowledge/恋爱军师/00-开场"
        assert (root / "knowledge/恋爱军师/00-开场/a.md").is_file()

    def test_accepts_full_name_with_same_prefix(self, root):
        self._slot(root)
        out = asset_edit.rename_dir("knowledge/恋爱军师/00-核心", "00-起手")
        assert out["new_path"] == "knowledge/恋爱军师/00-起手"

    def test_prefix_change_is_rejected(self, root):
        """数字前缀 = 槽位序号（预算按名字对），换它等于换槽位。"""
        self._slot(root)
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_dir("knowledge/恋爱军师/00-核心", "10-核心")
        assert ei.value.code == "forbidden"

    def test_archive_dir_cannot_be_renamed(self, root):
        """下划线前缀 = 归档语义，改名会把它变成槽位（语义漂移）。"""
        (root / "knowledge/恋爱军师/_草稿").mkdir(parents=True)
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_dir("knowledge/恋爱军师/_草稿", "草稿")
        assert ei.value.code == "not_allowed"

    def test_conflict_is_rejected(self, root):
        self._slot(root)
        self._slot(root, "00-已有")
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_dir("knowledge/恋爱军师/00-核心", "已有")
        assert ei.value.code == "exists"

    def test_empty_tail_is_rejected(self, root):
        self._slot(root)
        with pytest.raises(asset_edit.AssetError) as ei:
            asset_edit.rename_dir("knowledge/恋爱军师/00-核心", "00-")
        assert ei.value.code == "bad_name"
