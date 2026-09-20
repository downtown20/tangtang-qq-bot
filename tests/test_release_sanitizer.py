"""发布脱敏闸门（2026-09-19）。

## 这条闸门是为一次真实事故建的

v1.0/v1.1 的发布包里带着 **18 处本地绝对路径**（`D:/qq-小糖糖/...`），散在 6 个文档里。
而 `tools/准备发布.py` 每次都打印「敏感扫描：零命中」——因为那条 Windows 路径正则
有**两处笔误**，从上线起就是空转：

    WINPATH_RE = re.compile(r"[Dd]:\\\\[^\\s\\"'，。、]+")
                              ──┬──   ──┬──
                                │       └─ 字符类把反斜杠本身也排除在外，
                                │          就算匹配上也会在第一个 \\ 处截断
                                └─ 正则里 \\\\ 是「两个字面反斜杠」，
                                   而真实路径只有一个 → 永不匹配

这是**「写了基础设施却没接进业务」的变体**（CLAUDE.md 反模式 #12）：
扫描器在跑、有输出、报零命中，但它的判定逻辑从来没生效过。跑得再绿也没有意义。

## 口径

不测「正则长什么样」，测**「种一个泄漏进去，扫描器抓不抓得到」**——
测模式容易变成同义反复，测行为才拦得住下一次笔误。
"""

import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent

# 与 安装糖糖.py 加载 同步记忆.py 同一套路：中文文件名没法直接 import
_spec = importlib.util.spec_from_file_location(
    "prep_release", BASE / "tools" / "准备发布.py")
_prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prep)

LEAK = r"D:\qq-小糖糖\songs\a.wav"          # 真实泄漏的形态（反斜杠）
LEAK_FWD = "D:/qq-小糖糖/songs/a.wav"       # 同类，正斜杠写法
LEAK_LOWER = "d:\\qq-小糖糖\\tools\\x.py"   # 小写盘符


# ═══════════════════════════════════════════════════════
# 1. 行为测试：种一个泄漏，扫描器必须报出来
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("leak", [LEAK, LEAK_FWD, LEAK_LOWER])
def test_sanitizer_catches_planted_leak(tmp_path: Path, leak: str):
    """这是本文件最重要的一条——它就是当初漏掉的那一步。"""
    (tmp_path / "doc.md").write_bytes(f"命令行示例：\n  cd {leak}\n".encode("utf-8"))
    hits = _prep.scan_sensitive(tmp_path)
    assert hits, (
        f"敏感扫描漏掉了本地路径 {leak!r}——"
        f"WINPATH_RE 又坏了吗？（当前模式：{_prep.WINPATH_RE.pattern!r}）")


def test_sanitizer_catches_leak_hidden_in_binary(tmp_path: Path):
    """二进制文件里藏的路径也要抓到。

    这是第二次「扫描器形同虚设」：原实现按后缀白名单过滤 7 种扩展名，
    等于宣布「不在名单里的类型不可能有敏感信息」——而 .knowledge_index.sqlite3
    里就躺着 19 处本机路径（清源文件时漏掉的旧索引副本），随包发了出去。
    """
    blob = (b"SQLite format 3\x00" + b"\x00\x11\x22 d:\\qq-" + "小糖糖".encode()
            + b"\\knowledge\x00\x00 garbage \xff\xfe")
    (tmp_path / "index.sqlite3").write_bytes(blob)
    hits = _prep.scan_sensitive(tmp_path)
    assert hits, "二进制文件里的本机路径没被扫到——后缀过滤又开天窗了"
    assert any("二进制" in h for h in hits), f"应报成二进制命中，实际：{hits}"


def test_text_suffix_list_covers_known_text_types():
    """纯文本不能因为后缀冷门就被跳过。

    原名单只有 .py/.md/.yaml/.yml/.txt/.json/.bat 七种，
    而包里还发着 .j2（Jinja2 模板）、.html、.svg、.example、.gitignore——全是纯文本。
    """
    text_types = _prep.TEXT_SUFFIXES | _prep.TEXT_NAMES
    for s in (".py", ".md", ".j2", ".html", ".svg", ".example",
              ".gitignore", ".toml", ".cfg", ".sh"):
        assert s in text_types, f"{s} 不在文本扫描范围——纯文本被当二进制跳过了"


@pytest.mark.parametrize("planted", [
    "SNOWLUMA_TOKEN=SYNTHETICsampleTOKEN42",
    "NAPCAT_TOKEN=SYNTHETICsampleTOKEN42",
    "VOLC_TOKEN=abcdef1234567890",
    # 带引号的写法同样要命中（2026-09-20 审查补：原模式只认不带引号的，
    # 而 `.env` 里写成 `X_TOKEN="…"` 是完全合法的——「判据绑死形状」的又一例）
    'SNOWLUMA_TOKEN="SYNTHETICsampleTOKEN42"',
    "SNOWLUMA_TOKEN='SYNTHETICsampleTOKEN42'",
])
def test_sanitizer_catches_planted_token_assignment(tmp_path: Path, planted: str):
    """明文的 `XXX_TOKEN=真值` 必须拦得住。

    这是反模式 #30 的又一例：原规则写死成 `"NAPCAT_TOKEN" + "=8"`，只认**那一个
    具体赋值**。2026-09-20 把 `.env.example` 的键名改成 `SNOWLUMA_TOKEN` 之后，
    它就从「能挡住一个已知泄漏」变成「永远不可能命中」——而且不会报错。
    """
    (tmp_path / ".env.example").write_bytes(f"# 注释\n{planted}\n".encode("utf-8"))
    assert _prep.scan_sensitive(tmp_path), (
        f"明文 token 赋值漏掉了：{planted!r}——_TOK_ASSIGN 又绑死到某个字面量上了吗？")


def test_sanitizer_ignores_empty_and_placeholder_tokens(tmp_path: Path):
    """阴性对照：占位符不能被当成泄漏，否则这条检查会变成噪音然后被忽略。

    发布包里的 `.env.example` 就该长这样——空的，或者中文提示语。
    """
    (tmp_path / ".env.example").write_bytes(
        "SNOWLUMA_TOKEN=\nNAPCAT_TOKEN=请填写你的access_token\n".encode("utf-8"))
    hits = _prep.scan_sensitive(tmp_path)
    assert not hits, f"占位符被误报成泄漏：{hits}"


def test_sanitizer_catches_planted_secret(tmp_path: Path):
    """密钥与 QQ 号同样要拦得住（顺带确认那条路径没被一起改坏）。"""
    (tmp_path / "cfg.yaml").write_bytes(
        'api_key: "sk-abcdefghijklmnopqrstuvwxyz123456"\nowner: 12345678901\n'.encode("utf-8"))
    hits = _prep.scan_sensitive(tmp_path)
    assert len(hits) == 2, f"应命中 key 与 QQ 各一条，实际 {hits}"


def test_sanitizer_passes_clean_content(tmp_path: Path):
    """阴性对照：干净的文档不能误报，否则闸门会被当噪音忽略掉。

    后两条是两个**真实出现过的误报源**——盘符前加否定环视 `(?<![\\w/\\\\])` 才消掉：
      · CQ 码里的 `file:///D:/stickers/a.jpg` —— 盘符前是 `/`，那是协议格式不是个人路径
      · Python 源码里的 `returned:\\n`        —— 盘符前是字母，那是转义序列不是盘符
    没有这个环视，第一次全量扫描会报出 37 处，其中 9 处是这两类噪音。
    """
    (tmp_path / "doc.md").write_bytes(
        "相对路径写法：\n  cd <项目根目录>\n"
        "  --input_path \"songs/covers/htdemucs/PLANET/vocals.wav\"\n"
        "通用路径不算个人数据：C:\\Python310\\python.exe\n"
        "只有盘符没有路径：D:\n"
        "CQ 码格式：\"[CQ:image,file=file:///D:/stickers/a.jpg]\"\n"
        "转义序列：print(f\"returned:\\n{data}\")\n".encode("utf-8"))
    hits = _prep.scan_sensitive(tmp_path)
    assert not hits, f"干净内容被误报：{hits}"


# ═══════════════════════════════════════════════════════
# 1b. 正则在真人写的各种写法上都判得对
# ═══════════════════════════════════════════════════════

@pytest.mark.parametrize("text,should_report,why", [
    (r"D:\qq-小糖糖\songs\a.wav",       True,  "反斜杠路径"),
    ("D:/qq-小糖糖/songs/a.wav",         True,  "正斜杠路径"),
    ("cd d:\\qq-小糖糖",                 True,  "小写盘符 + cd"),
    ('--input_path "D:/qq-小糖糖/x"',    True,  "命令行参数"),
    ("[x](D:/qq-小糖糖/a.md)",           True,  "markdown 链接"),
    (r"D:\小糖糖-发布",                   True,  "发布输出目录"),
    (r"C:\Users\某个用户名\secret.docx",     True,  "★具体用户名的用户目录——真泄漏长这样"),
    ("http://127.0.0.1:3000/C:/Users/a/x", True, "URL 里的绝对路径（原来的写法会漏掉它）"),
    (r'"D:\\backslash\\escaped\\path"',  True,  "双反斜杠写法（原来的写法会漏掉它）"),
    ("file:///D:/stickers/a.jpg",       False, "CQ 码夹具——在放行表里"),
    (r"C:\Python310\python.exe",         False, "系统标准路径——在放行表里"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", False, "系统标准路径"),
    (r"C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python310\python.exe",
                                         False, "占位写法不是真用户名——必须放行"),
    ("returned:\\n",                    False, "转义序列"),
    ("D:",                               False, "只有盘符"),
])
def test_path_judgement(text: str, should_report: bool, why: str):
    """测的是**扫描器的判定**（正则 + 放行表），不是裸正则。

    只测正则会得出错误结论：`C:\\Python310` 确实匹配正则，但它在系统标准路径
    放行表里，扫描器不会报——用户关心的是「会不会报」，不是「正则匹不匹配」。
    """
    reported = [m.group() for m in _prep.WINPATH_RE.finditer(text)
                if not _prep._path_allowed(m.group())]
    assert bool(reported) == should_report, (
        f"判错了（{why}）：{text!r} 期望{'命中' if should_report else '放行'}，"
        f"实际{'命中 ' + str(reported) if reported else '放行'}")


# ═══════════════════════════════════════════════════════
# 1c. 脱敏分支必须认真实的配置键名
# ═══════════════════════════════════════════════════════

def test_safe_tar_extraction_exists_and_is_consistent():
    """`_extract_tar_safely` 在两个地方各有一份，必须实现一致。

    Python 3.10 的 `extractall()` 没有 `filter` 参数，默认完全信任压缩包内容——
    模型是从 GitHub Releases 拉的 tar.bz2，处于中间人环境或上游被替换时能写到
    目录之外（2026-09-19 安全审计列为唯一带本地代码执行潜力的面）。
    两处都要用上，且**改一处漏一处会红**。
    """
    import ast
    import textwrap

    def _body(rel: str):
        src = (BASE / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == "_extract_tar_safely":
                seg = ast.get_source_segment(src, node)
                fn = ast.parse(textwrap.dedent(seg)).body[0]
                # docstring 允许不同（各写各的上下文），比对去掉 docstring 的函数体
                fn.body = [s for s in fn.body if not (
                    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                return ast.dump(fn)
        return None

    a = _body("agent/asr.py")
    b = _body("tools/安装糖糖.py")
    assert a, "agent/asr.py 里没有 _extract_tar_safely"
    assert b, "tools/安装糖糖.py 里没有 _extract_tar_safely"
    assert a == b, "两处的安全解压实现不一致——改了一处漏了另一处"

    # 全文件只能有**一处** `tar.extractall(`——就是安全 helper 内部那一处。
    # 多出来说明有调用点绕过了 helper。
    # （`zipfile.extractall` 不用管：CPython 的 _extract_member 自带防穿越。）
    for rel in ("agent/asr.py", "tools/安装糖糖.py"):
        src = (BASE / rel).read_text(encoding="utf-8")
        n = src.count("tar.extractall(")
        assert n == 1, (
            f"{rel} 里有 {n} 处 tar.extractall( —— 应该只有 helper 内部那 1 处，"
            f"多出来的是绕过安全解压的调用点")


def test_scaffold_notes_survive_gitignore():
    """落点说明必须**真的能进仓库**，不能只躺在 zip 里。

    git 规则：**父目录被排除时，`!` 无法再包含其中的文件**。
    `.gitignore` 里原来写着 `voice_cache/` + `!voice_cache/摆放说明.txt`——
    那条否定是**惰性的**。2026-09-19 实测：voice_cache / share_images / SnowLuma
    三个落点说明**从来没进过仓库**（`git check-ignore` 证实），只有 zip 里有。
    clone 的人照 README 找落点指引，一个都找不到。
    修法是排除「目录内容」（`voice_cache/*`）而不是目录本身。
    """
    import subprocess

    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    notes = sorted(snap.rglob("摆放说明.txt"))
    assert notes, "快照里一个落点说明都没有？"
    ignored = []
    for p in notes:
        r = subprocess.run(["git", "check-ignore", "-q", p.as_posix()],
                           cwd=snap, capture_output=True)
        if r.returncode == 0:          # 0 = 被忽略
            ignored.append(p.relative_to(snap).as_posix())
    assert not ignored, (
        f"这些落点说明被 .gitignore 吃掉了，不会进仓库：{ignored}\n"
        f"  多半是写了 `目录/` —— 改成 `目录/*` 才能让 `!目录/文件` 生效")


def test_scrub_cfg_scrubs_the_real_config_key():
    """脱敏分支必须认 config.yaml 里**真实的**键名，不能绑死在字面量上。

    2026-09-19 安全审计发现的实例：改目录名 napcat→onebot 时，把
    `if "napcat" in c:` 顺手也改成了 `"onebot"`——而真实配置的键就是 `napcat`，
    这条分支从此**永不执行、静默不脱敏**。真 config 一旦把字面 token 写进去，
    就会原样发到公开仓库。安全控制最怕的就是这种「不报错但也不工作」。
    """
    cfg = {"napcat": {"access_token": "SECRET-TOKEN-SHOULD-NOT-SHIP",
                      "http_url": "http://127.0.0.1:3000"}}
    out = _prep.scrub_cfg(cfg)
    assert "SECRET-TOKEN-SHOULD-NOT-SHIP" not in str(out), f"token 没被脱敏：{out}"

    # 两个键名都得认——将来真改了配置键也不会再无声失效
    cfg2 = {"onebot": {"access_token": "ANOTHER-SECRET"}}
    assert "ANOTHER-SECRET" not in str(_prep.scrub_cfg(cfg2))


# ═══════════════════════════════════════════════════════
# 2. 两个扫描器必须同口径
# ═══════════════════════════════════════════════════════

def test_both_scanners_share_the_same_path_pattern():
    """准备发布.py 与 发布前检查.py 各有一份路径正则——两份漂移过一次，钉住。

    历史：两份都写成 `[Dd]:\\\\`，一起坏掉；修的时候也容易只修一份。
    """
    other = (BASE / "tools" / "发布前检查.py").read_text(encoding="utf-8")
    assert "WIN_RE" in other, "发布前检查.py 里找不到 WIN_RE"
    assert r"[Dd]:[\\/]" in other, \
        "发布前检查.py 的 WIN_RE 与 准备发布.py 不同口径——两份必须一致"


# ═══════════════════════════════════════════════════════
# 2b. 豁免清单是全仓唯一的白名单，不许悄悄变长
# ═══════════════════════════════════════════════════════

def test_scan_exempt_list_is_exactly_what_we_expect():
    """本文件是唯一被整体豁免的——因为它的存在意义就是装载泄漏样本。

    加豁免条目等于给扫描器开后门。这条断言不是防手滑，是防「反正扫不过，
    把它加进豁免算了」——那正是这个扫描器当初失效的方式（检查形同虚设）。
    """
    assert _prep.SCAN_EXEMPT_FILES == {"tests/test_release_sanitizer.py"}, \
        f"豁免清单变了：{_prep.SCAN_EXEMPT_FILES}——加条目请先在 docstring 里写明为什么非加不可"


# ═══════════════════════════════════════════════════════
# 3. 快照真的干净（跑过 准备发布.py 才算）
# ═══════════════════════════════════════════════════════

def test_release_snapshot_is_clean():
    """整份快照重扫一遍——零命中是发布放行的硬条件。

    没有快照就跳过：clone 主仓的人不必为了这条红。
    """
    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    hits = _prep.scan_sensitive(snap) + _prep.scan_env_values(snap)
    assert not hits, (
        f"快照里有 {len(hits)} 处敏感命中，不能发布：\n  " + "\n  ".join(hits[:10]))


def test_snapshot_has_no_sqlite_sidecar_files():
    """快照里只该有那一个 `.sqlite3`,不许有 `-wal` / `-shm` 边车。

    2026-09-19 实测：主库剔掉之后，它的两个边车没进 `KNOWLEDGE_IGNORE`,照样被
    copytree 搬进了快照。它们当时**没发出去**，但那是靠另外两道独立的规则各挡了
    一下（生成的 .gitignore + 打包器的后缀黑名单）——**一个东西要靠三道各自独立的
    规则才拦得住,说明哪一道都不是真正管着它**。哪天有人顺手改掉其中一条,剩下的
    照样沉默放行。现在由 `_build_knowledge_index` 末尾的 checkpoint 负责,这条钉住。
    """
    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    sidecars = sorted(
        p.relative_to(snap).as_posix()
        for p in snap.rglob("*")
        if p.is_file() and p.name.endswith(("-wal", "-shm", "-journal")))
    assert not sidecars, (
        f"快照里有 sqlite 边车文件：{sidecars}\n"
        f"  —— 见 tools/准备发布.py 的 _build_knowledge_index 末尾")


def test_shipped_index_matches_snapshot_sources_exactly():
    """随包的索引必须与快照里那些源文件**同源**——这条就是当初泄漏的探测器。

    ## 这条闸门是为一次真实泄漏建的

    索引里存着 `knowledge/*.md` 的**切块正文副本**。当年的顺序是：先把源文件里的
    本机路径清干净，索引才生成——不对，是反过来：**先建了索引，之后才去清源文件**。
    于是源文件干净了、索引里还留着清理前的旧文本（实测 60 处本机路径），
    随 v1.0/v1.1 发了出去。

    光看「索引里有没有敏感串」是堵不住的——那取决于扫描器认不认得那个串。
    这里换个**结构性**判据：索引里记的每篇文档 sha256，必须等于快照里那份文件
    此刻的 sha256；文档集合也必须一一对应。源文件改过而索引没重建 → 立刻红。
    """
    import hashlib
    import sqlite3
    import sys

    snap = BASE.parent / "小糖糖-发布"
    if not snap.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    idx = snap / "knowledge" / ".knowledge_index.sqlite3"
    assert idx.is_file(), (
        "快照里没有知识库索引——它现在是**要随包**的（省用户首启 6.2 秒向量计算）。\n"
        "  没生成的话：跑 python tools/准备发布.py，看 _build_knowledge_index 的报错")

    sys.path.insert(0, str(BASE))
    from agent.knowledge import discover_document_files

    kn = snap / "knowledge"
    on_disk = {}
    for f in discover_document_files(kn):
        content = f.read_text(encoding="utf-8").strip()
        if not content:
            continue
        rel = str(f.relative_to(kn)).replace("\\", "/")
        on_disk[rel] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    in_index = {p: s for p, s in con.execute("SELECT relative_path, sha256 FROM documents")}

    missing = sorted(set(on_disk) - set(in_index))
    extra = sorted(set(in_index) - set(on_disk))
    stale = sorted(p for p in set(on_disk) & set(in_index) if on_disk[p] != in_index[p])
    assert not (missing or extra or stale), (
        "随包的索引与快照源文件对不上——索引是旧的：\n"
        f"  索引里没有：{missing}\n"
        f"  索引里多出（源文件已删）：{extra}\n"
        f"  内容已变但索引未重建：{stale}\n"
        "  —— 索引存的是正文副本，不同源就等于把发布时点的旧文本一起发了出去")

    models = {m for (m,) in con.execute("SELECT DISTINCT model_name FROM embeddings")}
    junk = sorted(m for m in models if m != "BAAI/bge-small-zh-v1.5")
    assert not junk, (
        f"索引里混进了非发布模型的向量：{junk}\n"
        f"  —— 多半是拷了开发机那份（它带着 benchmark 跑的 bench-v1 向量，纯属垃圾）")


# ═══════════════════════════════════════════════════════
# 2. 豁免文件不许装「真值」
# ═══════════════════════════════════════════════════════

def test_exempt_files_never_contain_real_secrets():
    """被整体豁免的文件是为了装**格式样本**，不是为了装**真值**。

    2026-09-20 我自己踩的：往 `tests/test_release_sanitizer.py`（全仓唯一被
    `SCAN_EXEMPT_FILES` 整体跳过、又随 `tests/` 一起发布）里塞了维护者**真实的**
    SnowLuma token 当"样本"——扫描器照旧打印「零命中」，而它下次推公开仓就会出去。
    这条闸门拿 `.env` 里的真值去比对所有豁免文件。
    """
    env = BASE / ".env"
    if not env.exists():
        pytest.skip(".env 不存在（clone 出来的仓库里没有它）")

    secrets = []
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        # 太短的值会跟普通文本撞（`1`、`true` 之类），不拿它做判据
        if len(val) >= 8:
            secrets.append((key.strip(), val))
    assert secrets, "`.env` 里一个够长的值都没有——读取逻辑是不是坏了？"

    for rel in sorted(_prep.SCAN_EXEMPT_FILES):
        p = BASE / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        leaked = [k for k, v in secrets if v in text]
        assert not leaked, (
            f"{rel} 被整体豁免于脱敏扫描，却含有 .env 里的**真实值**：{leaked}\n"
            f"  豁免是为了装格式样本，不是装真值——请换成合成值（形状像、值与真值不同）。")


def test_env_value_scan_catches_a_real_value_by_shape_free_matching(tmp_path: Path):
    """按**真值**扫：不管密钥长什么样，只要真值出现在发布物里就报。

    这是 `scan_sensitive` 结构上抓不到的一类——它按**形状**认（路径/QQ/密钥样式），
    而"我的 token 原样出现在文件里"只有拿真值去比才能判定。
    2026-09-20 一天里撞了两次，两次 `scan_sensitive` 都报"零命中"：
      · 往被整体豁免的 `tests/test_release_sanitizer.py` 塞了真 token 当样本；
      · `tests/test_connect_check.py` 拿真 token 当 `mask_token()` 的输入。
    """
    env = tmp_path / ".env"
    env.write_text("SOME_KEY=Zq7-not-any-recognizable-shape-31\n", encoding="utf-8")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "doc.md").write_text("这一行里躺着 Zq7-not-any-recognizable-shape-31\n",
                                encoding="utf-8")
    hits = _prep.scan_env_values(snap, env)
    assert hits, "真值原样出现在快照里却没报出来"
    assert "SOME_KEY" in hits[0], f"没说清是哪个 key 的值：{hits}"


def test_env_value_scan_is_quiet_on_clean_content(tmp_path: Path):
    """阴性对照：干净内容不能误报，短值也不参与比对（否则会变成噪音）。"""
    env = tmp_path / ".env"
    env.write_text("LONG_KEY=Zq7-not-any-recognizable-shape-31\nSHORT=1\n",
                   encoding="utf-8")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "doc.md").write_text("这里只有 1 和一个无关的长串 abcdefghijklmnop\n",
                                 encoding="utf-8")
    assert not _prep.scan_env_values(snap, env), "干净内容被误报成泄漏"


def test_env_value_scan_never_leaks_the_value_into_its_own_report(tmp_path: Path):
    """命中报告里只许说**哪个 key**，不许回抄真值。

    否则同一个真值会被写进测试输出、CI 日志、聊天记录——比原泄漏还扩散。
    """
    env = tmp_path / ".env"
    env.write_text("AKEY=Zq7-not-any-recognizable-shape-31\n", encoding="utf-8")
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "doc.md").write_text("Zq7-not-any-recognizable-shape-31\n", encoding="utf-8")
    hits = _prep.scan_env_values(snap, env)
    assert hits
    for h in hits:
        assert "Zq7-not-any-recognizable-shape-31" not in h, (
            f"报告里回抄了真值本身：{h}")


def test_scanner_regexes_compile_cleanly_no_inline_flags():
    """扫描器的正则里不许出现**内联标志**（`(?m)` / `(?i)` 之类）。

    2026-09-20 独立复核抓到的：`_TOK_ASSIGN` 里写了 `(?m)`，而它被拼进 `KEY_RE` 的
    中间——内联标志不在表达式开头时，**Python ≥3.12 直接抛**
    `PatternError: global flags not at the start of the expression`，
    `准备发布.py` **导入即崩**；而 3.10 只发一条 DeprecationWarning，本机完全测不出来。

    这条闸门是**版本无关**的：把 DeprecationWarning 提升为错误再编译一遍——
    3.10 上它变成硬错误，3.12+ 上本来就是硬错误。
    （另外，扫描器是逐行 `search(line)` 的，每行的整串就是主题串，`^`/`$`
    天然按行生效，**根本不需要** `(?m)`。）
    """
    import re
    import warnings

    for name in ("KEY_RE", "WINPATH_RE", "QQ_RE"):
        rx = getattr(_prep, name)
        # ⚠ `re` 有内部编译缓存：同一个模式串再 compile 一次只会**返回缓存对象**，
        #   连解析都不做——警告自然不会出现。第一版闸门就是这么形同虚设的
        #   （阳性对照把 (?m) 种回去，它照样绿）。purge 掉才真的重新编译。
        re.purge()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            try:
                re.compile(rx.pattern)
            except (DeprecationWarning, re.error) as e:
                raise AssertionError(
                    f"{name} 的正则不能干净地编译：{e}\n"
                    f"  多半是片段里带了内联标志（如 `(?m)`）而它不在表达式开头——"
                    f"Python ≥3.12 会直接让 准备发布.py 导入失败。\n"
                    f"  当前模式：{rx.pattern[:160]!r}")
