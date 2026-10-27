# -*- coding: utf-8 -*-
"""设置页 schema 与 config.yaml 的双向契约（2026-10-25 第 47 轮 3.2 步）。

## 这组用例防的是什么

教训 #4（2026-08 那次「改了 config 没改控制台，用错模型一周」）：
**参数加进 `config.yaml` 就以为配好了，但界面上没有入口**——运营者只能去改
配置文件，而改配置文件没有校验、没有备份、没有「哪些键能改」的提示。

第 47 轮调研实测：`config.yaml` **120 个叶子键**，`settingsSchema.json` 只覆盖 63 个
（缺 57，扣掉不该手改的还剩 **19 个真缺口**，其中 `catch_up` 整组 7 个全缺——
而它正是「她为什么不知道我刚说的话」的第一答案来源）。

补完之后要**钉住它别再漏**，所以有了这组双向契约：

| 方向 | 断言 | 防的是 |
|---|---|---|
| 正向 | schema 里每个 path 都必须在 config.yaml 里存在 | 写错路径 → 界面显示空白、保存时静默建出一个野键 |
| 反向 | config.yaml 每个叶子键 ∈ schema ∪ 豁免 | 新加参数忘了给入口（#4 的机器版） |
| 导航 | 每个 schema 节都在 `SETTINGS_GROUPS` 里 | **`schema` 加节、导航没加 → 用户点不到**（#12 的机器版） |
| 可读 | 每字段有 label + hint | 只有键名的设置项对小白等于没说（#48） |

## 阳性对照（教训 #30/#37：闸门必须能红）

- 删掉 schema 里任意一个 section 的任意字段 → `test_every_config_key_has_an_entry_or_is_exempt` 红；
- 把某个 path 拼错（`catch_up.max_message` 少个 s）→ `test_every_schema_path_exists_in_config` 红；
- 把某个节从 `settingsGroups.ts` 删掉 → `test_every_schema_section_has_a_nav_item` 红；
- 删掉某个字段的 hint → `test_every_field_has_label_and_hint` 红。

（前两条已进 `变异_设置页契约.json`。）
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml  # noqa: E402

BASE = Path(__file__).parent.parent
SCHEMA_PATH = BASE / "desktop/src/lib/settingsSchema.json"
CONFIG_PATH = BASE / "config.yaml"
GROUPS_PATH = BASE / "desktop/src/lib/settingsGroups.ts"

#: 字段类型白名单（与 `SettingsFields.tsx::FieldControl` 的分支一一对应）。
#:
#: `choice` 靠 `CHOICES[path]` 表渲染成下拉框（type 本身不参与分支判断）；
#: `label` 是纯说明块，内容放 `hint`，`label` 可空。
#:
#: `secret`（2026-10-27）：不是普通输入框，而是「一份要写进 .env 的密钥」。
#: 独立的「连接密钥」页取消后，密钥跟着各自主人走（AI模型页 / SnowLuma 页 / 识图页），
#: 渲染成「已配置 + 掩码」和粘贴框，保存走 /api/secrets/save。
#: ⚠ 加这一条必须跟 `SettingsFields.tsx` 的渲染分支**同时**改：
#:    只加类型不实现渲染 → 设置页静默少一行；只实现渲染不加类型 → 这个测试立刻红。
FIELD_TYPES = {"bool", "int", "float", "str", "list", "label", "choice", "secret",
               "model_ref", "ollama_model"}

#: 不需要进设置页的 config 前缀/键 —— **每条都要写理由**。
#: 理由不是注释装饰：下一个人会想「为什么这个不放进去」，不写他就自己塞进去了。
EXEMPT: dict[str, str] = {
    "appearance.": "桌面端自身外观（主题/字号/配色），不是糖糖的运行参数",
    "blacklist.": "群管理页已有（按群批量编辑，单键拆到设置页会两处打架）",
    "groups.": "群管理页已有（按群号分节编辑 owner/admins/scenario）",
    "scenario_targets.": "群管理页已有（按人/群挂场景）",
    "profile.": "记忆画像页 / 数据库——8 个字段是一份档案，不适合逐条表单",
    "personality.": "role_card.md 才是人格第一来源（CLAUDE.md 三「改哪里」表）",
    "image_gen.": "功能已暂时关闭，设置页只有一句说明块",
    "llm.api_key": "密钥：写在 .env 里，环境变量优先；界面只显示是否已配",
    "llm.seductive_api_key": "密钥；且亲密模式已并入主模型（2026-10-24），勿单独配",
    "llm.seductive_base_url": "历史残留字段：亲密模式已并入主模型，留着只为兼容老配置",
    "llm.seductive_model": "历史残留字段：亲密模式已并入主模型，留着只为兼容老配置",
    "llm.seductive_provider": "历史残留字段：亲密模式已并入主模型，留着只为兼容老配置",
    "llm.vision.api_key": "密钥：写在 .env 里，界面只显示是否已配",
    "llm.models": "模型清单是数组，由模型页「AI 模型」单独管理（表单放不下结构化列表）",
    "llm.active_model": "当前模型由模型页的切换器管理，放设置页会出现两个切换点",
    "llm.light_effort": "轻量档位与模型页一起管理——单独放会让两处口径漂移",
    "napcat.access_token": "密钥：写在 .env 里，界面只显示是否已配",
    "scenario_reference.": "由「高级调参 → 特殊模式」区块管理——它是**列表**（一个场景一个包），"
                          "设置页的单值表单渲染不了「场景 + 目录 + 预算 + 是否要激活 + 有效期」这五项",
    "tuning.": "由「高级调参」页管理——每个常量要显示 当前值/默认值/作用/文件:行，"
                "设置页的表单渲染不了这四样，硬塞进去会变成一张看不懂的列表",
    # 2026-10-27：主人要求「设置中的图库打标只调参数与模型；打哪些图、怎么打去图库页」。
    # 所以 dir / rename 从设置页移走，改成**按次打标时由图库页传参**
    # （`agent/image_tagger.run(dir_path=..., rename=...)`）。
    # config 里的旧键**不删**：它们是兜底默认值。老配置里写着 `dir: stickers_cg` 的人
    # 升级后不该突然打标到别的目录——这是向后兼容的读取兜底，**不是忘了删**。
    # 若哪天这两个键从 config.yaml 里彻底删掉，把下面两条一起删——不要留一片空地给闸门巡视（#30）。
    "image_tagging.dir": "已移到图库页按次传参（打哪个库）；config 里的旧值只作兜底（老配置升级后不该换目录），设置页不再编辑",
    "image_tagging.rename": "已移到图库页按次传参（打完是否改名）；config 里的旧值只作兜底（老配置的行为不该被默默改掉），设置页不再编辑",
}


def _leaves(obj, prefix=""):
    """把嵌套 dict 拍平成 `{点号路径: 值}`。列表/标量都算叶子。"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = obj
    return out


def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _config_leaves():
    return _leaves(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))


def _schema_paths():
    return [f["path"] for s in _schema() for f in s["fields"]]


def _is_exempt(path: str) -> bool:
    for key in EXEMPT:
        if key.endswith(".") and path.startswith(key):
            return True
        if path == key:
            return True
    return False


# ── 正向：schema 里的 path 必须真实存在 ──────────────────────────

def test_every_schema_path_exists_in_config():
    """写错路径的后果：界面永远空白，保存时还会在 config 里建出一个野键。"""
    cfg = _config_leaves()
    bad = [p for p in _schema_paths()
           if not p.startswith("_") and p not in cfg]
    assert not bad, f"schema 里有 config.yaml 不存在的路径：{bad}"


def test_no_duplicate_paths():
    paths = _schema_paths()
    dups = sorted({p for p in paths if paths.count(p) > 1})
    assert not dups, f"同一路径在 schema 里出现多次（后一个会覆盖前一个）：{dups}"


def test_field_types_are_known():
    bad = [(f["path"], f["type"]) for s in _schema() for f in s["fields"]
           if f.get("type") not in FIELD_TYPES]
    assert not bad, f"未知字段类型（SettingsFields 不会渲染）：{bad}"


# ── 反向：config 的每个键都要有归宿（本轮的核心闸门）────────────

def test_every_config_key_has_an_entry_or_is_exempt():
    """**教训 #4 的机器版**：新加一个 config 参数，就必须同时回答「谁能在界面上改它」。

    不回答也行——把路径写进 `EXEMPT` 并给出理由。**沉默不是选项**。
    """
    schema_paths = set(_schema_paths())
    missing = sorted(
        p for p in _config_leaves()
        if p not in schema_paths and not _is_exempt(p)
    )
    assert not missing, (
        "这些 config 键既不在设置页、也不在豁免清单里——"
        "要么加进 desktop/src/lib/settingsSchema.json，"
        "要么在 tests/test_settings_schema_coverage.py 的 EXEMPT 里写明理由：\n  "
        + "\n  ".join(missing))


def test_exempt_entries_have_real_reasons():
    """豁免必须写理由，且理由要能回答「为什么它不该在设置页」。"""
    for key, reason in EXEMPT.items():
        assert isinstance(reason, str) and len(reason.strip()) >= 8, (
            f"豁免项 {key} 的理由太短（等于没说）：{reason!r}")


def test_exempt_entries_actually_exist():
    """豁免清单里不许有已经不存在的键——那会让闸门对着一片空地巡视（#30 同族）。"""
    cfg = _config_leaves()
    dead = [k for k in EXEMPT if not any(
        (p == k or p.startswith(k)) for p in cfg)]
    assert not dead, f"豁免清单里这些键在 config.yaml 里已经不存在了：{dead}"


# ── 导航：schema 有节，用户必须点得到 ───────────────────────────

def _nav_keys():
    """从 `settingsGroups.ts` 里取导航项的 key（它就是设置页左栏的单一事实源）。"""
    src = GROUPS_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"key:\s*'([a-z_]+)'", src))


def test_every_schema_section_has_a_nav_item():
    """**教训 #12 的机器版**：`schema` 加了节、导航没加 → 用户永远点不到那一节。"""
    nav = _nav_keys()
    missing = [s["key"] for s in _schema() if s["key"] not in nav]
    assert not missing, (
        f"这些设置节没有左侧导航入口（用户点不到）：{missing}；"
        f"请在 desktop/src/lib/settingsGroups.ts 的 SETTINGS_GROUPS 里加一项")


def test_nav_items_have_matching_schema():
    """反过来：导航里指向一个不存在的节 → 点进去空白。"""
    schema_keys = {s["key"] for s in _schema()}
    dead = sorted(k for k in _nav_keys() if k not in schema_keys)
    assert not dead, f"导航里有 schema 中不存在的节（点进去是空的）：{dead}"


# ── 可读：小白看得懂才叫设置项 ────────────────────────────────

def test_every_field_has_label_and_hint():
    """只有键名没有解释的设置项，对不写代码的人等于没说（教训 #48）。

    两种例外，各有原因：
      · `label` 类型是**纯说明块**，内容放 `hint`（`SettingsFields.tsx` 的 label 分支
        渲染的是 `f.hint || f.label`），所以要求「label 和 hint 至少有一个」；
      · `choice` 类型的说明同样必需——小白看到 `on / off / auto` 三个英文
        选项，不知道该选哪个。
    """
    bad = []
    for s in _schema():
        for f in s["fields"]:
            label = str(f.get("label") or "").strip()
            hint = str(f.get("hint") or "").strip()
            if f.get("type") == "label":
                if not (label or hint):
                    bad.append((s["key"], f["path"], "说明块是空的"))
                continue
            if not label:
                bad.append((s["key"], f["path"], "缺 label"))
            if not hint:
                bad.append((s["key"], f["path"], "缺 hint"))
    assert not bad, f"设置项缺说明：{bad}"


def test_catch_up_group_is_fully_exposed():
    """回归：`catch_up` 整组 7 个键是第 47 轮之前**全缺**的那一组。

    单独钉一条，因为它最容易在重构里被整段删掉——而它正是运营者查
    「她为什么不知道我刚说的话」的第一入口。
    """
    paths = set(_schema_paths())
    want = {
        "catch_up.enabled", "catch_up.inject_as_context", "catch_up.max_messages",
        "catch_up.max_missed_replies_per_group", "catch_up.min_gap_minutes",
        "catch_up.missed_reply_window_minutes", "catch_up.summarize_threshold",
    }
    missing = sorted(want - paths)
    assert not missing, f"离线补读这一组又缺了：{missing}"
