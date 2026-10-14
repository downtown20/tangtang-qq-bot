"""安装器、requirements 与体检依赖表的防漂移契约（K1）。

只解析文本：tools/ 不是包，直接 import 安装器会执行其模块级加载逻辑；
依赖契约只关心声明，不能为了测试而引入运行时副作用。
"""

import re
from pathlib import Path


BASE = Path(__file__).resolve().parent.parent
REQUIREMENTS = BASE / "requirements.txt"
INSTALLER = BASE / "tools" / "安装糖糖.py"
HEALTH_CHECK = BASE / "tools" / "体检.py"

# requirements 的包名与 import 名不总是一致；这里登记的是项目当前真实映射。
HEALTH_IMPORT_TO_PACKAGE = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "sherpa_onnx": "sherpa-onnx",
    "edge_tts": "edge-tts",
    "PySide6": "pyside6",
}

# torch / torchaudio 在 requirements.txt 里是**注释形式的判定表 + 手工命令**（第 25 行起），
# 不是可执行包行——因为「装哪一档」由机器决定，不能写死一行。真正可执行的声明在
# tools/安装糖糖.py 的 TORCH_VARIANTS（T54-A，2026-10-14 起由本机设备自动选档）。
EXTERNALLY_DECLARED_PACKAGES = {"torch", "torchaudio"}


def _normalise(name: str) -> str:
    """按 PyPI 名称等价规则比较：大小写与 -/_/. 不应造成假漂移。"""
    return re.sub(r"[-_.]+", "", name).lower()


def _package_from_spec(spec: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", spec)
    return _normalise(match.group(1)) if match else None


def _requirements_packages() -> set[str]:
    """取可执行包行；已移除/不采用/另装等注释行天然不进入集合。"""
    packages = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        package = _package_from_spec(line)
        if package:
            packages.add(package)
    return packages


def _quoted_package_specs(text: str) -> set[str]:
    packages = set()
    for spec in re.findall(r"[\"']([^\"']+)[\"']", text):
        package = _package_from_spec(spec)
        if package:
            packages.add(package)
    return packages


def _installer_packages() -> set[str]:
    """从 GROUPS 的 pkgs/extra/soft 与 TORCH_VARIANTS 的 pkgs 读取包名，不执行安装器。"""
    source = INSTALLER.read_text(encoding="utf-8")
    pkg_blocks = re.findall(r'"pkgs"\s*:\s*\[(.*?)\]', source, flags=re.DOTALL)
    extra_blocks = re.findall(r'"extra"\s*:\s*\[\(\[(.*?)\]', source, flags=re.DOTALL)
    soft_blocks = re.findall(r'"soft"\s*:\s*\[(.*?)\]', source, flags=re.DOTALL)
    assert pkg_blocks, "安装器 GROUPS 中未找到 pkgs 声明"
    # [!] 2026-10-14 T54-A：torch 不再写死在 GROUPS.extra 里（那样新机器只会拿到
    #   CPU 版），改成 GROUPS 声明 "torch": True + TORCH_VARIANTS 按本机设备给档位。
    #   契约不能因此失守——原来那句 assert extra_blocks 钉的是「torch 安装契约在」，
    #   现在改钉「档位表在、组声明在」，而各档的 "pkgs" 会被上面的正则照常收进来，
    #   覆盖面反而从 1 条清单变成 3 个档位。
    assert re.search(r'^TORCH_VARIANTS = \{', source, flags=re.MULTILINE), \
        "安装器缺少 torch 档位表（T54-A）——torch 安装契约丢失"
    assert '"torch": True' in source, "GROUPS 里没有「本组需要 torch」的声明（T54-A）"
    return set().union(*(_quoted_package_specs(block)
                         for block in pkg_blocks + extra_blocks + soft_blocks))


def _health_import_modules() -> set[str]:
    """读取 CRITICAL_DEPS/OPTIONAL_DEPS 的 import 名，不 import 体检脚本。"""
    source = HEALTH_CHECK.read_text(encoding="utf-8")
    blocks = re.findall(
        r"(?:CRITICAL_DEPS|OPTIONAL_DEPS)\s*=\s*\{(.*?)^\}",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert len(blocks) == 2, "体检依赖表结构变化；同步更新本契约解析器"
    return {
        module
        for block in blocks
        for module in re.findall(r'^\s*"([^\"]+)"\s*:', block, flags=re.MULTILINE)
    }


def test_requirements_packages_are_available_from_installer_groups():
    """requirements 每个真实包必须能由安装器某个 GROUPS.pkgs/extra 或 torch 档位安装。"""
    missing = _requirements_packages() - _installer_packages()
    assert not missing, (
        "requirements.txt 有真实依赖未被 tools/安装糖糖.py 的 GROUPS 安装；"
        "请同步加入对应组：" + ", ".join(sorted(missing))
    )


def test_installer_group_packages_are_declared_in_requirements():
    """安装器不得新增 requirements 未声明的 pip 包；torch / torchaudio 是文档化例外。"""
    unexpected = _installer_packages() - _requirements_packages() - EXTERNALLY_DECLARED_PACKAGES
    assert not unexpected, (
        "tools/安装糖糖.py 的 GROUPS 含 requirements.txt 未声明的包；"
        "请补清单，或为独立安装环境登记有理由的例外：" + ", ".join(sorted(unexpected))
    )


def test_health_dependency_imports_have_declared_packages():
    """体检表不能再报告 requirements 未声明的幽灵依赖。"""
    health_packages = {
        _normalise(HEALTH_IMPORT_TO_PACKAGE.get(module, module))
        for module in _health_import_modules()
    }
    ghosts = health_packages - _requirements_packages() - EXTERNALLY_DECLARED_PACKAGES
    assert not ghosts, (
        "tools/体检.py 的 CRITICAL_DEPS/OPTIONAL_DEPS 含未声明依赖；"
        "请补 requirements，或删除零使用的体检项：" + ", ".join(sorted(ghosts))
    )
