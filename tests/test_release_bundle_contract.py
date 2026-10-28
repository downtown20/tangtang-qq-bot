# -*- coding: utf-8 -*-
"""发布形态契约：从仓库直接 clone，必须能拿到「能启动的最小集合」。

## 为什么有这个文件（2026-10-26）

主人把 GitHub 仓库下载解压后反馈「内容并不规范也不全」。取证结果是三处缺口：

  1. 仓库里**没有** `config.example.yaml`——`tools/安装糖糖.py:1101` 的
     `bootstrap_config()` 靠它生成 `config.yaml`，它不在，函数就静默 `return False`，
     用户 clone 下来永远没有配置，屏幕上也没有一行提示。
  2. 仓库里**没有** `.env.example`，但 README 与用户手册都说「发布包里只有这两个模板」。
  3. README 说 `songs/`、`stickers*/`「随安装包自带」、clone 时「只有一份摆放说明」——
     三者都不在仓库里，`.gitignore` 把它们全排除了。

`tests/test_config_example_contract.py` 只在**有真实 config.yaml 的机器**上跑（干净 clone 下
`pytest.skip`），所以这些缺口此前没有任何闸门拦着——本项目教训 #30/#37 的「假绿」。

本文件只断言**仓库里看得见的东西**，不依赖 config.yaml、不依赖本机部署：
干净 clone 跑 `pytest`，缺哪一件就红在哪一件上。
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = BASE / "config.example.yaml"
ENV_EXAMPLE = BASE / ".env.example"
README = BASE / "README.md"
INSTALLER = BASE / "tools" / "安装糖糖.py"

#: 9-11 位纯数字 = QQ 号形态。`tools/准备发布.py` 的 scrub_cfg 用同一形状兜底脱敏。
_QQ_RE = re.compile(r"(?<![0-9])[1-9][0-9]{8,10}(?![0-9])")

#: 密钥形状（常见前缀 + 「一长串无空格 ASCII」两种）。模板里出现任何一个都算泄漏。
_KEY_RE = re.compile(
    r"(?:sk-|xai-|ghp_|AKIA|npm_)[A-Za-z0-9_\-]{12,}"
    r"|(?<![A-Za-z0-9_])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9_])"
)

#: 写进公开仓库即触碰硬红线的词（主人明确要求：本地私密内容一律不上传）。
_SENSITIVE_WORDS = ("色色", "未成年", "恋爱军师", "buddybackend")

#: 已知常量（socket 端口上限、errno、时间戳等）不是 QQ 号，与 准备发布.py 的
#: QQ_KNOWN_CONSTANTS 同口径——宁可白名单显式，也不要为了过闸门放宽正则。
_KNOWN_CONSTANTS = {
    "2147483647", "2147483648", "3221225477",
    "1787875200", "1787875201", "1787961600", "1787940000", "1700000000",
}


def _scan_leaks(text: str) -> list[str]:
    """扫一段文本：QQ 号形态 / 密钥形状 / 硬红线词。返回人话描述列表。"""
    leaks: list[str] = []
    for m in _QQ_RE.finditer(text):
        if m.group(0) not in _KNOWN_CONSTANTS:
            leaks.append(f"9-11 位纯数字（像 QQ 号）: {m.group(0)}")
    for m in _KEY_RE.finditer(text):
        leaks.append(f"密钥形状的字符串: {m.group(0)[:10]}…（{len(m.group(0))} 字符）")
    for word in _SENSITIVE_WORDS:
        if word in text:
            leaks.append(f"硬红线词: {word}")
    return leaks


# ═══════════════════════════════════════════════════════
# 1. 两个模板必须在仓库里，且能被解析
# ═══════════════════════════════════════════════════════

def test_config_example_exists_and_parses():
    """`config.example.yaml` 必须在仓库里，且是合法 YAML dict。

    它是 `安装糖糖.py` 的 `bootstrap_config()` 唯一的生成来源——不在，用户
    clone 后永远没有 config.yaml，而且（改之前）连一行提示都没有。
    """
    assert CONFIG_EXAMPLE.is_file(), (
        "仓库里没有 config.example.yaml —— 干净 clone 的用户会永远没有配置。"
        "它必须是 git 追踪的文件（.gitignore 不能排除它）。")
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "config.example.yaml 解析出来不是非空 dict"
    # 两个必须真的被替换的字段：占位符明显到「任何人看都知道要改」
    assert "你的" in str(data["bot"]["qq_id"]), "bot.qq_id 不是明显占位符"
    assert "你的" in str(data["bot"]["owner_qq"]), "bot.owner_qq 不是明显占位符"


def test_env_example_exists_and_is_all_placeholders():
    """`.env.example` 必须在仓库里，且每个值都是空的（全占位、绝无真实 token）。"""
    assert ENV_EXAMPLE.is_file(), (
        "仓库里没有 .env.example —— README / 用户手册都说发布包里有它。")
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    pairs = re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M)
    assert pairs, ".env.example 里一行 KEY=VALUE 都没有"
    filled = [(k, v) for k, v in pairs if v.strip()]
    assert not filled, f"这些变量不是空占位：{filled}"


def test_env_example_covers_every_reference_in_template():
    """模板里每个 `${VAR}` 都必须在 .env.example 里有同名的空行。"""
    yaml = pytest.importorskip("yaml")
    cfg_text = CONFIG_EXAMPLE.read_text(encoding="utf-8")
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    refs = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", cfg_text))
    assert refs, "模板里一个 ${VAR} 引用都没有——密钥是不是被写成了字面量？"
    env_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", env_text, re.M))
    missing = sorted(refs - env_keys)
    assert not missing, f"模板引用这些变量，.env.example 里却没有：{missing}"

    from agent.secret_refs import PROVIDER_KEY_ENV
    missing_provider = sorted(set(PROVIDER_KEY_ENV.values()) - env_keys)
    assert not missing_provider, (
        f"换 provider 要用的变量没在 .env.example 里列全：{missing_provider}")


def test_templates_do_not_leak_secrets_or_private_facts():
    """两个模板递归扫一遍：不许有 QQ 号、密钥、私密词。"""
    problems = []
    for path in (CONFIG_EXAMPLE, ENV_EXAMPLE):
        for leak in _scan_leaks(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}: {leak}")
    assert not problems, "模板里还有没脱敏干净的东西：\n  " + "\n  ".join(problems)


# ═══════════════════════════════════════════════════════
# 2. 发布必带文件不能被 .gitignore 挡掉
# ═══════════════════════════════════════════════════════

def _gitignored(rel: str) -> bool | None:
    """`git check-ignore` 判定：True=被忽略 / False=放行 / None=拿不到判定。"""
    git = shutil.which("git")
    if not git:
        return None
    proc = subprocess.run([git, "check-ignore", "-q", "--no-index", rel],
                          cwd=BASE, capture_output=True)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def test_publish_files_are_not_gitignored():
    """三个「发布必带」文件必须被 .gitignore 放行。

    2026-10-26 的真事故形态：文件在本地（`git status` 里是 `??`）却因为 `.env.*`
    或 `songs/*` 规则被挡在仓库外——**本地能看到、clone 下来没有**。
    `git check-ignore` 是唯一能区分「还没 add」和「被 ignore」的判据。
    """
    skipped = 0
    for rel in ("config.example.yaml", ".env.example", "songs/摆放说明.md"):
        ignored = _gitignored(rel)
        if ignored is None:
            skipped += 1
            continue
        assert ignored is False, (
            f"{rel} 被 .gitignore 忽略了——本地有、clone 没有。"
            f"需要一条 `!{rel}` 否定规则，且必须写在排除规则**之后**")
    if skipped:
        pytest.skip("环境里没有可用的 git，跳过 ignore 判定")


# ═══════════════════════════════════════════════════════
# 3. 「摆放说明」必须在仓库里（README 承诺过）
# ═══════════════════════════════════════════════════════

def test_songs_has_placement_note_in_repo():
    """`songs/` 下必须有可提交的说明文件。

    `.gitignore` 里 `songs/*` 会忽略整个目录内容，所以说明文件必须被
    `!songs/**.md` 之类的白名单放行——否则 clone 下来就是空的，README 又承诺过
    「有一份摆放说明」（README:205 原话）。
    """
    notes = sorted(p for p in (BASE / "songs").glob("*.md") if p.is_file())
    assert notes, "songs/ 下没有说明文件——README 承诺的那份摆放说明不存在"
    text = "\n".join(p.read_text(encoding="utf-8") for p in notes)
    for must in ("songs/audio/", "covers/separated/", "/歌单 重载"):
        assert must in text, f"songs 说明里没写清 {must}"


def test_material_notes_exist_and_are_neutral():
    """集中摆放说明必须存在，且不出现主人的私密部署事实。"""
    note = BASE / "docs" / "素材摆放说明.md"
    assert note.is_file(), "缺 docs/素材摆放说明.md——仓库里没有的素材就没有任何落点说明"
    text = note.read_text(encoding="utf-8")
    for word in ("未成年人", "恋爱军师", "色色参考", "色色范本"):
        assert word not in text, f"通用说明里出现了私密部署事实：{word}"


# ═══════════════════════════════════════════════════════
# 3. README 必须说真话
# ═══════════════════════════════════════════════════════

def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_does_not_claim_resources_are_bundled():
    """「随安装包自带」「跑一次安装即补齐」是假话，删掉后不许回来。"""
    text = _readme_text()
    for bad in ("随安装包自带", "跑一次 `安装糖糖.bat` 即可补齐"):
        assert bad not in text, f"README 又出现与事实不符的宣称：{bad}"
    assert "安装糖糖.bat` 只装依赖" in text or "不下载" in text, (
        "README 对安装糖糖.bat 的描述没有说明它不下载素材")


def test_readme_documents_material_preparation_and_desktop_build():
    """素材与桌面端都必须有可照着做的章节（缺了，clone 的人就卡在这两处）。"""
    text = _readme_text()
    assert "## 素材准备" in text, "README 没有「素材准备」章节"
    assert "docs/素材摆放说明.md" in text, "README 没有链接到集中摆放说明"
    assert "## 桌面端（可选）" in text, "README 没有桌面端构建章节"
    for cmd in ("cd desktop", "pnpm install", "pnpm dev", "pnpm build"):
        assert cmd in text, f"README 桌面端章节缺可复制粘贴的命令：{cmd}"


def test_readme_directory_tree_lists_new_landing_dirs():
    """目录结构代码块要跟上：`运维/`、`prompts/`、`config.example.yaml`。"""
    text = _readme_text()
    m = re.search(r"## 目录结构\s*\n+```text\n(.*?)```", text, re.S)
    assert m, "README 找不到「## 目录结构」下的 ```text 代码块"
    tree = m.group(1)
    for name in ("运维/", "prompts/", "config.example.yaml"):
        assert name in tree, f"目录结构里缺 {name}"
    assert "糖糖控制台_qt.py" not in tree, "目录结构还把 Qt 控制台写在根目录（它已移入 运维/）"
    # 2026-10-28：`变异/` 从索引移除（.gitignore 排除），README 若还列着它，
    # clone 的人会去找一个根本不存在的目录（教训 #35 文档与实物分家）。
    assert "变异/" not in tree, (
        "目录结构还写着 `变异/`——它 2026-10-28 起不进公开仓库（只在本地开发时存在）"
    )


# ═══════════════════════════════════════════════════════
# 4. 安装器：缺模板时必须说话（静默失败的根）
# ═══════════════════════════════════════════════════════

def _load_installer(tmp_path: Path):
    """导入安装器并把模块级 BASE 指到临时项目根（与 test_installer_wiring 同法）。"""
    spec = importlib.util.spec_from_file_location("_installer_bundle_under_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.BASE = tmp_path
    return mod


def test_bootstrap_config_warns_when_template_missing(tmp_path, capsys):
    """缺 config.example.yaml 时，必须打印明确警告——不许静默 return False。"""
    mod = _load_installer(tmp_path)
    result = mod.bootstrap_config(dry_run=False)
    out = capsys.readouterr().out
    assert result is False
    assert "找不到 config.example.yaml" in out, (
        f"缺模板时屏幕上什么线索都没有——用户只会看到「装完起不来」。实际输出：{out!r}")
    assert not (tmp_path / "config.yaml").exists()


def test_bootstrap_config_stays_silent_when_config_exists(tmp_path, capsys):
    """已有 config.yaml 时保持静默——不该打扰已经配置好的用户。"""
    mod = _load_installer(tmp_path)
    (tmp_path / "config.yaml").write_text("bot:\n  name: t\n", encoding="utf-8")
    assert mod.bootstrap_config(dry_run=False) is False
    assert capsys.readouterr().out.strip() == ""


def test_bootstrap_config_dry_run_and_real_run(tmp_path, capsys):
    """有模板时：--dry-run 不落盘、真跑会生成 config.yaml。"""
    mod = _load_installer(tmp_path)
    (tmp_path / "config.example.yaml").write_text("bot:\n  name: t\n", encoding="utf-8")
    assert mod.bootstrap_config(dry_run=True) is True
    assert not (tmp_path / "config.yaml").exists()
    assert "--dry-run" in capsys.readouterr().out

    assert mod.bootstrap_config(dry_run=False) is True
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "bot:\n  name: t\n"
    assert "已生成 config.yaml" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════
# 5. 阳性对照：扫描器自己必须能红（教训 #37）
# ═══════════════════════════════════════════════════════

def test_leak_scanner_goes_red_on_a_dirty_sample():
    """种一个脏样本，扫描器必须报出来——否则「零命中」只是「正则永假」。"""
    sample = (
        "bot:\n  owner_qq: " + "123456" + "789\n"          # 运行期拼出 9 位数字
        "llm:\n  api_key: " + "sk-" + "abcdefghijklmnopqrstuvwx\n"  # 运行期拼出密钥形状
        "note: " + "色色" + "参考\n"
    )
    leaks = _scan_leaks(sample)
    kinds = " | ".join(leaks)
    assert any("9-11" in x for x in leaks), f"没报 QQ 号形态：{kinds}"
    assert any("密钥" in x for x in leaks), f"没报密钥形状：{kinds}"
    assert any("硬红线" in x for x in leaks), f"没报硬红线词：{kinds}"
