#!/usr/bin/env python
"""
🎵 DeepFilterNet3 人声降噪 — 去除背景噪音，保留人声

原理: DeepFilterNet3 深度学习模型，专为人声降噪训练。
      输入 → 短时傅里叶变换 → 深度滤波网络 → 逆变换 → 输出

用法:
    python tools/denoise.py                          # 处理全部 songs/audio/*.wav
    python tools/denoise.py --song 光年之外           # 只处理一首
    python tools/denoise.py --dry-run                # 预览，不实际修改
    python tools/denoise.py --song 光年之外 --output cleaned.wav  # 指定输出
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

BASE = Path(__file__).resolve().parent.parent
AUDIO_DIR = BASE / "songs" / "audio"
MODEL_DIR = BASE / "assets" / "DeepFilterNet3"

# 全局模型缓存（进程级单例，避免重复加载）
_denoiser = None


def _get_denoiser(device: str = "auto"):
    """获取全局降噪器实例（单例，含 git 依赖修补）。"""
    global _denoiser
    if _denoiser is not None:
        return _denoiser

    # 修补 DeepFilterNet 的 git 依赖（headless 环境无 git 会崩溃）
    import df.utils
    df.utils.get_commit_hash = lambda: "bundled"
    df.utils.get_branch_name = lambda: "bundled-branch"
    df.utils.get_git_root = lambda: "."

    import df.logger
    if hasattr(df.logger, 'get_commit_hash'):
        df.logger.get_commit_hash = lambda: "bundled"
    if hasattr(df.logger, 'get_branch_name'):
        df.logger.get_branch_name = lambda: "bundled-branch"

    from df.enhance import init_df, enhance

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    # 使用内置模型，避免下载
    if MODEL_DIR.exists():
        model, df_state, _ = init_df(model_base_dir=str(MODEL_DIR))
    else:
        print(f"⚠️ 内置模型不存在 {MODEL_DIR}，尝试自动下载...")
        model, df_state, _ = init_df()

    if device == "cuda" and torch.cuda.is_available():
        model = model.to("cuda")
    else:
        model = model.to("cpu")

    model.eval()
    _denoiser = (model, df_state, enhance, device)
    print(f"✅ DeepFilterNet3 已加载 ({device})")
    return _denoiser


def denoise_file(
    input_path: Path,
    output_path: Path | None = None,
    device: str = "auto",
    dry_run: bool = False,
) -> dict:
    """
    对单个音频文件降噪。
    返回 {"path": str, "original_s": float, "denoised": bool, "error": str}
    """
    import torchaudio.transforms as tat
    from df.enhance import enhance

    result = {
        "path": str(input_path.relative_to(BASE)),
        "original_s": 0.0,
        "denoised": False,
        "skipped": "",
        "error": "",
    }

    if dry_run:
        info = sf.info(str(input_path))
        result["original_s"] = round(info.frames / info.samplerate, 1)
        return result

    try:
        model, df_state, _, dev = _get_denoiser(device)

        # 读音频
        audio, sr = sf.read(str(input_path), dtype='float32')
        result["original_s"] = round(len(audio) / sr, 1)

        # 转 tensor（保持原始声道）
        if audio.ndim == 1:
            audio_t = torch.from_numpy(audio).float()
        else:
            audio_t = torch.from_numpy(audio.T).float()  # [ch, samples]

        original_device = audio_t.device
        original_sr = sr

        # DeepFilterNet 需要 48kHz
        need_resample = sr != 48000
        if need_resample:
            resampler_in = tat.Resample(sr, 48000)
            audio_48k = resampler_in(audio_t)
        else:
            audio_48k = audio_t

        # enhance() 内部用 numpy，需在 CPU
        audio_48k_cpu = audio_48k.cpu()

        with torch.no_grad():
            # enhance 期望 2D: [batch, samples] 或 [samples]
            if audio_48k_cpu.ndim == 1:
                enhanced = enhance(model, df_state, audio_48k_cpu.unsqueeze(0)).squeeze(0)
            else:
                # 立体声：逐通道处理
                channels = []
                for ch in range(audio_48k_cpu.shape[0]):
                    ch_enhanced = enhance(model, df_state, audio_48k_cpu[ch].unsqueeze(0)).squeeze(0)
                    channels.append(ch_enhanced)
                enhanced = torch.stack(channels)

        # 重采样回原始采样率
        if need_resample:
            resampler_out = tat.Resample(48000, original_sr)
            enhanced = resampler_out(enhanced)

        # 匹配原始长度
        if enhanced.shape[-1] > audio_t.shape[-1]:
            enhanced = enhanced[..., :audio_t.shape[-1]]
        elif enhanced.shape[-1] < audio_t.shape[-1]:
            pad = audio_t.shape[-1] - enhanced.shape[-1]
            enhanced = torch.nn.functional.pad(enhanced, (0, pad))

        # 写输出
        out_np = enhanced.numpy()
        if audio.ndim == 2:
            out_np = out_np.T  # [ch, samples] → [samples, ch]
        else:
            out_np = out_np.squeeze()

        out_path = output_path or input_path

        sf.write(str(out_path), out_np, original_sr)
        result["denoised"] = True

    except Exception as e:
        result["error"] = str(e)

    return result


def format_result(r: dict) -> str:
    name = Path(r["path"]).stem
    if r.get("skipped"):
        return f"  {name}: ⏭ {r['skipped']}"
    if r.get("error"):
        return f"  {name}: ❌ {r['error']}"
    if r.get("denoised"):
        return f"  {name}: ✅ {r['original_s']:.1f}s 降噪完成"
    return f"  {name}: 🔍 将处理 {r['original_s']:.1f}s"


def main():
    parser = argparse.ArgumentParser(
        description="DeepFilterNet3 人声降噪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/denoise.py --dry-run        # 预览
  python tools/denoise.py                  # 处理全部歌曲
  python tools/denoise.py --song 光年之外   # 只处理一首
  python tools/denoise.py --song 光年之外 --output test.wav  # 输出到指定文件
        """,
    )
    parser.add_argument("--song", type=str, help="只处理指定歌名（不含 .wav）")
    parser.add_argument("--output", type=str, help="输出文件路径（仅 --song 时有效）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际修改文件")
    parser.add_argument("--device", type=str, default="auto",
                        choices=["auto", "cpu", "cuda"], help="推理设备")

    args = parser.parse_args()

    # 找文件
    if args.song:
        wav = AUDIO_DIR / f"{args.song}.wav"
        if not wav.exists():
            print(f"❌ 文件不存在: {wav}")
            sys.exit(1)
        wavs = [wav]
    else:
        wavs = sorted(AUDIO_DIR.glob("*.wav"))
        if not wavs:
            print("❌ songs/audio/ 下没有 .wav 文件")
            sys.exit(1)

    mode = "🔍 预览 (dry-run)" if args.dry_run else "🎵 降噪处理"
    print(f"{mode}  |  模型: DeepFilterNet3  |  设备: {args.device}")
    print()

    results = []
    for wav in wavs:
        out = Path(args.output) if args.output and args.song else None
        r = denoise_file(wav, output_path=out, device=args.device, dry_run=args.dry_run)
        results.append(r)
        print(format_result(r))

    print()
    if args.dry_run:
        print(f"📊 预览: {len(wavs)} 首待处理，确认后运行: python tools/denoise.py")
    else:
        ok = sum(1 for r in results if r["denoised"])
        err = sum(1 for r in results if r["error"])
        print(f"✅ 完成: {ok} 首已降噪，{err} 首失败")


if __name__ == "__main__":
    main()
