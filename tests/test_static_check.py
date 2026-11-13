"""静态检查闸门（tools/静态检查.py）—— 挡住「闸门静默缩小扫描范围」。

背景（2026-10-14 T54 实测取证）
────────────────────────────────
项目文档里的那条命令是：

    python -m pyflakes agent/*.py onebot/*.py main.py 糖糖控制台_qt.py | grep -i undefined

它在 Git Bash 下没问题（bash 展开 glob），但在 **pwsh / cmd 下 glob 不展开**，
pyflakes 收到字面量 `agent/*.py` 会报 Invalid argument 并跳过整个目录。实测：

    不展开（pwsh）：输出 24 行，**全是 Invalid argument**，undefined 命中 = 0
    显式展开      ：传入 82 个文件，输出 116 行，undefined 命中 = 0

⇒ **82 个文件只有 2 个真被扫过，而闸门照样报「干净」。**
这是本仓教训 #30（检查器写了但从没生效）的同一族：跑起来没报错 ≠ 真的扫了。

所以现在的闸门是 tools/静态检查.py：文件列表由 Python 自己 glob，**并且一旦
pyflakes 抱怨参数就立刻红灯**（`find_rejected`）。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCRIPT = BASE / "tools" / "静态检查.py"


def _run_capture(args: list[str]) -> subprocess.CompletedProcess:
    """跑子进程并把**两端都钉死为 UTF-8**（子进程 PYTHONIOENCODING + 父进程 encoding=）。

    为什么必须两端都钉（2026-10-16 T2.9 实测）：本仓在 Windows 上，父进程默认按
    locale(GBK) 解子进程输出；而子进程一旦从环境里继承了 `PYTHONIOENCODING=utf-8`
    （某些 shell / CI 包装会注入），中文输出就是「UTF-8 字节被按 GBK 读」——
    `扫描文件数 = N` 变成乱码，正则匹配不到 → **假红**。反过来若子进程被注入 latin-1，
    它连中文都打不出来（UnicodeEncodeError）→ 也红。两边都钉住，才与外部环境无关。

    ⚠ 不要退回 `text=True` 就走——那等于让判据依赖跑测试的人当时的环境变量。
    """
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(args, cwd=BASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def _load_module():
    spec = importlib.util.spec_from_file_location("static_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 纯函数：两条判据各自可测 ─────────────────────────────────────────
def test_find_rejected_catches_invalid_argument():
    mod = _load_module()
    lines = [
        "python.exe : agent/*.py: Invalid argument",
        "agent/handler.py:12: undefined name 'foo'",
    ]
    assert mod.find_rejected(lines) == [lines[0]]


def test_find_undefined_only_matches_undefined():
    mod = _load_module()
    lines = [
        "agent/x.py:1:1: 'os' imported but unused",
        "agent/y.py:2:3: undefined name 'zzz'",
        "agent/z.py:4:5: local variable 'q' is assigned to but never used",
    ]
    assert mod.find_undefined(lines) == [lines[1]]


def test_positive_control_rejected_line_means_gate_is_blind():
    """**阳性对照**：塞一行「参数被拒」进去，闸门必须能识别出来。

    没有这条，把 find_rejected 改成恒返回空表之后本文件其余测试照样全绿，
    而闸门会重新变成「只扫 2 个文件也报干净」。
    """
    mod = _load_module()
    assert mod.find_rejected(["pyflakes: No such file or directory: nope.py"])
    assert mod.find_undefined(["x.py:1: undefined name 'y'"])


# ── 端到端：真的跑一遍 ───────────────────────────────────────────────


def test_collect_matches_independent_glob():
    """收集到的文件数必须与独立 glob 的结果一致。

    这条挡的是「闸门悄悄少收文件」——例如有人把 TARGET_DIRS 改小，
    或者某个目录被改名之后 pathlib 静默返回空列表。
    """
    mod = _load_module()
    got = mod.collect()
    assert len(got) == _expected_file_count()
    assert len(got) > 50, f"只收到 {len(got)} 个文件 —— 闸门明显缩水了"
    assert all(p.suffix == ".py" for p in got)
    assert all(p.exists() for p in got)


def test_gate_runs_and_reports_the_scanned_count():
    out = _run_capture([sys.executable, str(SCRIPT)])
    assert out.returncode == 0, f"闸门报红：{out.stdout[-400:]}"
    m = re.search(r"扫描文件数 = (\d+)", out.stdout)
    assert m, f"输出里没有文件数 —— 无法发现闸门缩水：{out.stdout[:300]}"
    assert int(m.group(1)) == _expected_file_count()


def test_child_output_survives_a_hostile_ambient_encoding(monkeypatch):
    """阳性对照：外部注入 `PYTHONIOENCODING=latin-1` 时闸门仍然可读。

    没有这一步，`_run_capture` 里那句 env 钉死就是「写了但从没生效」——它本来就不会报错
    （本仓教训 #30 同族）。把 env 钉死去掉，本测试立刻红：latin-1 下子进程连「扫描文件数」
    这四个字都打不出来。
    """
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    out = _run_capture([sys.executable, str(SCRIPT)])
    assert out.returncode == 0, f"子进程在自己打不出中文时报红：{out.stderr[-300:]}"
    assert re.search(r"扫描文件数 = (\d+)", out.stdout), \
        "中文判据在恶意编码环境下读不到了 —— 说明两端钉死没生效"


def test_docs_point_at_the_script_not_a_bare_glob():
    """文档里不许再写那条「靠 shell 展开」的命令 —— 那正是本次踩的坑。

    判据用**代码形态**（命令串），不用中文短语，避免命中自己的说明文字。
    """
    for rel in ("CLAUDE.md", "AGENTS.md"):
        p = BASE / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        assert "tools/静态检查.py" in text, f"{rel} 没有指向新的静态检查闸门"
        # 判据是**可执行形态**（`-m pyflakes` + glob 参数），不是出现「pyflakes」和
        # 「*.py」两个词就算 —— 否则「不要手写那条命令」的告警文字自己会被判违规。
        offenders = [ln.strip() for ln in text.splitlines()
                     if "-m pyflakes" in ln and "*.py" in ln]
        assert offenders == [], (
            f"{rel} 里还留着靠 shell 展开 glob 的 pyflakes 命令"
            f"（pwsh 下会静默漏扫 80/82 个文件）：{offenders}"
        )


def _expected_file_count() -> int:
    n = 0
    for d in ("agent", "onebot"):
        p = BASE / d
        if p.is_dir():
            n += len(list(p.glob("*.py")))
    for name in ("main.py", "运维/糖糖控制台_qt.py"):
        if (BASE / name).exists():
            n += 1
    return n
