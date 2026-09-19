"""翻唱组件的自动安装通道（2026-09-20）。

## 为谁建的

主人的笔记本上点「模型」按钮弹「找不到目录」，追下去发现歌唱工作室**在任何机器上都
起不来**：发布包里没有 RVC 组件，而且装上了也会被 faiss×NumPy2 的 ABI 冲突挡住。
这一版把它接成可自动安装：模型走 Release 附件，demucs 环境本机现建。

## 口径

1. 解压函数**种真的攻击载荷进去**看它拒不拒（不是读代码看它「应该」会拒）
2. 「谁能装什么」这件事只允许一处定义——控制台的表与工具模块不许各写一份
"""

import ast
import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
CONSOLE = BASE / "糖糖控制台_qt.py"
TOOL = BASE / "tools" / "歌唱组件.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("singing_tool_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


# ═══════════════════════════════════════════════════════
# 1. 解压必须挡得住真的攻击载荷
# ═══════════════════════════════════════════════════════

def _zip_with(tmp_path: Path, name: str, data: bytes = b"x",
              external_attr: int = 0) -> Path:
    p = tmp_path / "t.zip"
    with zipfile.ZipFile(p, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = external_attr
        zf.writestr(info, data)
    return p


def test_extract_rejects_parent_traversal(tool, tmp_path):
    """`../` 穿越必须拒绝。

    这是从网络下载的包——中间人环境或上游 Release 被替换时，
    一个含 `../../启动/x.bat` 的小包就能一路写到目标目录之外。
    """
    zp = _zip_with(tmp_path, "../evil.txt")
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(ValueError, match="越界|越出"):
            tool.extract_zip_safely(zf, dest)
    assert not (tmp_path / "evil.txt").exists(), "文件真的被写到目标目录之外了"


def test_extract_rejects_absolute_path(tool, tmp_path):
    zp = _zip_with(tmp_path, "/abs_evil.txt")
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(ValueError, match="越界|越出"):
            tool.extract_zip_safely(zf, dest)


def test_extract_rejects_symlink_entry(tool, tmp_path):
    """符号链接条目必须拒绝——解压出来会指向别处，后续写入就跑到外面去了。"""
    zp = _zip_with(tmp_path, "link", b"../../", external_attr=0o120777 << 16)
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(ValueError, match="符号链接"):
            tool.extract_zip_safely(zf, dest)


def test_extract_allows_normal_pack(tool, tmp_path):
    """阴性对照：正常的包必须能解出来，否则闸门会被当噪音忽略掉。"""
    zp = tmp_path / "ok.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("Retrieval-based-Voice-Conversion-WebUI/tools/infer_cli.py", "x")
        zf.writestr("assets/weights/hutao.pth", "y")
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        n = tool.extract_zip_safely(zf, dest)
    assert n == 2
    assert (dest / "assets/weights/hutao.pth").is_file()


# ═══════════════════════════════════════════════════════
# 2. 「谁能装什么」只允许一处定义
# ═══════════════════════════════════════════════════════

KINDS = {"pack", "env", "pkg"}


def _console_pipeline_table() -> dict:
    tree = ast.parse(CONSOLE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_PIPELINE_DOWNLOADS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("糖糖控制台_qt.py 里找不到 _PIPELINE_DOWNLOADS")


def test_pipeline_kinds_are_all_known():
    """kind 只能是这三种之一——写错一个字母会让它悄悄走不到任何分支。"""
    table = _console_pipeline_table()
    bad = {k: v[2] for k, v in table.items() if v[2] not in KINDS}
    assert not bad, (
        f"这些条目的 kind 不认识：{bad}\n"
        f"  只允许 {sorted(KINDS)}——"
        f"pack=来自歌唱模型包 / env=本机现建 / pkg=随包自带")


def test_pack_items_match_the_installer_module():
    """控制台标成 `pack` 的项，必须就是 tools/歌唱组件.py 里那份清单。

    同一件事（模型包里有哪些文件）在两处各写一份，迟早漂移——
    而漂移的表现是「界面说还缺 X」，可下载包里根本没有 X。
    """
    tool = _load_tool()
    table = _console_pipeline_table()
    console_pack = {rel for rel, _u, kind in table.values() if kind == "pack"}
    assert console_pack, "控制台里一个 pack 项都没有——安装通道没接上？"
    assert console_pack == set(tool.PACK_ITEMS.values()), (
        f"两处对「模型包里有什么」说法不一致：\n"
        f"  控制台独有：{sorted(console_pack - set(tool.PACK_ITEMS.values()))}\n"
        f"  工具模块独有：{sorted(set(tool.PACK_ITEMS.values()) - console_pack)}")


def test_console_delegates_instead_of_duplicating():
    """控制台必须**调用**共享模块，不许自己再写一份下载/建环境逻辑。

    2026-09-20 之前它是自己写的（`_PipelineDownloadThread` 里逐项 urllib 下载）——
    那样安装器就没法复用，两条路会各自腐化。
    """
    src = CONSOLE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_download_missing_pipeline":
            fn = node
    assert fn is not None, "找不到 _download_missing_pipeline"
    seg = ast.get_source_segment(src, fn) or ""
    assert "_singing_tool()" in seg, (
        "_download_missing_pipeline 没有走 _singing_tool()——"
        "共享模块被绕过了，两份实现会漂移")
    assert "urllib.request" not in seg, (
        "_download_missing_pipeline 里又出现了自己写的下载逻辑——应该调共享模块")


def test_pack_name_is_version_agnostic():
    """模型包的文件名不许带版本号。

    它由 RVC 与 HuTao 模型决定，与糖糖的版本无关；控制台/安装器按**固定名**
    去 Release 里找（跨多条 Release 合并查表），带版本号就每次都要重传 400 M。
    """
    tool = _load_tool()
    import re
    assert not re.search(r"v?\d+\.\d+", tool.PACK_NAME), (
        f"模型包名里出现了版本号：{tool.PACK_NAME!r}——它应当跨版本复用")
    assert tool.PACK_NAME.endswith(".zip")


# ═══════════════════════════════════════════════════════
# 1b. 端到端：用**真产物的布局**装一遍，缺件判据必须清零
# ═══════════════════════════════════════════════════════

def _packer_module():
    spec = importlib.util.spec_from_file_location(
        "packer_under_test", BASE / "tools" / "打包歌唱模型包.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pack_layout_matches_where_it_is_extracted(tool, tmp_path, monkeypatch):
    """模型包解完之后，缺件判据必须为空——**用真产物的布局，不是自己编的**。

    ## 这条是为一个真事故建的（2026-09-20 复核时抓出）

    真包顶层是 `assets/ configs/ infer/ tools/`（**没有** `Retrieval-based-...`
    前缀，因为 `打包歌唱模型包.py` 打的就是「一个 RVC 根」），而 `PACK_ITEMS`
    要的是带前缀的路径、`install_pack` 又解到项目根。于是：

        装完 401 MB → missing_pack_items() 一项都没满足 → 用户再点一次 → 再下 401 MB

    死循环，而且文件散在项目根污染 `tools/` 与 `assets/`。

    **当时 12 条测试全绿**——因为 `test_extract_allows_normal_pack` 的样本是
    自己编的布局（带前缀），与真产物不是同一个东西。这条闸门改成从打包脚本
    自己的 `SUBSET` 推布局，两边就再也分不开了。
    """
    import hashlib

    packer = _packer_module()
    stub = tmp_path / "stub-pack.zip"
    with zipfile.ZipFile(stub, "w") as zf:
        for rel in packer.SUBSET:
            if rel.endswith(".py"):
                zf.writestr(rel, "# stub\n")
            else:
                zf.writestr(f"{rel}/.keep", "x")

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(tool, "pack_asset", lambda: {
        "url": stub.as_uri(),
        "digest": "sha256:" + hashlib.sha256(stub.read_bytes()).hexdigest(),
        "size": stub.stat().st_size,
    })

    assert tool.missing_pack_items(root), "造的场景本来就该是缺的，否则这条什么都没测"

    ok = tool.install_pack(root, log=lambda *_: None)
    assert ok is True, "install_pack 自己就返回了失败"
    assert tool.missing_pack_items(root) == [], (
        "装完仍缺——包内布局与安装判据对不上。这正是 2026-09-20 出过的那个事故：\n"
        f"  包顶层的顶层名 = {sorted({r.split('/')[0] for r in packer.SUBSET})}\n"
        f"  判据要的路径   = {sorted(tool.PACK_ITEMS.values())}")

    # temp_files/ 是下载暂存目录，本来就该在项目根（打包器已把它排除出包）
    stray = sorted(p.name for p in root.iterdir()
                   if p.is_dir() and p.name not in (tool.RVC_DIRNAME, "temp_files"))
    assert not stray, (
        f"解压把东西散布到项目根了：{stray}——落点应当是 {tool.RVC_DIRNAME}/ 一个子目录")


def test_extract_rejects_entries_outside_the_whitelist(tool, tmp_path):
    """夹带项目文件的包必须被拒——这条挡的是「覆盖 + 代码执行」。

    没有白名单时，一个被替换的附件能覆盖 `agent/handler.py`、`config.yaml`、`.env`，
    还能写 `venv_demucs/Scripts/python.exe`——而那个文件下一步就会被控制台
    subprocess 执行。这些路径一个都不「穿越」，光挡 `..` 是挡不住的。
    """
    zp = tmp_path / "evil.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("infer/modules/legit.py", "ok")        # 白名单内，排在前面
        zf.writestr("agent/handler.py", "# 攻击者版本")
        zf.writestr(".env", "DEEPSEEK_API_KEY=攻击者的")
        zf.writestr("venv_demucs/Scripts/python.exe", "MZ fake")

    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(ValueError, match="白名单"):
            tool.extract_zip_safely(zf, dest, tool.PACK_ALLOWED_TOP, tool.PACK_ALLOWED_EXACT)

    for bad in ("agent/handler.py", ".env", "venv_demucs/Scripts/python.exe"):
        assert not (dest / bad).exists(), f"{bad} 被写进去了"
    assert not (dest / "infer/modules/legit.py").exists(), (
        "先全量校验再落盘——被拒的包不该有任何一个条目落盘")


def test_extract_rejects_compression_bomb(tool, tmp_path):
    """高压缩比的包必须被拒——2026-09-20 复核实测放大 1028 倍可通过。

    300 MB 全零在 zip 里只有 299 KB。没有总量闸门时，一个这样的小包
    能在 0.4 秒内铺满磁盘。
    """
    zp = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("assets/big.bin", b"\x00" * (8 * 1024 * 1024))
    dest = tmp_path / "out"
    dest.mkdir()
    with zipfile.ZipFile(zp) as zf:
        with pytest.raises(ValueError, match="上限|压缩炸弹"):
            tool.extract_zip_safely(zf, dest, tool.PACK_ALLOWED_TOP, tool.PACK_ALLOWED_EXACT,
                                    max_total=1024 * 1024, max_entry=1024 * 1024)
    assert not (dest / "assets/big.bin").exists()


def test_demucs_half_installed_is_not_treated_as_ready(tool, tmp_path):
    """半装成的 demucs 环境不许判成「已就绪」。

    venv 的 python.exe 在 `python -m venv` 那一步就落盘了，pip 失败它照样在。
    只看解释器会把半成品当成品，而且**再也不会重跑 pip**——2026-09-20 实测
    第二次调用 0 个子进程直接返回 True，用户只能自己想到去删 venv_demucs。
    """
    root = tmp_path / "proj"
    (root / "venv_demucs" / "Scripts").mkdir(parents=True)
    (root / "venv_demucs" / "Scripts" / "python.exe").write_bytes(b"MZ")

    assert tool.demucs_ready(root) is False, (
        "只有解释器、没有哨兵，却判成了就绪——哨兵（DEMUCS_SENTINEL）不能省")

    (root / tool.DEMUCS_SENTINEL).write_text("ok\n", encoding="utf-8")
    assert tool.demucs_ready(root) is True


def test_referenced_tools_are_whitelisted():
    """随包代码引用的 tools/ 脚本，必须在发布白名单里。

    2026-09-20 实际漏过一次：新增的 `tools/歌唱组件.py` 没进 `TOOLS_KEEP_NAMES`，
    于是它**没进快照**——而控制台会按路径去调它。用户在「一键全流程」里点
    「自动安装」会拿到 FileNotFoundError，全程没有任何提示说「这个工具没随包发」。

    `tools/` 是白名单制（默认全剔），所以「新加一个工具」和「让它随包发」
    是两件事——不写闸门就只能靠人记得。
    """
    import re
    whitelist = set()
    src = (BASE / "tools" / "准备发布.py").read_text(encoding="utf-8")
    m = re.search(r"TOOLS_KEEP_NAMES = \{(.*?)\n\}", src, re.S)
    assert m, "解析不到 TOOLS_KEEP_NAMES"
    whitelist = set(re.findall(r'"([^"]+\.py)"', m.group(1)))
    assert whitelist, "TOOLS_KEEP_NAMES 里一个 .py 都没有？"

    # 随包代码里出现的 `BASE / "tools" / "某脚本.py"` 引用。
    # ⚠ 必须带上 BASE：`rvc_dir / "tools" / "infer_cli.py"` 指的是 RVC 自带的
    #   tools 目录，不是本项目的——不限定前缀会把它误报进来。
    referenced = set()
    shipped = [CONSOLE, BASE / "main.py", BASE / "tools" / "安装糖糖.py"]
    shipped += sorted((BASE / "agent").glob("*.py"))
    for f in shipped:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for name in re.findall(r'BASE\s*/\s*"tools"\s*/\s*"([^"]+\.py)"', text):
            referenced.add(name)
    assert referenced, "没扫到任何 tools/ 引用——判据失效了？"

    missing = sorted(referenced - whitelist)
    assert not missing, (
        f"这些脚本被随包代码引用、却不在发布白名单里（不会进快照）：{missing}\n"
        f"  —— 用户点下去会 FileNotFoundError。把它们加进 tools/准备发布.py 的 "
        f"TOOLS_KEEP_NAMES")


# 控制台会调、但**不属于**歌唱流水线依赖的脚本。缺了它们不影响一键全流程，
# 所以不放进 _PIPELINE_DOWNLOADS（那张表是「开跑前自检」用的）。
# 登记是必需的——不登记就没人替「它到底算不算依赖」做决定。
_NOT_PIPELINE_DEPS = {
    "同步记忆.py",           # 记忆打包/解包（菜单里单独的入口）
    "标注贴图情绪.py",       # 贴图情绪标注
    "检查SnowLuma更新.py",   # SnowLuma 版本检查
    "歌唱组件.py",           # 它本身就是缺件的**修复者**，不是被检查的对象
}


def test_console_referenced_tools_are_pipeline_tracked():
    """控制台会调的每个 tools/ 脚本，要么在缺件表里，要么在登记过的白名单里。

    2026-09-20 的实例：`denoise.py` / `trim_silence.py` 被「一键全流程」调用，
    却**既不在发布白名单里、也不在缺件表里**——用户走到第 3 步才炸，
    而不是开跑前就被告知缺件。两个洞各堵一个：白名单由
    `test_referenced_tools_are_whitelisted` 管，这张表由本条管。
    """
    import re
    text = CONSOLE.read_text(encoding="utf-8")
    referenced = set(re.findall(r'BASE\s*/\s*"tools"\s*/\s*"([^"]+\.py)"', text))
    assert referenced, "没扫到控制台里的 tools/ 引用——判据失效了？"

    tracked = {rel.split("/", 1)[1] for rel, _u, _k in _console_pipeline_table().values()
               if rel.startswith("tools/")}
    untracked = sorted(referenced - tracked - _NOT_PIPELINE_DEPS)
    assert not untracked, (
        f"这些脚本控制台会调、却不在缺件表里：{untracked}\n"
        f"  —— 用户会在流水线跑到一半才失败，而不是开跑前看到「缺什么」。\n"
        f"  加进 _PIPELINE_DOWNLOADS，或登记到 _NOT_PIPELINE_DEPS 并写明为什么不算依赖")


def test_pipeline_subprocesses_use_sys_executable():
    """跑子脚本必须用 `sys.executable`，不许写裸 `"python"`。

    裸 `"python"` 走 PATH——而这台机器上可能有多个 Python（项目自己的启动脚本
    `启动控制台.bat` 就专门解析 3.10，因为「机器上有 3.14 时 `py` 默认指向新版本」）。
    子脚本跑在别的解释器上，缺依赖的样子是「它自己报 ImportError」，
    而父进程只看到一个非零退出码。

    2026-09-20 修掉的两处（denoise / trim_silence）就是这么写的。
    """
    import re
    text = CONSOLE.read_text(encoding="utf-8")
    bad = re.findall(r'\[\s*"python"\s*,', text)
    assert not bad, (
        f"控制台里有 {len(bad)} 处用裸 \"python\" 起子进程——改用 sys.executable。\n"
        f"  它会走 PATH，可能落到另一个 Python 上")


def test_demucs_env_is_created_not_shipped():
    """demucs 环境必须是**本机现建**，不能随包发。

    venv 的 `pyvenv.cfg` 里记的是建它那台机器的 Python **绝对路径**（`home` 那一行）。
    发到别人机器上那个路径不存在，venv 直接失效——而失效的样子是
    「python.exe 一闪而过」，很难查。
    """
    tool = _load_tool()
    import inspect
    src = inspect.getsource(tool.install_demucs_env)
    assert "-m" in src and "venv" in src, (
        "install_demucs_env 没有创建虚拟环境——它是不是改成解压随包的 venv 了？")
