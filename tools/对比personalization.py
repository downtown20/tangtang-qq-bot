#!python3.10
"""对比笔记本版 personalization.py 与重建版——逐方法 diff。

2026-08-16：BirthdayGreeter 被 heredoc 正则误删后从 transcript 编辑链重建。
本脚本用于验证重建质量：笔记本 Syncthing 离线时保留了原版文件。

用法：
    python tools/对比personalization.py <笔记本文件路径>
输出：
    每个方法/函数：逐字一致 / 有差异(统一 diff) / 单侧缺失
"""
import ast
import difflib
import sys
from pathlib import Path


def extract_symbols(path: Path) -> dict[str, str]:
    """提取顶层 class/def 的源码文本（ast 精确切片，含 docstring）"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    symbols: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            symbols[node.name] = "\n".join(lines[node.lineno - 1: node.end_lineno])
    return symbols


def compare_one(name: str, laptop: str | None, rebuilt: str | None) -> str:
    if laptop is None and rebuilt is None:
        return f"  {name}: 两侧都无（跳过）"
    if laptop is None:
        return f"  {name}: ⚠️ 笔记本无，重建版有（可能批三新增，正常）"
    if rebuilt is None:
        return f"  {name}: ❌ 笔记本有，重建版缺失！"
    if laptop == rebuilt:
        return f"  {name}: ✅ 逐字一致"
    diff = difflib.unified_diff(
        laptop.splitlines(), rebuilt.splitlines(),
        fromfile=f"笔记本/{name}", tofile=f"重建/{name}", lineterm=""
    )
    return f"  {name}: ⚠️ 有差异：\n" + "\n".join(f"    {l}" for l in list(diff)[:40])


def main():
    if len(sys.argv) != 2:
        print("用法: python tools/对比personalization.py <笔记本文件路径>")
        sys.exit(1)
    laptop_path = Path(sys.argv[1])
    rebuilt_path = Path(__file__).resolve().parent.parent / "agent" / "personalization.py"
    if not laptop_path.exists():
        print(f"找不到文件: {laptop_path}")
        sys.exit(1)

    laptop = extract_symbols(laptop_path)
    rebuilt = extract_symbols(rebuilt_path)
    names = sorted(set(laptop) | set(rebuilt))

    print(f"笔记本: {laptop_path}")
    print(f"重建版: {rebuilt_path}\n")
    for name in names:
        print(compare_one(name, laptop.get(name), rebuilt.get(name)))
    print("\n结论：全部 ✅ 即逐字还原；有 ⚠️ 的以笔记本版为准（重点看 start/_loop）。")


if __name__ == "__main__":
    main()
