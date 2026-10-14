# RVC v4 训练计划 —— 不惜代价追求最佳音色

> 2026-08-01 | 目标：一次到位，不妥协

---

## 数据集

| 歌 | 格式 | 时长 | 品质 |
|------|------|------|:--:|
| G.E.M.邓紫棋 - 泡沫 | WAV 96kHz | 4:19 | 极高 |
| 『ユイカ』 - 好きだから。 | WAV 96kHz | 4:59 | 极高 |
| 曾沛慈 - 一个人想着一个人 | WAV 96kHz | 4:04 | 极高 |
| 王唯旖 - 舍得 | WAV 96kHz | 4:22 | 极高 |
| 敏感自卑偏激 - 你在 不在 | MP3 44.1kHz | 3:52 | 中等 |
| 在的星星 - 最后一页 | MP3 44.1kHz | 0:45 | 短，可舍弃 |

**总计：~22 分钟 → 分离后预计 18+ 分钟训练数据**

---

## v4 优化清单

### 对比前三个版本

| 参数 | v1 | v2 | v3 | **v4** |
|------|:--:|:--:|:--:|:--:|
| 分离模型 | htdemucs | htdemucs(7月) | htdemucs(7月) | **4步流水线** |
| 数据量 | 13.8min | 11.7min | 22.8min | **~20min** |
| 采样率 | 40k | 40k | 40k | **48k** |
| 训练轮数 | 200 | 200 | 200 | **300** |
| 批大小 | 6 | 6 | 6 | **8** |
| 音色搭配 | 郭采洁+王贰浪 | 杂 | 杂 | **邓紫棋+ユイカ+曾沛慈 等多层次** |
| 推理参数 | rmvpe | fcpe | fcpe | **fcpe + index=0 + protect=0.5** |

### 为什么 48k

40k 采样率砍掉了 20kHz 以上的高频泛音——人声的"空气感""通透感"就在这个频段。48k 保留到 24kHz，声音更清亮。代价：显存高 20%，训练慢 15%。

### 为什么 300 轮

200 轮是社区标准。300 轮收敛更充分——像慢炖，多炖一小时肉更烂。

---

## 执行步骤

### Step 1：人声分离（用 4 步流水线）

对 RVC_3 的 6 首歌逐一跑：

```bash
cd <项目根>
venv_demucs\Scripts\python tools/separate_vocals.py "<项目根>\音频\RVC_3\G.E.M.邓紫棋 - 泡沫.wav"
venv_demucs\Scripts\python tools/separate_vocals.py "<项目根>\音频\RVC_3\『ユイカ』 - 好きだから。.wav"
# ... 全部 6 首
```

约 1.5 小时（6 首 × 15 分钟）。

### Step 2：检查干声质量

听每首的 `_FINAL.wav`——确认干净无残留。

### Step 3：下载 48k 预训练模型

```bash
# 用 Motrix 下载两个文件，放到：
# <项目根>\Retrieval-based-Voice-Conversion-WebUI\assets\pretrained_v2\

https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/pretrained_v2/f0G48k.pth
https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/pretrained_v2/f0D48k.pth
```

### Step 4：准备训练数据

清理、切片、放入 RVC dataset 目录（用之前的脚本，改路径）。

### Step 5：训练

```bash
cd <项目根>
py -3.10 tools/train_rvc.py --name tangtang_v4 --sr 48k --epochs 300 --batch 8 --save-every 20
```

预计 2-3 小时（300 轮 × 48k）。

### Step 6：转推理格式 + 索引

同 v3 流程。

### Step 7：推理测试

```bash
cd <项目根>\Retrieval-based-Voice-Conversion-WebUI
"<用户目录>/AppData/Local/Programs/Python/Python310/python.exe" tools/infer_cli.py --input_path "<项目根>/songs/covers/htdemucs/PLANET/vocals.wav" --model_name "tangtang_v4" --index_path "<项目根>/Retrieval-based-Voice-Conversion-WebUI/logs/tangtang_v4/added_*_v2.index" --opt_path "<项目根>/songs/audio/_test_v4.wav" --f0method "fcpe" --index_rate 0.0 --protect 0.5
```

---

## 音色分析

这次选的歌手：

| 歌手 | 音色特点 | 提供什么 |
|------|------|------|
| 邓紫棋 | 有力、清亮、高音穿透 | 亮度+力道 |
| ユイカ | 日系少女声、清澈 | 少女感 |
| 曾沛慈 | 温暖中高女声 | 温柔底色 |
| 王唯旖 | 清亮抒情 | 通透感 |

**这组比 v3 的搭配亮得多。** 邓紫棋的高音穿透力 + ユイカ 的少女清透，应该能解决"偏男性/粗"的问题。如果还不够亮，再加 1-2 首更高音域的。

---

## 风险与对策

| 风险 | 对策 |
|------|------|
| MP3 源带来压缩伪影 | 听分离结果，不好就丢弃那两首 |
| 48k 显存不够 (OOM) | batch 降到 4 |
| 48k 预训练模型下载慢 | Motrix 下，两个文件 ~150MB |
| 音色还是偏粗 | 去 `songs/covers/htdemucs/` 加 1-2 首更高亮的翻唱 |
