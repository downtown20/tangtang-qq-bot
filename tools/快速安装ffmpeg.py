"""
ffmpeg 快速安装器 — 从 gyan.dev 下载 essentials 版（~50MB，不是 253MB 全量版）
直接双击运行，或: python 快速安装ffmpeg.py
"""
import urllib.request, zipfile, shutil, os
from pathlib import Path

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_DIR = Path.home() / "ffmpeg"
SYSTEM32 = Path(os.environ["WINDIR"]) / "System32"

print("🎬 ffmpeg 快速安装 (essentials ~50MB)")
print()

# 1. 下载
tmp = Path(os.environ["TEMP"]) / "ffmpeg_ess.zip"
if tmp.exists() and tmp.stat().st_size > 1000000:
    print(f"📦 使用已缓存的文件: {tmp.stat().st_size//1024//1024}MB")
else:
    print(f"📥 下载 {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        dl = 0
        chunks = []
        while True:
            chunk = resp.read(65536)
            if not chunk: break
            chunks.append(chunk)
            dl += len(chunk)
            if total:
                print(f"\r  {dl*100//total}%  {dl//1024//1024}/{total//1024//1024}MB", end="")
        print()
        tmp.write_bytes(b"".join(chunks))
    print(f"✅ 下载完成: {tmp.stat().st_size//1024//1024}MB")

# 2. 解压
print("📦 解压...")
FFMPEG_DIR.mkdir(exist_ok=True)
with zipfile.ZipFile(tmp) as zf:
    zf.extractall(FFMPEG_DIR)
tmp.unlink()
print("✅ 解压完成")

# 3. 找到 ffmpeg.exe
exe = None
for root, dirs, files in os.walk(str(FFMPEG_DIR)):
    if "ffmpeg.exe" in files:
        exe = Path(root) / "ffmpeg.exe"
        break
if not exe:
    print("❌ 找不到 ffmpeg.exe！")
    input("按回车退出...")
    exit(1)

# 4. 复制到 System32
try:
    shutil.copy2(exe, SYSTEM32 / "ffmpeg.exe")
    print(f"✅ ffmpeg.exe → {SYSTEM32}")
except PermissionError:
    print("⚠️ 权限不足，换用户 PATH...")
    bin_str = str(exe.parent.resolve())
    os.system(f'setx PATH "{bin_str};%PATH%"')
    print(f"✅ {bin_str} 已加入 PATH（新开终端生效）")

# 5. 验证
print()
import subprocess as sp
r = sp.run([str(SYSTEM32 / "ffmpeg.exe"), "-version"], capture_output=True, text=True)
if r.returncode == 0:
    print(f"🎉 安装成功！{r.stdout.split(chr(10))[0]}")
else:
    print("⚠️ 安装可能有问题，重启终端后再试")

input("\n按回车退出...")
