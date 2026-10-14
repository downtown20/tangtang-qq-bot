"""用 emotion2vec 给丛雨音频打情绪标签"""
import os, json, gc
from pathlib import Path

import numpy as np
from collections import Counter

from funasr import AutoModel

# 加载模型（只加载一次）
print("Loading emotion2vec model...")
model = AutoModel(model="iic/emotion2vec_plus_large", trust_remote_code=True)
print("Model loaded")

# 2026-08-15：相对项目根，不再硬编码盘符
base = str(Path(__file__).resolve().parent.parent / '音频' / '丛雨')
files = sorted(f for f in os.listdir(base) if f.endswith('.wav'))
total = len(files)

# 按场景分组统计
scene_emotions = {}
results = {}

for i, fname in enumerate(files):
    path = os.path.join(base, fname)
    scene = fname.split('_')[0]
    try:
        res = model.generate(input=path, output_dir=None)
        r = res[0]
        # 取最高概率的标签
        labels = r['labels']
        scores = r['scores']
        best_idx = scores.index(max(scores))
        emotion = labels[best_idx].split('/')[0]  # 只取中文部分，如 "吃惊"
    except Exception as e:
        emotion = f'error:{e}'

    results[fname] = emotion
    scene_emotions.setdefault(scene, []).append(emotion)

    if (i + 1) % 50 == 0:
        print(f'{i+1}/{total}')
        gc.collect()

# 统计每个场景的情绪分布
print("\n=== 各场景情绪分布 ===")
for scene in sorted(scene_emotions):
    counts = Counter(scene_emotions[scene])
    top = counts.most_common(3)
    print(f"{scene} ({len(scene_emotions[scene])}条): {top}")

# 保存
out = str(Path(__file__).resolve().parent.parent / 'voice_cache' / 'emotion_labels.json')
with open(out, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\nSaved to {out}")
