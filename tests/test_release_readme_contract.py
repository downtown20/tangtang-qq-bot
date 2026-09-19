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
              for p in (BASE / "songs" / "covers" / "separated").glob("*_FINAL.*")
              if p.suffix.lower() in (".wav", ".mp3")}
    assert facts["歌曲数"][0] == len(audio), \
        f"README 写 {facts['歌曲数'][0]} 首歌，按曲库口径实际 {len(audio)}"


def _packer_tag() -> str:
    packer = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
    m = re.search(r'^TAG = "([^"]+)"', packer, re.M)
    assert m, "打包脚本里找不到 TAG"
    return m.group(1)


def test_release_tag_is_three_part():
    """版本号必须三段式 `vX.YY.ZZ`（2026-09-20 主人定）。

    v1.0~v1.5 用的两位式已弃用——那样很快就数到 2.x，而糖糖还在早期。
    新规则主号保持 0：`v0.01.00` → `v0.02.00` → …

    例外：`v1.5` 是切换前已发布的最后一条，允许留着；下一版起必须三段式。
    """
    tag = _packer_tag()
    if tag == "v1.5":
        return                                    # 切换前的终点，别再往上加
    assert re.fullmatch(r"v\d+\.\d{2}\.\d{2}", tag), (
        f"版本号格式不对：{tag!r}——应为三段式 v0.MM.PP（如 v0.01.00）。\n"
        f"  两位式（v1.6 / v2.0）已弃用，见 tools/打包发布版本.py 里 TAG 上方的规则")


def test_changelog_newest_version_matches_tag():
    """更新日志里最新那条必须就是当前 TAG——改版本号就得同步写更新日志。

    这条闸门管的是「发完版忘了写更新日志」这件小事，它每年都会发生。
    """
    log = BASE / "docs" / "发布" / "更新日志.md"
    assert log.is_file(), "找不到 docs/发布/更新日志.md"
    versions = re.findall(r"^## (v[\d.]+)", log.read_text(encoding="utf-8"), re.M)
    assert versions, "更新日志里没解析到版本小节（格式：`## vX.Y.Z（日期）`）"
    assert versions[0] == _packer_tag(), (
        f"更新日志最新一条是 {versions[0]}，而打包脚本的 TAG 是 {_packer_tag()}——"
        f"两者必须一致（发版时要往更新日志顶部加一节）")


def test_anti_pattern_count_matches_claude_md():
    """README 说「N 条错误模式」，CLAUDE.md 的反模式表就得真有 N 行。

    那张表只会往上长（每踩一次坑加一条），而 README 里的数字是手写的——
    不钉住的话，过几个月它会变成一句很体面的假话。
    （2026-09-19 实证：写这句话时表已经从 29 长到了 35。）
    """
    readme, _ = _readme()
    text = readme.read_text(encoding="utf-8")
    claimed = _claimed(text, r"(\d+) 条错误模式")
    if claimed is None:
        pytest.skip("README 未声明错误模式条数")

    claude = (BASE / "CLAUDE.md").read_text(encoding="utf-8")
    actual = len(re.findall(r"^\| (\d+) \|", claude, re.M))
    assert actual, "CLAUDE.md 里没解析到反模式表——表格格式变了？"
    assert claimed == actual, \
        f"README 写 {claimed} 条错误模式，CLAUDE.md 的表实际有 {actual} 条"

    # 发版说明里也有同一个数字，一并钉住
    note = BASE / "docs" / "发布" / "发版说明_v1.md"
    if note.is_file():
        m = re.search(r"(\d+) 条已踩过的坑", note.read_text(encoding="utf-8"))
        if m:
            assert int(m.group(1)) == actual, \
                f"发版说明写 {m.group(1)} 条已踩过的坑，CLAUDE.md 实际 {actual} 条"


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

    # 与打包脚本实际产出的名字对齐（README 说的就是用户要下载的那个）。
    # 版本号也从打包脚本读——不在这里写死，否则每次升版都要记得改第 4 处。
    packer = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
    m = re.search(r'PKG_NAME = f"([^"]+)"', packer)
    assert m, "打包脚本里找不到 PKG_NAME"
    mt = re.search(r'^TAG = "([^"]+)"', packer, re.M)
    assert mt, "打包脚本里找不到 TAG"
    expect = m.group(1).replace("{TAG}", mt.group(1))
    assert expect in named, \
        f"打包产物是 {expect}，README 里却写 {named} —— 用户会找不到文件"


# ═══════════════════════════════════════════════════════
# 3b. 所有随包发出的用户文档，链接也要在发布形态下可达
# ═══════════════════════════════════════════════════════

# 用户会读到的文档（相对快照根）。新增用户文档请登记到这里。
SHIPPED_USER_DOCS = [
    "README.md",
    "CLAUDE.md",
    "docs/用户手册/使用说明.md",
    "docs/用户手册/文件同步指南.md",
    "docs/用户手册/知识库维护.md",
    "docs/模块地图.md",
]


def test_shipped_user_docs_have_no_dead_links():
    """不只是 README——用户手册里的链接断了同样让人卡住，而且更隐蔽。

    2026-09-19 审计抓到的实例：`使用说明.md` 末尾指向
    `docs/开发规划/系统评估_20260729.md`，那份文件早已移进 `归档/`，
    而归档目录整个不在发布包里——发布版用户点它 100% 断链，工作树里却一切正常。
    """
    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")

    broken = []
    for rel in SHIPPED_USER_DOCS:
        p = snap / rel
        if not p.is_file():
            broken.append(f"{rel}（文件本身没进快照）")
            continue
        for target in _links(p.read_text(encoding="utf-8")):
            # README 在仓库根，`../../` 由 GitHub 解析成仓库主页——那几条另有规则
            if target.startswith("../../") and rel == "README.md":
                if target.removeprefix("../../") not in GITHUB_REPO_SCOPED:
                    broken.append(f"{rel} → {target}（越出仓库且不是已知 GitHub 页面）")
                continue
            if not (p.parent / target).resolve().exists():
                broken.append(f"{rel} → {target}")
    assert not broken, "随包发出的文档里有断链：\n  " + "\n  ".join(broken)


# ═══════════════════════════════════════════════════════
# 3c. 目录结构：落点目录一个不能少；两个「OneBot」必须写明身份
# ═══════════════════════════════════════════════════════

def _readme_dir_block(text: str) -> str:
    """取 README 「## 目录结构」下的那个 ```text 代码块"""
    m = re.search(r"## 目录结构\s*\n+```text\n(.*?)```", text, re.S)
    assert m, "README 里找不到「## 目录结构」下的 ```text 代码块——章节标题或围栏改了？"
    return m.group(1)


def test_readme_directory_tree_lists_every_landing_dir():
    """每个「要用户自己往里放东西」的目录，都必须在 README 的目录结构里露脸。

    判据从快照推导、不写死目录名：**带 `摆放说明.txt` 的目录 = 落点目录**。

    2026-09-19 实证：七个落点目录里，`SnowLuma/` / `share_images/` / `voice_cache/`
    三个都没进目录结构——其中 SnowLuma 偏偏是唯一一个**必须**用户手动放的。
    读者在「上手」里看到「解压到 `SnowLuma/` 下」，翻到「目录结构」却没有这一行，
    只能自己猜它该放哪、和旁边的 `onebot/` 是什么关系。
    """
    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    landing = sorted(p.parent.name for p in snap.glob("*/摆放说明.txt"))
    assert landing, "快照里一个落点说明都没有？——判据失效了，先确认 摆放说明.txt 还在生成"

    readme, _ = _readme()
    tree = _readme_dir_block(readme.read_text(encoding="utf-8"))
    listed = {ln.split("/")[0].strip() for ln in tree.splitlines() if "/" in ln}
    missing = [d for d in landing if d not in listed]
    assert not missing, (
        f"这些落点目录有摆放说明、却没进 README 的目录结构：{missing}\n"
        f"  （落点目录 = 用户得自己往里放东西的目录，读者要在这里才找得到它）")


def _directory_listing(path: Path) -> tuple[list[str], str]:
    """取该文档里「目录清单」那些行——歧义正是发生在这里。

    README 是「## 目录结构」的代码块；使用说明.md 是以 ``| `名字/` `` 开头的表格行。
    刻意**只认清单内部**：身份说明写在正文别处不算数——读者是在对着这份清单
    找「SnowLuma 该放哪」的，说明就得在他眼睛所在的那一行。
    """
    text = path.read_text(encoding="utf-8")
    if path.name == "README.md":
        return _readme_dir_block(text).splitlines(), "目录结构代码块"
    lines = [ln for ln in text.splitlines() if re.match(r"\|\s*`[^`]+/`", ln)]
    return lines, "目录表"


def test_two_onebots_are_told_apart_in_user_docs():
    """`onebot/` 和 `SnowLuma/` 必须在**目录清单里**各自写明身份。

    两个名字都含「OneBot」，一边是糖糖**自己**的代码（随包自带、用户不用管），
    一边是**第三方**程序（要用户去下载解压）——不写清楚，读者完全可能以为
    SnowLuma 该解压进 `onebot/`。这正是 2026-09-19 主人当场问出来的那个歧义：
    「目录结构里没有 SnowLuma，换成了 onebot，那 SnowLuma 到底要解压到 onebot 嘛？」

    闸门只认事实（谁是自己的 / 谁是第三方的），不锁具体措辞——
    换一种说法写清楚照样过，写不清楚就红。
    """
    docs = [(DEV_README if DEV_README.is_file() else ROOT_README, "README"),
            (BASE / "docs" / "用户手册" / "使用说明.md", "使用说明.md")]
    bad = []
    for path, label in docs:
        if not path.is_file():
            bad.append(f"{label}（文件不在，路径变了？）")
            continue
        lines, where = _directory_listing(path)
        assert lines, f"{label} 的{where}里一行都没取到——清单格式变了？"
        if not [ln for ln in lines if "onebot/" in ln and ("自己" in ln or "本项目" in ln)]:
            bad.append(f"{label} 的{where}里，没有一行说明 `onebot/` 是糖糖自己的代码"
                       f"——读者会以为它是放第三方 OneBot 实现（SnowLuma）的地方")
        if not [ln for ln in lines if "SnowLuma/" in ln and "第三方" in ln]:
            bad.append(f"{label} 的{where}里，没有一行说明 `SnowLuma/` 是第三方程序"
                       f"——读者会以为它随包自带、或该放进 onebot/")
    assert not bad, "两个「OneBot」的身份没说清：\n  " + "\n  ".join(bad)


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
