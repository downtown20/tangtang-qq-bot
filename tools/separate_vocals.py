"""
人声分离四步流水线 — MDX23C → Denoise → Bleed → VR

用法:
  venv_demucs\Scripts\python tools/separate_vocals.py "输入文件.wav" [--output 输出目录]

示例:
  venv_demucs\Scripts\python tools/separate_vocals.py "音频/新歌.wav"
  venv_demucs\Scripts\python tools/separate_vocals.py "音频/新歌.wav" --output "音频/clean_vocals"

输出:
  输出目录/{歌名}_FINAL.wav  ← 最终干净人声

依赖:
  - venv_demucs (Python 3.10 + audio-separator + onnxruntime)
  - uvr5_models/ 目录下的模型文件
"""

import argparse
import os
import sys
from pathlib import Path


def find_stem(output_dir, suffix):
    """在输出目录找到匹配的文件。"""
    for f in Path(output_dir).glob(f"*{suffix}*.wav"):
        return str(f)
    for f in Path(output_dir).glob("*.wav"):
        if suffix in f.name:
            return str(f)
    return None


def run_step(model_file, input_file, output_dir, desc, stem_hint=None):
    """运行一步分离，返回输出文件路径。"""
    from audio_separator.separator import Separator

    print(f"\n{'='*50}")
    print(f"  {desc}")
    print(f"{'='*50}")

    s = Separator(model_file_dir='uvr5_models', output_dir=output_dir, output_format='WAV')
    s.load_model(model_filename=model_file)
    s.separate(input_file)

    # 找到输出文件
    song_name = Path(input_file).stem

    # 根据模型不同，用不同规则找输出文件
    stem_map = {
        'MDX23C': ('Vocals', 'MDX23C'),
        'denoise': ('dry', 'denoise'),
        'bleed': ('Bleed', 'bleed_suppressor'),
        'vr': ('No Reverb', 'UVR-DeEcho-DeReverb'),
    }

    if stem_hint and stem_hint in stem_map:
        stem_name, model_hint = stem_map[stem_hint]
        result = find_stem(output_dir, f"({stem_name})")
        if result:
            print(f"  → {Path(result).name[:80]}")
            return result

    # fallback: 返回最新的 wav
    wavs = sorted(Path(output_dir).glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if wavs:
        print(f"  → {wavs[0].name[:80]}")
        return str(wavs[0])

    raise FileNotFoundError(f"No output found after {desc}")


def separate(input_file, output_dir, keep_intermediate=False):
    """
    四步流水线: MDX23C → Denoise → Bleed → VR

    Args:
        input_file: 输入音频文件路径
        output_dir: 输出目录
        keep_intermediate: 是否保留中间文件

    Returns:
        最终输出文件路径
    """
    song_name = Path(input_file).stem
    # 每首歌独立子目录，互不干扰
    base_dir = Path(output_dir).resolve()
    work_dir = str(base_dir / song_name)
    os.makedirs(work_dir, exist_ok=True)

    # Step 1: MDX23C 提取人声
    step1 = run_step(
        model_file='MDX23C-8KFFT-InstVoc_HQ.ckpt',
        input_file=input_file,
        output_dir=work_dir,
        desc='Step 1/4: MDX23C 提取人声',
        stem_hint='MDX23C',
    )

    # Step 2: Denoise 去噪
    step2 = run_step(
        model_file='denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt',
        input_file=step1,
        output_dir=work_dir,
        desc='Step 2/4: Denoise 去噪',
        stem_hint='denoise',
    )

    # Step 3: Bleed Suppressor 去漏音
    step3 = run_step(
        model_file='mel_band_roformer_bleed_suppressor_v1.ckpt',
        input_file=step2,
        output_dir=work_dir,
        desc='Step 3/4: Bleed Suppressor 去漏音',
        stem_hint='bleed',
    )

    # Step 4: VR 去混响
    step4 = run_step(
        model_file='UVR-DeEcho-DeReverb.pth',
        input_file=step3,
        output_dir=work_dir,
        desc='Step 4/4: VR 去混响',
        stem_hint='vr',
    )

    # 重命名为 FINAL，放到 shared output dir
    final_path = base_dir / f"{song_name}_FINAL.wav"
    import shutil
    shutil.copy2(step4, final_path)
    print(f"\n{'='*50}")
    print(f"  ✅ 完成！")
    print(f"  {final_path}")
    print(f"{'='*50}")

    # 清理中间文件（只删当前歌的中间产物，不动别人的 FINAL）
    if not keep_intermediate:
        final_name = Path(final_path).name
        for f in Path(work_dir).glob("*.wav"):
            if f.name == final_name:
                continue
            # 只删包含当前歌名的中间文件
            if song_name in f.name:
                f.unlink()
        print("  中间文件已清理")

    return str(final_path)


def main():
    parser = argparse.ArgumentParser(
        description='人声分离四步流水线: MDX23C → Denoise → Bleed → VR',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s "音频/新歌.wav"
  %(prog)s "音频/新歌.wav" --output "音频/clean_vocals"
  %(prog)s "音频/新歌.wav" --keep  # 保留中间文件调试
  %(prog)s "音频/RVC_3"            # 处理整个文件夹
        """
    )
    parser.add_argument('inputs', nargs='+', help='输入文件或文件夹')
    parser.add_argument('--output', '-o', default=None, help='输出目录（默认与输入同目录）')
    parser.add_argument('--keep', '-k', action='store_true', help='保留中间文件')

    args = parser.parse_args()

    # Collect all files (expand folders)
    all_files = []
    for inp in args.inputs:
        p = Path(inp)
        if not p.exists():
            print(f"Skip: {inp} (not found)")
            continue
        if p.is_dir():
            for ext in ['*.wav', '*.mp3', '*.flac', '*.m4a']:
                all_files.extend(sorted(p.glob(ext)))
        else:
            all_files.append(p)

    if not all_files:
        print("No audio files found.")
        sys.exit(1)

    print(f"\nProcessing {len(all_files)} file(s):")
    for f in all_files:
        print(f"  {f.name}")

    for i, f in enumerate(all_files, 1):
        print(f"\n{'#'*50}")
        print(f"  [{i}/{len(all_files)}] {f.name}")
        print(f"{'#'*50}")
        out_dir = args.output or str(f.parent / 'separated')
        try:
            separate(str(f), out_dir, args.keep)
        except Exception as e:
            print(f"  ❌ Failed: {e}")


if __name__ == '__main__':
    main()
