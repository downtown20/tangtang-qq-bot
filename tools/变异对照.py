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

或写一份 spec（推荐；串里有换行时只能这样；spec 都放在仓库根的 `变异/` 目录下）：

    python tools/变异对照.py --spec 变异/变异.json

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
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def _read(path: Path):
    """返回 (文本, sha256[:16], 是否 CRLF)。文本保留原始换行。"""
    raw = path.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()[:16], ("\r\n" in raw.decode("utf-8"))


# ═══════════════════════════════════════════════════════════════
# 崩溃自愈：try/finally 只挡得住异常和 Ctrl-C，**挡不住进程被强杀**
#
# 2026-10-26 实际发生过一次：一次硬杀（超时 kill）把 `agent/store.py` 的
# M3 变异体（`_revision_text(actor).strip() or "unknown"` 去掉兜底）留在了
# 工作区。之后所有 pytest 结果都不可信——基线自己在变异体上跑，红绿都无意义。
# 这正是教训 #45「测量工具会污染被测系统」的硬杀版本。
#
# 对策：改文件**之前**先把原文落到系统临时目录，还原成功后删掉。
# 下次跑同一个 target 时若发现备份还在，就说明上次没跑完 → 先自愈再继续。
# 备份放系统临时目录（不进仓库、不受 .gitignore 影响，也不会被 git status 看见）。
# ═══════════════════════════════════════════════════════════════
def _bak_path(path: Path) -> Path:
    """备份文件路径：按 target 绝对路径取 hash，同一文件永远同一个备份。"""
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / ("tt_mutation_bak_" + key)


def _meta_path(path: Path) -> Path:
    """备份的元数据：记住这份备份对应哪个 target（供 --recover-all 扫全盘用）。"""
    return _bak_path(path).with_name(_bak_path(path).name + ".meta")


def _recover_if_dirty(path: Path) -> bool:
    """上次被硬杀过？把备份写回去。返回 True 表示这次做了恢复。"""
    bak = _bak_path(path)
    if not bak.is_file():
        return False
    try:
        path.write_bytes(bak.read_bytes())
    except OSError as e:
        print("[!] 检测到上次运行被中断，但自动还原失败：" + str(e), file=sys.stderr)
        return False
    bak.unlink(missing_ok=True)
    _meta_path(path).unlink(missing_ok=True)
    print("[!] 检测到上次运行被中断（备份残留），已自动还原：" + str(path), file=sys.stderr)
    print("    还原后请重跑一遍基线——上次的测试结果不可信。", file=sys.stderr)
    # 自愈是「用备份无条件覆盖」，代价要讲明白：中断之后如果你手动改过这个文件，
    # 那些改动现在没了（2026-10-27 审查 Minor）。用 git 能看回来。
    print("    ⚠ 若在中断之后手动改过这个文件，这次还原会覆盖那些改动——"
          "请 `git diff` 核对一下再继续。", file=sys.stderr)
    return True


def _recover_all() -> int:
    """扫临时目录里所有残留备份，逐个还原。硬杀后不想再跑 spec 时用这个。

    退出码要能区分「没有残留」「都恢复了」「有恢复失败的」——
    旧版写的是 `0 if found >= 0 else 1`，`found` 是 int 永远 >= 0，
    等于这条命令**永远报成功**（2026-10-27 审查 Minor）。
    """
    found = 0
    failed = 0
    for meta in sorted(Path(tempfile.gettempdir()).glob("tt_mutation_bak_*.meta")):
        try:
            target = Path(meta.read_text(encoding="utf-8").strip())
        except OSError:
            continue
        # 只有 meta、没有 bak：说明备份已被别的一次运行清掉了，这条 meta 是脏的。
        bak = Path(str(meta)[: -len(".meta")])
        if not bak.is_file():
            print("[·] 跳过（备份已不存在）：" + str(target), file=sys.stderr)
            meta.unlink(missing_ok=True)
            continue
        if not target.is_file():
            print("[·] 跳过（target 已不存在）：" + str(target), file=sys.stderr)
            meta.unlink(missing_ok=True)
            continue
        if _recover_if_dirty(target):
            found += 1
        else:
            failed += 1
    print("恢复完成：%d 个文件成功%s"
          % (found, "" if not failed else "，%d 个失败（见上方报错）" % failed))
    return 0 if failed == 0 else 1


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
    ap.add_argument("--recover-all", action="store_true",
                    help="扫描并还原所有「上次被硬杀留下的变异体」——不跑测试，只修工作区")
    ap.add_argument("--root", default=str(BASE), help="根目录（默认仓库根；自测时可指向临时目录）")
    args = ap.parse_args(argv)

    if args.recover_all:
        return _recover_all()

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
    # 先自愈：上次若被硬杀，工作区里可能正躺着一个变异体。必须在读文本之前还原，
    # 否则基线会跑在变异体上——红绿都失去意义（教训 #45）。
    _recover_if_dirty(path)
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
        # 落备份 → 改坏 → 跑 → 一定还原 → 删备份。
        # 硬杀可能让 finally 不执行，那时备份会留下，下次跑同一 target 时自愈。
        bak, meta = _bak_path(path), _meta_path(path)
        try:
            bak.write_bytes(text.encode("utf-8"))
            meta.write_text(str(path), encoding="utf-8")
            path.write_text(mutated, encoding="utf-8", newline="")
            got = _run_tests(tests, root)
        finally:
            path.write_text(text, encoding="utf-8", newline="")
            bak.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
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