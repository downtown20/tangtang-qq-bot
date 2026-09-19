"""控制台的「打开目录」按钮：缺目录时必须说得出原因。

## 这条闸门是为主人实机反馈建的（2026-09-19）

他在笔记本上装的是发布包，点控制台 → 曲库设置 → 「模型」按钮，弹出一句

    目录不存在
    找不到目录：<项目根>/Retrieval-based-Voice-Conversion-WebUI/assets/weights

然后就只能反过来问我：「我选的模式是基础的文字对话，是基础的文字对话没有这个
RVC 翻唱功能嘛？」——**用户拿到一个路径，不知道为什么会缺，也不知道下一步做什么。**

查下来比问题本身更糟：那个目录在发布包里**对所有模式都不存在**（RVC 组件、
venv_demucs、uvr5_models 一个都没随包），安装器里「唱歌」那一项给的只是 41 首
预录成品。所以「点一下给个提示」不够——得**事前**就说清这块要额外组件。

## 口径

不测「文案长什么样」（那是同义反复），测**结构性的事实**：
控制台会去打开的每个目录，只要在发布形态下不存在，调用点就必须带 `feature=`，
而那个键必须在 `_MISSING_DIR_HINT` 里查得到解释。
"""

import ast
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
CONSOLE = BASE / "糖糖控制台_qt.py"
SNAPSHOT = BASE.parent / "小糖糖-发布"


def _console_tree() -> ast.Module:
    return ast.parse(CONSOLE.read_text(encoding="utf-8"))


def _resolve_path(node: ast.AST) -> str | None:
    """把 `BASE / "a" / "b"` 这种字面量链解析成 "a/b"；解析不出返回 None。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
        rhs = cur.right
        if not (isinstance(rhs, ast.Constant) and isinstance(rhs.value, str)):
            return None
        parts.append(rhs.value)
        cur = cur.left
    if not (isinstance(cur, ast.Name) and cur.id == "BASE"):
        return None
    return "/".join(reversed(parts))


def _open_dir_calls() -> list[tuple[str | None, str | None, int]]:
    """(相对路径, feature 键, 行号) —— 扫出所有 _open_dir(BASE / ...) 调用点。"""
    out = []
    for node in ast.walk(_console_tree()):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_open_dir"):
            continue
        if not node.args:
            continue
        rel = _resolve_path(node.args[0])
        key = None
        for kw in node.keywords:
            if kw.arg == "feature" and isinstance(kw.value, ast.Constant):
                key = kw.value.value
        out.append((rel, key, node.lineno))
    return out


def _hint_keys() -> set[str]:
    for node in ast.walk(_console_tree()):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_MISSING_DIR_HINT" for t in node.targets):
            return {k.value for k in node.value.keys}
    raise AssertionError("糖糖控制台_qt.py 里找不到 _MISSING_DIR_HINT")


# ═══════════════════════════════════════════════════════
# 1. 结构性：发布形态下不存在的目录，必须给得出解释
# ═══════════════════════════════════════════════════════

def test_every_openable_dir_missing_in_release_has_an_explanation():
    """控制台会打开的目录里，凡发布形态下没有的，调用点都要带 `feature=`。

    这才是真正拦住这次问题的判据——「文案写得好不好」测不出来，
    「有没有给这个目录准备一句人话」可以。
    """
    if not SNAPSHOT.is_dir():
        pytest.skip("快照不存在——先跑 python tools/准备发布.py")
    keys = _hint_keys()
    bad = []
    for rel, key, line in _open_dir_calls():
        if rel is None:                       # 动态拼的路径，跳过
            continue
        if (SNAPSHOT / rel).exists():
            continue                          # 发布形态下有，用户点得到
        if key is None:
            bad.append(f"糖糖控制台_qt.py:{line} → {rel}（没带 feature=）")
        elif key not in keys:
            bad.append(f"糖糖控制台_qt.py:{line} → {rel}（feature={key!r} 在提示表里没有定义）")
    assert not bad, (
        "这些目录在发布包里不存在，用户点下去只会看到一句「找不到目录」：\n  "
        + "\n  ".join(bad)
        + "\n  —— 给调用点加 feature=，并在 _MISSING_DIR_HINT 里写明它属于哪个功能、怎么装")


def test_hint_keys_all_carry_actionable_text():
    """每条解释都要说清「属于什么功能」和「怎么才能有」，不能只描述缺了什么。"""
    tree = _console_tree()
    hints = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_MISSING_DIR_HINT" for t in node.targets):
            hints = ast.literal_eval(node.value)
    assert hints, "_MISSING_DIR_HINT 解析不出来"

    for key, (title, text) in hints.items():
        assert title and text, f"{key} 的文案是空的"
        assert len(text) >= 40, f"{key} 的说明太短，说不清来龙去脉：{text!r}"
        # 「怎么才能有」——要么给补装路径，要么明确说不需要它
        assert ("安装" in text or "不需要" in text or "随包" in text), (
            f"{key} 只说缺了什么，没说用户该怎么办：{text!r}")


def test_rvc_hint_says_plain_singing_does_not_need_it():
    """翻唱工作站的说明必须点破「点歌播放不需要它」。

    主人真正的困惑是「我是不是少了什么功能」——不把这句话写死，
    用户会以为自己的糖糖是残的。
    """
    text = CONSOLE.read_text(encoding="utf-8")
    for needle in ("不需要", "41 首预录成品"):
        assert needle in text, (
            f"翻唱组件的说明里没有「{needle}」——用户会以为自己的糖糖缺了功能")


# ═══════════════════════════════════════════════════════
# 2. 面板状态必须来自单一真相源
# ═══════════════════════════════════════════════════════

def test_studio_hint_reuses_the_pipeline_table():
    """歌唱工作室顶部状态必须用 `_PIPELINE_DOWNLOADS`，不许另抄一份。

    同一件事（缺哪些组件）有两处判定，迟早漂移——两处不一致时，
    用户会看到「组件齐备」然后一键全流程报「缺少 5 项」。
    """
    tree = _console_tree()
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_studio_availability_hint":
            fn = node
    assert fn is not None, "找不到 _studio_availability_hint"
    seg = ast.get_source_segment(CONSOLE.read_text(encoding="utf-8"), fn)
    assert "_PIPELINE_DOWNLOADS" in seg, (
        "_studio_availability_hint 没有用 _PIPELINE_DOWNLOADS——缺组件这件事有了第二个判定点")


def test_vision_key_is_labelled_by_purpose_not_vendor():
    """识图的 Key 要按「用来干什么」命名，不能按厂商命名。

    2026-09-19 主人实机反馈：**「第二个 key 显示的是千问的，改成识图 API Key，
    不要误导人」**。

    误导在哪：识图有两种方式，选「本地 MiniCPM-V」**根本不用填 Key**，
    而「千问 VL API Key」这个名字看起来像「要用识图就得先弄个千问账号」。
    厂商名不是不能出现——它得待在说明里（告诉用户去哪申请），但不该占据标题。
    """
    import re
    src = CONSOLE.read_text(encoding="utf-8")
    m = re.search(r'\("QWEN_KEY",\s*"([^"]+)"', src)
    assert m, "找不到识图 Key 的标签定义（格式变了吗？）"
    label = m.group(1)
    assert "千问" not in label, f"标签又变回厂商名了：{label!r}——它该待在说明里"
    assert "识图" in label, f"标签没说明它是干什么用的：{label!r}"
    # 环境变量名不能跟着改：用户的 .env 里就是 QWEN_KEY，改了会静默失效
    assert '"QWEN_KEY"' in src, "环境变量名被改了——既有 .env 会失效"


def test_missing_dir_message_is_a_pure_function():
    """弹窗文案要能不开窗口就断言到——放进 GUI 方法的测试迟早被跳过。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_console_probe", CONSOLE)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                    # 缺 PySide6 等依赖
        pytest.skip(f"控制台模块导入不了（{type(e).__name__}）：{e}")

    # 用不带盘符的假路径：带盘符的字面量会被发布脱敏扫描当成真泄漏
    # （test_release_sanitizer 的阳性对照就是靠这个判据），没必要为一条文案测试去动豁免表
    fake = Path("<本机没有这个目录>")

    title, text = mod._missing_dir_message(fake, "rvc")
    assert "翻唱" in title or "翻唱" in text
    assert "RVC" in text or "翻唱" in text

    title, text = mod._missing_dir_message(fake)
    assert "不存在" in title
