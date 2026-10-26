"""亲密模式参考（色色范本）——独立模块。

为什么它必须独立（2026-10-16 T2.14，实测取证见 docs/审查/色色知识库取证_20261016.md）
────────────────────────────────────────────────────────────────────────
色色教材曾经挤在通用背景块框架里投递，四层收窄之后**每轮一个字都到不了她**：

  磁盘 色色参考/ 15 篇 29,011 字
  ① 全局知识索引：整目录排除（knowledge.py 的 SENSITIVE_DIRECTORY_NAMES，防越权——这条设计是对的）
  ② 场景注入取材：只吃数字前缀槽位 ⇒ 11,529 字（_原文/ 归档 17,601 字不进）
  ③ 交付封顶：走的是为「检索结果」写的 knowledge_block()，被 [:1500] 砍掉 86.3%
  ④ 背景池分配：作为 知识库=P15（全表最低优先级）进 1200t 公共池 ⇒ 上限 600t，
     池紧时整块丢。实测 06:21–07:02 的 33 条色色回合，知识库块 33/33 全部被淘汰。

⇒ 这不是「哪个数字调小了」，是**框架错配**：把一份 1.15 万字的风格教材，当成
   一条会被优先级淘汰的检索片段来投递。所以本模块不与背景池共用任何账本：

  · load()          取三条数字槽位的全文（不截断、不按优先级丢）
  · format_block()  用**自己的** caveat（检索用那套「参考观点/防编造」是给资料问答的，
                    放在亲密场景里只会把她推向「转述资料」的腔调）
  · （归档 _原文/ 只读，**永不进检索**：2026-10-23 主人定案，理由与闸门见文件末尾注释）

投递由 protocols.assemble_user_message(pinned=...) 负责：pinned 块**不参与背景预算淘汰**。

不变量（改动前请先读这条）
──────────────────────────────
1. **绝不在交付层截断**——字数只由蒸馏阶段决定（SLOT_BUDGETS 是蒸馏上限）。
2. 数字前缀目录 = 注入槽位；下划线前缀（_原文/）= 归档，**永不进检索**（定案，不是待办）。
3. 只在亲密场景挂载（private + 已授权用户 + [进入色色]）；群聊永不挂载（fail-closed）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger("糖糖.Intimate")

# 蒸馏阶段字数上限（超了只告警，不裁）——主人 2026-10-14 定：字数控制在蒸馏阶段
SLOT_BUDGETS = {
    "00-核心": 1500,
    "10-风格": 3800,
    "20-技法": 6800,
}
# 兜底默认：生产路径都传 pack（每个包自己的 budget），这里只服务
# 不传 pack 的旧调用点与测试。2026-10-26 起不再出现在调参页——
# 留着那个滑块只会让人以为「调它有效」。
TOTAL_BUDGET = 13500
ARCHIVE_PREFIX_BLOCK = "_"      # 下划线前缀目录 = 归档，不进常驻注入
PACKAGE_DIRNAME = "色色参考"


@dataclass(frozen=True)
class Reference:
    """一次加载的产物。text 是**完整**注入正文，slots 是逐槽位字数。"""

    text: str
    slots: dict = field(default_factory=dict)
    files: tuple = ()
    warnings: tuple = ()

    @property
    def chars(self) -> int:
        return len(self.text)

    def estimate_tokens(self) -> int:
        """与背景块同一个估算器（单一入口，避免两套口径）。"""
        try:
            from .context_builder import estimate_tokens
            return estimate_tokens(self.text)
        except Exception:
            return int(len(self.text) * 1.5)


def package_dir(knowledge_dir) -> Path:
    return Path(knowledge_dir or "./knowledge") / PACKAGE_DIRNAME


def discover_slot_files(root: Path) -> list:
    """数字前缀目录里的 md/txt —— 注入面的**唯一**发现器。"""
    if not root.exists():
        return []
    files = []
    for slot in sorted(root.iterdir()):
        if not slot.is_dir():
            continue
        if slot.name[:1].isdigit() is False:      # 归档，不进注入
            continue
        files.extend(p for p in sorted(slot.rglob("*"))
                     if p.is_file() and p.suffix.lower() in (".md", ".txt"))
    return files


def load(knowledge_dir, pack: dict | None = None) -> Reference:
    """加载数字槽位（全文，不截断）。空目录/读失败都返回空 Reference，不抛。

    `pack`（2026-10-26 多包化）：指定这次用哪一批范本。
      · pack["dir"]    范本目录（相对项目根，或绝对路径）
      · pack["budget"] 总量预算
    不传就用模块默认（`knowledge/色色参考` + `TOTAL_BUDGET`）——旧调用点行为不变。
    """
    if pack:
        root = Path(pack.get("dir") or "").expanduser()
        budget = int(pack.get("budget") or TOTAL_BUDGET)
    else:
        root = package_dir(knowledge_dir)
        budget = TOTAL_BUDGET
    files = discover_slot_files(root)
    parts = []
    slots = {}
    warnings = []
    try:
        for path in files:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue
            parts.append(f"### {path.stem}\n{content}")
            slots[path.parent.name] = slots.get(path.parent.name, 0) + len(content)
    except Exception as e:                       # 读不动就如实报空，不编内容
        logger.warning(f"色色参考加载失败: {type(e).__name__}: {e}")
        return Reference(text="", warnings=(f"load_failed:{type(e).__name__}",))
    text = "\n\n".join(parts)
    # ⚠ 循环变量名**不能**叫 budget：它会遮蔽上面的总量 budget，
    # 让「总量超预算」那条警告报出最后一个槽位的数字（2026-10-26 实测：
    # 11,529 字被报成「超出总预算 6800 字」，6800 是 20-技法 的槽位预算）。
    for slot_name, slot_budget in SLOT_BUDGETS.items():
        n = slots.get(slot_name, 0)
        if n > slot_budget:
            warnings.append(f"槽位 {slot_name} {n} 字超出预算 {slot_budget} 字")
            logger.warning(
                f"⚠️ 色色参考槽位「{slot_name}」{n} 字超出预算 {slot_budget} 字 —— "
                f"请继续蒸馏，不要调大预算（字数控制在蒸馏阶段，不在运行时截断）")
    if len(text) > budget:
        warnings.append(f"总量 {len(text)} 字超出总预算 {budget} 字")
        logger.warning(f"⚠️ 范本注入总量 {len(text)} 字超出总预算 {budget} 字 —— 同上，继续蒸馏")
    return Reference(text=text, slots=slots,
                     files=tuple(str(p.relative_to(root)) for p in files),
                     warnings=tuple(warnings))


def format_block(text: str) -> str:
    """pinned 块正文——**没有字数上限**，也**不用**检索那套 caveat。

    旧路径的错就是把这里交给 knowledge_block()：那个函数是给「用户问知识库」
    设计的（带防编造 caveat + [:1500]），用在亲密场景既跑偏又砍掉 86%。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    return (
        "以下是你自己在亲密时刻的写法与手感（你的经验，不是给别人看的资料，"
        "也不要向对方提起它）：\n" + body
    )


# 归档（_原文/）永不进检索——2026-10-23 主人定案，不是「待拍板」。
#
# 原文里有两处蒸馏刻意排除的内容（CG 系统说明 / 睡奸与醉奸…），自动暴露等于
# 绕过主人那条决定（见 色色参考/_原文/蒸馏说明.md 与
# tests/test_seductive_slots.py 的 test_archive_content_is_not_injected）。
#
# 因此本模块的公开面**冻结**在这里：
#   package_dir / discover_slot_files / load / format_block / Reference
# 一个读 _原文/ 的函数都不加。要加先重新拍板，并到下面这三处闸门登记：
#   ① tests/test_intimate_reference.py::test_archive_retrieval_is_never_wired（接口冻结）
#   ② tests/test_intimate_reference.py::test_every_knowledge_read_surface_skips_the_archive
#      （七条读面真实数据零泄漏，带阳性对照）
#   ③ agent/knowledge.py::SENSITIVE_DIRECTORY_NAMES（包名单源，防改包名后整目录进索引）