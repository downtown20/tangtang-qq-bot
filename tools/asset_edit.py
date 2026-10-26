# -*- coding: utf-8 -*-
"""可编辑资产层 —— 白名单 + 备份 + 校验（2026-10-26 第 49 轮 B1）。

## 这个模块存在的理由

主人要「参数完全暴露可以改，自由度拉满」。但**自由度必须配保险绳**：

- 这些文本直接进 system prompt。改坏一条，她可能从「有点别扭」变成
  「答非所问」——而你要到下一次跟她说话才发现，中间她可能已经跟别人聊过。
- 所以「备份」「校验」「白名单」不是附加功能，是**允许你去改的前提**。

## 三条铁律

1. **白名单，不是黑名单**（教训 #31：按类型开天窗，1392 个文件从未被扫）。
   只有下表里的根可读可写；其余一律 `AssetPathError`。
   给一个通用文件管理器 = 给一把能删 `memory.db` 的刀。
2. **先备份，再校验，最后写**。顺序不能反：校验失败也要留下「你改了什么」的痕迹，
   否则下次想回滚时发现压根没备份。
3. **校验失败拒绝写入**，并给出人类可读的原因（YAML 错误带行号）。

## 不做

- 不做热重载（`config.yaml` 的既有语义就是「保存后重启」；再加一套会造出
  第二套生效语义，教训 #38）。
- 不做 diff / 版本树（那是 `MemoryHistory` 的活）。备份目录 + 时间戳够用。
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from tools.scenario_rename import sync_rename as _sync_scenario_targets

from agent.prompt_overrides import (
    GROUPS_PATH as _PROMPT_GROUPS_PATH,
    read_groups as _read_prompt_groups,
    render_groups_yaml as _render_groups_yaml,
    replace_group_title as _replace_group_title,
    split_front_matter as _split_prompt_front_matter,
)

import yaml

#: 项目根（`tools/` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)

#: 备份根目录
BACKUP_ROOT = PROJECT_ROOT / "_backups"

#: 单个文本资产的字节上限。协议文本单条 8KB 已经远超实际（实测最长约 400 字），
#: 给足余量但不允许把一整本书塞进 system prompt。
MAX_TEXT_BYTES = 64 * 1024

#: 白名单规格。
#:
#: `root` 是相对项目根的路径（目录或单文件）；`exts` 是允许的后缀；
#: `group` 只用于界面分组，`desc` 是给不写代码的人看的说明。
#:
#: ⚠ 新增根必须同时回答「为什么它可写」——写在本表里就是回答。
ASSET_SPECS: list[dict] = [
    {
        # 身份卡不止一张：role_card.md（默认）、role_card_murasame.md、
        # role_card_michele.md……用 `/角色` 切换，同时切身份/声线/表情包三样。
        # 2026-10-26 之前只列了 role_card.md，另外两张**连编辑都进不去**。
        "key": "roles",
        "root": ".",
        "pattern": "role_card*.md",
        "kind": "glob",
        "exts": (".md",),
        "group": "人格",
        "label": "身份卡",
        "desc": ("一张卡 = 一套身份。`/角色` 切换时同时换声线与表情包，"
                 "所以不要只改卡不改配套——那会得到人格和声音不一样的缝合怪"),
        "injection": "forced",
    },
    {
        "key": "scenario",
        "root": "scenarios",
        "kind": "dir",
        "exts": (".yaml",),
        "group": "人格",
        "label": "场景人格卡",
        "desc": "每张卡一套语气与敏感度；要挂到人身上才生效，改完在群管理里重挂或重启",
        "injection": "forced",
    },
    {
        "key": "prompt",
        "root": "prompts",
        "kind": "dir",
        "exts": (".md",),
        "group": "规则",
        "label": "协议条文",
        "desc": ("行为协议的覆盖层：文件存在则用它，删掉就回落到代码内置默认值。"
                 "这是「规则自由度」的入口"),
        "injection": "forced",
    },
    # ── 必要背景知识：按「怎么进上下文」分，不按内容名分 ──────────────
    #
    # 主人 2026-10-26 的原话：「用『狗头军师』太具体了，应该把这部分说成
    # 必要背景知识……她未必全是色色内容，结构布局上需要一个通用的范式，
    # 而不是照着我的文件结构抄。」
    #
    # 所以 `label` 写的是**注入方式**（常驻直投 / 检索命中），不写内容名；
    # 内容名只出现在 `root` 路径里（那是物理位置，必须如实显示）。
    # 换一套语料只需要改 `root`，界面文案一个字都不用动。
    #
    # 第三种方式「蒸馏进提示词」不在这里——它就是 `prompts/` 里的两条
    # （conversation_craft / knowledge_boundary），在「规则」分组里改。
    # **不给它第二个人口**：同一份文本两个编辑入口 = 两份内容迟早不一致。
    {
        "key": "knowledge_scenario",
        "root": "knowledge/色色参考",
        "kind": "dir",
        "exts": (".md", ".txt"),
        "group": "必要背景知识",
        "label": "场景触发",
        # ⚠ 这里曾经写成「常驻直投 / 每轮都在」——**是错的**；后来又写成
        # 「群聊永不挂载」——`allow_groups` 落地后**也不对了**。
        # 现在的口径：私聊看「已挂该模式 + 该模式已激活」；群聊不看激活，
        # 看该模式自己的 `allow_groups`（默认关）。见 agent/handler.py 两条场景分支。
        # 文案只写机制，不写具体槽位名（槽位名是使用者的内容设定，
        # 2026-10-26 主人：「00-核心是明文写法……使用通用注释」）。
        "desc": ("对应模式激活后才注入，平时一个字都不进上下文。"
                 "数字前缀目录 = 槽位（会整块注入），下划线前缀目录 = 归档（不注入）"),
        "injection": "scenario",
        # 触发它的场景（`scenarios/*.yaml` 的 name）。界面据此显示「哪个模式会用到」，
        # 而不是把场景名写死在文案里。
        "triggered_by": ("seductive",),
    },
    {
        "key": "knowledge_retrieval",
        "root": "knowledge",
        # 排除那个「常驻直投」包，否则同一批文件会在两个分节里各显示一次
        "exclude": ("knowledge/色色参考",),
        "kind": "dir",
        "exts": (".md", ".txt"),
        "group": "必要背景知识",
        "label": "检索命中",
        "desc": ("只有她主动检索时才可能被查到——不保证读到。"
                 "所以「必须让她知道的事」不要放这里，"
                 "放「场景触发」的槽位，或「规则」里的协议条文"),
        "injection": "retrieval",
    },
]

#: 分组的整体说明（界面显示在分组标题下面一行）。
#:
#: 为什么需要：「必要背景知识」下三种进入方式，光看「常驻直投 / 检索命中」
#: 两个词分不清第三种（蒸馏进提示词）在哪——它不在这个分组里，
#: 不写清楚就会被认为「漏了」。
GROUP_DESC: dict[str, str] = {
    "必要背景知识": ("她「知道的东西」有三种进入方式："
                    "①蒸馏进提示词——每轮都在，在『规则』分组里改；"
                    "②场景触发——对应模式激活后才注入，平时不在；"
                    "③检索命中——她主动查了才有"),
}


#: 错误码 → HTTP 状态。放在这里而不是 bridge 里：**「这个错该怎么回」是这个模块的职责**，
#: 让 HTTP 层再维护一张映射表就是两个判定点（教训 #38）。
_ASSET_STATUS = {
    "bad_path": 400,
    "bad_ext": 400,
    "bad_name": 400,
    "bad_yaml": 400,
    "bad_encoding": 400,
    "not_resettable": 400,
    "not_empty": 400,
    "not_allowed": 403,
    "forbidden": 403,
    "not_found": 404,
    "exists": 409,
    "too_large": 413,
}

#: 新建文件时的最小骨架（`{stem}` = 文件名去后缀）。
#:
#: 为什么要骨架而不是写空文件：**新建出来的东西必须立刻生效**。
#: 空 `.yaml` 会被 `_validate` 拒（顶层不是映射）；就算放行了，
#: `scenario.py` 也会因为「缺少 name 字段」跳过它——界面上多了一个场景，
#: 实际群管理里挂不上（教训 #48 同族：界面撒谎）。
_FILE_SKELETON = {
    "scenario": (
        "name: {stem}\n"
        "type: overlay\n"
        "display: {stem}\n"
        "description: （还没写）\n"
        "tone: |\n"
        "  （这个场景下她的语气边界）\n"
    ),
    "knowledge_retrieval": "# {stem}\n\n（还没写）\n",
    "roles": ("# {stem}\n\n## 你是谁\n\n（一句话说清她是谁、怎么说话。"
              "⚠ 身份卡只写身份，不写规则——规则进协议条文）\n"),
    "knowledge_scenario": "# {stem}\n\n（还没写）\n",
}

#: 目录命名规则（第 49 轮 B5）。
#:
#: 为什么必须有规则、不能随便建：**色色参考的目录名就是语义**——
#: `discover_slot_files()` 只认「首字符是数字」的目录，下划线前缀就是归档。
#: 随便建一个 `新目录/` 既不进注入也不报错，界面上看着「加好了」，
#: 实际她永远读不到（教训 #48 的同族：界面撒谎）。
#:
#: `None`（不在表里）= 这一类不支持建目录：`prompts/` 与 `scenarios/` 是**平铺**的，
#: 各自有固定读取路径，建子目录只会造出「看得见、不生效」的假目录。
_DIR_RULES = {
    "knowledge_scenario": {
        "pattern": r"^(?:\d{2}-|_)[^\s/\\:*?\"<>|]{1,38}$",
        # 不举具体槽位名当例子：槽位名是使用者的内容设定（2026-10-26 主人：
        # 「00-核心是明文写法……使用通用注释」）。只讲规则。
        "label": "数字前缀 = 槽位（会整块注入），下划线前缀 = 归档（不注入）",
    },
    "knowledge_retrieval": {
        # 这一类的目录名**没有语义**（知识库是递归扫描的，不看前缀），
        # 所以不限制命名风格；只挡掉不可能做目录名的字符。
        "pattern": r"^[^\s/\\:*?\"<>|]{1,39}$",
        "label": "分类目录：这一类的目录名没有语义（递归扫描），随便取即可",
    },
}


class AssetError(ValueError):
    """资产操作的统一错误。带 `code`（给界面判断）与 `status`（给 HTTP 层）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status = _ASSET_STATUS.get(code, 400)


def _norm_rel(rel: str) -> str:
    """把用户给的相对路径归一并做**第一道**拒绝。

    这里只做「明显非法」的过滤（空、绝对路径、`..`、反斜杠统一成斜杠），
    真正的白名单判定在 `_spec_for` + `_resolve`（两道，缺一不可）。
    """
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        raise AssetError("bad_path", "路径为空")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise AssetError("bad_path", "不接受绝对路径")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise AssetError("bad_path", "不接受 .. 路径")
    if any(p.startswith("~") for p in parts):
        raise AssetError("bad_path", "不接受家目录写法")
    return "/".join(parts)


def _role_label(path: Path) -> str:
    """读身份卡首行 `# 标题` 当显示名；读不出来就退回文件名。

    **不维护一张「文件名 → 人名」的映射表**：那种表换一张卡就要改代码，
    而且改名之后界面还显示旧名字（教训 #52 的同一种病）。
    """
    try:
        for line in path.read_text(encoding="utf-8").split("\n"):
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem


def _excluded(spec: dict, rel: str) -> bool:
    """这条相对路径是否落在该 spec 的排除区里（`exclude` 是目录前缀列表）。"""
    return any(rel == ex or rel.startswith(ex + "/") for ex in spec.get("exclude", ()))


#: 可被 `assets.yaml` 覆盖的字段。
#:
#: 只允许这三样：界面叫它什么、它在哪、一句话说明。
#: `kind` / `exts` / `injection` 属于**机制**，不许在数据里改——
#: 机制一改，界面显示的和真实行为就会分叉，而分叉之后没人能一眼看出哪个是真的。
_OVERRIDABLE_FIELDS = ("label", "root", "desc")

#: 注册表文件名（项目根）。**可选**：不存在就用代码默认值。
ASSET_REGISTRY = "assets.yaml"


def load_registry() -> dict:
    """读 `assets.yaml`，返回 `{spec_key: {可覆盖字段}}`。

    文件不存在 / 坏掉 / 结构不对 → 返回空 dict（界面退回代码默认值）。
    绝不抛异常：这个文件坏了不该让「人格规则」整页打不开。
    """
    path = PROJECT_ROOT / ASSET_REGISTRY
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return {}
    items = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        out[key] = {
            k: str(item[k]) for k in _OVERRIDABLE_FIELDS
            if k in item and item[k] is not None
        }
    return out


def effective_specs() -> list[dict]:
    """ASSET_SPECS + 注册表覆盖（注册表是**可选**的增强，不是替代）。"""
    registry = load_registry()
    if not registry:
        return [dict(s) for s in ASSET_SPECS]
    specs = []
    for spec in ASSET_SPECS:
        merged = dict(spec)
        for field, value in (registry.get(spec["key"]) or {}).items():
            merged[field] = value
        specs.append(merged)
    return specs


def write_registry_entry(key: str, **fields) -> dict:
    """改一条注册（界面「改名 / 换路径」用）。返回写后的整份注册表。

    只允许 `_OVERRIDABLE_FIELDS` 里的字段；其它字段直接拒绝——
    「能不能改机制」这件事不该由一个参数决定。
    """
    bad = [f for f in fields if f not in _OVERRIDABLE_FIELDS]
    if bad:
        raise AssetError("not_allowed", f"这些字段属于机制，不能在这里改：{'、'.join(bad)}")
    known = {s["key"] for s in ASSET_SPECS}
    if key not in known:
        raise AssetError("not_found", f"没有这一类资产：{key}")

    path = PROJECT_ROOT / ASSET_REGISTRY
    current = load_registry()
    current.setdefault(key, {}).update({k: str(v) for k, v in fields.items()})
    payload = {
        "assets": [{"key": k, **v} for k, v in current.items()],
        # 文件头写清楚它是可选的、能改什么——下一个接手的人不用翻代码
        "#": None,
    }
    payload.pop("#")
    text = (
        "# 人格与规则：每一类资产的显示名与路径（**可选文件**）\n"
        "#\n"
        "# 不存在这个文件时，界面用代码里的默认值；写了哪条就覆盖哪条。\n"
        "# 只能改三样：label（界面叫什么）/ root（在哪）/ desc（一句说明）。\n"
        "# kind / exts / injection 属于机制，改机制要改代码——否则界面和真实行为会分叉。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    )
    path.write_text(text, encoding="utf-8", newline="")
    return current


def _spec_for(rel: str) -> dict:
    """这条路径属于哪个白名单根？不属于就拒绝。

    `exclude` 也参与匹配（不只是显示过滤）：同一批文件只能有一个归属，
    否则「编辑它时按哪个 spec 校验后缀」就会随表顺序变化——
    那是一个随代码顺序漂移的判定（教训 #38）。
    """
    norm = _norm_rel(rel)
    for spec in effective_specs():
        root = spec["root"]
        if spec["kind"] == "file":
            if norm == root:
                return spec
        elif spec["kind"] == "glob":
            # 只匹配根目录下的文件名（不许带子路径）——`role_card*.md` 就是根目录那几张卡
            if "/" not in norm and fnmatch.fnmatch(norm, spec["pattern"]):
                return spec
        else:
            if (norm == root or norm.startswith(root + "/")) and not _excluded(spec, norm):
                return spec
    raise AssetError("forbidden", f"不在可编辑白名单里：{norm}")


def _resolve(rel: str) -> tuple[Path, dict]:
    """白名单路径 → 绝对路径（**第二道**：符号链接逃逸检查）。"""
    spec = _spec_for(rel)
    norm = _norm_rel(rel)
    target = (PROJECT_ROOT / norm).resolve()
    root = (PROJECT_ROOT / spec["root"]).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        # `resolve()` 之后跳出了白名单根 —— 说明中间有符号链接指向外面
        raise AssetError("forbidden", "路径解析后跳出了允许范围") from None
    if spec["kind"] in ("file", "glob") and target != root:
        # glob 型的 root 是「.」，target 永远不可能等于它——单独判断：
        # glob 只接受**根目录下**的匹配文件（`_spec_for` 已经挡掉了带斜杠的）
        if spec["kind"] == "file":
            raise AssetError("forbidden", "单文件资产不接受子路径")
        if spec["kind"] == "glob" and "/" in norm:
            raise AssetError("forbidden", "这一类只接受根目录下的文件")
    if target.suffix and target.suffix.lower() not in spec["exts"]:
        raise AssetError(
            "bad_ext", f"该资产只接受 {'/'.join(spec['exts'])}，收到 {target.suffix}")
    return target, spec


def _backup_dir() -> Path:
    """本次写入的备份目录 —— **每次唯一**（时间戳精确到微秒）。

    ⚠ 第一版只精确到**秒**，结果同一秒内的两次保存写进同一个目录、
    后写的覆盖前写的：`write → restore` 在 0.5 秒内跑完时，备份文件被
    「当前版本」覆盖，"回到第一次保存前"就永远做不到。
    2026-10-26 单测 `test_restore_from_backup` 当场抓到——这正是 B1
    「保险绳」存在的意义：**备份机制本身也会有 bug，而它一出错就没得救**。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    d = BACKUP_ROOT / f"编辑_{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate(spec: dict, content: str, name: str) -> list[str]:
    """校验内容。返回**提醒**（非阻塞），不合法抛 `AssetError`。

    YAML 用 `safe_load` 真解析：只查后缀等于没查。
    """
    warnings: list[str] = []
    size = len(content.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise AssetError(
            "too_large",
            f"{name} 有 {size} 字节，超过单文件上限 {MAX_TEXT_BYTES} 字节。"
            f"这些文本会整段进 system prompt，太长会挤掉真正要说的内容。")
    if not content.strip():
        warnings.append(f"{name} 现在是空的——保存后这个资产将不产生任何内容")
    if name.endswith(".yaml"):
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f"（第 {mark.line + 1} 行第 {mark.column + 1} 列）" if mark else ""
            raise AssetError(
                "bad_yaml",
                f"YAML 解析失败{where}：{getattr(exc, 'problem', exc)}") from None
        if not isinstance(parsed, (dict, list)):
            raise AssetError(
                "bad_yaml",
                f"YAML 顶层必须是映射或列表，现在是 {type(parsed).__name__}")
        if isinstance(parsed, dict) and not parsed:
            warnings.append(f"{name} 解析出空映射——这个场景会不生效")
    if spec.get("key") == "prompt" and name.endswith(".md"):
        warnings.extend(_validate_prompt_meta(content, name))
    return warnings


def _validate_prompt_meta(content: str, name: str) -> list[str]:
    """协议文件头的校验 —— **只提醒，不拦**。

    为什么不拦：文件头的 `group` 指向 `prompts/_groups.yaml`，而那份文件是
    使用者自己也能改的。拦下来的话，「先加组、再改协议」这个顺序就被堵死了。
    但这些错误**必须说出来**：2026-10-26 之前 `group_title` 写在文件头里，
    改了不生效、两条撞名还会合并成一组——静默失效正是那次的病根。
    """
    warnings: list[str] = []
    meta, body = _split_prompt_front_matter(content)
    if not body.strip():
        warnings.append(f"{name} 只有文件头、没有正文——保存后这条协议会回落到内置默认")
    if "group_title" in meta:
        warnings.append(
            f"{name} 头里的 `group_title` 已经不起作用了：组标题从 2026-10-26 起"
            f"只在 prompts/_groups.yaml 里改（或桌面端双击组标题）。这行留着无害，删掉更干净")
    if "name" in meta:
        warnings.append(
            f"{name} 头里的 `name` 没有被读取过（文件名就是身份），删掉更干净")
    group = str(meta.get("group") or "").strip()
    if group:
        known = {g["key"] for g in _read_prompt_groups()}
        if group not in known:
            warnings.append(
                f"{name} 的 `group: {group}` 不在分组表里（现有 {', '.join(sorted(known))}）"
                f"——界面上会落到「未分组」。要新开一组请在 prompts/_groups.yaml 里加一条")
    return warnings


def list_assets() -> dict:
    """可编辑资产清单（按 group 分组，只列存在的文件）。"""
    groups: list[dict] = []
    for spec in effective_specs():
        root = PROJECT_ROOT / spec["root"]
        files: list[dict] = []
        if spec["kind"] == "glob":
            for p in sorted(PROJECT_ROOT.glob(spec["pattern"])):
                if not p.is_file() or p.suffix.lower() not in spec["exts"]:
                    continue
                info = _file_info(p, spec)
                # 卡片名从首行 `# 标题` 读——**不硬编码「糖糖/丛雨/米雪儿」**：
                # 换一张卡、改个名，界面跟着变（教训 #52）
                info["label"] = _role_label(p)
                info["is_default"] = p.name == "role_card.md"
                files.append(info)
        elif root.is_file():
            files = [_file_info(root, spec)]
        elif root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in spec["exts"]:
                    continue
                if _excluded(spec, p.relative_to(PROJECT_ROOT).as_posix()):
                    continue
                files.append(_file_info(p, spec))
        groups.append({
            "key": spec["key"], "label": spec["label"], "group": spec["group"],
            "desc": spec["desc"], "root": spec["root"],
            # 这份内容怎么进她的上下文：
            #   forced    = 每轮都在（蒸馏进提示词 / 身份卡）
            #   scenario  = 对应模式激活后才注入（不是常驻！）
            #   retrieval = 她主动查了才有
            "injection": spec.get("injection", "none"),
            # 触发它的场景名（只有 scenario 型有）——界面据此显示「哪个模式会用到」
            "triggered_by": list(spec.get("triggered_by", ())),
            "exists": root.exists(), "files": files,
            # 结构管理（B5）：这一类能不能加篇目 / 加目录 / 删篇目
            #
            # `can_create_file` 对 `prompts/` 是 False：只有 `OVERRIDABLE` 里那 22 个
            # 固定文件名会被 `prompt_overrides` 读取，新建任意 `foo.md` 不生效——
            # 界面上给个「＋篇」就成了撒谎的按钮。
            "can_create_file": spec["kind"] in ("dir", "glob") and spec["key"] != "prompt",
            # 删除只开放给知识类：`prompts/` 用「恢复默认」（语义更准），
            # `scenarios/` 删了群里的挂载会失效，要走群管理重挂，不该在树里随手删。
            "can_delete": spec["key"].startswith("knowledge"),
            "dir_rule": _DIR_RULES.get(spec["key"], {}).get("label", ""),
            "can_mkdir": spec["key"] in _DIR_RULES,
            "dirs": list_dirs(spec),
            # 前端新建篇目时用来补默认后缀（不写后缀 = bad_ext，体验差且费解）
            "exts": list(spec["exts"]),
            # 场景类：把 yaml 里的 display / description / type 读出来当**卡片**显示。
            # 只给文件名的话，使用者看到的是 `psychology.yaml`，
            # 而她在群里挂的是「🧠 心理陪伴」——两边对不上（教训 #48 同族：界面撒谎）。
            "cards": _scenario_cards(spec) if spec["key"] == "scenario" else [],
        })
    # 同一 group 的相邻合并，界面按 group 分节
    merged: list[dict] = []
    for g in groups:
        if merged and merged[-1]["group"] == g["group"]:
            merged[-1]["items"].append(g)
        else:
            merged.append({
                "group": g["group"],
                "group_desc": GROUP_DESC.get(g["group"], ""),
                "items": [g],
            })
    return {"groups": merged, "backup_root": str(BACKUP_ROOT)}


def _scenario_cards(spec: dict) -> list[dict]:
    """读 `scenarios/*.yaml` 的卡片信息（display / description / type）。

    读不动或字段不全的**照样返回**（display 回落成 name 或文件名）：
    一个 yaml 写坏了，不应该让整页资产列表打不开；
    但它必须显示出来——那正是最需要被改的那张卡。
    """
    import yaml

    root = PROJECT_ROOT / spec["root"]
    if not root.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(root.glob("*.yaml")):
        if p.suffix.lower() not in spec["exts"]:
            continue
        info = _file_info(p, spec)
        data: dict = {}
        warning = ""
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
            else:
                warning = "这个 yaml 顶层不是映射，场景不会生效"
        except Exception as exc:  # noqa: BLE001 - 卡片要显示，不能因为解析失败就消失
            warning = f"yaml 解析失败：{getattr(exc, 'problem', exc)}"
        out.append({
            **info,
            "name": str(data.get("name") or p.stem),
            "display": str(data.get("display") or data.get("name") or p.stem),
            "description": str(data.get("description") or ""),
            "type": str(data.get("type") or "overlay"),
            "warning": warning,
        })
    return out


def _file_info(path: Path, spec: dict) -> dict:
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    try:
        st = path.stat()
        return {
            "path": rel,
            "name": path.name,
            "size": int(st.st_size),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "editable": True,
        }
    except OSError:
        return {"path": rel, "name": path.name, "size": 0, "mtime": "", "editable": False}


def read_asset(rel: str) -> dict:
    """读一个资产：内容 + 元信息 + 可用备份清单。"""
    path, spec = _resolve(rel)
    if not path.is_file():
        raise AssetError("not_found", f"文件不存在：{_norm_rel(rel)}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise AssetError("bad_encoding", "文件不是 UTF-8 文本") from None
    st = path.stat()
    return {
        "path": _norm_rel(rel),
        "group": spec["group"],
        "label": spec["label"],
        "desc": spec["desc"],
        "content": content,
        "size": len(content.encode("utf-8")),
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "backups": list_backups(rel),
        "apply_hint": _apply_hint(spec),
    }


def rename_dir(rel: str, new_name: str) -> dict:
    """给一个目录（槽位 / 分类）改名。**数字前缀保留**，只改 `-` 后面那段。

    `new_name` 可以是「开场」也可以「00-开场」——两种都接受，
    前端预填「去掉前缀的部分」或完整名都能调。
    """
    import re as _re

    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    if not path.is_dir():
        raise AssetError("not_found", f"目录不存在：{norm}")

    old_name = path.name
    m = _re.match(r"^(\d+)-(.*)$", old_name)
    if not m:
        raise AssetError(
            "not_allowed",
            f"「{old_name}」不是带数字前缀的槽位目录，不能改名（分类/归档目录改名会影响检索归属）")
    prefix, _tail = m.group(1), m.group(2)

    raw_name = (new_name or "").strip()
    if not raw_name:
        raise AssetError("bad_name", "新名字不能为空")
    if "/" in raw_name or "\\" in raw_name:
        raise AssetError("forbidden", "改名只改这一层目录名，不能带路径")
    if raw_name.startswith(".") or "_" == raw_name[:1]:
        raise AssetError("bad_name", "槽位名不能以点或下划线开头（下划线 = 归档，语义不同）")

    # 用户传完整名就校验前缀一致；传半段就补前缀
    if _re.match(r"^\d+-", raw_name):
        new_prefix, new_tail = raw_name.split("-", 1)
        if new_prefix != prefix:
            raise AssetError(
                "forbidden",
                f"数字前缀不能从 {prefix} 改成 {new_prefix}——前缀是槽位序号，"
                f"换它等于换一个槽位（预算按名字对）")
        new_name_full = raw_name
    else:
        new_tail = raw_name
        new_name_full = f"{prefix}-{new_tail}"

    if not new_tail.strip():
        raise AssetError("bad_name", "「-」后面不能是空的")
    if new_name_full == old_name:
        return {
            "ok": True, "path": norm, "new_path": norm,
            "unchanged": True, "apply_hint": _apply_hint(spec),
        }

    target = path.with_name(new_name_full)
    if target.exists():
        raise AssetError("exists", f"同名目录已存在：{new_name_full}")

    new_rel = (path.parent / new_name_full).relative_to(PROJECT_ROOT).as_posix()
    if _spec_for(new_rel)["key"] != spec["key"]:
        raise AssetError("forbidden", "改名后不属于这一类资产了")

    os.replace(path, target)
    logger.info("目录改名：%s → %s", norm, new_rel)

    return {
        "ok": True,
        "path": norm,
        "new_path": new_rel,
        "unchanged": False,
        "apply_hint": _apply_hint(spec),
        # 改名不动内容，但**槽位预算按名字对**（SLOT_BUDGETS 的键是 '00-核心' 这种），
        # 所以改名后那一条预算告警会失效——这句必须说，不然用户以为「只改了个名字」。
        "note": "目录内容一个字没动；但槽位字数告警是按名字对的，改名后那条告警不再对这个槽位生效",
    }


def rename_asset(rel: str, new_name: str) -> dict:
    """给一个资产文件改名（**只改文件名，不改路径**）。

    `new_name` 传纯文件名（不带目录、不带扩展名也行——没写扩展名就沿用原来的）。
    """
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    if not path.is_file():
        raise AssetError("not_found", f"文件不存在：{norm}")

    raw = (new_name or "").strip()
    if not raw:
        raise AssetError("bad_name", "新名字不能为空")
    if "/" in raw or "\\" in raw:
        raise AssetError("forbidden", "改名只改文件名，不能带目录（搬家走新建 + 删除）")
    if raw.startswith("."):
        raise AssetError("bad_name", "新名字不能以点开头")

    # 扩展名：没写就沿用原文件的；写了就必须一致
    old_suffix = path.suffix
    new_suffix = Path(raw).suffix
    if new_suffix and new_suffix.lower() != old_suffix.lower():
        raise AssetError(
            "bad_extension",
            f"扩展名不能从 {old_suffix} 改成 {new_suffix}——换了扩展名这一条就不归「{spec['label']}」了")
    if not new_suffix:
        raw = raw + old_suffix

    target = path.with_name(raw)
    if target == path:
        return {
            "ok": True, "path": norm, "new_path": norm, "backup": "",
            "unchanged": True, "apply_hint": _apply_hint(spec),
        }
    if target.exists():
        raise AssetError("exists", f"同名文件已存在：{target.name}")

    # 改名后必须还落在同一类资产里（同名目录下，天然满足；这里仍校验一次）
    new_rel = (path.parent / raw).relative_to(PROJECT_ROOT).as_posix()
    try:
        if _spec_for(new_rel)["key"] != spec["key"]:
            raise AssetError("forbidden", "改名后不属于这一类资产了")
    except AssetError:
        raise

    # 先备份（改名也是破坏性操作：名字丢了就找不回来了）
    d = _backup_dir()
    dst = d / norm
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)

    os.replace(path, target)
    logger.info("资产改名：%s → %s", norm, new_rel)

    return {
        "ok": True,
        "path": norm,
        "new_path": new_rel,
        "backup": dst.relative_to(BACKUP_ROOT).as_posix(),
        "unchanged": False,
        "apply_hint": _apply_hint(spec),
        # 名字变了但**内容没变**：场景卡的 name/display 都在内容里，所以挂载不受影响。
        # 这句要如实说——否则用户会以为改名把场景弄坏了。
        "note": "只改了文件名，文件内容一个字没动；场景挂载（按 display）和触发（按 name）都不受影响"
        if spec["key"] in ("scenario", "roles") else "",
    }


def _apply_hint(spec: dict) -> str:
    """改完怎么生效——**必须说清楚**，不能让人以为存完就变了。"""
    if spec["key"] == "persona":
        return "保存后执行 /人格 重载（或重启糖糖）生效"
    if spec["key"] == "scenario":
        return "保存后在群管理页重新挂一次场景，或重启糖糖"
    if spec["key"] == "prompt":
        return "保存后**重启糖糖**生效（协议文本在启动时拼接）"
    return "保存后 /知识库 重载，或重启糖糖"


def list_backups(rel: str) -> list[dict]:
    """某个资产的可用备份（新的在前），最多 20 条。"""
    norm = _norm_rel(rel)
    out: list[dict] = []
    if not BACKUP_ROOT.is_dir():
        return out
    for d in sorted(BACKUP_ROOT.glob("编辑_*"), reverse=True):
        f = d / norm
        if f.is_file():
            out.append({
                "dir": d.name,
                "path": f.relative_to(BACKUP_ROOT).as_posix(),
                "size": int(f.stat().st_size),
            })
        if len(out) >= 20:
            break
    return out


def write_asset(rel: str, content: str) -> dict:
    """写入：**先备份原文件 → 再校验新内容 → 最后落盘**。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    # 单文件与 glob 型（身份卡）都**只能编辑已有文件**：
    # 新建走 `create_asset`，否则「写一个不存在的路径」等于静默创建，
    # 而 `write_asset` 的语义是「改这一份」。
    if spec["kind"] in ("file", "glob") and not path.exists():
        raise AssetError(
            "not_found",
            f"{norm} 还不存在。要新建请用「＋篇」（新建走 create_asset，不走保存）")
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    # 场景卡改名会**静默断开挂载**（`scenario_targets` 里存的就是 display 名，
    # 见 agent/scenario.py:138-150）。所以在覆盖之前把旧 display 读出来，
    # 写完再把挂载点改过去——没有这一步，改名就是一次无声的功能下线。
    old_display = ""
    if (spec["key"] == "scenario" and path.suffix.lower() in (".yaml", ".yml")
            and path.is_file()):
        try:
            old_display = str((yaml.safe_load(path.read_text(encoding="utf-8")) or {})
                              .get("display") or "")
        except (yaml.YAMLError, OSError):
            old_display = ""

    backup_rel = ""
    if path.is_file():
        # 1) 先备份（校验失败也要留下痕迹：下次想回滚时才有东西可回）
        d = _backup_dir()
        dst = d / norm
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        backup_rel = dst.relative_to(BACKUP_ROOT).as_posix()
    # 2) 再校验
    warnings = _validate(spec, content, path.name)
    # 3) 最后写
    path.write_text(content, encoding="utf-8", newline="")

    # 4) 改名 → 把挂载点一起改过去
    #    只动 `scenario_targets` 那个块的缩进范围，config.yaml 其余字节不变
    #    （不能 yaml.safe_dump：那份文件的注释是它的一半价值）
    renamed_mounts: list[str] = []
    if old_display:
        try:
            new_display = str((yaml.safe_load(content) or {}).get("display") or "")
        except yaml.YAMLError:
            new_display = ""
        if new_display:
            renamed_mounts = _sync_scenario_targets(PROJECT_ROOT, old_display, new_display)

    return {
        "ok": True,
        "path": norm,
        "backup": backup_rel,
        "warnings": warnings,
        "apply_hint": _apply_hint(spec),
        "size": len(content.encode("utf-8")),
        # 改名时跟着改过去的挂载点（QQ 号 / 群号）；空列表 = 没有挂载点受影响
        "renamed_mounts": renamed_mounts,
    }


def restore_asset(rel: str, backup: str) -> dict:
    """从备份恢复（同样先备份当前版本，保证「恢复」本身也可撤销）。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    src = (BACKUP_ROOT / str(backup or "")).resolve()
    try:
        src.relative_to(BACKUP_ROOT.resolve())
    except ValueError:
        raise AssetError("forbidden", "备份路径不在 _backups 里") from None
    if not src.is_file():
        raise AssetError("not_found", f"备份不存在：{backup}")
    # 当前版本也备份一次（恢复错了还能回来）
    if path.is_file():
        d = _backup_dir()
        dst = d / norm
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, path)
    return {"ok": True, "path": norm, "from": backup,
            "apply_hint": _apply_hint(spec)}


def reset_asset(rel: str) -> dict:
    """恢复默认。

    **只对 `prompts/` 有意义**：协议文本的「默认」住在代码里（`DEFAULT_PROMPTS`），
    删掉覆盖文件就自动回落。其他资产没有「出厂默认」——用 `restore_asset` 从备份挑一版。
    """
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    if spec["key"] != "prompt":
        raise AssetError(
            "not_resettable",
            "这一类没有内置默认值；如果要回到旧版本，请用「从备份恢复」")
    if path.is_file():
        d = _backup_dir()
        dst = d / norm
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        path.unlink()
        return {"ok": True, "path": norm, "deleted": True,
                "apply_hint": "已删除覆盖文件 → 重启糖糖后回落到代码内置默认"}
    return {"ok": True, "path": norm, "deleted": False,
            "apply_hint": "本来就没有覆盖文件，当前用的就是内置默认"}


# ═══════════════════════════════════════════════════════════════
# 协议分组（2026-10-26 二次修正）—— 组标题从文件头收敛到一份组表
#
# 修的是主人报的三个连在一起的毛病：
#   ① 改某条协议的 `group` → 它跑到别的组（位置迁移）；
#   ② 改某条的 `group_title` → 同组排前面的文件先占位，改了不生效；
#   ③ 两条的显示标题撞名 → 前端按标题文字切组，两个组被**合并**成一个。
# 根因同一个：组是**组级**的东西（顺序/名字/说明），却被复制成条目级副本。
#
# 所以组的唯一真相是 `prompts/_groups.yaml`，条目文件头只留 `group: <key>` 引用。
# ═══════════════════════════════════════════════════════════════

def prompt_groups() -> dict:
    """分组清单（顺序 = 界面顺序），带每组的条目数。

    `unknown_files` 是把 `group:` 写成了组表里没有的 key 的条目数——
    这些条目在界面上会落到「未分组」，数量必须显式报出来，不能让人自己数。
    """
    from agent.prompt_overrides import list_overrides

    rows = list_overrides()
    counts: dict[str, int] = {}
    unknown = 0
    for r in rows:
        if r.get("group_known"):
            counts[r["group"]] = counts.get(r["group"], 0) + 1
        else:
            unknown += 1
    groups = []
    for g in _read_prompt_groups():
        groups.append({
            "key": g["key"],
            "title": g["title"],
            "desc": g["desc"],
            "files": counts.get(g["key"], 0),
        })
    try:
        shown = _PROMPT_GROUPS_PATH.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        # 组表被指到项目外（测试用 tmp）——如实给出绝对路径，不要假装是相对路径
        shown = str(_PROMPT_GROUPS_PATH)
    return {
        "ok": True,
        "path": shown,
        "exists": _PROMPT_GROUPS_PATH.is_file(),
        "groups": groups,
        "unknown_files": unknown,
    }


def save_prompt_group_title(key: str, title: str) -> dict:
    """改一个组的显示名（桌面端双击组标题走这里）。

    **先备份 → 再文本级替换 → 最后回读校验**。回读这一步不是形式：
    2026-10-26 踩过「脚本跑过了但 CSS 没落盘」——报「已改」只能依据读回来的文件。
    """
    group_key = str(key or "").strip()
    new_title = str(title or "").strip()
    if not group_key:
        raise AssetError("bad_name", "没指定要改哪个组")
    if not new_title:
        raise AssetError("bad_name", "组标题不能为空——空标题会让这一组显示成「未分组」")
    if "\n" in new_title or "\r" in new_title:
        raise AssetError("bad_name", "组标题不能换行")
    if len(new_title) > 40:
        raise AssetError("bad_name", f"组标题最多 40 字，现在 {len(new_title)} 字")
    known = {g["key"] for g in _read_prompt_groups()}
    if group_key not in known:
        raise AssetError(
            "not_found",
            f"没有叫「{group_key}」的组（现有 {', '.join(sorted(known))}）")

    created = False
    if not _PROMPT_GROUPS_PATH.is_file():
        _PROMPT_GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROMPT_GROUPS_PATH.write_text(_render_groups_yaml(), encoding="utf-8",
                                       newline="")
        created = True
    raw = _PROMPT_GROUPS_PATH.read_text(encoding="utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    updated = _replace_group_title(raw, group_key, new_title)
    if not updated:
        raise AssetError(
            "not_found",
            f"{_PROMPT_GROUPS_PATH.name} 里找不到「{group_key}」这一段——"
            f"文件被手工改坏了？可以删掉它让系统重新生成")

    d = _backup_dir()
    dst = d / "prompts" / _PROMPT_GROUPS_PATH.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_PROMPT_GROUPS_PATH, dst)
    _PROMPT_GROUPS_PATH.write_text(updated if nl == "\n" else updated.replace("\n", nl),
                                   encoding="utf-8", newline="")

    # 回读：只认文件里现在的值
    got = next((g["title"] for g in _read_prompt_groups() if g["key"] == group_key), None)
    if got != new_title:
        raise AssetError(
            "verify_failed",
            f"写入后回读到的标题是「{got}」，与预期的「{new_title}」不一致。"
            f"改前版本在 {dst.relative_to(BACKUP_ROOT).as_posix()}")
    return {
        "ok": True,
        "key": group_key,
        "title": got,
        "backup": dst.relative_to(BACKUP_ROOT).as_posix(),
        "created": created,
        # 组标题是界面文案，不进 system prompt —— 但界面要重新拉一次清单才看到
        "apply_hint": "刷新列表即可看到（组标题只是界面文案，不影响她收到的内容）",
    }


# ═══════════════════════════════════════════════════════════════
# 结构管理（第 49 轮 B5）—— 新建 / 删除 篇目与目录
#
# 主人要的是 C：「还要能新建/删除/改结构（槽位、分类目录）」。
# 也就是说知识库不只是「改文字」，还要能**加一篇、加一个分类、加一个槽位**。
#
# 三条与 write_asset 一致、但在这批里更容易踩的规矩：
#
# 1. **删除也要先备份**。删一篇 md 比改坏一篇更不可逆——磁盘上没有第二份。
# 2. **删目录只允许空目录**。`rmtree` 一次删 23 篇是「误操作面」最大的操作，
#    而它的价值只有「清理空壳」。不值得为省一次点击把保险绳剪了。
# 3. **目录名过规则**（`_DIR_RULES`）。色色参考的目录名**就是语义**
#    （数字前缀 = 进注入，下划线 = 归档），不能随便建。
# ═══════════════════════════════════════════════════════════════

def _dir_rule(spec: dict) -> dict:
    rule = _DIR_RULES.get(spec["key"])
    if not rule:
        raise AssetError(
            "not_allowed",
            f"「{spec['label']}」不支持新建目录——这一类是平铺的，"
            f"建出来的子目录不会被读取（界面上看得见、实际不生效）")
    return rule


def _dir_kind(name: str) -> str:
    """目录分类：`slot` 进常驻注入 / `archive` 归档不进 / `category` 普通分类。"""
    if name[:1].isdigit():
        return "slot"
    if name.startswith("_"):
        return "archive"
    return "category"


def list_dirs(spec: dict) -> list:
    """某个白名单根下的所有目录（含空目录——新建的槽位正是空的）。"""
    if spec["kind"] != "dir":
        return []
    root = PROJECT_ROOT / spec["root"]
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_dir() or p == root:
            continue
        rel = p.relative_to(PROJECT_ROOT).as_posix()
        if _excluded(spec, rel):
            continue
        depth = rel.count("/") - spec["root"].count("/")
        out.append({
            "path": rel,
            "name": p.name,
            "kind": _dir_kind(p.name),
            "depth": depth,
            "empty": not any(p.iterdir()),
            # 前端据此显示「进注入 / 不看它」——**这一个字节都不能猜**
            "in_injection": _dir_kind(p.name) == "slot",
        })
    return out


def create_asset(rel: str, content: str = "") -> dict:
    """新建一个文本资产（**不覆盖**：已存在就报 `exists`）。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    if spec["kind"] == "file":
        raise AssetError("not_allowed", f"单文件资产不允许新建：{norm}")
    if path.exists():
        raise AssetError("exists", f"已存在同名文件：{norm}（要改请用编辑器）")
    if not path.parent.is_dir():
        raise AssetError(
            "not_found",
            f"上级目录不存在：{path.parent.name or spec['root']}——先建目录，再建篇目")
    if not content.strip():
        # 空文件对 yaml 类会当场被拒、对知识类则是「盯着一个空壳」，两种都糟
        content = _FILE_SKELETON.get(spec["key"], "").format(stem=path.stem)
    warnings = _validate(spec, content, path.name)
    path.write_text(content, encoding="utf-8", newline="")
    return {
        "ok": True,
        "path": norm,
        "created": True,
        "warnings": warnings,
        "apply_hint": _apply_hint(spec),
        "size": len(content.encode("utf-8")),
    }


def delete_asset(rel: str) -> dict:
    """删除一个文本资产（**先备份**，删错了还能从备份捞回来）。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    # 只有知识类允许删除。身份卡 / 场景卡 / 协议条文都不行：
    # 删掉身份卡她可能起不来；删掉协议条文要改回代码默认值才能恢复。
    # 这里判 `key` 而不是 `kind`——界面上的 `can_delete` 是同一个判据，
    # 两处不一致就会出现「按钮不给点，但 API 能删」。
    if not spec["key"].startswith("knowledge"):
        raise AssetError(
            "not_allowed",
            f"「{spec['label']}」不允许删除——这一类用「恢复默认」或直接改内容")
    if not path.is_file():
        raise AssetError("not_found", f"文件不存在：{norm}")
    d = _backup_dir()
    dst = d / norm
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    path.unlink()
    return {
        "ok": True,
        "path": norm,
        "deleted": True,
        "backup": dst.relative_to(BACKUP_ROOT).as_posix(),
        "apply_hint": _apply_hint(spec),
    }


def create_dir(rel: str) -> dict:
    """新建目录（槽位 / 分类）。名字必须过 `_DIR_RULES`。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    rule = _dir_rule(spec)
    root = (PROJECT_ROOT / spec["root"]).resolve()
    if path == root:
        raise AssetError("exists", "这一类目录已经存在")
    if path.exists():
        raise AssetError("exists", f"已存在同名目录：{norm}")
    if not path.parent.is_dir():
        raise AssetError("not_found", f"上级目录不存在：{path.parent.name}")
    if not re.match(rule["pattern"], path.name):
        raise AssetError(
            "bad_name", f"目录名不符合规则：{path.name}。{rule['label']}")
    path.mkdir(parents=False)
    kind = _dir_kind(path.name)
    warnings = []
    # 按**注入方式**给提醒（不按内容名）：换一套语料时这些话照样成立
    if spec.get("injection") == "scenario":
        if kind == "slot":
            warnings.append(
                "新建槽位没有单独字数预算——只有总量预算会告警。"
                "建议蒸馏到 2000 字以内，否则会稀释其他槽位。"
                "注意这一包只在对应模式激活时注入，平时不在上下文里")
        else:
            warnings.append("归档目录不会被注入；只有数字前缀的槽位才会被读到")
    elif spec.get("injection") == "retrieval":
        warnings.append("新分类目录里的 md 会被知识库扫描；改完执行 /知识库 重载 才生效")
    return {
        "ok": True,
        "path": norm,
        "name": path.name,
        "kind": kind,
        "in_injection": kind == "slot",
        "warnings": warnings,
        "apply_hint": _apply_hint(spec),
    }


def delete_dir(rel: str) -> dict:
    """删除**空**目录。非空一律拒绝——一次误删几十篇的代价不值这点方便。"""
    path, spec = _resolve(rel)
    norm = _norm_rel(rel)
    _dir_rule(spec)
    root = (PROJECT_ROOT / spec["root"]).resolve()
    if path == root:
        raise AssetError("not_allowed", "不能删除白名单根目录本身")
    if not path.is_dir():
        raise AssetError("not_found", f"目录不存在：{norm}")
    left = list(path.iterdir())
    if left:
        raise AssetError(
            "not_empty",
            f"目录里还有 {len(left)} 项——先删掉或移走内容，再删目录"
            f"（不允许一次性递归删除）")
    path.rmdir()
    return {"ok": True, "path": norm, "deleted": True, "apply_hint": _apply_hint(spec)}
