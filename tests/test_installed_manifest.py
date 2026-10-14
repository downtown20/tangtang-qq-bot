"""「装了什么」的机读记录（2026-09-20）。

## 为什么要有这份记录

主人 2026-09-20 反馈：「我在安装糖糖里选了 1、4、6 和运维的，识图和语音都没有，
但日志警告我没下载某某模型，我分不清到底是识图/语音的模型缺失，还是 1/4/6 里的缺。」

这句话点出一个结构性缺陷：**功能开关说的是"要不要用"，不是"装没装"。**
config.yaml 里有 `voice.enabled` / `llm.vision.enabled`，但对 BGE、重排序、
StructBERT 这些没有开关的组件，运行期只能"我探测不到就喊"——
所以用户看到的警告跟他勾了什么**没有任何逻辑关系**。

`agent/installed.py` + 安装器写下的 `installed_features.json` 补上这条。

## 这个文件钉住的三件事

  1. `FEATURE_NAMES` 与安装器 `FEATURES` **逐项同名同序**——
     错位会把提示指到**别的**功能上，那比不提示更坏（用户会去重装一个已经有了的功能）
  2. 安装器**真的会写**那份清单（写了没接线 = 死代码，本项目的老毛病）
  3. 「不知道」和「没装」**必须分开**——把前者当后者会静默吞掉真实故障
"""
import importlib.util
import json
import logging
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
INSTALLER = BASE / "tools" / "安装糖糖.py"


def _load_installer(tmp_path, monkeypatch):
    """导入安装器并把模块级 BASE 指到临时项目根（与 test_installer_wiring 同款）"""
    spec = importlib.util.spec_from_file_location("_installer_manifest_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "BASE", tmp_path)
    return mod


# ═══════════════════════════════════════════════════════════
# 1. 序号对齐——错位比缺失更坏
# ═══════════════════════════════════════════════════════════

def test_feature_names_match_the_installer_menu(tmp_path, monkeypatch):
    """安装器上第 N 项叫什么，`agent/installed.py` 就得叫什么。

    这两份列表描述的是同一样东西（用户在安装器里勾的那几行），
    错位会让 `missing_note` 说出「它属于『5. 图形控制台』」这种不着边际的话。
    """
    from agent import installed
    mod = _load_installer(tmp_path, monkeypatch)
    names = [f[0] for f in mod.FEATURES]
    assert names == installed.FEATURE_NAMES, (
        "安装器的 FEATURES 与 agent/installed.py 的 FEATURE_NAMES 不一致：\n"
        f"  安装器 : {names}\n"
        f"  installed: {installed.FEATURE_NAMES}")


def test_feature_constants_point_at_the_right_slots():
    """具名常量与列表下标对齐（裸下标跨文件最容易错位）。"""
    from agent import installed
    assert installed.FEATURE_NAMES[installed.FEAT_MEMORY] == "聊天+高质量记忆"
    assert installed.FEATURE_NAMES[installed.FEAT_VOICE] == "发语音+听懂语音消息"
    assert installed.FEATURE_NAMES[installed.FEAT_VISION] == "本地识图"
    assert installed.FEATURE_NAMES[installed.FEAT_SING] == "唱歌"
    assert installed.FEATURE_NAMES[installed.FEAT_DOC] == "读文档"
    assert installed.FEATURE_NAMES[installed.FEAT_CONSOLE] == "图形控制台"


# ═══════════════════════════════════════════════════════════
# 2. 安装器真的会写（写了没接线 = 死代码）
# ═══════════════════════════════════════════════════════════

def test_installer_writes_the_manifest(tmp_path, monkeypatch):
    from agent import installed
    mod = _load_installer(tmp_path, monkeypatch)
    mod.write_manifest([0, 3, 5, 7], dry_run=False)
    path = tmp_path / installed.MANIFEST_NAME
    assert path.is_file(), "安装器没写安装清单——运行期只能靠猜"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["choices"] == [0, 3, 5, 7]
    assert data["features"] == ["聊天+高质量记忆", "唱歌", "图形控制台", "运维工具"]


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    from agent import installed
    mod = _load_installer(tmp_path, monkeypatch)
    mod.write_manifest([0], dry_run=True)
    assert not (tmp_path / installed.MANIFEST_NAME).exists(), "--dry-run 不该落盘"


def test_manifest_write_failure_does_not_break_install(tmp_path, monkeypatch, capsys):
    """写不进去（磁盘满/权限）不能中断安装——没有它只是提示说不清，比装不上强。"""
    from agent import installed
    mod = _load_installer(tmp_path, monkeypatch)

    def _boom(*a, **kw):
        raise OSError("模拟磁盘满")
    monkeypatch.setattr(Path, "write_text", _boom, raising=False)
    mod.write_manifest([0, 3], dry_run=False)   # 不应抛
    assert "[!]" in capsys.readouterr().out


def test_installer_calls_write_manifest_in_the_install_flow():
    """光有函数不算接线——必须在 cmd_install 里被调用。"""
    src = INSTALLER.read_text(encoding="utf-8")
    assert "write_manifest(choices, dry_run)" in src, (
        "write_manifest 定义了但没接进 cmd_install——那份清单永远不会存在")


# ═══════════════════════════════════════════════════════════
# 3. 「不知道」≠「没装」
# ═══════════════════════════════════════════════════════════

def _write_manifest(tmp_path, payload):
    (tmp_path / "installed_features.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_absent_manifest_means_unknown_not_absent(tmp_path):
    """没有清单文件 = 不知道（老安装 / 开发机）。**不是"没装"。**

    把「不知道」当「没装」会静默吞掉真实故障——而那正是这个模块要解决的那类问题。
    """
    from agent import installed
    installed.load(tmp_path, refresh=True)          # 清缓存
    assert installed.choices(tmp_path) is None
    assert installed.has(installed.FEAT_VOICE, tmp_path) is None
    assert installed.should_warn(installed.FEAT_VOICE, tmp_path) is True


def test_corrupt_manifest_also_means_unknown(tmp_path):
    """清单坏了也得退回「不知道」，不能崩、也不能猜。"""
    from agent import installed
    (tmp_path / "installed_features.json").write_text("{ 这不是 json", encoding="utf-8")
    installed.load(tmp_path, refresh=True)
    assert installed.choices(tmp_path) is None


def test_manifest_with_wrong_shape_means_unknown(tmp_path):
    from agent import installed
    _write_manifest(tmp_path, {"version": 1, "features": ["x"]})   # 缺 choices
    installed.load(tmp_path, refresh=True)
    assert installed.choices(tmp_path) is None


# ═══════════════════════════════════════════════════════════
# 4. 按清单说话的三种说法
# ═══════════════════════════════════════════════════════════

def test_not_installed_feature_is_never_logged(tmp_path):
    """**没勾过的功能一个字都不打**——这就是主人抱怨的那堆噪音的来源。"""
    from agent import installed
    _write_manifest(tmp_path, {"version": 1, "choices": [0, 3, 5], "features": []})
    installed.load(tmp_path, refresh=True)

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("糖糖.测试")
    cap = _Cap()
    log.addHandler(cap)
    try:
        logged = installed.log_missing(log, installed.FEAT_VOICE, "语音识别模型",
                                       root=tmp_path)
    finally:
        log.removeHandler(cap)
    assert logged is False, "没装的语音功能却报了警"
    assert lines == []


def test_installed_but_missing_is_a_warning_with_the_fix(tmp_path):
    """装过但没就位 → WARNING，而且要说清"重跑安装器可续传"。"""
    from agent import installed
    _write_manifest(tmp_path, {"version": 1, "choices": [0, 1], "features": []})
    installed.load(tmp_path, refresh=True)

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append((record.levelname, record.getMessage()))

    log = logging.getLogger("糖糖.测试")
    cap = _Cap()
    log.addHandler(cap)
    try:
        installed.log_missing(log, installed.FEAT_VOICE, "语音识别模型", root=tmp_path)
    finally:
        log.removeHandler(cap)
    assert lines and lines[0][0] == "WARNING"
    msg = lines[0][1]
    assert "语音识别模型" in msg
    assert "装过" in msg and "安装糖糖.bat" in msg, f"没说怎么补：{msg}"


def test_unknown_state_says_so_without_crying_wolf(tmp_path):
    """读不到清单 → INFO，说清「不确定」，但不摆出故障的架势。"""
    from agent import installed
    installed.load(tmp_path, refresh=True)

    lines = []

    class _Cap(logging.Handler):
        def emit(self, record):
            lines.append((record.levelname, record.getMessage()))

    log = logging.getLogger("糖糖.测试")
    log.setLevel(logging.DEBUG)   # 默认 root 是 WARNING，INFO 会被丢掉
    cap = _Cap()
    log.addHandler(cap)
    try:
        installed.log_missing(log, installed.FEAT_MEMORY, "BGE 语义向量模型", root=tmp_path)
    finally:
        log.removeHandler(cap)
    assert lines and lines[0][0] == "INFO", lines
    assert "无法判断" in lines[0][1]


def test_note_never_tells_the_user_to_run_a_shell_command(tmp_path):
    """提示里**不许**再出现 `python -c "from modelscope import ..."` 那种东西。

    2026-09-20 之前 BGE / Reranker 两条就是这么写的：把一条相对 cwd 的
    pip/modelscope 命令甩给终端用户，他不知道在哪个目录、哪个解释器跑。
    正确的下一跳是"重跑安装糖糖.bat"。
    """
    from agent import installed
    for state in ({"version": 1, "choices": [0], "features": []}, None):
        if state is None:
            (tmp_path / "installed_features.json").unlink(missing_ok=True)
        else:
            _write_manifest(tmp_path, state)
        installed.load(tmp_path, refresh=True)
        for idx, what in ((installed.FEAT_MEMORY, "BGE 语义向量模型"),
                          (installed.FEAT_VOICE, "语音识别（sherpa-onnx 依赖）")):
            note = installed.missing_note(idx, what, root=tmp_path)
            assert "python -c" not in note
            assert "modelscope" not in note
            assert "pip install" not in note


@pytest.mark.parametrize("choices,expect", [
    ([0, 3, 5, 7], "1. 聊天+高质量记忆、4. 唱歌、6. 图形控制台、8. 运维工具"),
    ([0, 1], "1. 聊天+高质量记忆、2. 发语音+听懂语音消息"),
])
def test_installed_summary_is_human_readable(tmp_path, choices, expect):
    """摘要要能**逐项报出安装器菜单上的编号+名字**，而不是一串下标。

    这条原先是 `assert expect or True`——恒真，等于什么都没断言
    （Reality Check D6 抓到的）。现在断言真实内容。
    """
    from agent import installed
    _write_manifest(tmp_path, {"version": 1, "choices": choices, "features": []})
    installed.load(tmp_path, refresh=True)
    summary = installed.installed_summary(tmp_path)
    assert summary == "安装时勾了：" + expect


def test_installed_summary_says_unknown_when_no_manifest(tmp_path):
    from agent import installed
    installed.load(tmp_path, refresh=True)
    assert "读不到安装清单" in installed.installed_summary(tmp_path)


def test_cache_is_keyed_by_path_not_only_mtime(tmp_path):
    """缓存必须带**路径**维度。

    只按 mtime 缓存的话，两个不同目录的清单只要 mtime 相同就会互相串味——
    测试里用 tmp_path 极易撞上，而撞上时的表现是**假绿**：
    断言通过了，但它验的不是自己想验的那份文件。（Reality Check D4 实测）
    """
    import os
    from agent import installed
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _write_manifest(a, {"version": 1, "choices": [5], "features": []})
    _write_manifest(b, {"version": 1, "choices": [7], "features": []})
    stamp = 1_700_000_000
    for p in (a / "installed_features.json", b / "installed_features.json"):
        os.utime(p, (stamp, stamp))      # 强制 mtime 相同
    installed.load(a, refresh=True)
    assert installed.choices(a) == [5]
    assert installed.choices(b) == [7], "两个不同目录的清单串味了——缓存键缺 path"
