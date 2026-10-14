"""下载 MiniCPM-V 4.6 —— 命令行 git 方式"""
import subprocess, os, shutil
from pathlib import Path

# 2026-08-15：相对项目根，不再硬编码盘符
dst = str(Path(__file__).resolve().parent.parent / 'models' / 'minicpm-v')
if os.path.exists(dst):
    shutil.rmtree(dst)

# git clone 关 SSL
env = {**os.environ, 'GIT_SSL_NO_VERIFY': '1'}
r = subprocess.run([
    'git', 'clone', '--depth', '1',
    'https://hf-mirror.com/openbmb/MiniCPM-V-4_6',
    dst
], env=env)
if r.returncode == 0:
    print('OK')
else:
    print(f'git clone failed: {r.returncode}')
    print('手动下载：浏览器打开 https://hf-mirror.com/openbmb/MiniCPM-V-4_6')
    print(f'下载所有文件到 {dst}/')
