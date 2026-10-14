"""米雪儿长音频的离线参考音切片、转写与情绪标注工具。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import json
import math
import re
import subprocess
import sys
import wave
from collections import Counter
from pathlib import Path

import numpy as np


SILENCE_START_RE = re.compile(r"silence_start: ([0-9.]+)")
SILENCE_END_RE = re.compile(
    r"silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)"
)


def parse_silences(log_text: str) -> list[tuple[float, float]]:
    """从 ffmpeg silencedetect 日志解析 ``(开始, 结束)``。"""
    silences: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in log_text.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            pending_start = float(start_match.group(1))
        end_match = SILENCE_END_RE.search(line)
        if end_match and pending_start is not None:
            silence_end = float(end_match.group(1))
            if silence_end > pending_start:
                silences.append((pending_start, silence_end))
            pending_start = None
    return silences


def build_segments(
    total_duration: float,
    silences: list[tuple[float, float]],
    *,
    min_duration: float = 2.0,
    max_duration: float = 12.0,
    padding: float = 0.2,
) -> list[tuple[float, float]]:
    """用长静音作为句界，返回经过首尾留白和时长过滤的候选段。"""
    segments: list[tuple[float, float]] = []
    speech_start = 0.0
    for silence_start, silence_end in silences:
        start = max(0.0, speech_start - padding)
        end = min(total_duration, silence_start + padding)
        if min_duration <= end - start <= max_duration:
            segments.append((start, end))
        speech_start = silence_end

    start = max(0.0, speech_start - padding)
    if min_duration <= total_duration - start <= max_duration:
        segments.append((start, total_duration))
    return segments


def probe_duration(source: Path) -> float:
    """读取媒体真实时长。"""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def detect_silences(source: Path, noise_db: float, minimum: float) -> list[tuple[float, float]]:
    """运行一次 ffmpeg 静音检测。"""
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={minimum}",
            "-f",
            "null",
            "NUL",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg silencedetect failed")
    return parse_silences(result.stderr)


def cut_segment(source: Path, destination: Path, start: float, end: float) -> None:
    """将一个候选段一次解码为 48kHz 单声道 PCM WAV。"""
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(source),
            "-t",
            f"{end - start:.6f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-n",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"failed to cut {destination.name}")


def analyze_wav(path: Path) -> dict[str, float | int]:
    """测量参考音的实际时长、电平、削波及静音占比。"""
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
    if channels != 1 or sample_width != 2 or samples.size == 0:
        raise ValueError(f"候选音频不是有效的单声道 16-bit PCM: {path}")

    values = samples.astype(np.float64)
    peak = float(np.max(np.abs(values)))
    rms = float(np.sqrt(np.mean(values * values)))
    block_size = max(1, sample_rate // 50)
    usable = samples.size - samples.size % block_size
    blocks = values[:usable].reshape(-1, block_size)
    block_rms = np.sqrt(np.mean(blocks * blocks, axis=1)) if usable else np.array([])
    silence_threshold = 32768.0 * 10 ** (-45.0 / 20.0)
    silence_ratio = float(np.mean(block_rms < silence_threshold)) if block_rms.size else 1.0
    return {
        "sample_rate": sample_rate,
        "duration": round(samples.size / sample_rate, 6),
        "rms_dbfs": round(20 * math.log10(max(rms, 1.0) / 32768.0), 3),
        "peak_dbfs": round(20 * math.log10(max(peak, 1.0) / 32768.0), 3),
        "clipped_samples": int(np.count_nonzero(np.abs(samples.astype(np.int32)) >= 32767)),
        "silence_ratio": round(silence_ratio, 4),
    }


def quality_flags(metrics: dict[str, float | int]) -> list[str]:
    """给需要人工复核的技术指标打标，不替人做情绪筛选。"""
    flags: list[str] = []
    duration = float(metrics["duration"])
    if duration < 3.0 or duration > 10.0:
        flags.append("nonideal_duration")
    if int(metrics["clipped_samples"]) > 0 or float(metrics["peak_dbfs"]) > -0.1:
        flags.append("near_clipping")
    if float(metrics["rms_dbfs"]) < -35.0:
        flags.append("too_quiet")
    if float(metrics["silence_ratio"]) > 0.4:
        flags.append("too_much_silence")
    return flags


def write_manifest(path: Path, manifest: list[dict[str, object]]) -> None:
    """原子更新生成目录内的清单。"""
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def enrich_quality(output_dir: Path) -> list[dict[str, object]]:
    """为已完成的切片追加无模型依赖的技术质检字段。"""
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest:
        metrics = analyze_wav(output_dir / str(item["file"]))
        item.update(metrics)
        item["quality_flags"] = quality_flags(metrics)
        item["technical_pass"] = not item["quality_flags"]
    write_manifest(manifest_path, manifest)
    return manifest


def normalize_asr_text(text: str) -> str:
    """移除标点和空白，仅用于比较两套 ASR，不改写原始转写。"""
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text)).lower()


def asr_agreement(first: str, second: str) -> float:
    """返回两套转写的字符级相似度。"""
    first_normalized = normalize_asr_text(first)
    second_normalized = normalize_asr_text(second)
    if not first_normalized or not second_normalized:
        return 0.0
    return round(
        difflib.SequenceMatcher(None, first_normalized, second_normalized).ratio(), 4
    )


async def _sensevoice_transcriptions(paths: list[Path]) -> list[str]:
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from agent.asr import VoiceRecognizer

    recognizer = VoiceRecognizer()
    if not await recognizer.init():
        raise RuntimeError("SenseVoice 初始化失败")
    return [await recognizer.transcribe(str(path)) for path in paths]


def enrich_asr(output_dir: Path) -> list[dict[str, object]]:
    """用 Whisper 和 SenseVoice 双重转写候选音频。"""
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [output_dir / str(item["file"]) for item in manifest]
    if not all("whisper_text" in item for item in manifest):
        from faster_whisper import WhisperModel

        whisper_dir = Path(__file__).resolve().parents[1] / "asr_models" / "whisper-small"
        whisper = WhisperModel(str(whisper_dir), device="cpu", compute_type="int8")
        for index, (item, path) in enumerate(zip(manifest, paths), start=1):
            segments, _ = whisper.transcribe(
                str(path),
                language="zh",
                beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            item["whisper_text"] = "".join(segment.text for segment in segments).strip()
            if index % 10 == 0:
                print(f"Whisper: {index}/{len(manifest)}", flush=True)
        write_manifest(manifest_path, manifest)
        del whisper

    sensevoice_texts = asyncio.run(_sensevoice_transcriptions(paths))
    for item, text in zip(manifest, sensevoice_texts):
        item["sensevoice_text"] = text
        item["asr_agreement"] = asr_agreement(str(item["whisper_text"]), text)
        flags: list[str] = []
        if not item["whisper_text"] or not text:
            flags.append("empty_asr")
        elif float(item["asr_agreement"]) < 0.55:
            flags.append("asr_disagreement")
        item["asr_flags"] = flags
    write_manifest(manifest_path, manifest)
    return manifest


def pick_emotion(labels: list[str], scores: list[float]) -> tuple[str, float, dict[str, float]]:
    """整理 emotion2vec 输出，并返回最高分标签。"""
    if not labels or len(labels) != len(scores):
        raise ValueError("emotion2vec 标签与分数不完整")
    normalized = {
        label.split("/")[0]: round(float(score), 6)
        for label, score in zip(labels, scores)
    }
    emotion, score = max(normalized.items(), key=lambda item: item[1])
    return emotion, score, normalized


def enrich_emotions(output_dir: Path) -> list[dict[str, object]]:
    """用本地 emotion2vec 为候选段打声学情绪标签。"""
    from funasr import AutoModel

    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    model_dir = (
        Path.home()
        / ".cache"
        / "modelscope"
        / "hub"
        / "models"
        / "iic"
        / "emotion2vec_plus_large"
    )
    if not model_dir.is_dir():
        raise FileNotFoundError(f"本地 emotion2vec 模型不存在: {model_dir}")
    model = AutoModel(model=str(model_dir), trust_remote_code=True, disable_update=True)

    for index, item in enumerate(manifest, start=1):
        if "emotion" in item:
            continue
        result = model.generate(
            input=str(output_dir / str(item["file"])),
            output_dir=None,
            disable_pbar=True,
        )[0]
        emotion, score, all_scores = pick_emotion(result["labels"], result["scores"])
        item["emotion"] = emotion
        item["emotion_score"] = score
        item["emotion_scores"] = all_scores
        if index % 10 == 0:
            write_manifest(manifest_path, manifest)
            print(f"emotion2vec: {index}/{len(manifest)}", flush=True)
    write_manifest(manifest_path, manifest)
    return manifest


def choose_shortlist(
    manifest: list[dict[str, object]], per_emotion: int = 8
) -> dict[str, list[dict[str, object]]]:
    """按硬质量门槛分组排序，仍保留人工最终决定。"""
    eligible = [
        item
        for item in manifest
        if not item.get("quality_flags")
        and not item.get("asr_flags")
        and float(item.get("asr_agreement", 0.0)) >= 0.75
    ]
    grouped: dict[str, list[dict[str, object]]] = {}
    for emotion in sorted({str(item["emotion"]) for item in eligible}):
        candidates = [item for item in eligible if item["emotion"] == emotion]
        candidates.sort(
            key=lambda item: (
                float(item["emotion_score"]),
                float(item["asr_agreement"]),
            ),
            reverse=True,
        )
        grouped[emotion] = candidates[:per_emotion]
    return grouped


def finalize_outputs(output_dir: Path) -> dict[str, object]:
    """生成便于人工复核的 CSV、短名单和汇总报告。"""
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        item["review_flags"] = list(item.get("quality_flags", [])) + list(
            item.get("asr_flags", [])
        )
        item["prompt_text_candidate"] = item.get("sensevoice_text", "")

    csv_fields = [
        "id",
        "file",
        "source_start",
        "source_end",
        "duration",
        "rms_dbfs",
        "peak_dbfs",
        "clipped_samples",
        "silence_ratio",
        "technical_pass",
        "whisper_text",
        "sensevoice_text",
        "asr_agreement",
        "emotion",
        "emotion_score",
        "review_flags",
        "prompt_text_candidate",
    ]
    with (output_dir / "review_manifest.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for item in manifest:
            row = dict(item)
            row["review_flags"] = ";".join(item["review_flags"])
            writer.writerow(row)

    shortlist = choose_shortlist(manifest)
    (output_dir / "shortlist.json").write_text(
        json.dumps(shortlist, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    emotion_counts = Counter(str(item["emotion"]) for item in manifest)
    summary: dict[str, object] = {
        "total_clips": len(manifest),
        "technical_pass": sum(not item["quality_flags"] for item in manifest),
        "asr_review_needed": sum(bool(item["asr_flags"]) for item in manifest),
        "shortlist_clips": sum(len(items) for items in shortlist.values()),
        "emotion_counts": dict(emotion_counts),
        "shortlist_counts": {emotion: len(items) for emotion, items in shortlist.items()},
    }
    (output_dir / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_slicing(
    source: Path,
    output_dir: Path,
    *,
    noise_db: float,
    minimum_silence: float,
    min_duration: float,
    max_duration: float,
    padding: float,
) -> list[dict[str, object]]:
    """切出候选音频并写入带源时间轴的初始清单。"""
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"输出目录非空，拒绝覆盖: {output_dir}")
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    total_duration = probe_duration(source)
    silences = detect_silences(source, noise_db, minimum_silence)
    segments = build_segments(
        total_duration,
        silences,
        min_duration=min_duration,
        max_duration=max_duration,
        padding=padding,
    )

    manifest: list[dict[str, object]] = []
    for index, (start, end) in enumerate(segments, start=1):
        clip_name = f"michele_{index:04d}.wav"
        cut_segment(source, clips_dir / clip_name, start, end)
        manifest.append(
            {
                "id": f"michele_{index:04d}",
                "file": f"clips/{clip_name}",
                "source_start": round(start, 6),
                "source_end": round(end, 6),
                "duration": round(end - start, 6),
            }
        )

    write_manifest(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--noise-db", type=float, default=-38.0)
    parser.add_argument("--minimum-silence", type=float, default=1.2)
    parser.add_argument("--min-duration", type=float, default=2.0)
    parser.add_argument("--max-duration", type=float, default=12.0)
    parser.add_argument("--padding", type=float, default=0.2)
    parser.add_argument("--enrich-quality", action="store_true")
    parser.add_argument("--enrich-asr", action="store_true")
    parser.add_argument("--enrich-emotion", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"源音频不存在: {args.source}")
    if args.enrich_quality:
        manifest = enrich_quality(args.output_dir.resolve())
        print(f"技术质检完成: {len(manifest)} 段 → {args.output_dir.resolve()}")
        return
    if args.enrich_asr:
        manifest = enrich_asr(args.output_dir.resolve())
        print(f"双ASR转写完成: {len(manifest)} 段 → {args.output_dir.resolve()}")
        return
    if args.enrich_emotion:
        manifest = enrich_emotions(args.output_dir.resolve())
        print(f"情绪标注完成: {len(manifest)} 段 → {args.output_dir.resolve()}")
        return
    if args.finalize:
        summary = finalize_outputs(args.output_dir.resolve())
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    manifest = run_slicing(
        args.source.resolve(),
        args.output_dir.resolve(),
        noise_db=args.noise_db,
        minimum_silence=args.minimum_silence,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        padding=args.padding,
    )
    print(f"切片完成: {len(manifest)} 段 → {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
