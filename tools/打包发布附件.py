#!/usr/bin/env python3
"""打包 GitHub Release 附件（2026-09-05）— 输出到 发布附件/ 目录

包清单（发布规划书 v2 附件）：
  1. 语音推理集 gpt-sovits/ 6.5G —— 拆 4 卷（GitHub 单附件 ≤2GB）
  2. 预录歌曲包 songs/audio 40 首 + separated 补差 1 首 + songs/*.txt
  3. 歌唱模型包 hutao.pth/hutao.index/hubert_base.pt
  4. 角色贴图包 stickers_michele + stickers_murasame

用法：python tools/打包发布附件.py [输出目录]
"""
import sys
import zipfile
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE.parent / "发布附件"
VOL_LIMIT = 1_900 * 1024 * 1024  # 每卷 ~1.9G（留余量给 zip 头）


def _add_zip(zf: zipfile.ZipFile, path: Path, rel_base: Path, compress=False) -> None:
    zf.write(path, path.relative_to(rel_base).as_posix(),
             compress_type=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)


def pack_voice(out_dir: Path) -> list[Path]:
    """gpt-sovits/ 拆卷打包（按文件累积切 1.9G/卷）。

    Windows 文件名不允许 '?'——先写 ASCII 临时名，收尾统一重命名为中文正式名。
    """
    src = BASE / "gpt-sovits"
    files = sorted(p for p in src.rglob("*") if p.is_file())
    total_mb = sum(f.stat().st_size for f in files) / 2**20
    print(f"🗣 语音推理集 {total_mb:.0f}MB / {len(files)} 文件 → 拆卷…")
    parts, part_no, size = [], 1, 0
    zf = zipfile.ZipFile(out_dir / f"_voice_part_{part_no}.zip", "w")
    for f in files:
        sz = f.stat().st_size
        if size + sz > VOL_LIMIT:
            zf.close()
            parts.append(part_no)
            part_no += 1
            size = 0
            zf = zipfile.ZipFile(out_dir / f"_voice_part_{part_no}.zip", "w")
        _add_zip(zf, f, src)
        size += sz
    zf.close()
    parts.append(part_no)
    done = []
    for i in parts:
        p = out_dir / f"_voice_part_{i}.zip"
        target = out_dir / f"小糖糖-语音推理集-{i}of{len(parts)}.zip"
        p.replace(target)
        done.append(target)
        print(f"  ✅ {target.name}（{target.stat().st_size / 2**30:.2f}G）")
    return done


def pack_songs(out_dir: Path) -> Path:
    """audio 40 首 + separated 补差 1 首 + 歌词 txt。"""
    audio = BASE / "songs" / "audio"
    sep = BASE / "songs" / "covers" / "separated"
    txts = sorted((BASE / "songs").glob("*.txt"))
    audio_stems = {p.stem for p in audio.glob("*.wav")}
    extra = [p for p in sep.glob("*_FINAL.wav")
             if p.stem.removesuffix("_FINAL") not in audio_stems]
    n = len(list(audio.glob("*.wav")))
    out = out_dir / "小糖糖-预录歌曲包.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for wav in sorted(audio.glob("*.wav")):
            _add_zip(zf, wav, BASE / "songs")
        for wav in extra:
            _add_zip(zf, wav, BASE / "songs")
        for t in txts:
            _add_zip(zf, t, BASE / "songs")
    print(f"🎵 预录歌曲包: audio {n} 首 + 补差 {len(extra)} 首 + 歌词 {len(txts)} 个")
    print(f"  ✅ {out.name}（{out.stat().st_size / 2**20:.0f}M）")
    return out


def pack_model(out_dir: Path) -> Path:
    """歌唱模型包：hutao + hubert。"""
    w = BASE / "Retrieval-based-Voice-Conversion-WebUI"
    items = [w / "assets/weights/hutao.pth", w / "assets/weights/hutao.index",
             w / "assets/hubert/hubert_base.pt"]
    out = out_dir / "小糖糖-歌唱模型包.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for p in items:
            _add_zip(zf, p, BASE)
    print(f"🎤 歌唱模型包: 3 文件")
    print(f"  ✅ {out.name}（{out.stat().st_size / 2**20:.0f}M）")
    return out


def pack_stickers(out_dir: Path) -> Path:
    out = out_dir / "小糖糖-角色贴图包.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for d in ("stickers_michele", "stickers_murasame"):
            for p in sorted((BASE / d).rglob("*")):
                if p.is_file():
                    _add_zip(zf, p, BASE)
    print(f"🖼 角色贴图包: michele + murasame")
    print(f"  ✅ {out.name}（{out.stat().st_size / 2**20:.0f}M）")
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"📦 附件打包 → {OUT}\n")
    pack_voice(OUT)
    pack_songs(OUT)
    pack_model(OUT)
    pack_stickers(OUT)
    total = sum(f.stat().st_size for f in OUT.iterdir() if f.is_file())
    print(f"\n✅ 全部打包完成：{OUT}（{total / 2**30:.2f}G）")


if __name__ == "__main__":
    main()
