#!/usr/bin/env python3
"""把 README 里的 Mermaid 图渲染成 PNG（2026-09-19）。

用途：README 靠 Mermaid 承载"她是怎么活的"这张架构图，而 GitHub 渲染失败时
**只会显示成一段代码块**——不报错、不提醒，读者看到一堆箭头就划走了。
所以发版前必须在真浏览器里渲一遍，用眼睛确认。

做法：抽 ```mermaid 块 → 塞进一个引用 mermaid CDN 的 HTML → 无头 Chrome 截图。
零依赖（Chrome 本机已有），不装 playwright。

用法：
    python tools/渲染架构图.py                 # 渲染 README 里的图
    python tools/渲染架构图.py --readme 路径    # 指定文件
输出：项目同级目录「架构图渲染/<序号>-<类型>.png」
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({ startOnLoad: false, theme: 'default' });
window.__done = false;
try {
  await mermaid.run({ querySelector: '.mermaid' });
  window.__ok = true;
} catch (e) {
  window.__ok = false;
  document.body.insertAdjacentHTML('beforeend',
    '<pre id="err" style="color:#c00;font:14px monospace">' + String(e) + '</pre>');
}
window.__done = true;
</script>
<style>
  body { font-family: system-ui, sans-serif; padding: 24px; background: #fff; }
  .mermaid { margin: 0 0 40px; }
</style></head>
<body>
<!--BLOCKS-->
</body></html>
"""


def _find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def _extract_blocks(text: str) -> list[str]:
    return [b.strip() for b in re.findall(r"```mermaid\n(.*?)```", text, re.S)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readme", default=str(BASE / "docs" / "发布" / "README.md"))
    args = ap.parse_args()

    readme = Path(args.readme)
    if not readme.is_file():
        print(f"❌ 找不到 {readme}")
        return 1
    chrome = _find_chrome()
    if not chrome:
        print("❌ 未找到 Chrome / Edge——无法做真浏览器验证")
        return 1

    blocks = _extract_blocks(readme.read_text(encoding="utf-8"))
    if not blocks:
        print("❌ README 里没有 mermaid 图")
        return 1

    out = BASE.parent / "架构图渲染"
    out.mkdir(parents=True, exist_ok=True)
    print(f"🧭 渲染 {len(blocks)} 张图 → {out}")

    for i, block in enumerate(blocks, 1):
        kind = block.split()[0] if block.split() else "diagram"
        html = out / f"_tmp_{i}.html"
        html.write_text(_HTML.replace("<!--BLOCKS-->",
                                      f'<pre class="mermaid">{block}</pre>'),
                        encoding="utf-8")
        png = out / f"{i:02d}-{kind}.png"
        r = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--virtual-time-budget=15000",
             "--window-size=1150,1500",
             f"--screenshot={png}", html.as_uri()],
            capture_output=True, text=True, timeout=120)
        ok = png.is_file() and png.stat().st_size > 5000
        print(f"  {'✅' if ok else '❌'} {png.name}"
              + ("" if ok else f"  (chrome rc={r.returncode})"))
        html.unlink(missing_ok=True)

    print("\n👀 请打开这些 PNG 用眼睛确认：图渲染出来了，不是一片空白或错误提示。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
