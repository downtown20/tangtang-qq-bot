# -*- coding: utf-8 -*-
"""协议文本覆盖层的闸门（2026-10-26 第 49 轮）。

## 这组用例防的是什么

「协议条文可改」是整个参数暴露工程里**收益最高、也最危险**的一块：
它决定她说话的方式，而改坏的后果是**静默**的（她答非所问，但界面一切正常）。

两个最危险的失效方式：

1. **把代码契约当文案放开**：`SPLIT_MARKER = "[分段]"` 被正则用来切消息。
   让人把它改成 `[分条]`，分段就永远不生效——而「保存成功」照样显示。
   所以白名单必须**只含协议条文**，且有一条测试钉住那 13 个契约常量**不在**里面。
2. **覆盖层坏了拖垮启动**：`prompts/x.md` 编码错/超长/全空白时，必须**静默回落默认值**，
   而不是让 `protocols` import 失败（那等于糖糖起不来，而你可能正在睡觉）。

## 阳性对照（教训 #30/#37：闸门必须能红）

- 把 `SPLIT_MARKER` 加进 `OVERRIDABLE` → `test_code_contract_constants_are_not_overridable` 红；
- `read_override` 不检查白名单 → `test_read_override_ignores_non_whitelisted` 红；
- 超长/坏编码不回落 → `test_broken_override_falls_back_to_default` 红；
- `apply_overrides` 无条件覆盖 → `test_apply_only_touches_whitelisted` 红。
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import prompt_overrides as po  # noqa: E402

PROTOCOLS = Path(__file__).parent.parent / "agent" / "protocols.py"

#: **代码契约**：这些常量被 `re` / `in` / `==` 用来匹配、切分、渲染结构。
#: 改掉它们不会「让文案变丑」，会**让功能静默失效**。
CONTRACT_CONSTANTS = {
    "SPLIT_MARKER", "QUOTE_MARKER", "SILENCE_MARKER", "POKE_BACK_MARKER",
    "UNCERTAIN_MARK", "CORRECTED_MARK", "MOOD_REPORT_LABEL",
    "INTIMATE_REFERENCE_LABEL", "OPEN_LOOP_LABEL",
    "QUOTE_RESOLVED", "QUOTE_MISSING", "QUOTE_NONE", "POKE_DEGRADED_REPLY",
}


def _protocol_string_consts() -> dict[str, str]:
    """从源码 AST 读 `protocols.py` 的全部字符串常量（不经 import，避免拿到覆盖后的值）。"""
    tree = ast.parse(PROTOCOLS.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        try:
            val = ast.literal_eval(node.value)
        except Exception:
            continue
        if isinstance(val, str):
            out[tgt.id] = val
    return out


@pytest.fixture()
def prompt_dir(tmp_path, monkeypatch):
    """把 `PROMPT_DIR` 指到临时目录——**绝不碰真实的 prompts/**。"""
    d = tmp_path / "prompts"
    d.mkdir()
    monkeypatch.setattr(po, "PROMPT_DIR", d)
    return d


# ── 白名单本身 ────────────────────────────────────────────────

def test_every_overridable_const_exists_and_is_string():
    """白名单不许腐烂：每条都得能在 protocols.py 里找到对应的字符串常量。"""
    consts = _protocol_string_consts()
    missing = [c for c in po.OVERRIDABLE.values() if c not in consts]
    assert not missing, f"白名单指向了不存在的常量（改名了？）：{missing}"


def test_code_contract_constants_are_not_overridable():
    """**最关键的一条**：标记/枚举常量绝对不许被「编辑文案」碰。

    它们被正则和字符串比较使用。开放覆盖 = 开放「让分段静默失效」。
    """
    leaked = CONTRACT_CONSTANTS & set(po.OVERRIDABLE.values())
    assert not leaked, (
        f"这些是代码契约（解析器靠它匹配），绝不能进覆盖白名单：{leaked}")


def test_overridable_only_contains_protocol_prose():
    """白名单里的常量必须真的是「给人读的条文」——太短的多半是标记，不该放进来。"""
    consts = _protocol_string_consts()
    too_short = [(n, len(consts[c])) for n, c in po.OVERRIDABLE.items()
                 if len(consts[c]) < 8]
    assert not too_short, f"这些短得像标记，不该开放编辑：{too_short}"


def test_overridable_names_are_unique():
    names = list(po.OVERRIDABLE)
    assert len(names) == len(set(names)), "文件名重复"
    consts = list(po.OVERRIDABLE.values())
    assert len(consts) == len(set(consts)), "同一个常量被映射了两次"


# ── 读取与回落 ────────────────────────────────────────────────

def test_read_override_returns_file_content(prompt_dir):
    (prompt_dir / "thinking.md").write_text("我的新版思考协议\n", encoding="utf-8")
    assert po.read_override("thinking") == "我的新版思考协议\n"


def test_read_override_ignores_non_whitelisted(prompt_dir):
    """**不在白名单里的 name 一律返回空**——即使文件真的存在。

    否则有人只要知道常量名，就能绕过白名单改掉 `split_marker`。
    """
    (prompt_dir / "split_marker.md").write_text("[分条]\n", encoding="utf-8")
    assert po.read_override("split_marker") == ""
    assert po.read_override("SPLIT_MARKER") == ""
    assert po.read_override("../../etc/passwd") == ""


@pytest.mark.parametrize("content", ["", "   \n\n  ", "\ufeff"])
def test_blank_override_falls_back(prompt_dir, content):
    """空白内容 = 没覆盖（不能让她拿到一段空协议）。"""
    (prompt_dir / "thinking.md").write_text(content, encoding="utf-8")
    assert po.read_override("thinking") == ""


def test_broken_override_falls_back_to_default(prompt_dir):
    """**覆盖层坏了不能拖垮启动**：超长 / 坏编码都静默回落。"""
    (prompt_dir / "thinking.md").write_bytes(b"\xff\xfe\x00bad")
    assert po.read_override("thinking") == ""

    big = "长" * (po.MAX_BYTES + 10)
    (prompt_dir / "thinking.md").write_text(big, encoding="utf-8")
    assert po.read_override("thinking") == "", "超长文本必须被忽略，不能进 system prompt"


def test_missing_file_falls_back(prompt_dir):
    assert po.read_override("thinking") == ""


# ── 应用覆盖 ──────────────────────────────────────────────────

def test_apply_overrides_replaces_only_listed_constants(prompt_dir):
    (prompt_dir / "thinking.md").write_text("新版\n", encoding="utf-8")
    mod = {"THINKING_PROTOCOL": "内置默认", "SPLIT_MARKER": "[分段]"}
    applied = po.apply_overrides(mod)
    assert mod["THINKING_PROTOCOL"] == "新版\n"
    assert mod["SPLIT_MARKER"] == "[分段]", "契约常量被误覆盖了"
    assert applied == ["THINKING_PROTOCOL"]


def test_apply_is_noop_when_content_is_identical(prompt_dir):
    """导出的 md 内容与默认值一致时**不应**报告「被覆盖」（否则启动日志天天喊）。"""
    (prompt_dir / "thinking.md").write_text("内置默认\n", encoding="utf-8")
    mod = {"THINKING_PROTOCOL": "内置默认\n"}
    assert po.apply_overrides(mod) == []
    assert mod["THINKING_PROTOCOL"] == "内置默认\n"


def test_apply_ignores_unknown_keys_in_globals(prompt_dir):
    (prompt_dir / "thinking.md").write_text("新版\n", encoding="utf-8")
    mod: dict = {}          # 常量还没定义（理论上不会发生，但别炸）
    assert po.apply_overrides(mod) == []
    assert "" not in mod.values()


def test_list_overrides_shape(prompt_dir):
    (prompt_dir / "thinking.md").write_text("新版\n", encoding="utf-8")
    rows = po.list_overrides()
    assert len(rows) == len(po.OVERRIDABLE)
    one = next(r for r in rows if r["name"] == "thinking")
    assert one["active"] is True and one["chars"] == 3
    assert one["path"] == "prompts/thinking.md"
    other = next(r for r in rows if r["name"] == "grounding")
    assert other["active"] is False


# ── 导出物与源码默认一致（真实目录）──────────────────────────

def test_exported_files_match_source_defaults():
    """`prompts/*.md` 里**未改动**的那些，其**正文**必须与 `protocols.py` 默认值一致。

    这条防的是「导出脚本写歪了」——导出的文字与运行时用的默认值不是同一份，
    改完再删文件回滚时会回到一个**不同的**默认值（那种 bug 极难发现）。
    已改过的文件（正文 ≠ 默认）跳过：那是用户的编辑成果，不该被测试判红。

    ⚠ 2026-10-26：文件多了 front-matter（协议名/分组/说明/来源，主人要求
    「和场景卡一样在文档内可改」），所以比对的是**剥离头部之后的正文**。
    直接比整份文件的话，22 条全部会被判「用户改过」，这条闸门就静默失效了。
    """
    prompts = po.PROMPT_DIR
    if not prompts.is_dir():
        pytest.skip("没有 prompts/ 目录（还没导出过）")
    consts = _protocol_string_consts()
    checked = 0
    for name, const in po.OVERRIDABLE.items():
        p = prompts / f"{name}.md"
        if not p.is_file():
            continue
        _, body = po.split_front_matter(p.read_text(encoding="utf-8"))
        if body != consts[const]:
            continue        # 用户改过的，不做一致性判断
        checked += 1
        assert body.strip(), f"{p.name} 是空的"
    assert checked >= 15, f"只核对到 {checked} 个未改动的导出文件，太少了"


def test_deleting_override_restores_default(prompt_dir):
    """回滚判据：删掉 md → 下一次 apply 不改任何东西。"""
    (prompt_dir / "thinking.md").write_text("新版\n", encoding="utf-8")
    mod = {"THINKING_PROTOCOL": "内置默认"}
    po.apply_overrides(mod)
    assert mod["THINKING_PROTOCOL"] == "新版\n"

    (prompt_dir / "thinking.md").unlink()
    mod2 = {"THINKING_PROTOCOL": "内置默认"}
    assert po.apply_overrides(mod2) == []
    assert mod2["THINKING_PROTOCOL"] == "内置默认"


# ══════════════════════════════════════════════════════════════
# 协议元数据（2026-10-26 返工，主人验收第 3 条）
#
# 22 条协议原来只有文件平铺，改之前不知道「这条管什么」。
# 现在分组与一句话作用都是**数据**（PROMPT_GROUPS / PROMPT_META），
# 界面直接渲染。这两张表和 OVERRIDABLE 必须严格对齐——多一条少一条都是 bug。
# ══════════════════════════════════════════════════════════════

def test_every_overridable_entry_has_meta():
    missing = [k for k in po.OVERRIDABLE if k not in po.PROMPT_META]
    assert not missing, f"这些协议没有分组/作用说明：{missing}"


def test_meta_has_no_stale_entries():
    extra = [k for k in po.PROMPT_META if k not in po.OVERRIDABLE]
    assert not extra, f"这些元数据指向不存在的协议：{extra}"


def test_every_meta_group_exists():
    keys = {k for k, _, _ in po.PROMPT_GROUPS}
    bad = {n: g for n, (g, _) in po.PROMPT_META.items() if g not in keys}
    assert not bad, f"分组 key 不在 PROMPT_GROUPS 里：{bad}"


def test_groups_are_unique_and_have_titles():
    keys = [k for k, _, _ in po.PROMPT_GROUPS]
    assert len(keys) == len(set(keys)), "分组 key 重复"
    for key, title, desc in po.PROMPT_GROUPS:
        assert title and desc, f"{key} 缺标题或说明"


def test_every_meta_has_one_line_desc():
    """一句话作用必须是**一句话**：过长就变成第二份正文（会腐烂）。"""
    for name, (_group, desc) in po.PROMPT_META.items():
        assert desc, f"{name} 没有作用说明"
        assert len(desc) <= 60, f"{name} 的作用说明太长（{len(desc)} 字），要一句话"


def test_list_overrides_carries_group_and_desc():
    rows = po.list_overrides()
    assert len(rows) == len(po.OVERRIDABLE)
    for row in rows:
        assert row["group"], row
        assert row["group_title"], row
        assert row["desc"], row


def test_list_overrides_is_sorted_by_group():
    """输出顺序按 PROMPT_GROUPS，界面才能直接渲染（排序只在一个地方做）。"""
    rows = po.list_overrides()
    order = {k: i for i, (k, _, _) in enumerate(po.PROMPT_GROUPS)}
    seq = [order[r["group"]] for r in rows]
    assert seq == sorted(seq), "条目没有按分组顺序排列"


# ══════════════════════════════════════════════════════════════
# 协议来源（2026-10-26 第二轮验收第 4 条）
#
# 主人问「狗头军师的内容被蒸馏进了规则，但我好像在规则里没有看到」——
# 它一直在（conversation_craft / knowledge_boundary），缺的只是界面上那个标签。
# ══════════════════════════════════════════════════════════════

def test_origin_keys_are_overridable():
    """来源表只能指向真实存在的协议——指向不存在的名字等于一条永不到用的注释。"""
    extra = [k for k in po.PROMPT_ORIGIN if k not in po.OVERRIDABLE]
    assert not extra, extra


def test_distilled_protocols_carry_origin():
    """那两条从知识语料蒸馏进来的协议必须带来源，否则界面上看不出它们从哪来。"""
    for name in ("conversation_craft", "knowledge_boundary"):
        assert po.PROMPT_ORIGIN.get(name), name
        assert "蒸馏" in po.PROMPT_ORIGIN[name], po.PROMPT_ORIGIN[name]


def test_list_overrides_exposes_origin():
    """`origin` **文件头优先**（2026-10-26 二次修正）。

    修之前 `list_overrides()` 只读代码里的 `PROMPT_ORIGIN` 表，文件里
    `origin: 知识` 是死数据——主人报「标签 origin 更改保存后不生效」就是这个。
    """
    rows = {r["name"]: r for r in po.list_overrides()}
    for name in ("conversation_craft", "knowledge_boundary"):
        meta = po.read_meta(name)
        if "origin" in meta:
            assert rows[name]["origin"] == meta["origin"], name
        else:
            assert rows[name]["origin"] == po.PROMPT_ORIGIN[name], name
    # 没有来源的协议必须是空串（不是 None、不是 "原生"）——前端据此决定要不要渲染标签
    assert rows["comprehension"]["origin"] == ""


def test_blank_origin_in_file_hides_the_tag(tmp_path, monkeypatch):
    """文件头写 `origin:`（空值）= **显式不要标签**，必须压过代码默认。

    靠 `in meta` 判断而不是 `meta.get("origin")` 的真值判断：真值判断下
    「清空标签」永远不生效——因为空串是假值，代码会以为是「没写」。
    """
    monkeypatch.setattr(po, "PROMPT_DIR", tmp_path)
    assert po.PROMPT_ORIGIN.get("conversation_craft"), "前提：代码默认有来源"
    (tmp_path / "conversation_craft.md").write_text(
        "---\ngroup: reply\ndesc: x\norigin: ''\n---\n\n正文\n", encoding="utf-8")
    row = [r for r in po.list_overrides() if r["name"] == "conversation_craft"][0]
    assert row["origin"] == "", "文件头显式空串没有压过代码默认"

    # 反过来：文件头没写 origin 时，代码默认仍然生效
    (tmp_path / "conversation_craft.md").write_text(
        "---\ngroup: reply\ndesc: x\n---\n\n正文\n", encoding="utf-8")
    row = [r for r in po.list_overrides() if r["name"] == "conversation_craft"][0]
    assert row["origin"] == po.PROMPT_ORIGIN["conversation_craft"]


# ── 元数据外置到文档（2026-10-26 主人要求）────────────────────────────
#
# 「协议条目得知识蒸馏标签不要明文化，每条协议的标签和注释都不要明文，
#   和场景卡一样在文档内可改。」

def test_front_matter_never_leaks_into_prompt():
    """文件头是文案，**绝不能**跟着正文进 system prompt。

    这个错的形状很隐蔽：她不会报错，只是回答里带上「---」和「name:」——
    而且只有在某条协议被覆盖过的时候才出现。
    """
    for name in po.OVERRIDABLE:
        text = po.read_override(name)
        if not text:
            continue
        assert not text.lstrip().startswith("---"), f"{name} 的正文带着 YAML 头"
        assert "group_title:" not in text, f"{name} 的正文里混进了元数据字段"


def test_every_prompt_file_has_metadata():
    """每条协议的文件都要有文件头——没有的话界面只能显示文件名。"""
    missing = [n for n in po.OVERRIDABLE if not po.read_meta(n).get("group")]
    assert not missing, f"这些协议文件缺文件头（或 YAML 写坏了）：{missing}"


def test_metadata_is_editable_from_the_file(tmp_path, monkeypatch):
    """改文件头的 desc → 界面读到的 desc 跟着变（真改真读，不是静态断言）。"""
    monkeypatch.setattr(po, "PROMPT_DIR", tmp_path)
    (tmp_path / "thinking.md").write_text(
        "---\n" + "name: 多想一会儿\n"
        "group: understand\ngroup_title: 怎么理解对方\n"
        "desc: 这是我从文件里改的说明\n"
        "origin: \n"
        "---\n\n正文内容\n", encoding="utf-8")
    meta = po.read_meta("thinking")
    assert meta["desc"] == "这是我从文件里改的说明"
    assert meta["name"] == "多想一会儿"
    assert po.read_override("thinking") == "正文内容\n"

    # 关键：界面读的是 `list_overrides()`，不是 `read_meta()`。
    # 只测 read_meta 的话，元数据外置到文档这件事只做了一半——
    # 界面上看到的还是代码里那张表（这正是第一次变异对照抓出来的假绿）。
    row = [r for r in po.list_overrides() if r["name"] == "thinking"][0]
    assert row["desc"] == "这是我从文件里改的说明", row["desc"]
    assert row["group_title"] == "怎么理解对方", row["group_title"]

    # 没写文件头的那几条必须回落代码默认，不能变空
    other = [r for r in po.list_overrides() if r["name"] == "comprehension"][0]
    assert other["desc"], "没有文件头的协议丢了说明"


def test_broken_yaml_falls_back_to_body():
    """YAML 写坏 → 当没有头，正文照常返回。

    这条守的是「覆盖层坏了不能拖垮启动」：文件头是给人看的，
    正文才是她真正要用的东西。
    """
    broken = "---" + chr(10) + ":::" + chr(10) + "bad" + chr(10) + "---" + chr(10) + "正文" + chr(10)
    meta, body = po.split_front_matter(broken)
    assert meta == {}
    assert "正文" in body


def test_no_yaml_header_means_plain_body():
    """老文件（只有正文）行为完全不变。"""
    meta, body = po.split_front_matter("就是一段正文\n")
    assert meta == {}
    assert body == "就是一段正文\n"
