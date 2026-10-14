# 糖糖 RVC 唱歌音色优化方案

> 2026-08-01 | v2 模型基线已就绪，以下是进一步优化路线

---

## 当前基线

| 项目 | 值 |
|------|-----|
| 模型 | `tangtang_v2` |
| 训练数据 | 十年 / 好きだから。/ 把回忆拼好给你（3 首翻唱干声，11.7 分钟）|
| 采样率 | 40k |
| 版本 | v2 |
| 第一版效果 | 比 v1 干净很多，无沙哑电音 |

---

## 优化路线（按优先级）

### 一、推理参数调优（零成本，5 分钟）

不改模型，只调推理参数。四种组合对比：

```
输入: PLANET vocals
模型: tangtang_v2

组合 A: --f0method harvest --index_rate 0.0 --protect 0.5  （最平滑干净）
组合 B: --f0method harvest --index_rate 0.3 --protect 0.33 （平衡）
组合 C: --f0method rmvpe    --index_rate 0.3 --protect 0.33 （当前）
组合 D: --f0method fcpe     --index_rate 0.0 --protect 0.5  （最自然）
```

**运行命令**（在 `Retrieval-based-Voice-Conversion-WebUI` 目录下）：

```bash
# 组合 A
py -3.10 tools/infer_cli.py --input_path "<项目根>/songs/covers/htdemucs/PLANET/vocals.wav" --model_name "tangtang_v2" --index_path "<项目根>/Retrieval-based-Voice-Conversion-WebUI/logs/tangtang_v2/added_IVF100_Flat_nprobe_1_tangtang_v2_v2.index" --opt_path "<项目根>/songs/audio/_test_A.wav" --f0method "harvest" --index_rate 0.0 --protect 0.5

# 组合 B
... --f0method "harvest" --index_rate 0.3 --protect 0.33 --opt_path ".../_test_B.wav"

# 组合 D  
... --f0method "fcpe" --index_rate 0.0 --protect 0.5 --opt_path ".../_test_D.wav"
```

组合 C 已经生成（`_test_v2_planet.wav`），不用跑。

听完四个版本，挑最好的参数组合作为默认。

---

### 二、扩展训练数据（1-2 小时）

从 `songs/covers/htdemucs/` 中再选 1-2 首音色接近的翻唱干声。

**候选**（按音色匹配度排列）：

| 翻唱 | 预估音色 | 时长 |
|------|------|------|
| 小幸运 | 年轻温柔女声 | ~3.1min |
| 遇见 | 干净清澈女声 | ~3.5min |
| 光年之外 | 有力量的年轻女声 | ~3.9min |
| 红豆 | 温柔抒情女声 | ~4.3min |
| 隐形的翅膀 | 清亮女声 | ~3.7min |
| 晴天 | 温暖女声翻唱 | ~4.6min |

**流程**：

1. 听干声 → 选 1-2 首
2. 运行数据准备脚本（替换路径即可）：

```bash
cd /d <项目根>
py -3.10 -c "
import numpy as np, soundfile as sf
from pathlib import Path
import shutil

# 原有 3 首 + 新增的（替换为你选的歌）
sources = [
    'songs/covers/htdemucs/十年/vocals.wav',
    'songs/covers/htdemucs/好きだから。/vocals.wav',
    'songs/covers/htdemucs/把回忆拼好给你/vocals.wav',
    'songs/covers/htdemucs/小幸运/vocals.wav',   # 示例，替换
]

clean_dir = Path('RVC_dataset/clean_v3')
rvc_input = Path('Retrieval-based-Voice-Conversion-WebUI/dataset/tangtang_v3')
segments_dir = Path('RVC_dataset/segments_v3')

for d in [clean_dir, rvc_input, segments_dir]:
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob('*'): f.unlink()

for src in sources:
    name = Path(src).parent.name
    data, sr = sf.read(src)
    if data.ndim > 1: data = data.mean(axis=1)
    peak = np.abs(data).max()
    if peak > 0: data = data / peak * 0.7
    threshold = 0.005
    non_silent = np.abs(data) > threshold
    if non_silent.any():
        idx = np.where(non_silent)[0]
        start = max(0, idx[0] - sr//10)
        end = min(len(data), idx[-1] + sr//10)
        data = data[start:end]
    out = clean_dir / '{}.wav'.format(name)
    sf.write(str(out), data, sr)
    print('{} -> {:.0f}s'.format(name, len(data)/sr))

total_seg = 0
for wav_file in sorted(clean_dir.glob('*.wav')):
    name = wav_file.stem
    data, sr = sf.read(str(wav_file))
    energy = np.abs(data)
    window = int(sr * 0.05)
    voice_mask = np.zeros(len(data), dtype=bool)
    for i in range(0, len(data) - window, window):
        if np.max(energy[i:i+window]) > 0.01:
            voice_mask[i:i+window] = True
    pos, seg_idx = 0, 0
    while pos < len(data):
        while pos < len(data) and not voice_mask[pos]: pos += 1
        if pos >= len(data): break
        seg_start = pos
        end = min(seg_start + int(sr * 5.0), len(data))
        best_end = end
        search_start = max(seg_start + int(sr * 2.5), end - int(sr * 1.0))
        for p in range(search_start, end, int(sr * 0.1)):
            if not voice_mask[p]: best_end = p; break
        seg_data = data[seg_start:best_end]
        if len(seg_data)/sr < 2.5 or np.abs(seg_data).max() < 0.01:
            pos = best_end; continue
        seg_name = '{}_{:03d}.wav'.format(name, seg_idx)
        sf.write(str(segments_dir / seg_name), seg_data, sr)
        shutil.copy2(str(segments_dir / seg_name), str(rvc_input / seg_name))
        seg_idx += 1; total_seg += 1
        pos = best_end
    print('{} -> {} segments'.format(name, seg_idx))

total_dur = sum(sf.info(str(f)).duration for f in segments_dir.glob('*.wav'))
print('Total: {} segments, {:.1f} min'.format(total_seg, total_dur/60))
print('Next: py -3.10 tools/train_rvc.py --name tangtang_v3 --sr 40k --epochs 200 --batch 6 --save-every 20')
```

3. 训练：

```bash
cd /d <项目根>
py -3.10 tools/train_rvc.py --name tangtang_v3 --sr 40k --epochs 200 --batch 6 --save-every 20
```

4. 训练完成后转推理格式 + 索引（和之前一样）。

---

### 三、提升采样率到 48k（2 小时）

48k 高频细节更多，声音更通透。需要重新训练。

```bash
cd /d <项目根>
py -3.10 tools/train_rvc.py --name tangtang_v3_48k --sr 48k --epochs 200 --batch 4 --save-every 20
```

注意：48k 显存占用更高，batch 降到 4。

---

### 四、切换 PyTorch 稳定版（30 分钟）

当前 PyTorch 2.14.0.dev 是开发版，影响人声分离质量。训练完成后切换：

```bash
# 1. 安装稳定版
py -3.10 -m pip install torch==2.13.0 torchaudio==2.13.0

# 2. 回退 train.py, extract_feature_print.py, extract_f0_print.py 的 patches
#    这三行 monkey-patch 代码在 2.13.0 不需要:
#      import torch as _t; _orig = _t.load; ...
```

**注意**：切换 PyTorch 后 RVC 推理需要重新测试。如果 RVC 报 `weights_only` 错误，再重新打 patches。

---

### 五、集成到唱歌系统

RVC 模型就绪后，接入糖糖的唱歌流程：

1. 用 DiffSinger 或预制翻唱 → Demucs 分离 → RVC 转糖糖音色
2. 升级 `sing` 工具让 LLM 知道可以"哼两句"
3. 歌词注入系统提示词，让 LLM 在对话中自然引用

详见 `docs/开发规划/唱歌说话自然融合方案.md`

---

## 执行顺序建议

```
1. 推理参数调优（5 分钟）         ← 最快见效
   ↓
2. 扩展训练数据（选 1-2 首）      ← 提升最大
   ↓
3. 重练 v3（1-2 小时）
   ↓
4. 可选：48k 训练
   ↓
5. 切换 PyTorch 稳定版
   ↓
6. 集成唱歌系统
```

---

## 快速命令速查

```bash
# 数据准备好后的完整训练流程
cd /d <项目根>
py -3.10 tools/train_rvc.py --name tangtang_v3 --sr 40k --epochs 200 --batch 6 --save-every 20

# 训练完成后转推理格式（需要手动跑，参考之前步骤）
# 然后测试
cd Retrieval-based-Voice-Conversion-WebUI
py -3.10 tools/infer_cli.py --input_path "<项目根>/songs/covers/htdemucs/PLANET/vocals.wav" --model_name "tangtang_v3" --index_path "新增模型时确认路径" --opt_path "<项目根>/songs/audio/_test_v3.wav" --f0method "harvest" --index_rate 0.3 --protect 0.5
```
