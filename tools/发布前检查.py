#!/usr/bin/env python3
"""发布前检查（2026-09-19）——除单元测试之外的三道闸门。

单元闸门在 `tests/test_release_readme_contract.py`（pytest 跑）。
本脚本补上那三道「pytest 管不到、但发出去就会丢人」的检查：

  功能闸门  README 里每一句关于包的说法，逐条对照实际包内容
  安全闸门  敏感信息 / HTML 与 Mermaid 注入面 / 外链协议
  浏览器闸门  Mermaid 图真的渲染得出来（失败时 GitHub 只会显示成代码块，
             不报错、不提醒——只能靠真浏览器验）

用法：
    python tools/准备发布.py && python tools/打包发布版本.py
    python tools/发布前检查.py            # 全部跑一遍
    python tools/发布前检查.py --skip-browser   # 跳过浏览器闸门（快）
退出码 0 = 全过；非 0 = 有闸门没过。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
OUT = BASE.parent / "小糖糖-发布版"


def _release_tag() -> str:
    """版本号从打包脚本读——单一来源。

    以前这里和 打包发布版本.py 各写一份字面量，改版本要同时改两处；
    漏一处就会去找一个不存在的包，报出来的错却像是「包没打出来」。
    """
    src = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
    m = re.search(r'^TAG = "([^"]+)"', src, re.M)
    if not m:
        raise RuntimeError("tools/打包发布版本.py 里找不到 TAG——版本号单一来源断了")
    return m.group(1)


PKG = OUT / f"tangtang-{_release_tag()}.zip"

QQ_RE = re.compile(r"\b[1-9]\d{8,10}\b")
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}")
# 同 tools/准备发布.py——原写法 r"[Dd]:\\\\..." 在正则里要求「两个」反斜杠，
# 而真实路径只有一个，所以从来没匹配过。2026-09-19 修。
WIN_RE = re.compile(r"[Dd]:[\\/][^\s\"'，。、]+")
DANGER_HTML = [r"<script", r"javascript:", r"onerror\s*=", r"onload\s*=",
               r"<iframe", r"<object", r"<embed", r"data:text/html"]
# 已知常量：int32 边界、崩溃码、测试夹具里的时间戳（与 准备发布.py 同口径）
QQ_KNOWN = {"2147483647", "2147483648", "3221225477",
            "1787875200", "1787875201", "1787961600", "1787940000", "1700000000"}

_results: list[tuple[str, bool, str]] = []


def check(gate: str, name: str, ok: bool, detail: str = "") -> None:
    _results.append((gate, ok, f"{name}{(' — ' + detail) if detail else ''}"))


def _read_pkg() -> tuple[zipfile.ZipFile, str, str]:
    z = zipfile.ZipFile(PKG)
    return z, z.read("README.md").decode("utf-8"), z.read("docs/发布/发版说明_v1.md").decode("utf-8")


# ═══════════════════════════════════════════════════════
# 功能闸门：README 的说法 vs 包内实际
# ═══════════════════════════════════════════════════════

def gate_functional() -> None:
    G = "功能"
    if not PKG.is_file():
        check(G, "安装包存在", False, f"找不到 {PKG}——先跑 打包发布版本.py")
        return
    check(G, "安装包存在", True, f"{PKG.stat().st_size / 2**20:.0f} MB")
    z, readme, _ = _read_pkg()
    names = set(z.namelist())
    read = lambda p: z.read(p).decode("utf-8")  # noqa: E731

    # 包里的糖糖声线也是转码后的 .mp3（见 打包发布版本.py 的 _voice_mp3）——
    # 只认 .wav 会在换格式当天数出 0 首，而这是个"看起来还在跑"的静默失效
    rvc = {n.split("/")[-1].rsplit(".", 1)[0] for n in names
           if n.startswith("songs/audio/") and n.lower().endswith((".wav", ".mp3"))}
    # 包里的原声是转码后的 .mp3（见 打包发布版本.py 的 _original_mp3），不是 .wav
    orig = {n.split("/")[-1].removesuffix("_FINAL.mp3") for n in names
            if n.startswith("songs/covers/separated/") and n.endswith("_FINAL.mp3")}
    audio = rvc | orig

    # ⚠ 这里不能只数个数。2026-09-19 实测踩过：zip 内路径写成 `{p.stem}_FINAL.mp3`
    # 而 p.stem 已经是「歌名_FINAL」→ 打出「歌名_FINAL_FINAL.mp3」，41 个文件一个都对不上，
    # 而「原声 41 个」的计数检查全绿。要比的是**两版对不对得上**。
    # 41 首里只有 1 首（いきものがかり - SAKURA）天生只有原声版。
    both = rvc & orig
    check(G, "原声与糖糖声线两版对得上（不是只数个数）", len(both) >= len(rvc) - 1,
          f"糖糖声线 {len(rvc)} 首、原声 {len(orig)} 首，能对上 {len(both)} 首")
    stick = [n for n in names if n.startswith("stickers/")
             and n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))]
    mods = [n for n in names if re.fullmatch(r"agent/[^/]+\.py", n)
            and not n.endswith("__init__.py")]
    adr = [n for n in names if re.match(r"docs/decisions/ADR-.*\.md$", n)]
    plan = [n for n in names if re.fullmatch(r"docs/开发规划/[^/]+\.md", n)]

    # README 声称的数字 vs 包内实际
    for label, claimed_pat, actual in (
            ("歌曲数", r"(\d+) 首预录成品", len(audio)),
            ("表情包张数", r"(\d+) 张表情包", len(stick)),
            ("业务模块数", r"(\d+) 个业务模块", len(mods)),
            ("ADR 篇数", r"(\d+) 篇 ADR", len(adr)),
            ("技术报告篇数", r"(\d+) 篇技术设计与规划报告", len(plan))):
        m = re.search(claimed_pat, readme)
        ok = bool(m) and int(m.group(1)) == actual
        check(G, f"README 声称的{label}与包内一致", ok,
              f"声称 {m.group(1) if m else '未写'} / 实际 {actual}")

    inst = read("tools/安装糖糖.py")
    for label, ok, detail in (
            ("安装器有双向配置接线", "_config_switches" in inst and "wire_config" in inst, ""),
            ("默认勾选 = 聊天+记忆/唱歌/控制台", "DEFAULT_FEATURES = [0, 3, 5]" in inst, ""),
            ("装完给出「还差什么」清单", "def report_ready" in inst, ""),
            ("模型下载支持断点续传", ".part" in inst and "Range" in inst, ""),
            ("SnowLuma 前置提示 + 官方链接",
             "check_prerequisites" in inst and "github.com/SnowLuma/SnowLuma/releases" in inst, "")):
        check(G, label, ok, detail)

    # 每个「要用户自己放东西」的目录都得有落点说明。
    # 2026-09-19 主人指出：包里 6 个骨架目录都带摆放说明，唯独 SnowLuma 连目录都没有——
    # 而它偏偏是**唯一一个必须用户手动放**的组件（第三方，不能随包发）。
    for rel in ("SnowLuma/摆放说明.txt", "gpt-sovits/摆放说明.txt", "songs/摆放说明.txt",
                "share_images/摆放说明.txt", "voice_cache/摆放说明.txt",
                "stickers_michele/摆放说明.txt", "stickers_murasame/摆放说明.txt"):
        check(G, f"落点说明「{rel}」在包内", rel in names)

    for rel, label in (("docs/模块地图.md", "模块地图"),
                       ("docs/架构图谱/糖糖架构图谱.html", "架构图谱"),
                       ("docs/用户手册/使用说明.md", "使用说明"),
                       ("CLAUDE.md", "CLAUDE.md"), ("LICENSE", "LICENSE")):
        check(G, f"README 链接的「{label}」在包内", rel in names)


# ═══════════════════════════════════════════════════════
# 安全闸门
# ═══════════════════════════════════════════════════════

def gate_security() -> None:
    G = "安全"
    if not PKG.is_file():
        check(G, "待检文件存在", False, "缺安装包")
        return
    _, readme, note = _read_pkg()

    hits = []
    for label, text in (("README", readme), ("发版说明", note)):
        for i, line in enumerate(text.splitlines(), 1):
            if KEY_RE.search(line) or WIN_RE.search(line):
                hits.append(f"{label}:{i}"); continue
            for m in QQ_RE.findall(line):
                if m not in QQ_KNOWN:
                    hits.append(f"{label}:{i}"); break
    check(G, "无敏感信息（QQ号/密钥/个人路径）", not hits, f"命中 {len(hits)} 处 {hits[:3]}")

    danger = [p for p in DANGER_HTML if re.search(p, readme, re.I)]
    check(G, "无脚本注入面（script/iframe/javascript:）", not danger, str(danger))
    tags = sorted(set(re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)[ >/]", readme)))
    check(G, "HTML 标签在 GitHub 白名单内", set(tags) <= {"b", "br", "div", "i", "p", "em"},
          f"用到 {tags}")

    blocks = re.findall(r"```mermaid\n(.*?)```", readme, re.S)
    bad = [k for k in ("click ", "href", "javascript", "<script")
           if any(k in b.lower() for b in blocks)]
    check(G, "Mermaid 图无 click/href 注入", not bad, str(bad))

    links = sorted(set(re.findall(r"\]\((https?://[^)]+)\)", readme)))
    insecure = [u for u in links if not u.startswith("https://")]
    check(G, "外链全部 HTTPS", not insecure, str(insecure))
    domains = sorted({urllib.parse.urlparse(u).netloc for u in links})
    check(G, "外链域名收敛", set(domains) <= {"github.com", "img.shields.io"}, str(domains))


# ═══════════════════════════════════════════════════════
# 浏览器闸门
# ═══════════════════════════════════════════════════════

def gate_browser() -> None:
    G = "浏览器"
    runner = BASE / "tools" / "渲染架构图.py"
    # encoding 显式指定 UTF-8——Windows 上 text=True 默认走 GBK，读子进程的中文输出会炸
    r = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    ok = r.returncode == 0 and "✅" in r.stdout
    check(G, "README 的 Mermaid 图可渲染", ok, "截图在项目同级「架构图渲染」目录，请肉眼确认")
    if not ok:
        print(r.stdout[-600:], r.stderr[-400:])

    # 从 zip 里取出来验——发布的正是 zip 里那一份，不是工作区里的
    rel = "docs/架构图谱/糖糖架构图谱.html"
    if not PKG.is_file() or rel not in zipfile.ZipFile(PKG).namelist():
        check(G, "架构图谱 HTML 可渲染", False, "包内找不到图谱")
        return
    tmp = BASE.parent / "架构图渲染" / "_from_pkg.html"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(zipfile.ZipFile(PKG).read(rel))
    graph = tmp
    chrome = _find_chrome()
    if not chrome:
        check(G, "架构图谱 HTML 可渲染", False, "未找到 Chrome/Edge")
        return
    r = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                        "--virtual-time-budget=20000", "--dump-dom", graph.as_uri()],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    dom = r.stdout
    # vis-network 加载成功会建出 canvas 并显著撑大 DOM
    ok = "vis-network" in dom and dom.count("<canvas") >= 1 and len(dom) > 100_000
    check(G, "架构图谱 HTML 可渲染（vis-network 生效）", ok,
          f"DOM {len(dom)} 字节 / canvas {dom.count('<canvas')}")


def _find_chrome() -> str | None:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).is_file():
            return p
    return None


# ═══════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()

    print("🚦 发布前检查\n")
    gate_functional()
    gate_security()
    if not args.skip_browser:
        gate_browser()

    print()
    width = max(len(n) for _, _, n in _results) + 2
    for gate in dict.fromkeys(g for g, _, _ in _results):
        rows = [(ok, n) for g, ok, n in _results if g == gate]
        passed = sum(1 for ok, _ in rows if ok)
        print(f"── {gate}闸门  {passed}/{len(rows)} ──")
        for ok, name in rows:
            print(f"  {'✅' if ok else '❌'} {name:<{width}}")

    failed = [(g, n) for g, ok, n in _results if not ok]
    print()
    if failed:
        print(f"❌ 有 {len(failed)} 项没过——修完重跑本脚本")
        return 1
    print(f"✅ 全部 {len(_results)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
