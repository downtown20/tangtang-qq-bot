"""一次性取证脚本：复现 NapCat get_record 返回的文件格式（2026-08-24）。

背景：ffmpeg 转码失败三次（.amr.amr 文件）——需确认 get_record 给的
到底是标准 AMR 还是 SILK v3（QQ 语音历史格式）。token 从 config.yaml
读取，绝不出现在命令行。
"""
import json
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
nap = cfg.get("napcat", {})
token = nap.get("access_token", "")
http_url = nap.get("http_url", "http://127.0.0.1:3000").rstrip("/")

FILES = [
    "8779767942706a8257b26d5a03debd76.amr",  # 2026-08-24 20:21:31 失败
    "9cdeb83515d33eaf75f5ad944c44e87f.amr",  # 20:00:16 失败
    "33f36ea245a77c1b1d33d3a16e25a058.amr",  # 19:59:53 失败
]

for f in FILES:
    r = httpx.post(
        f"{http_url}/get_record",
        json={"file": f, "access_token": token},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    print(f"{f} -> http {r.status_code}")
    try:
        d = r.json()
    except Exception:
        print("  非 JSON 响应:", r.text[:200])
        continue
    print("  status:", d.get("status"), "| retcode:", d.get("retcode"))
    data = d.get("data") or {}
    if not isinstance(data, dict):
        print("  data:", repr(data)[:120])
        continue
    url_or_path = data.get("file", "")
    print("  file 字段:", repr(url_or_path)[:160])
    if not url_or_path:
        continue
    # 拿到内容：可能是本地路径或 URL
    try:
        if url_or_path.startswith("http"):
            content = httpx.get(url_or_path, timeout=15).content
        else:
            p = Path(url_or_path)
            content = p.read_bytes() if p.exists() else None
        if content is None:
            print("  本地路径不存在")
            continue
        print(f"  大小 {len(content)} 字节 | 头 16 字节: {content[:16]!r}")
        print("  魔数:", content[:9])
    except Exception as e:
        print("  取内容失败:", e)
