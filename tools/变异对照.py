"""变异对照（mutation control）——把「改坏了会不会红」变成一条命令。

为什么需要它（2026-10-16 夜实测，见 CLAUDE.md 教训 #37）
────────────────────────────────────────────────────────
本项目的验证尺子会**自己骗人**，一夜撞到四种形态：
  ① 检查器写了但从不生效（正则永远匹配不到，照样报「零命中」）；
  ② 判据测的是环境不是行为（「30ms 内 tick≥5」实际测的是 Windows 定时器粒度）；
  ③ 两端口径不一致（父进程 GBK 解子进程 UTF-8 → 中文判据乱码，假红）；
  ④ **变异太弱**（插了一行但 return 还在 ⇒ 行为没变 ⇒ 照样绿 = 假变异）。
本工具专治 ④：把「故意改坏 → 必须红 → 还原」做成一条可复现的命令，
并**主动把 GREEN 的变异标成假变异**——假绿比假红危险得多，因为它长得和真绿一样。

用法
────
    python tools/变异对照.py --target agent/foo.py --tests tests/test_foo.py \
        --mut "M1:旧串=>新串" --mut "M2:旧串=>新串"

或写一份 spec（推荐；串里有换行时只能这样）：

    python tools/变异对照.py --spec 变异.json

    {"target": "agent/foo.py",
     "tests": "tests/test_foo.py",
     "mutations": [{"name": "M1", "old": "a = 1", "new": "a = 2"}]}

约定（都是踩出来的）
──────────────────
- old 锚点必须**恰好出现一次**：出现 0 次（锚点写错/文件已改）或 ≥2 次（锚点不唯一）都直接报错退出，
  绝不「尽量匹配」——本仓是 CRLF，多行锚点写成 LF 会 count=0，本工具自动适配换行风格。
- **无论测试红绿，退出前一定还原**，并核对 sha256 与改前**逐字节一致**；还原失败立刻非零退出。
- 所有变异都红 → 退出码 0；有 GREEN（假变异）→ 退出码 2 并打印警告；出错 → 1。
  想显式接受某个 GREEN（例如「这个变异本来就不改变行为」），用 --allow-green M1。
- 子进程编码两端都钉死为 UTF-8（与 tests/test_static_check.py 的 _run_capture 同口径，
  理由见教训 #37 形态 ③）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _read(path: Path):
    """返回 (文本, sha256[:16], 是否 CRLF)。文本保留原始换行。"""
    raw = path.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()[:16], ("\r\n" in raw.decode("utf-8"))


def _norm(anchor: str, crlf: bool) -> str:
    """把锚点里的换行统一成目标文件的风格（CRLF 仓的隐形杀手）。"""
    flat = anchor.replace("\r\n", "\n")
    return flat.replace("\n", "\r\n") if crlf else flat


def _run_tests(tests: str, root: Path):
    """返回 dict：真红只看 pytest 的失败计数，不看退出码。

    2026-10-16 取证子代理实测出的洞：**任何非零退出都算 RED** —— 于是「种一个语法错误」
    也会被报成 RED OK（假 RED）。退出码非零但一个用例都没失败 = 语法/收集错误，
    那不是「判据抓住了违规」，必须单独报出来（INVALID）。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    out = subprocess.run([sys.executable, "-m", "pytest", tests, "-q"],
                         cwd=root, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env)
    text = (out.stdout or "") + (out.stderr or "")
    m = re.search(r"(\d+) failed", text)
    failed = int(m.group(1)) if m else 0
    rc = out.returncode or 0
    return {"rc": rc, "failed": failed,
            "invalid": rc != 0 and failed == 0,
            "tail": text[-1500:]}


def _apply(text: str, old: str, new: str, crlf: bool):
    """锚点必须恰好一次；否则返回 None（调用方报错退出）。"""
    o, n = _norm(old, crlf), _norm(new, crlf)
    if text.count(o) != 1:
        return None
    return text.replace(o, n, 1)


def main(argv=None) -> int:
    # 自己的 stdout/stderr 也钉死为 UTF-8：否则外部注入 PYTHONIOENCODING=latin-1 时，
    # 本工具打印中文会自己抛 UnicodeEncodeError（与它要防的形态 ③ 是同一个病）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="变异对照：改坏 -> 必须红 -> 还原")
    ap.add_argument("--spec", help="JSON 规格文件（target/tests/mutations）")
    ap.add_argument("--target", help="被测文件（相对仓库根）")
    ap.add_argument("--tests", help="pytest 目标（文件或 文件::用例）")
    ap.add_argument("--mut", action="append", default=[], help="NAME:旧串=>新串（可重复）")
    ap.add_argument("--allow-green", action="append", default=[], help="显式接受这些变异为 GREEN（会记进 JSON 报告）")
    ap.add_argument("--expect-fail", action="append", default=[],
                    help="NAME:子串 —— 该变异变红时必须命中这个子串（证明红在对的断言上）")
    ap.add_argument("--json", help="把结果写成 JSON")
    ap.add_argument("--root", default=str(BASE), help="根目录（默认仓库根；自测时可指向临时目录）")
    args = ap.parse_args(argv)

    if args.spec:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        target, tests = spec["target"], spec["tests"]
        muts = [(m["name"], m["old"], m["new"]) for m in spec["mutations"]]
        expect = {m["name"]: m.get("expect_fail", "") for m in spec["mutations"]}
    else:
        target, tests = args.target, args.tests
        muts = []
        expect = {}
        for item in args.expect_fail:
            k, _, v = item.partition(":")
            expect[k] = v
        for item in args.mut:
            name, _, body = item.partition(":")
            old, _, new = body.partition("=>")
            muts.append((name, old, new))
    if not (target and tests and muts):
        print("需要 --target/--tests 与至少一个 --mut，或一份 --spec", file=sys.stderr)
        return 1

    root = Path(args.root).resolve()
    path = (root / target).resolve()
    if root not in path.parents or not path.is_file():
        print("目标不合法（必须在仓库内且是文件）：" + str(path), file=sys.stderr)
        return 1
    text, sha0, crlf = _read(path)
    print("目标 %s  sha256=%s  %s  %d 字符" % (target, sha0, "CRLF" if crlf else "LF", len(text)))

    base = _run_tests(tests, root)
    print("基线：" + ("RED（基线就红，先修基线）" if base["rc"] != 0 else "GREEN OK"))
    if base["rc"] != 0:
        print("基线是红的——变异对照无意义。先让基线绿。", file=sys.stderr)
        return 1

    rows = []
    for name, old, new in muts:
        mutated = _apply(text, old, new, crlf)
        if mutated is None:
            print(name + ": 锚点不唯一或找不到（count != 1）——锚点写错了，不猜。", file=sys.stderr)
            return 1
        try:
            path.write_text(mutated, encoding="utf-8", newline="")
            got = _run_tests(tests, root)
        finally:
            path.write_text(text, encoding="utf-8", newline="")
        _, sha1, _ = _read(path)
        if sha1 != sha0:
            print(name + ": 还原失败！" + sha0 + " -> " + sha1, file=sys.stderr)
            return 1
        want = expect.get(name, "")
        if got["invalid"]:
            verdict = "INVALID 非零退出但不是测试失败（语法/收集错误）——不算红"
        elif got["failed"] and want and want not in got["tail"]:
            verdict = "INVALID 红了但没红在期望的断言上（expect_fail 未命中）"
        elif got["failed"]:
            verdict = "RED OK"
        else:
            verdict = "GREEN 假变异"
        print(name + ": " + verdict)
        rows.append({"name": name, "red": got["failed"] > 0,
                     "invalid": bool(got["invalid"]) or bool(got["failed"] and want and want not in got["tail"]),
                     "expect_fail_ok": (not want) or (want in got["tail"]),
                     "failed": got["failed"], "rc": got["rc"], "tail": got["tail"][-600:]})
        red = got["failed"] > 0 and not rows[-1]["invalid"]
        if not red:
            print("    —— 这个变异没有让判据变红：要么变异太弱（改的地方不改变行为），要么判据没覆盖这条路径。都不合格。")

    fake = [r["name"] for r in rows
            if (not r["red"] or r.get("invalid")) and r["name"] not in args.allow_green]
    _, sha2, _ = _read(path)
    print("还原核对：sha256 " + sha2 + (" == 改前 OK" if sha2 == sha0 else " !! 不一致 !!"))
    bad = [r["name"] for r in rows if r.get("invalid")]
    print("合计 %d 个变异：%d 真红 / %d 不合格%s" % (
        len(rows), sum(1 for r in rows if r["red"] and not r.get("invalid")), len(fake),
        ("（其中 INVALID %s：非零退出但不是测试失败，或 red 在错的断言上）" % bad) if bad else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"target": target, "tests": tests, "sha": sha0, "rows": rows, "fake": fake},
            ensure_ascii=False, indent=1), encoding="utf-8")
    if fake:
        print("假变异：" + str(fake) + " —— 变异对照不合格（想显式接受用 --allow-green）", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())