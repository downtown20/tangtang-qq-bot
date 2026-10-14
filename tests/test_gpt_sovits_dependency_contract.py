# -*- coding: utf-8 -*-
"""GPT-SoVITS 的依赖必须被声明（2026-10-15 T55 清理的防复删闸门）。

## 为什么有这条闸门

2026-10-15 清理「翻唱制作链」（CosyVoice3 / RVC / DiffSinger / 歌唱工作室 / demucs / UVR5）时，
`librosa` 与 `pypinyin` 差点跟着一起被删掉——它们原先挂在 requirements.txt 的
「L4 唱歌辅助」组里，看起来就像 RVC 的残留。

实际上它们是 **GPT-SoVITS 的硬依赖**，而且是最难事后发现的那一类：

  · `pypinyin`  → gpt-sovits/GPT_SoVITS/text/（chinese.py / chinese2.py / tone_sandhi.py /
                  text/g2pw/g2pw.py …），12 处 `from pypinyin import ...`
  · `librosa`   → gpt-sovits/GPT_SoVITS/TTS_infer_pack/TTS.py、GPT_SoVITS/utils.py、
                  GPT_SoVITS/module/mel_processing.py …
  · `soundfile` / `matplotlib` / `jieba-fast` 同理（见 GPT_SOVITS_DEPENDENCIES）

GPT-SoVITS 由 agent/service_manager.py 用 `sys.executable` 启动——**与主程序同一个解释器**，
不是独立 venv。所以删掉声明在本机不会立刻报错（开发机早就装过了），只会在**新机器**上
表现成「GPT-SoVITS 进程起来就退出」：用户看到的是语音功能凭空消失，日志里是一段 import 失败。

## 闸门钉的四件事（每条都从真实文件读出来，不硬编码副本）

1. 正向：requirements.txt 声明了 librosa / pypinyin / soundfile / matplotlib / jieba-fast。
2. 一致性：tools/安装糖糖.py 的 GROUPS 至少有一个组声明了它们。解析**复用**
   tests/test_install_deps_contract.py 的 helpers（不复制解析器）——那个文件的
   `test_requirements_packages_are_available_from_installer_groups` 守的是全局契约
   「requirements ⊆ 安装器」；这里把 GPT-SoVITS 这五个单独钉住，两层各守一段。
3. 反向：requirements.txt 不再**声明** faiss-cpu / pyworld / torchfcpe / fairseq
   （翻唱链依赖，已随链删除；它们回来 = 有人把链路接回来了，或抄了一份旧清单）。
   判据只看**可执行包行**——注释里写「已移除 xxx」是允许的（文件里已有这种先例）。
4. 存在性：`gpt-sovits/GPT_SoVITS/**/*.py` 里真的有文件 import 这些包（glob 扫全目录，
   **不写死文件名**）。没有这条，闸门就退化成「清单里写了就算数」的同义反复——
   而这五条依赖之所以必须留，恰恰是因为 GPT-SoVITS 真的在用它们。

## 阳性对照（教训 #30：检查器要能认得出）

`test_gate_reports_a_planted_missing_declaration` 把 requirements 文本在**内存里**挖掉
`librosa>=0.10` 那一行，断言核心判定报出它；磁盘上的 requirements.txt 一个字都不动。
另配两条覆盖面断言（`test_importer_scan_actually_covers_gpt_sovits`）——「跑起来没报错」
不等于「真的扫了」（教训 #30 同族）。
"""

from __future__ import annotations

import ast
import importlib.util
import warnings
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
REQUIREMENTS = BASE / "requirements.txt"
INSTALLER = BASE / "tools" / "安装糖糖.py"
GPT_SOVITS_ROOT = BASE / "gpt-sovits" / "GPT_SoVITS"

# 包名（requirements 里的声明名）→ 代码里的 import 名
GPT_SOVITS_DEPENDENCIES = {
    "librosa": "librosa",
    "pypinyin": "pypinyin",
    "soundfile": "soundfile",
    "matplotlib": "matplotlib",
    "jieba-fast": "jieba_fast",
}

# 翻唱制作链的依赖——链路已删，不许回来
REMOVED_WITH_THE_COVER_PIPELINE = ("faiss-cpu", "pyworld", "torchfcpe", "fairseq")


def _deps_contract():
    """加载同目录的 test_install_deps_contract.py——包名解析只此一份，不复制。"""
    path = Path(__file__).with_name("test_install_deps_contract.py")
    spec = importlib.util.spec_from_file_location("_deps_contract_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTRACT = _deps_contract()


def _declared(text: str) -> set[str]:
    """requirements 文本里的**可执行包行**（注释行/空行天然不计）；名字已规范化。"""
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = _CONTRACT._package_from_spec(line)
        if name:
            names.add(name)
    return names


def _missing(text: str, needed=None) -> list[str]:
    """纯判定：给定 requirements 文本，哪些 GPT-SoVITS 依赖没被声明。

    抽成纯函数是为了阳性对照——可以把文本改坏了喂进来，不必动磁盘上的真文件。
    """
    have = _declared(text)
    return sorted(p for p in (needed or GPT_SOVITS_DEPENDENCIES)
                  if _CONTRACT._normalise(p) not in have)


def _sovits_python_files() -> list[Path]:
    """glob 全目录（不写死文件名）——第三方目录换新版时文件名会变。"""
    return sorted(GPT_SOVITS_ROOT.rglob("*.py"))


def _importers() -> tuple[dict[str, set[str]], int, list[str]]:
    """(import 名 → 引用它的文件相对路径, 成功解析的文件数, 解析失败的文件名)。

    用 AST 而不是正则：`import librosa as l` / `from librosa.filters import mel` 都要算数，
    而字符串里出现 librosa 不算（那个目录里大量注释/文档提到这些包名）。
    """
    wanted = set(GPT_SOVITS_DEPENDENCIES.values())
    found: dict[str, set[str]] = {}
    parsed = 0
    broken: list[str] = []
    for path in _sovits_python_files():
        try:
            with warnings.catch_warnings():
                # gpt-sovits 是第三方目录：它自己的字面量里有非法转义序列，
                # ast.parse 会顺手报 SyntaxWarning/DeprecationWarning——与本项目无关，
                # 别让它污染 pytest 的 warnings summary（真正的漂移会被它淹掉）。
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            broken.append(path.name)
            continue
        parsed += 1
        rel = str(path.relative_to(GPT_SOVITS_ROOT)).replace(chr(92), "/")
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [(node.module or "").split(".")[0]]
            for name in names:
                if name in wanted:
                    found.setdefault(name, set()).add(rel)
    return found, parsed, broken


class TestDependenciesAreDeclared:
    """① requirements.txt 与 ② 安装器 GROUPS —— 声明侧。"""

    def test_requirements_declares_every_gpt_sovits_dependency(self):
        """删掉任一行的症状是「新机器上 GPT-SoVITS 起不来」，本机看不出来。"""
        text = REQUIREMENTS.read_text(encoding="utf-8")
        missing = _missing(text)
        assert not missing, (
            "requirements.txt 没有声明这些 GPT-SoVITS 硬依赖：" + ", ".join(missing)
            + "\n它们不是翻唱链依赖——GPT-SoVITS 与主程序同解释器运行，缺了直接 import 失败。"
            "\n（2026-10-15 清理翻唱链时 librosa/pypinyin 差点被当成 RVC 残留删掉）")

    def test_installer_declares_every_gpt_sovits_dependency(self):
        """安装器勾「发语音+听懂语音消息」后必须能装上这些包。

        解析器来自 test_install_deps_contract.py（那边的全局契约守「requirements ⊆ 安装器」）；
        这里把它窄到 GPT-SoVITS 这五个——即使全局契约被改松，这一条仍然拦得住。
        """
        have = _CONTRACT._installer_packages()
        missing = sorted(p for p in GPT_SOVITS_DEPENDENCIES
                         if _CONTRACT._normalise(p) not in have)
        assert not missing, (
            "tools/安装糖糖.py 的 GROUPS 里没有任何组声明：" + ", ".join(missing)
            + "\n只写 requirements.txt 不够——安装器才是用户装依赖的入口。")

    def test_installer_and_requirements_helpers_agree_on_the_source(self):
        """防呆：解析器若被改成读别的文件，上面的两条断言会对着空气绿。"""
        assert REQUIREMENTS.is_file() and INSTALLER.is_file()
        assert _CONTRACT.REQUIREMENTS == REQUIREMENTS, "包名解析器的 requirements 路径变了"
        assert _CONTRACT.INSTALLER == INSTALLER, "包名解析器的安装器路径变了"

    @pytest.mark.parametrize("package", REMOVED_WITH_THE_COVER_PIPELINE)
    def test_cover_pipeline_dependencies_stay_out_of_requirements(self, package):
        """反向闸门：翻唱链的依赖不许回到 requirements.txt（注释里提一句无所谓）。"""
        declared = _declared(REQUIREMENTS.read_text(encoding="utf-8"))
        assert _CONTRACT._normalise(package) not in declared, (
            f"requirements.txt 又声明了 {package}——它是已删除的翻唱制作链的依赖"
            "（faiss-cpu/pyworld 是 RVC 索引与音色转换、torchfcpe 是 DiffSinger、fairseq 是 RVC 训练），"
            "回来意味着链路被接回来了，或者抄了一份旧清单。")


class TestDependenciesAreActuallyUsed:
    """④ 存在性 —— 「它真的是 GPT-SoVITS 的依赖」的实证。"""

    def test_importer_scan_actually_covers_gpt_sovits(self):
        """覆盖面自证：闸门必须真的扫到了东西（教训 #30：跑起来没报错 ≠ 真的扫了）。"""
        assert GPT_SOVITS_ROOT.is_dir(), (
            f"找不到 {GPT_SOVITS_ROOT}——GPT-SoVITS 目录不在的话，④ 会退化成空断言")
        files = _sovits_python_files()
        assert len(files) >= 50, (
            f"gpt-sovits/GPT_SoVITS 下只扫到 {len(files)} 个 .py——glob 范围是不是弄窄了？")
        _, parsed, broken = _importers()
        assert parsed >= 50, f"AST 只成功解析了 {parsed} 个文件，覆盖不足"
        assert len(broken) <= 5, f"太多文件解析不了，闸门覆盖面不可信：{broken}"

    @pytest.mark.parametrize("package,module", sorted(GPT_SOVITS_DEPENDENCIES.items()))
    def test_gpt_sovits_really_imports_it(self, package, module):
        """每条声明都必须有真实的 importer——否则它只是清单里的一个名字。"""
        found, parsed, _ = _importers()
        users = sorted(found.get(module, ()))
        assert users, (
            f"gpt-sovits/GPT_SoVITS 下没有任何文件 import {module}（扫了 {parsed} 个文件）——"
            f"那 requirements.txt 里的 {package} 是什么？"
            "\n要么这个包真的没人用（那它不该占着依赖清单），要么扫描范围/文件名写死了漏掉了它。")


class TestPositiveControl:
    """阳性对照（教训 #30）——不碰磁盘上的 requirements.txt。"""

    def test_gate_reports_a_planted_missing_declaration(self):
        text = REQUIREMENTS.read_text(encoding="utf-8")
        planted = "\n".join(line for line in text.splitlines()
                            if not line.strip().startswith("librosa"))
        assert planted != text, "requirements.txt 里找不到以 librosa 开头的行——阳性对照失去锚点"
        assert _missing(planted) == ["librosa"], (
            "把 librosa 那一行挖掉后，判定却没报它——这条闸门认不出真样本（教训 #30）")

    def test_intact_text_reports_nothing(self):
        """阴性对照：完好的文本必须干净，否则闸门会被当噪音忽略。"""
        assert _missing(REQUIREMENTS.read_text(encoding="utf-8")) == []
