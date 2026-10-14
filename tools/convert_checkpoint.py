"""将云训练 checkpoint (fairseq 格式) 转为推理可用格式"""
import torch, os, shutil

src = "assets/weights/tangtang.pth"
print(f"Loading: {src}")
raw = torch.load(src, map_location="cpu")

# 云训练格式: {"model": state_dict, "iteration": int, "optimizer": ..., "learning_rate": float}
# 推理格式: {"weight": state_dict, "config": [...], "f0": int, "version": str}

out = {}
out["weight"] = raw["model"]
out["f0"] = 0       # --f0 0
out["version"] = "v2"  # --v v2

# 从 v1 40k config + 实际权重推断的完整 config
# SynthesizerTrnMs768NSFsid_nono 参数顺序:
# spec_channels, segment_size, inter_channels, hidden_channels, filter_channels,
# n_heads, n_layers, kernel_size, p_dropout, resblock, resblock_kernel_sizes,
# resblock_dilation_sizes, upsample_rates, upsample_initial_channel,
# upsample_kernel_sizes, spk_embed_dim, gin_channels, sr
out["config"] = [
    768,                          # spec_channels (v2 = 768-dim HuBERT)
    12800,                        # segment_size = sr * 0.32 = 40000 * 0.32
    192,                          # inter_channels
    192,                          # hidden_channels
    768,                          # filter_channels
    2,                            # n_heads
    6,                            # n_layers
    3,                            # kernel_size
    0,                            # p_dropout
    "1",                          # resblock (type 1 for 40k)
    [3, 7, 11],                   # resblock_kernel_sizes
    [[1, 3, 5], [1, 3, 5], [1, 3, 5]],  # resblock_dilation_sizes
    [10, 10, 2, 2],               # upsample_rates (sr=40k)
    512,                          # upsample_initial_channel
    [16, 16, 4, 4],               # upsample_kernel_sizes
    out["weight"]["emb_g.weight"].shape[0],  # spk_embed_dim (实际n_spk)
    256,                          # gin_channels
    40000,                        # sr
]

# 备份原文件
bak = src + ".fairseq_backup"
if not os.path.exists(bak):
    shutil.copy2(src, bak)
    print(f"Backup: {bak}")

# 保存转换后文件
torch.save(out, src)
print(f"Saved: {src}")
print(f"  config: {out['config']}")
print(f"  version: {out['version']}")
print(f"  f0: {out['f0']}")
print(f"  weight keys: {len(out['weight'])}")
print("Done!")
