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
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HARNESS = BASE / "tools" / "变异对照.py"

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