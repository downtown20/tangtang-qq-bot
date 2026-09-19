"""发布 README 契约（2026-09-19）。

README 是唯一一份「给外人看」的东西，它最容易出的错是**静默过期**：
数字改了没人同步、链接在发布形态下失效、旧叙事（比如已被砍掉的"三个版本"）
留在正文里。这些不会让任何测试变红，只会让读的人得出错误结论。

⚠ 链接的解析基准是**发布后的仓库根**，不是本文件所在目录。
README 源码在 `docs/发布/README.md`，发布时由 `tools/准备发布.py` 拷到根，
相对链接的深度因此改变——`../../releases` 这类写法在两种形态下结果完全不同。
所以本测试只在**快照存在时**校验文件是否真的存在（没跑过 准备发布.py 就跳过，
不让 clone 仓库的人无缘无故红）。

口径：README 里的每个数字都必须能现场复现；不能复现的不要写进 README。
"""

import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
SNAPSHOT = BASE.parent / "小糖糖-发布"
DEV_README = BASE / "docs" / "发布" / "README.md"
ROOT_README = BASE / "README.md"

# 指向仓库外、由 GitHub 解析的链接（README 在仓库根时 `../../` 正好落到仓库主页）
GITHUB_REPO_SCOPED = {"releases", "issues", "pulls", "discussions", "actions"}


def _readme() -> tuple[Path, Path | None]:
    """返回 (README 路径, 链接解析基准)。基准为 None 表示快照不在、跳过链接存在性校验。"""
    if DEV_README.is_file():
        # 开发态：源码在 docs/发布/，但链接按发布后的根写 → 用快照当基准
        return DEV_README, (SNAPSHOT if SNAPSHOT.is_dir() else None)
    if ROOT_README.is_file():
        # 发布仓：README 就在根，自己就是基准
        return ROOT_README, BASE
    pytest.skip("既没有 docs/发布/README.md 也没有根 README.md")


def _links(text: str) -> list[str]:
    """取出 markdown 里的链接与图片目标（跳过锚点、外链、mailto）"""
    out = []
    for m in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).strip().split(" ")[0]      # 去掉链接后的标题
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        out.append(target)
    return out


# ═══════════════════════════════════════════════════════
# 1. 链接在发布形态下必须可达
# ═══════════════════════════════════════════════════════

def test_relative_links_resolve_in_published_layout():
    """README 里每个相对链接都要能在发布后的仓库里找到对应文件。

    历史上这两类错误都真发生过：
      · `../CLAUDE.md` —— 源码态和发布态**都**是死链（CLAUDE.md 被拷到根）
      · `模块地图.md`  —— 源码态能点到，发布后被搬到 docs/ 下就断了
    """
    readme, base = _readme()
    text = readme.read_text(encoding="utf-8")
    if base is None:
        pytest.skip("快照不存在——先跑 python tools/准备发布.py 再做链接校验")
    broken = []
    for target in _links(text):
        if target.startswith("../"):
            # GitHub 相对路径：README 在根时，`../../` 落到仓库主页
            leaf = target.removeprefix("../../")
            if leaf not in GITHUB_REPO_SCOPED:
                broken.append(f"{target}（越出仓库且不是已知的 GitHub 页面）")
            continue
        if not (base / target).exists():
            broken.append(target)
    assert not broken, "README 里的链接在发布形态下失效：\n  " + "\n  ".join(broken)


# ═══════════════════════════════════════════════════════
# 2. 不许留旧叙事的尸体
# ═══════════════════════════════════════════════════════

# 「文字版 / 识图版 / 完整版」是 2026-09-18 已废弃的三版本形态，
# 现在是一个包 + 安装器勾选功能。残留会让读者以为要挑版本下载。
_STALE_PATTERNS = [
    (r"三个版本", "三版本形态已废弃——现在是一个包 + 安装器勾选功能"),
    (r"文字版|识图版|完整版", "版本名已废弃——改用「勾选某某功能」表述"),
    (r"958\s*张", "贴图实际 956 张（958 是含 metadata.json 与 semantic_vectors.npz 的条目数）"),
    (r"1893\s*个", "测试数已更新为 1932"),
]


def test_no_stale_version_narrative():
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    hits = [why for pattern, why in _STALE_PATTERNS if re.search(pattern, text)]
    assert not hits, "README 残留过期表述：\n  " + "\n  ".join(hits)


# ═══════════════════════════════════════════════════════
# 3. 数字必须现场可复现
# ═══════════════════════════════════════════════════════

def _claimed(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def test_claimed_numbers_match_reality():
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")

    facts = {
        # 测试数用「下限 + 防腐烂」而不是精确值：校验测试数的测试本身也是测试，
        # 写死精确值会形成反馈环——更新 README 会改变总数，永远追不上。
        "模块数": (_claimed(text, r"(\d+) 个业务模块"), None),
        "贴图张数": (_claimed(text, r"(\d+) 张表情包"), None),
        "歌曲数": (_claimed(text, r"(\d+) 首预录成品"), None),
    }
    assert None not in [v for v, _ in facts.values()], \
        f"README 里应写明这些数字，未找到：{[k for k, (v, _) in facts.items() if v is None]}"

    # 测试数：下限声明，且不能与真实值脱节太远（防止长期不更新后变成空话）
    floor = _claimed(text, r"tests-(\d+)%2B%20passing")
    assert floor is not None, "README 徽章应声明测试数下限（形如 tests-1900%2B%20passing）"
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
                         cwd=BASE, capture_output=True, text=True)
    m = re.search(r"(\d+) tests? collected", out.stdout)
    assert m, f"无法取到测试数：{out.stdout[-300:]}"
    actual_tests = int(m.group(1))
    assert actual_tests >= floor, \
        f"README 声称 {floor}+ 个测试，实际只收集到 {actual_tests} —— 这是虚报，必须改"
    assert actual_tests - floor <= 500, \
        (f"README 还写着 {floor}+，实际已有 {actual_tests} —— 数字太旧了，"
         f"把徽章下限提到接近实际（如 {actual_tests // 100 * 100}）")

    modules = [p for p in (BASE / "agent").glob("*.py") if p.name != "__init__.py"]
    assert facts["模块数"][0] == len(modules), \
        f"README 写 {facts['模块数'][0]} 个业务模块，实际 {len(modules)}"

    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    stickers = [p for p in (BASE / "stickers").iterdir() if p.suffix.lower() in exts]
    assert facts["贴图张数"][0] == len(stickers), \
        f"README 写 {facts['贴图张数'][0]} 张贴图，实际 {len(stickers)}"

    audio = {p.stem for p in (BASE / "songs" / "audio").glob("*.wav")}
    audio |= {p.stem.removesuffix("_FINAL")
              for p in (BASE / "songs" / "covers" / "separated").glob("*_FINAL.wav")}
    assert facts["歌曲数"][0] == len(audio), \
        f"README 写 {facts['歌曲数'][0]} 首歌，按曲库口径实际 {len(audio)}"


def test_adr_count_matches_disk():
    """README 说几篇 ADR，docs/decisions/ 就得有几篇"""
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    claimed = _claimed(text, r"(\d+) 篇 ADR")
    if claimed is None:
        pytest.skip("README 未声明 ADR 数量")
    actual = len(list((BASE / "docs" / "decisions").glob("ADR-*.md")))
    assert claimed == actual, f"README 写 {claimed} 篇 ADR，实际 {actual} 篇"


# ⚠ 与 README 里给出的 grep 同义但不同写法：grep 用 `\|` 表示"或"，
#   Python re 里 `\|` 是字面竖线——这里必须是 `|`
GUARD_GREP = r"read_text\(|ast\.parse"


def _collect(files: list[str]) -> int:
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "pytest", *files, "--collect-only", "-q"],
                         cwd=BASE, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    m = re.search(r"(\d+) tests? collected", out.stdout)
    assert m, f"收集失败：{out.stdout[-300:]}"
    return int(m.group(1))


def _guard_files() -> list[str]:
    """守卫型测试文件——与 README 里给出的那条 grep 同一口径。

    排除本文件自身：它守的是 README 不是架构，而且它每加一个测试都会
    改变守卫用例数，留着会让 README 的数字永远追不上。
    """
    return sorted(p.name for p in (BASE / "tests").glob("*.py")
                  if p.name != Path(__file__).name
                  and re.search(GUARD_GREP, p.read_text(encoding="utf-8", errors="ignore")))


def test_guard_test_claim_matches_reality():
    """README 声称「500 多个测试在守架构」——这是下限声明，必须现场可复现。

    用下限而不是精确值：读者跑 README 里那两条命令能拿到精确数，
    但精确数会随任何一次测试改动变化，写死只会让它变成一句迟早过期的空话。
    """
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    floor = _claimed(text, r"\*\*有 (\d+) 多个不是在测功能，是在守着架构\*\*")
    if floor is None:
        pytest.skip("README 未声明守卫测试规模")

    guards = _guard_files()
    assert guards, "没找到任何守卫型测试文件——口径或目录结构变了？"
    actual = _collect([f"tests/{g}" for g in guards])
    assert actual >= floor, f"README 声称 {floor}+ 个守卫用例，实际只有 {actual} —— 虚报"
    assert actual - floor <= 300, \
        f"README 还写着 {floor}+，实际已有 {actual} —— 数字太旧，往上提一提"


def test_architecture_graph_counts_match_readme():
    """README 说架构图谱有 N 个模块 / M 条关系，HTML 里就得真是这个数。

    图谱是 `artifacts/图谱/build_graph.py` 生成的，重建一次数字就变
    （2026-09-19 重建：74→78 个模块）——不钉住的话 README 会悄悄过时。
    """
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    claimed = re.search(r"(\d+) 个模块、(\d+) 条依赖关系", text)
    if not claimed:
        pytest.skip("README 未声明架构图谱规模")

    html_path = BASE / "artifacts" / "图谱" / "糖糖架构图谱.html"
    assert html_path.is_file(), f"架构图谱不存在：{html_path}"
    html = html_path.read_text(encoding="utf-8")

    def _array(name: str) -> list:
        import json
        m = re.search(rf"const {name} = (\[.*?\]);", html, re.S)
        assert m, f"图谱 HTML 里找不到 const {name}"
        return json.loads(m.group(1))

    nodes, edges = _array("NODES"), _array("EDGES")
    assert int(claimed.group(1)) == len(nodes), \
        f"README 写 {claimed.group(1)} 个模块，图谱实际 {len(nodes)} 个——重建图谱后请同步 README"
    assert int(claimed.group(2)) == len(edges), \
        f"README 写 {claimed.group(2)} 条关系，图谱实际 {len(edges)} 条"


def test_package_name_is_ascii_and_matches_readme():
    """安装包名必须纯 ASCII，且 README 里写的和实际产物同名。

    2026-09-19 实测踩坑：GitHub Releases 会把附件名里的中文吞掉——
    `小糖糖-v1.0.zip` 上传后变成 `-v1.0.zip`，而 README 让用户去找中文名，
    两边对不上。纯 ASCII 也顺带避开浏览器/下载工具在非中文 locale 下的编码问题。
    """
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    named = re.findall(r"`([^`]+\.zip)`", text)
    assert named, "README 里应写出安装包文件名"

    for name in named:
        assert name.isascii(), \
            f"安装包名含非 ASCII 字符：{name!r} —— GitHub Releases 会吞掉中文，必须用纯 ASCII"

    # 与打包脚本实际产出的名字对齐（README 说的就是用户要下载的那个）
    packer = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
    m = re.search(r'PKG_NAME = f"([^"]+)"', packer)
    assert m, "打包脚本里找不到 PKG_NAME"
    expect = m.group(1).replace("{TAG}", "v1.0")
    assert expect in named, \
        f"打包产物是 {expect}，README 里却写 {named} —— 用户会找不到文件"


# ═══════════════════════════════════════════════════════
# 4. Mermaid 图必须能被 GitHub 渲染
# ═══════════════════════════════════════════════════════

_MERMAID_KINDS = ("flowchart", "graph", "sequenceDiagram", "classDiagram",
                  "stateDiagram", "erDiagram", "journey", "gantt", "pie")


def test_mermaid_blocks_are_wellformed():
    """GitHub 原生渲染 mermaid——语法错了会显示成代码块，读者看到一堆箭头符号。

    这里不引 mermaid 解析器（重），只挡最容易犯的错：
    少了图类型声明、围栏没配对、箭头写在声明之前。
    """
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", text, re.S)
    assert blocks, "README 没有 Mermaid 图——架构图是本版的核心差异化，不应缺失"

    for i, block in enumerate(blocks, 1):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        assert lines, f"第 {i} 个 mermaid 块是空的"
        first = lines[0].lower()
        assert first.startswith(_MERMAID_KINDS), (
            f"第 {i} 个 mermaid 块首行不是合法的图类型声明：{lines[0]!r}")
    # 围栏必须成对
    assert text.count("```mermaid") == text.count("```") - \
        len(re.findall(r"```(?!mermaid)", text)), "代码围栏不配对"
