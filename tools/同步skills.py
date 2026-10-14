# -*- coding: utf-8 -*-
"""
把 Claude Code 的 skills 镜像到 Codex CLI（2026-08-25）

背景：Codex 原生支持 SKILL.md 技能（同一开放格式，内容零改动），但
- Codex 不读 .claude/skills（openai/codex 源码无此路径，网上「直接读」说法不实）
- Codex 对 symlink 支持有未修 bug（openai/codex #31592 等）——不要用软链接共享
所以用 robocopy 镜像。

纪律（不要改）：
- 故意不用 /MIR：~/.codex/skills 里有 Codex 自带的 .system 内置技能和
  evidence-review 等手工安装技能，/MIR 会把目标侧独有的文件删掉。
  只做增量拷贝（新增/更新，不删除）——冗余技能无害（Codex 只认 */SKILL.md）。
- Codex 硬限制：description > 500 字符会被静默跳过（开放规范是 1024）；
  技能目录注入上限约 55 个/8k 字符，超了后面的技能被丢弃。脚本只警告不裁剪。

用法：python tools/同步skills.py
新装/修改技能后重跑；Codex 重启会话才重新加载技能目录。

下载新 skill 的标准流程（~/.claude/skills 是唯一源头，镜像单向 .claude→.codex）：
1. 把下载的 skill 文件夹放到 ~/.claude/skills/<name>/（用户级）或
   <项目根>\.claude\skills\<name>/（项目级）——必须是 <name>/SKILL.md 结构。
   也可用万能安装器（npx ai-agent-skills install <名>/<GitHub地址>）一次装多端。
2. 跑本脚本镜像到 Codex（装完必跑兜底：安装器装到哪个目录不保证，本脚本保证
   「.claude 里有的 .codex 必有」；幂等无害）。
3. 两边重启会话：CC 新开会话自动加载；Codex 完全退出重开，/skills 可见。
内容零转换（同一 SKILL.md 规范，Claude 专属字段 Codex 自动忽略）；
description 别超 500 字（Codex 限制比规范严，超了静默跳过）。
"""
import re
import subprocess
import sys
from pathlib import Path

# 项目根由本文件位置算出——不写死盘符/用户名（agent/paths.py 的不变量：
# 「任何电脑复制源码 + 装好依赖即可运行」）。2026-10-15 T55 修正：
# 这里原本硬编码本机项目根绝对路径，换机器即失效。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIRRORS = [
    (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"),
    (PROJECT_ROOT / ".claude" / "skills", PROJECT_ROOT / ".codex" / "skills"),
]


def _robocopy(src: Path, dst: Path) -> bool:
    # robocopy 退出码 <8 视为成功（1=有文件复制，也正常）
    r = subprocess.run(
        ["robocopy", str(src), str(dst), "/E", "/XD", ".git",
         "/NFL", "/NDL", "/NJH", "/NJS"],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    return r.returncode < 8


def _check_frontmatter(skills_root: Path):
    """返回 (SKILL.md 总数, [超限名单])。"""
    total, problems = 0, []
    for md in sorted(skills_root.rglob("SKILL.md")):
        total += 1
        text = md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^---\s*\n(.*?)\n---", text, re.S)
        if not m:
            problems.append(f"{md.parent.name}: 无 frontmatter——Codex 会跳过")
            continue
        dm = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if dm and len(dm.group(1)) > 500:
            problems.append(f"{md.parent.name}: description {len(dm.group(1))} 字（>500）——Codex 会跳过")
    return total, problems


def main() -> bool:
    """镜像 + 检查。**返回是否全部成功**（2026-10-14 台账 T11）。

    为什么必须返回而不是只 print：`_robocopy()` 一直能正确判断失败（robocopy 退出码
    >=8 返回 False），但 main() 过去隐式返回 None，脚本**永远以退出码 0 结束**——
    自动化/CI 无法感知失败，只能靠人读 stdout 里的「失败」二字。
    这与 tools/准备发布.py 的教训同源：真正的红线不能只靠人眼。
    """
    failed: list[str] = []
    for src, dst in MIRRORS:
        if not src.is_dir():
            print(f"[跳过] 源不存在: {src}")
            continue
        ok = _robocopy(src, dst)
        print(f"[{'OK' if ok else '失败'}] {src.name} -> {dst}")
        if not ok:
            failed.append(str(dst))

    print()
    for label, root in [("用户级", MIRRORS[0][0]), ("项目级", MIRRORS[1][0])]:
        if not root.is_dir():
            continue
        total, problems = _check_frontmatter(root)
        print(f"[{label}] {root}：共 {total} 个 SKILL.md")
        for p in problems:
            print(f"  ⚠️ {p}")
        if total > 50:
            print(f"  ⚠️ 技能数 {total} 接近/超过 Codex 注入上限（约 55）——可能被截断丢弃")

    if failed:
        print(f"\n[×] 镜像失败 {len(failed)} 个目标：{failed}")
        print("    上面标 [失败] 的那几条没同步过去——Codex 那边仍是旧技能。")
        return False
    return True


if __name__ == "__main__":
    _rc = 0
    try:
        # 2026-10-14 台账 T11：把结果变成退出码——自动化才看得见失败。
        if not main():
            _rc = 1
    finally:
        # 双击 .py 时窗口跑完即关（看起来像闪退）——stdin 是终端才暂停等回车；
        # 命令行/管道/自动化调用不受影响（isatty 在 cmd 管道下可能误判 True，
        # EOFError 守卫兜底：无输入源立即退出，不报错不阻塞）
        if sys.stdin.isatty():
            try:
                input("按回车键退出...")
            except EOFError:
                pass
        sys.exit(_rc)
