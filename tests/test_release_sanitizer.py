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
    hits = _prep.scan_sensitive(snap)
    assert not hits, (
        f"快照里有 {len(hits)} 处敏感命中，不能发布：\n  " + "\n  ".join(hits[:10]))
