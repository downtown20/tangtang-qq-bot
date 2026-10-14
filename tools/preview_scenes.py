"""预览每个丛雨场景的代表音频并注册为 GPT-SoVITS speaker"""
import os, soundfile, subprocess
from pathlib import Path

# 2026-08-15：相对项目根，不再硬编码盘符
base = str(Path(__file__).resolve().parent.parent / '音频' / '丛雨')
groups = {}
for f in sorted(os.listdir(base)):
    if f.endswith('.wav'):
        prefix = f.split('_')[0]
        groups.setdefault(prefix, []).append(f)

print("=== 丛雨音频 9 个场景 ===\n")
for k in sorted(groups):
    mids = sorted(groups[k])
    mid = mids[len(mids)//2]
    path = os.path.join(base, mid)
    info = soundfile.info(path)
    print(f"{k}: {len(groups[k])} 条音频, 代表: {mid} ({info.duration:.1f}s)")

print("\n请听每个场景的代表音频，记录情绪。\n可用的情绪标签（对应糖糖语音）：")
print("  normal 日常, happy 开心, excited 兴奋, coquettish 撒娇")
print("  sad 难过, gentle 温柔, tsundere 傲娇")
print("\n然后告诉我每个场景对应什么情绪。比如：")
print("  mur001=normal, mur003=happy, mur007=sad ...")
