"""
自动下载并配置 ffmpeg
从 GitHub 下载 Windows 版 ffmpeg，解压到本地，加入 PATH
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

FFMPEG_URLS = [
    # BtbN 的 Windows 编译版 (推荐)
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    # 备用: Gyan.dev 的 essentials 版
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]
INSTALL_DIR = Path.home() / "ffmpeg"


def main():
    print("🎬 小糖糖 — ffmpeg 自动安装器")
    print()

    # 1. 检查是否已安装
    if shutil.which("ffmpeg"):
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        print(f"✅ ffmpeg 已可用: {result.stdout.split(chr(10))[0]}")
        return

    # 2. 下载（多源尝试）
    zip_path = INSTALL_DIR / "ffmpeg.zip"
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    downloaded = False
    for url in FFMPEG_URLS:
        print(f"📥 尝试下载: {url[:60]}...")
        try:
            resp = urlopen(url, timeout=300)
            if resp.status != 200:
                print(f"   状态码 {resp.status}，换下一个源...")
                continue
            total = int(resp.headers.get("Content-Length", 0))
            dl = 0
            with open(zip_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    dl += len(chunk)
                    if total:
                        pct = dl * 100 // total
                        mb = dl / 1024 / 1024
                        total_mb = total / 1024 / 1024
                        print(f"\r  下载中... {pct}% ({mb:.1f}/{total_mb:.1f} MB)", end="")
            print()
            print("✅ 下载完成")
            downloaded = True
            break
        except Exception as e:
            print(f"   失败: {e}")

    if not downloaded:
        print("❌ 所有下载源都失败了")
        print("💡 请手动下载 ffmpeg: https://ffmpeg.org/download.html")
        print("   下载 Windows 版，把 bin/ffmpeg.exe 放到 C:\\Windows\\System32\\ 即可")
        print("   或者用 winget: winget install ffmpeg")
        return

    # 3. 解压
    print("📦 解压中...")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(INSTALL_DIR)
        zip_path.unlink()
        print("✅ 解压完成")
    except Exception as e:
        print(f"❌ 解压失败: {e}")
        return

    # 4. 找到 ffmpeg.exe（不管什么目录结构）
    ffmpeg_exe = None
    ffprobe_exe = None
    for root, dirs, files in os.walk(INSTALL_DIR):
        for f in files:
            if f == "ffmpeg.exe":
                ffmpeg_exe = Path(root) / f
            elif f == "ffprobe.exe":
                ffprobe_exe = Path(root) / f
        if ffmpeg_exe and ffprobe_exe:
            break

    if not ffmpeg_exe:
        print("❌ 在解压文件中找不到 ffmpeg.exe，压缩包结构可能有变")
        print("💡 请手动下载: https://ffmpeg.org/download.html")
        return

    bin_dir = ffmpeg_exe.parent
    print(f"   找到: {ffmpeg_exe}")

    system32 = Path(os.environ.get("WINDIR", "C:\\Windows")) / "System32"

    try:
        shutil.copy2(ffmpeg_exe, system32 / "ffmpeg.exe")
        shutil.copy2(ffprobe_exe, system32 / "ffprobe.exe")
        print(f"✅ ffmpeg.exe → {system32}")
    except PermissionError:
        print("⚠️ 权限不足，改用 PATH 方式...")
        # 加入用户 PATH
        bin_str = str(bin_dir.resolve())
        try:
            result = subprocess.run(
                ["setx", "PATH", f"{bin_str};%PATH%"],
                capture_output=True, text=True, shell=True,
            )
            if result.returncode == 0:
                print(f"✅ {bin_str} 已加入 PATH（新开命令行生效）")
            else:
                print(f"⚠️ setx 失败: {result.stderr}")
                print(f"💡 请手动把 {bin_str} 加入系统环境变量 Path")
        except Exception as e:
            print(f"⚠️ 自动加入 PATH 失败: {e}")
            print(f"💡 请手动把 {bin_str} 加入系统环境变量 Path")
            print("   或者把 ffmpeg.exe 复制到 C:\\Windows\\System32\\")

    # 5. 验证
    print()
    if shutil.which("ffmpeg"):
        print("🎉 ffmpeg 安装成功！")
    else:
        print("⚠️ 安装完成但需要新开命令行窗口才能生效")
        print(f"   ffmpeg 位置: {ffmpeg_exe}")


if __name__ == "__main__":
    main()
