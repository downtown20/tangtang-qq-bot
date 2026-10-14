#!/usr/bin/env python
"""
🎵 自动切除前奏/尾奏静音 — 基于 RMS 能量检测

原理: 分窗算能量 → 找到歌声真正开始/结束的位置 → 裁剪
安全: 支持 dry-run 预览，不会误删有效音频

用法:
    python tools/trim_silence.py                    # 处理全部 songs/audio/*.wav
    python tools/trim_silence.py --song 光年之外     # 只处理一首
    python tools/trim_silence.py --dry-run           # 预览，不实际修改
    python tools/trim_silence.py --aggressive        # 切更狠（阈值 -30dB）
    python tools/trim_silence.py --gentle            # 更保守（阈值 -40dB）
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent.parent
AUDIO_DIR = BASE / "songs" / "audio"

# 默认参数（经过大量歌曲验证的经验值）
WINDOW_MS = 100          # 分析窗口大小（毫秒）
HEAD_THRESHOLD_DB = -35  # 前奏：低于此值视为静音
TAIL_THRESHOLD_DB = -40  # 尾奏：更严格（渐弱结尾容易误切）
HEAD_MARGIN_MS = 50      # 前奏安全边距，防止切到起音瞬态
TAIL_MARGIN_MS = 1000    # 尾奏安全边距，防止切到渐弱尾音
MIN_KEEP_SEC = 5.0       # 最少保留秒数（防止把短音频切没）


def rms_db(chunk: np.ndarray) -> float:
    """计算一段音频的 RMS 能量（dB），处理单声道/立体声。"""
    if chunk.ndim == 2:
        chunk = chunk.mean(axis=1)  # 立体声 → 取均值
    rms = np.sqrt(np.mean(chunk.astype(np.float64) ** 2))
    if rms < 1e-12:
        return -120.0  # 完全静音
    return float(20 * np.log10(rms))


def find_start(samples: np.ndarray, sample_rate: int,
               window_samples: int, threshold_db: float) -> int:
    """找到第一个能量超过阈值的窗口，返回采样点索引。"""
    n_windows = len(samples) // window_samples
    for i in range(n_windows):
        start = i * window_samples
        chunk = samples[start:start + window_samples]
        if rms_db(chunk) > threshold_db:
            return start
    return 0  # 整首都很安静，不切


def find_end(samples: np.ndarray, sample_rate: int,
             window_samples: int, threshold_db: float) -> int:
    """从末尾反向找到最后一个能量超过阈值的窗口，返回采样点索引。"""
    n_windows = len(samples) // window_samples
    for i in range(n_windows - 1, -1, -1):
        start = i * window_samples
        chunk = samples[start:start + window_samples]
        if rms_db(chunk) > threshold_db:
            return start + window_samples  # 保留该窗口的末尾
    return len(samples)  # 整首安静，不切


def trim_audio(path: Path, head_db: float, tail_db: float,
               dry_run: bool = False) -> dict:
    """
    对单个音频文件切前奏尾奏。
    返回 {"path": str, "original_s": float, "trimmed_s": float,
          "cut_head_s": float, "cut_tail_s": float, "skipped": str}
    """
    info = sf.info(str(path))
    sample_rate = info.samplerate
    total_samples = info.frames
    original_dur = total_samples / sample_rate

    window_samples = int(sample_rate * WINDOW_MS / 1000)
    head_margin = int(sample_rate * HEAD_MARGIN_MS / 1000)
    tail_margin = int(sample_rate * TAIL_MARGIN_MS / 1000)

    # 读整首
    samples, _ = sf.read(str(path), dtype='float64')

    # 找起点
    raw_start = find_start(samples, sample_rate, window_samples, head_db)
    start_sample = max(0, raw_start - head_margin)

    # 找终点
    raw_end = find_end(samples, sample_rate, window_samples, tail_db)
    end_sample = min(total_samples, raw_end + tail_margin)

    # 安全检查
    keep_samples = end_sample - start_sample
    keep_dur = keep_samples / sample_rate
    cut_head = start_sample / sample_rate
    cut_tail = (total_samples - end_sample) / sample_rate

    result = {
        "path": str(path.relative_to(BASE)),
        "original_s": round(original_dur, 1),
        "cut_head_s": round(cut_head, 1),
        "cut_tail_s": round(cut_tail, 1),
        "trimmed_s": round(keep_dur, 1),
        "skipped": "",
    }

    # 跳过太短的
    if keep_dur < MIN_KEEP_SEC:
        result["skipped"] = f"切除后仅 {keep_dur:.1f}s，跳过（最少 {MIN_KEEP_SEC}s）"
        return result

    # 跳过无意义的（切 < 0.3 秒没什么意义）
    if cut_head < 0.3 and cut_tail < 0.3:
        result["skipped"] = "前奏/尾奏均 < 0.3s，无需切除"
        return result

    if dry_run:
        return result

    # ── 执行 ──
    trimmed = samples[start_sample:end_sample]

    # 写新文件（直接覆盖，不备份。Qt 工作室会在覆盖前弹确认框）
    sf.write(str(path), trimmed, sample_rate)
    return result


def format_result(r: dict) -> str:
    """格式化单条结果。"""
    name = Path(r["path"]).stem
    if r["skipped"]:
        return f"  {name}: ⏭ {r['skipped']}"
    return (
        f"  {name}: {r['original_s']:.1f}s → {r['trimmed_s']:.1f}s "
        f"(前奏 -{r['cut_head_s']:.1f}s, 尾奏 -{r['cut_tail_s']:.1f}s)"
    )


def main():
    parser = argparse.ArgumentParser(
        description="自动切除歌曲前奏/尾奏静音",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/trim_silence.py --dry-run       # 预览所有歌曲
  python tools/trim_silence.py                 # 正式处理全部
  python tools/trim_silence.py --song 光年之外  # 只处理一首
  python tools/trim_silence.py --aggressive     # 更激进
  python tools/trim_silence.py --gentle         # 更保守
        """,
    )
    parser.add_argument("--song", type=str, help="只处理指定歌名（不含 .wav）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际修改文件")
    parser.add_argument("--aggressive", action="store_true",
                        help="激进模式（前奏阈值 -30dB，尾奏 -35dB）")
    parser.add_argument("--gentle", action="store_true",
                        help="保守模式（前奏阈值 -40dB，尾奏 -45dB）")

    args = parser.parse_args()

    # 阈值
    head_db = HEAD_THRESHOLD_DB
    tail_db = TAIL_THRESHOLD_DB
    if args.aggressive:
        head_db, tail_db = -30, -35
    elif args.gentle:
        head_db, tail_db = -40, -45

    # 找文件
    if args.song:
        wavs = [AUDIO_DIR / f"{args.song}.wav"]
        if not wavs[0].exists():
            print(f"❌ 文件不存在: {wavs[0]}")
            sys.exit(1)
    else:
        wavs = sorted(AUDIO_DIR.glob("*.wav"))
        if not wavs:
            print("❌ songs/audio/ 下没有 .wav 文件")
            sys.exit(1)

    mode = "🔍 预览 (dry-run)" if args.dry_run else "✂️ 正式切除"
    print(f"{mode}  |  前奏阈值 {head_db}dB  |  尾奏阈值 {tail_db}dB")
    print()

    results = []
    total_cut = 0.0
    skipped = 0

    for wav in wavs:
        r = trim_audio(wav, head_db, tail_db, dry_run=args.dry_run)
        results.append(r)
        print(format_result(r))
        if not r["skipped"]:
            total_cut += r["cut_head_s"] + r["cut_tail_s"]
        else:
            skipped += 1

    print()
    done = len(wavs) - skipped
    if args.dry_run:
        print(f"📊 预览: {done} 首可切，{skipped} 首跳过，预计切除 {total_cut:.1f}s")
        print("   确认后运行: python tools/trim_silence.py")
    else:
        print(f"✅ 完成: {done} 首已处理，{skipped} 首跳过，共切除 {total_cut:.1f}s")


if __name__ == "__main__":
    main()
