"""变异对照工具（tools/变异对照.py）的仪器自检。

为什么必须测这个工具：它本身是「判据的判据」——如果它会把**假变异**当成通过，
那它就是本项目教训 #37 里最危险的一类仪器（假绿长得和真绿一样）。
所以这里的每一条都是**阳性对照**：
  · 真变异 → 必须报 RED；
  · 空变异（只改注释）→ 必须报「假变异」并以退出码 2 失败，**不许当成通过**；
  · 锚点不唯一 → 必须拒绝执行（不猜、不模糊匹配）；
  · 外部注入 PYTHONIOENCODING=latin-1 → 工具自己仍要能打印中文并正常工作；
  · 每次跑完必须**逐字节还原**目标文件。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
HARNESS = BASE / "tools" / "变异对照.py"


@pytest.fixture(autouse=True)
def _sweep_test_backups():
    """清掉本文件在**全局临时目录**里留下的变异备份。

    备份是工具为了「硬杀自愈」故意落在 `tempfile.gettempdir()` 的。正常路径会删掉，
    但**测试失败时**（比如阳性对照故意弄坏工具）测试里的 `_simulate_hard_kill` 已经把
    备份写出来了，断言失败抛异常就没人清——于是下一次 `--recover-all` 会跑去
    「恢复」一个 pytest 临时目录里的死文件。只清 meta 指向 pytest 临时目录的那些，
    绝不碰真实运行留下的残留（那个该由 `--recover-all` 正经处理）。
    """
    yield
    tmpdir = Path(tempfile.gettempdir())
    for meta in tmpdir.glob("tt_mutation_bak_*.meta"):
        try:
            pointed = meta.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if "pytest-of-" not in pointed:
            continue
        meta.unlink(missing_ok=True)
        meta.with_name(meta.name[: -len(".meta")]).unlink(missing_ok=True)

MOD = 'SMALL = "small"\n\n\ndef classify(n):\n    if n > 10:\n        return "big"\n    return SMALL\n'
TEST = "from mod import classify\n\n\ndef test_big():\n    assert classify(20) == \"big\"\n"


def _fixture(tmp_path: Path):
    (tmp_path / "mod.py").write_text(MOD, encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(TEST, encoding="utf-8")
    return hashlib.sha256((tmp_path / "mod.py").read_bytes()).hexdigest()


def _run(tmp_path: Path, muts, env_extra=None, extra_args=None):
    cmd = [sys.executable, str(HARNESS), "--root", str(tmp_path),
           "--target", "mod.py", "--tests", "test_mod.py"]
    for m in muts:
        cmd += ["--mut", m]
    cmd += list(extra_args or [])
    env = {**os.environ, **(env_extra or {})}
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env)
    return out.returncode, out.stdout, out.stderr


def test_real_mutation_is_reported_red(tmp_path):
    sha0 = _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"])
    assert rc == 0, f"真变异应当通过：rc={rc} {out[-300:]} {err[-300:]}"
    assert "M1: RED OK" in out
    sha1 = hashlib.sha256((tmp_path / "mod.py").read_bytes()).hexdigest()
    assert sha1 == sha0, "目标文件必须逐字节还原"


def test_weak_mutation_is_flagged_not_accepted(tmp_path):
    """核心阳性对照：空变异必须被报成假变异，而不是「通过」。"""
    _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M2:def classify(n):=>def classify(n):  # 只改注释"])
    assert rc == 2, f"假变异必须以 2 退出：rc={rc} {out[-300:]}"
    assert "GREEN 假变异" in out
    assert "假变异" in err


def test_weak_mutation_can_be_explicitly_allowed(tmp_path):
    """--allow-green 是唯一的例外通道：显式接受某个 Green 变异，退出码才回到 0。"""
    _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M2:def classify(n):=>def classify(n):  # 只改注释"],
                       extra_args=["--allow-green", "M2"])
    assert rc == 0, f"显式接受后应当通过：rc={rc} {err[-200:]}"


def test_ambiguous_anchor_is_refused(tmp_path):
    sha0 = _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M3:SMALL=>BIG"])
    assert rc == 1, f"锚点不唯一必须拒绝执行：rc={rc}"
    assert "锚点" in err
    sha1 = hashlib.sha256((tmp_path / "mod.py").read_bytes()).hexdigest()
    assert sha1 == sha0, "拒绝执行时也不能改动文件"


def test_survives_hostile_ambient_encoding(tmp_path):
    """外部注入 latin-1 时，工具自己仍要能打印中文（与教训 #37 形态 ③ 同源）。"""
    _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"],
                           env_extra={"PYTHONIOENCODING": "latin-1"})
    assert rc == 0, f"编码不该影响判定：rc={rc} {err[-300:]}"
    assert "M1: RED OK" in out


def test_baseline_must_be_green(tmp_path):
    """基线就红时，变异对照没有意义——工具必须拒绝继续。"""
    _fixture(tmp_path)
    (tmp_path / "test_mod.py").write_text(
        "from mod import classify\n\n\ndef test_big():\n    assert classify(1) == \"big\"\n",
        encoding="utf-8")
    rc, out, err = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"])
    assert rc == 1
    assert "基线" in out or "基线" in err

def test_syntax_error_is_invalid_not_red(tmp_path):
    """取证子代理实测出的洞：**非零退出不等于判据抓住违规**。

    种一个语法错误进去，pytest 退出码也是非零 —— 旧实现会报「RED OK」（假 RED）。
    正确行为：报 INVALID 并算不合格。
    """
    _fixture(tmp_path)
    rc, out, err = _run(tmp_path, ["M4:def classify(n):=>def classify(n)"])
    assert rc == 2, f"假 RED 必须算不合格：rc={rc} {out[-300:]}"
    assert "INVALID" in out, out[-400:]


def test_expect_fail_must_land_on_the_right_assertion(tmp_path):
    """只证明「红了」不够 —— 必须证明红在**期望的那条断言**上。"""
    _fixture(tmp_path)
    rc_bad, out_bad, _ = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"],
                              extra_args=["--expect-fail", "M1:这个子串根本不会出现"])
    assert rc_bad == 2, f"expect_fail 未命中应当不合格：rc={rc_bad}"
    assert "expect_fail 未命中" in out_bad, out_bad[-400:]

    rc_ok, out_ok, _ = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"],
                            extra_args=["--expect-fail", "M1:test_big"])
    assert rc_ok == 0, f"命中期望断言应当通过：rc={rc_ok} {out_ok[-300:]}"
    assert "M1: RED OK" in out_ok


# ═══════════════════════════════════════════════════════════════
# 硬杀自愈（2026-10-26 真实事故）
#
# `try/finally` 挡得住异常和 Ctrl-C，**挡不住进程被强杀**。那次事故里
# `agent/store.py` 的 M3 变异体（去掉 `or "unknown"` 兜底）留在了工作区，
# 之后所有 pytest 结果都跑在变异体上——红绿都不可信。
#
# 对策是「改之前先落备份、还原成功后删备份；下次跑同一 target 发现备份还在
# 就说明上次没跑完 → 先自愈」。下面用「手工构造硬杀后的状态」来验证，
# 不真去 kill 进程（那样既慢又不稳定）。
# ═══════════════════════════════════════════════════════════════
def _harness_module():
    """加载 tools/变异对照.py 拿内部函数（模块级只有常量，`main()` 在守卫里）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_mutation_harness", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _simulate_hard_kill(tmp_path: Path) -> Path:
    """把工作区摆成「上次被硬杀」的样子：target 是变异体，备份是原文。

    返回备份路径，供断言清理情况。
    """
    harness = _harness_module()
    target = (tmp_path / "mod.py").resolve()
    original = target.read_bytes()
    # 1) 落备份（工具会在改坏之前做这件事）
    bak = harness._bak_path(target)
    bak.write_bytes(original)
    harness._meta_path(target).write_text(str(target), encoding="utf-8")
    # 2) 把 target 改成变异体（模拟「改坏之后、还原之前被强杀」）
    target.write_bytes(original.replace(b'SMALL = "small"', b'SMALL = "MUTATED"'))
    return bak


def test_hard_kill_leftover_is_recovered_before_baseline(tmp_path):
    """被硬杀留下的变异体，必须在下一次运行**读文本之前**还原。

    若不还原，基线会跑在变异体上——这正是那次事故的危害。
    """
    _fixture(tmp_path)
    bak = _simulate_hard_kill(tmp_path)
    assert bak.is_file(), "前置条件：备份应当残留"

    rc, out, err = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"])

    assert "已自动还原" in (out + err), f"必须报告自愈：{out[-300:]} {err[-300:]}"
    # 关键：基线用的是**还原后**的干净文件，所以基线必须绿且变异必须红
    assert rc == 0, f"自愈后应当正常跑完：rc={rc} {out[-400:]}"
    assert "基线：GREEN OK" in out, out[-400:]
    assert "M1: RED OK" in out, out[-400:]
    # 自愈后备份要清掉，否则下次会「重复还原」覆盖掉用户真正的新改动
    assert not bak.is_file(), "自愈成功后备份必须删除"


def test_recover_all_sweeps_leftovers(tmp_path):
    """`--recover-all`：不想再跑 spec 时，也能把工作区扫干净。"""
    _fixture(tmp_path)
    bak = _simulate_hard_kill(tmp_path)

    rc, out, err = _run(tmp_path, [], extra_args=["--recover-all"])

    assert rc == 0, f"rc={rc} {out[-300:]} {err[-300:]}"
    assert "已自动还原" in (out + err), f"必须报告恢复：{out[-300:]} {err[-300:]}"
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == MOD, "内容必须还原成原文"
    assert not bak.is_file(), "恢复后备份必须删除"


def test_successful_run_leaves_no_backup(tmp_path):
    """正常跑完不许留备份 —— 否则下一次运行会误判成「上次被硬杀」。"""
    _fixture(tmp_path)
    harness = _harness_module()
    target = (tmp_path / "mod.py").resolve()

    rc, out, _ = _run(tmp_path, ["M1:    if n > 10:=>    if n > 100:"])

    assert rc == 0, out[-300:]
    assert not harness._bak_path(target).is_file(), "正常路径不该留备份"
    assert not harness._meta_path(target).is_file(), "正常路径不该留 meta"
