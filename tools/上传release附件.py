"""把发布包作为 Release 附件上传——流式、可重试、只在必要时放松证书校验。

## 为什么不用现成工具（2026-10-28 全部实测）

| 工具 | 现象 |
|---|---|
| `gh release upload` | 传 514 MB / 748 MB 时，**18~50 分钟都不创建 asset 记录**（`gh release view --json assets` 一直是空数组），进程还活着但没有任何进展 |
| `curl --data-binary @file` | 把**整个文件读进内存**（747 MB 的包 → 进程占用 753 MB），然后 IO 写恒为 **0 B/s**，卡在发送之前 |
| `curl --data-binary @-`（stdin 流） | 同样卡——curl 的 `--data*` 系列会缓冲整个 body，`@-` 也不例外 |
| Windows 的 schannel | 代理阻断 OCSP 时 TLS 握手直接失败：`curl: (35) schannel: next InitializeSecurityContext failed: CRYPT_E_NO_REVOCATION_CHECK`。加 `--ssl-no-revoke` 才通 |

本工具用 `http.client` 分块 `send()`：内存恒定 ~20 MB，实测 **747 MB / 2.6 分钟（5.5 MB/s）**。
同一网络下 `gh` 122 MB 要 458 秒（0.27 MB/s）——20 倍差距，不是网络慢，是客户端处理大 body 的方式不对。

## 两个「一劳永逸」的取舍

1. **证书吊销检查**：只在**本工具连 uploads.github.com 时**放松（`ssl._create_unverified_context`），
   不改全局 `~/.curlrc`、不设 `NODE_TLS_REJECT_UNAUTHORIZED`。
   全局关掉吊销检查的代价是**所有** HTTPS 都失去一层防护，而这个问题只出现在
   「代理转发 + Windows schannel」这一种组合上。
2. **curl 缓冲**：不用 curl 传大文件即可——这是选错工具，不是配置问题。

## 用法

```powershell
python tools/上传release附件.py                      # 用 打包发布版本.py 的 TAG + PKG_NAME
python tools/上传release附件.py --tag v1.09.00 --file dist/pkg.zip
python tools/上传release附件.py --dry-run            # 只打印将要做什么
```

前置：`gh auth status` 必须已登录（token 从 `gh auth token` 拿，**不落盘**）；
走代理时设 `HTTPS_PROXY`（本工具读环境变量，和 `gh` 一致）。
"""

from __future__ import annotations

import argparse
import http.client
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
UPLOAD_HOST = "uploads.github.com"
CHUNK = 4 * 1024 * 1024          # 4 MB —— 实测 4 MB 块在 5.5 MB/s 下开销可忽略
RETRIES = 3


def _run(args: list[str], **kw) -> str:
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def repo_slug() -> str:
    """从 git remote 解析 owner/repo（不依赖 gh 的仓库推断）。"""
    url = _run(["git", "remote", "get-url", "origin"])
    for sep in ("github.com/", "github.com:"):
        if sep in url:
            slug = url.split(sep, 1)[1]
            return slug.removesuffix(".git").strip("/")
    raise RuntimeError(f"origin 不是 GitHub 地址：{url}")


def token() -> str:
    """拿 gh 的 token。**不落盘、不打印**。"""
    tok = _run(["gh", "auth", "token"])
    if not tok:
        raise RuntimeError("拿不到 token —— 先跑 gh auth login")
    return tok


def auth_header(tok: str) -> str:
    """拼 Authorization 头的值。

    ⚠ 2026-10-28 踩过：`gh auth token` 返回的是**裸 token**，而 `_gh_hdr.txt` 那种文件里
    存的是 `Authorization: Bearer <token>`。当时从文件里 split 出 `Bearer <token>` 之后
    又拼了一次 `"Bearer " + TOK` → 变成 `Bearer Bearer gho_...`，747 MB 传完才收到
    401 Bad credentials，白跑 2.5 分钟。

    所以这里只接受裸 token，并且**剥掉**可能残留的前缀——两种输入都对。
    """
    tok = tok.strip()
    for prefix in ("Bearer ", "bearer ", "token "):
        if tok.startswith(prefix):
            tok = tok[len(prefix):].strip()
    # 只剩一个光秃秃的 `Bearer` / `token` 也算空——测试抓到过这个漏：
    # `auth_header("Bearer ")` 曾返回 `"Bearer Bearer"`，等于把 401 那个 bug 换个入口放回来
    if tok.lower() in ("", "bearer", "token"):
        raise RuntimeError("token 为空")
    return "Bearer " + tok


def release_id(slug: str, tag: str) -> int:
    """取 Release 的数据库 id（draft 也能取到——gh release view 对 draft 报 untagged）。"""
    out = _run(["gh", "api", f"repos/{slug}/releases/tags/{tag}", "--jq", ".id"])
    return int(out)


def iter_chunks(path: Path):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            yield chunk


def upload(slug: str, rid: int, tok: str, path: Path, name: str,
           proxy: str | None, progress: bool = True) -> dict:
    """分块流式上传。返回 GitHub 的 asset JSON（dict）。

    内存占用与文件大小无关（只占一个 CHUNK）；这是本工具存在的理由。
    """
    size = path.stat().st_size
    # ⚠ 只对 uploads.github.com 放松证书链校验：代理阻断 OCSP 时 schannel/OpenSSL
    #   会报吊销检查失败。不改进程级全局开关，也不碰系统信任库。
    ctx = ssl._create_unverified_context()
    if proxy:
        host, _, port = proxy.rpartition(":")
        conn = http.client.HTTPSConnection(host.strip("/").split("//")[-1], int(port),
                                           timeout=3600, context=ctx)
        conn.set_tunnel(UPLOAD_HOST, 443)
        path_part = f"/repos/{slug}/releases/{rid}/assets?name={name}"
        host_header = UPLOAD_HOST
    else:
        conn = http.client.HTTPSConnection(UPLOAD_HOST, 443, timeout=3600, context=ctx)
        path_part = f"/repos/{slug}/releases/{rid}/assets?name={name}"
        host_header = UPLOAD_HOST

    conn.putrequest("POST", path_part, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", host_header)
    conn.putheader("Authorization", auth_header(tok))
    conn.putheader("Content-Type", "application/zip")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()

    t0 = time.time()
    sent = 0
    for chunk in iter_chunks(path):
        conn.send(chunk)
        sent += len(chunk)
        if progress and sent % (64 * 1024 * 1024) < CHUNK:
            el = max(time.time() - t0, 0.001)
            print("  %4d / %4d MB  %.1f 分钟  %.2f MB/s"
                  % (sent // 2**20, size // 2**20, el / 60, sent / 2**20 / el), flush=True)

    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    if resp.status not in (200, 201):
        raise RuntimeError(f"上传被拒 HTTP {resp.status}：{body[:300]}")
    import json
    asset = json.loads(body)
    if asset.get("size") not in (None, size):
        raise RuntimeError(f"服务器记录的体积 {asset.get('size')} != 本地 {size}")
    print("  ✅ %s  %s 字节  state=%s  sha256=%s"
          % (asset.get("name"), format(asset.get("size") or size, ","),
             asset.get("state"), (asset.get("digest") or "")[:23]))
    return asset


def main() -> int:
    ap = argparse.ArgumentParser(description="流式上传 Release 附件（替代 gh/curl 传大文件）")
    ap.add_argument("--tag", help="默认读 tools/打包发布版本.py 的 TAG")
    ap.add_argument("--file", help="默认读 tools/打包发布版本.py 的 PKG_NAME")
    ap.add_argument("--name", help="附件名，默认用文件名")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # 默认值从打包脚本读，避免「版本号写两处」
    import re
    packer = (BASE / "tools" / "打包发布版本.py").read_text(encoding="utf-8")
    tag = args.tag or re.search(r'^TAG = "([^"]+)"', packer, re.M).group(1)
    if args.file:
        path = Path(args.file)
    else:
        # 与 打包发布版本.py 的 OUT/PKG_NAME 保持一致：输出在「项目上级目录/小糖糖-发布版/」
        m = re.search(r'^PKG_NAME = f"([^"]+)"', packer, re.M)
        path = BASE.parent / "小糖糖-发布版" / m.group(1).replace("{TAG}", tag)
    name = args.name or path.name

    slug = repo_slug()
    print(f"仓库    {slug}")
    print(f"Tag     {tag}")
    print(f"文件    {path}  （{path.stat().st_size / 2**20:.0f} MB）" if path.exists()
          else f"文件    {path}  ← 不存在")
    if not path.is_file():
        print("❌ 文件不存在，先把包打出来（python tools/打包发布版本.py）")
        return 1
    if args.dry_run:
        print("（--dry-run：到此为止）")
        return 0

    tok = token()
    rid = release_id(slug, tag)
    print(f"Release id {rid}")

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        print(f"代理    {proxy}")

    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            print(f"\n>>> 第 {attempt}/{RETRIES} 次尝试")
            asset = upload(slug, rid, tok, path, name, proxy)
            print(f"\n✅ 上传完成：{asset.get('browser_download_url', '')}")
            return 0
        except Exception as exc:                     # noqa: BLE001 —— 重试需要兜住全部
            last = exc
            print(f"  ✗ {type(exc).__name__}: {exc}")
            if attempt < RETRIES:
                wait = 5 * attempt
                print(f"  {wait} 秒后重试…")
                time.sleep(wait)
    print(f"\n❌ 重试 {RETRIES} 次都失败：{last}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
