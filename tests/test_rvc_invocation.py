"""RVC 调用契约：索引必须用**纯 ASCII 相对路径**传，且 cwd 必须是 RVC 目录。

## 这条闸门是为一次「静默降级」建的（2026-09-19 实测）

faiss 的 C++ 端用窄字符 `fopen` 打开索引文件。在中文 Windows 上，绝对路径里的中文
（项目装在中文名的目录下时必然如此）会按 ANSI 代码页解释成乱码：

    绝对路径（含中文）  → RuntimeError: FileIOReader ... could not open
    相对路径（纯 ASCII）→ ntotal=95555   ✓

而 RVC 对索引加载失败**不报错**：照样出音频，只是音色相似度更低。A/B 实测同一段
12 秒人声，两种路径的输出 sha256 不同——索引真的没生效过，而界面上一个错都没有。

## 口径

不测「代码里写了什么字符串」，测**这个字符串能不能被 faiss 打开**：
ASCII、相对、且调用处的 cwd 是 RVC 目录（相对路径的前提）。
"""

import ast
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
CONSOLE = BASE / "糖糖控制台_qt.py"
RVC_DIRNAME = "Retrieval-based-Voice-Conversion-WebUI"


def _tree() -> ast.Module:
    return ast.parse(CONSOLE.read_text(encoding="utf-8"))


def _module_const(name: str):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"糖糖控制台_qt.py 里找不到常量 {name}")


def test_rvc_index_constant_is_ascii_and_relative():
    """索引路径常量必须是相对路径且纯 ASCII——这两条是 faiss 能打开的前提。

    绝对路径会在中文安装目录下静默失败（见模块 docstring）。
    """
    rel = _module_const("RVC_INDEX_REL")
    assert rel.isascii(), f"索引路径含非 ASCII 字符：{rel!r}——faiss 的窄字符 fopen 打不开"
    assert not Path(rel).is_absolute(), f"索引路径是绝对路径：{rel!r}——中文目录下会静默降级"
    assert ":" not in rel, f"索引路径里出现了盘符：{rel!r}"
    assert rel.endswith("hutao.index"), f"索引路径看着不对：{rel!r}"


def test_index_path_call_sites_use_the_constant():
    """两处 RVC 调用都必须用那个常量，不许再自己拼绝对路径。

    「一键全流程」和「RVC 音色转换」是两条独立路径，各写一遍必然会漏一处——
    这正是当初的形态。
    """
    src = CONSOLE.read_text(encoding="utf-8")
    tree = _tree()

    def _assigned_names(lineno: int) -> list[str]:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.lineno == lineno:
                return [getattr(t, "id", "") for t in node.targets]
        return []

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "index_path":
                    hits.append(node)
    assert len(hits) >= 2, f"只找到 {len(hits)} 处 index_path 赋值——调用点变了？"

    for node in hits:
        seg = ast.get_source_segment(src, node.value) or ""
        assert "RVC_INDEX_REL" in seg, (
            f"糖糖控制台_qt.py:{node.lineno} 的 index_path 没用 RVC_INDEX_REL：{seg!r}\n"
            f"  —— 自己拼路径会在中文安装目录下静默降级成无索引")


def test_rvc_subprocesses_run_with_rvc_cwd():
    """相对路径只有在 cwd = RVC 目录时才成立，所以 cwd 必须钉住。

    两处调用都要传 cwd；漏了就变成「相对谁的相对路径」，比绝对路径还难查。
    """
    src = CONSOLE.read_text(encoding="utf-8")
    tree = _tree()
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_run_cmd"):
            continue
        text = ast.get_source_segment(src, node) or ""
        if "infer_script" not in text and "infer_cli" not in text:
            continue
        checked += 1
        assert "rvc_dir" in text, (
            f"糖糖控制台_qt.py:{node.lineno} 跑 infer_cli 时没传 cwd=rvc_dir——"
            f"索引用的是相对路径，cwd 不对就找不到")
    assert checked, "没扫到跑 infer_cli 的 _run_cmd 调用点——判据失效了"


def test_requirements_pin_a_numpy_that_rvc_can_run_on():
    """requirements 必须钉住一个 RVC 能跑的 numpy。

    2026-09-19 实测：只写 `numpy>=1.24`，新用户装到 NumPy 2.x，而 RVC 依赖链里
    `pyworld` 是 NumPy 1.x 编译的、`numba 0.56` 要求 numpy<1.24——
    结果是 `from infer.modules.vc.modules import VC` 直接 ImportError，
    **歌唱工作室在任何机器上都起不来**。

    实测能同时满足的：numpy 1.23.5（numba 0.56 要 <1.24，pyworld 要 1.x）
    + faiss-cpu 1.7.4（1.9+ 的 wheel 反过来只认 NumPy 2，会报 No module named 'numpy._core'）。
    """
    req = (BASE / "requirements.txt").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in req.splitlines() if ln.strip() and not ln.startswith("#")]
    numpy_lines = [ln for ln in lines if ln.lower().startswith("numpy")]
    assert numpy_lines, "requirements.txt 里没有 numpy"

    import re
    spec = numpy_lines[0]
    m = re.search(r"<\s*([\d.]+)", spec)
    assert m, (
        f"numpy 没有上界：{spec!r}\n"
        f"  —— 装到 2.x 会让 pyworld / numba 报二进制不兼容，歌唱工作室起不来")
    assert float(m.group(1)) <= 1.24, (
        f"numpy 上界是 {m.group(1)}，但 numba 0.56 要求 numpy<1.24：{spec!r}")

    faiss_lines = [ln for ln in lines if ln.lower().startswith("faiss")]
    assert faiss_lines, (
        "requirements.txt 里没有 faiss-cpu——RVC 的索引靠它，缺了新用户照样起不来")
    assert "1.7.4" in faiss_lines[0] or re.search(r"<\s*1\.9", faiss_lines[0]), (
        f"faiss 版本没钉在 NumPy 1.x 能用的范围：{faiss_lines[0]!r}\n"
        f"  —— 1.9+ 的 wheel 只认 NumPy 2，与 numpy<2 的钉子冲突")
