# -*- coding: utf-8 -*-
"""协议分组（2026-10-26 二次修正）—— 组级属性从条目文件头收敛到 `prompts/_groups.yaml`。

## 修的是什么

主人原话：「我的协议条目好像有点乱，具体是怎么理解和怎么回这两部分有一些乱」。
拆开来是三个连在一起的症状：

1. **改 `group` → 条目跑了**。文件头的 `group: understand` 被改成 `reply`，
   条目就挪到「怎么回」组里去了（后端按 group 排序）。主人在备份里留下的
   现场是 `comprehension.md` 在 20:35–20:42 之间被 understand/reply 来回改了
   四次——那不是他手抖，是他找不到「怎么让这条换个组」。

2. **改 `group_title` → 不生效**。`list_overrides()` 用 `setdefault` 收集，
   同组排在前面的文件先占位，后面几条改了静默忽略。用户不知道该改哪一条。

3. **两个组合并**。前端按**显示标题文字**切组（`title !== cur`），
   所以两个不同 key 只要最终标题相同就渲染成一组；而同一个组若被排到
   不相邻的位置又会被切成两段。

三者同一个根因：**组是组级的东西（顺序/名字/说明），却被复制成条目级副本**。
N 份副本 + 隐式优先级 = 「改哪个生效」不可知。

## 现在的分工

| 是什么 | 住在哪 |
|---|---|
| 组的顺序、显示名、一句话说明 | `prompts/_groups.yaml`（唯一真相） |
| 这一条归哪个组 | 条目文件头 `group: <key>`（只做引用） |
| 这一条的说明、来源 | 条目文件头 `desc` / `origin` |
| 默认值 | `PROMPT_GROUPS` / `PROMPT_META` / `PROMPT_ORIGIN`（文件没写才用） |

⚠ `origin` 也是这次修的：之前 `list_overrides()` 只读代码表，文件里
`origin: 知识` 是死数据（主人报「标签 origin 更改保存后不生效」）。
"""
import pytest

from agent import prompt_overrides as po
from tools import asset_edit as ae


# ── 组表的自洽性 ──────────────────────────────────────────────

def test_groups_are_file_backed():
    """组表必须是**文件**说了算，不能回落到代码默认。

    回落本身是好事（文件被删了界面还能用），但如果生产环境一直在回落，
    就说明「在文件里改组标题」这条路根本没通——那是静默失效。
    """
    assert po.GROUPS_PATH.is_file(), f"{po.GROUPS_PATH} 不存在——组标题只能改代码了"
    groups = po.read_groups()
    assert len(groups) >= 6, [g["key"] for g in groups]
    assert all(g["source"] == "file" for g in groups), "有组没从文件读到"
    assert all(g["title"].strip() for g in groups), "有组的标题是空的"
    assert all(g["desc"].strip() for g in groups), "有组没有一句话说明"


def test_every_file_points_to_a_known_group():
    """每条协议的 `group` 必须是组表里存在的 key。

    拼错一个字母（`replay` ← `reply`）会静默落到「未分组」；界面现在会标红，
    但**根本不该让它进仓库**——这条闸门盯住生产配置。
    """
    known = {g["key"] for g in po.read_groups()}
    bad = [r["name"] for r in po.list_overrides() if not r["group_known"]]
    assert not bad, f"这些协议的 group 不在组表里：{bad}"
    assert known, "组表是空的"


def test_each_group_is_one_contiguous_block():
    """同一组必须是**一段连续**的行，且段的顺序就是组表顺序。

    这是「位置迁移」和「同组被拆两段」的回归闸门：排序点只有一个
    （`list_overrides()`），它排错了界面就会把一组拆成两处。
    """
    rows = po.list_overrides()
    blocks: list[str] = []
    for r in rows:
        if not blocks or blocks[-1] != r["group"]:
            blocks.append(r["group"])
    assert len(blocks) == len(set(blocks)), f"同一组被拆成了多段：{blocks}"

    order = [g["key"] for g in po.read_groups()]
    known = [k for k in blocks if k in order]
    assert known == [k for k in order if k in known], (known, order)


def test_read_groups_falls_back_when_file_missing(tmp_path, monkeypatch):
    """组表不在时回落代码默认——少一个文件不能让界面上一个分组都没有。

    回落是**降级**，不是常态（常态必须走文件，见 `test_groups_are_file_backed`）。
    """
    monkeypatch.setattr(po, "GROUPS_PATH", tmp_path / "_groups.yaml")
    groups = po.read_groups()
    assert len(groups) == len(po.PROMPT_GROUPS)
    assert all(g["source"] == "code" for g in groups)


def test_read_groups_survives_broken_yaml(tmp_path, monkeypatch):
    """YAML 写坏了 → 回落代码默认，**绝对不能抛**。

    这个函数跑在她的启动路径上：一个手误的缩进不能让她发不出消息。
    """
    p = tmp_path / "_groups.yaml"
    p.write_text("- key: [unclosed\n  title: x\n", encoding="utf-8")
    monkeypatch.setattr(po, "GROUPS_PATH", p)
    groups = po.read_groups()
    assert len(groups) == len(po.PROMPT_GROUPS)
    assert all(g["source"] == "code" for g in groups)


def test_all_rows_carry_the_group_facts():
    """每条都要带上组级事实（标题/说明/是否已知），界面不自己推。"""
    rows = po.list_overrides()
    assert rows
    for r in rows:
        assert r["group_title"].strip(), r["name"]
        for key in ("group", "group_desc", "group_known", "stale_group_title"):
            assert key in r, f"{r['name']} 缺字段 {key}"


# ── 组标题只有一份（不再从文件头读）──────────────────────────

def test_group_title_in_file_header_is_ignored(tmp_path, monkeypatch):
    """文件头的 `group_title` 不再生效——组标题只有组表一份。

    只「静默忽略」是不够的：`list_overrides()` 必须把 `stale_group_title`
    报出来，否则用户在那个文件头里改了标题、保存、界面没变——
    又是一次「改了没反应」。
    """
    monkeypatch.setattr(po, "PROMPT_DIR", tmp_path)
    monkeypatch.setattr(po, "GROUPS_PATH", tmp_path / "_groups.yaml")  # 不存在 → 回落代码表
    (tmp_path / "thinking.md").write_text(
        "---\ngroup: understand\ngroup_title: 伪造标题\ndesc: x\n---\n\n正文\n",
        encoding="utf-8")
    row = [r for r in po.list_overrides() if r["name"] == "thinking"][0]
    assert row["group_title"] == "怎么理解对方", row["group_title"]
    assert row["stale_group_title"] is True


def test_unknown_group_key_is_flagged(tmp_path, monkeypatch):
    """拼错的 group 落「未分组」+ `group_known=False`（界面标红），不静默吞掉。"""
    monkeypatch.setattr(po, "PROMPT_DIR", tmp_path)
    monkeypatch.setattr(po, "GROUPS_PATH", tmp_path / "_groups.yaml")
    (tmp_path / "thinking.md").write_text(
        "---\ngroup: replay\ndesc: x\n---\n\n正文\n", encoding="utf-8")
    row = [r for r in po.list_overrides() if r["name"] == "thinking"][0]
    assert row["group"] == "replay"
    assert row["group_known"] is False
    assert row["group_title"] == "未分组"


# ── 改标题（桌面端双击组头走这条）────────────────────────────

@pytest.fixture()
def group_env(tmp_path, monkeypatch):
    """把组表和备份目录都搬进 tmp —— 测完不污染真实的 `prompts/_groups.yaml`。"""
    groups_file = tmp_path / "prompts" / "_groups.yaml"
    groups_file.parent.mkdir(parents=True)
    groups_file.write_text(po.render_groups_yaml(), encoding="utf-8")
    backups = tmp_path / "_backups"
    monkeypatch.setattr(po, "GROUPS_PATH", groups_file)
    monkeypatch.setattr(ae, "_PROMPT_GROUPS_PATH", groups_file)
    monkeypatch.setattr(ae, "BACKUP_ROOT", backups)
    return groups_file, backups


def test_save_group_title_lands_backs_up_and_keeps_comments(group_env):
    """改标题：落盘、可回读、先备份、**头部说明注释不能被洗掉**。

    用 `yaml.safe_dump` 整份重写会把这个文件的注释和顺序感一起洗掉
    （`config.yaml` 上踩过两次，教训 #7），所以这里是文本级替换，
    只动目标那一块的 `title:` 行。
    """
    groups_file, backups = group_env
    before = groups_file.read_text(encoding="utf-8")
    res = ae.save_prompt_group_title("memory", "记不清的时候")
    assert res["ok"] is True
    assert res["title"] == "记不清的时候"

    # 只认读回来的文件，不信返回值（教训 #53）
    after = groups_file.read_text(encoding="utf-8")
    assert "记不清的时候" in after
    assert "# 协议分组" in after, "头部说明被洗掉了"
    assert "`key` 是**结构**" in after, "头部说明被洗掉了"
    assert after.count("key:") == before.count("key:"), "组的条数变了"

    # 备份是改前版本
    assert res["backup"], "没有备份"
    assert (backups / res["backup"]).read_text(encoding="utf-8") == before

    got = [g["title"] for g in po.read_groups() if g["key"] == "memory"]
    assert got == ["记不清的时候"]


@pytest.mark.parametrize("key,title,code", [
    ("nope", "x", "not_found"),
    ("memory", "", "bad_name"),
    ("memory", "   ", "bad_name"),
    ("memory", "a\nb", "bad_name"),
    ("memory", "x" * 41, "bad_name"),
])
def test_save_group_title_rejects_bad_input(group_env, key, title, code):
    """坏输入一律拒绝，且**文件一个字节都没动**。"""
    groups_file, _ = group_env
    before = groups_file.read_text(encoding="utf-8")
    with pytest.raises(ae.AssetError) as ei:
        ae.save_prompt_group_title(key, title)
    assert ei.value.code == code
    assert groups_file.read_text(encoding="utf-8") == before


def test_replace_group_title_missing_key_returns_empty():
    """找不到 key 时返回空串——调用方必须报错，绝不静默写入。"""
    assert po.replace_group_title(po.render_groups_yaml(), "nope", "x") == ""


def test_replace_group_title_quotes_special_chars():
    """标题里有 `#` / `:` / 前导 `-` 时必须被引号包住，否则语义被 YAML 吃掉。"""
    tricky = "怎么回 # 说人话"
    text = po.replace_group_title(po.render_groups_yaml(), "reply", tricky)
    assert text
    import yaml
    parsed = yaml.safe_load(text.split("---\n", 1)[1])
    assert [g for g in parsed if g["key"] == "reply"][0]["title"] == tricky


def test_prompt_groups_bridge_shape(group_env):
    """`prompt_groups()` 给界面的形状：每组带条目数，拼错的单独计数。"""
    out = ae.prompt_groups()
    assert out["ok"] and out["exists"]
    assert [g["key"] for g in out["groups"]] == [g["key"] for g in po.read_groups()]
    assert all(isinstance(g["files"], int) for g in out["groups"])
    assert out["unknown_files"] == 0


# ── 前端不许按标题切组（源码闸门）────────────────────────────

def test_frontend_splits_groups_by_key_not_by_title():
    """`renderPromptTree` 必须按 `group` **key** 切，不能按显示标题切。

    按标题切是「两个组合并」的成因：两个不同 key 撞了同名标题就渲染成一整组。
    后端已经保证 key 是对的，前端再按文字切一次等于把后端的分组扔了。
    """
    src = (po.PROJECT_ROOT / "desktop/src/pages/Assets.tsx").read_text(encoding="utf-8")
    start = src.index("const renderPromptTree")
    body = src[start:start + 5000]
    assert "if (key !== cur)" in body, "renderPromptTree 没有按 group key 切组"
    assert "title !== cur" not in body, "renderPromptTree 又按显示标题切组了（会合并同名组）"
    assert "onDoubleClick={() => beginRenameGroup(" in body, "组头不能双击改名"
