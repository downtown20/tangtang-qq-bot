"""
歌词文件段落标记迁移工具 🎵

自动检测副歌（最长重复段落），给歌词加 [主歌1] [副歌] [主歌2] [尾声] 等标记。

原则：
  - 只找最长的一个重复块 → 副歌
  - 副歌前面的 → 主歌1
  - 副歌之间的 → 主歌2、主歌3...
  - 最后一个副歌后面的 → 尾声
  - 每个段落只保留一个实例（用于生成一份音频）
  - 检测不到重复结构的 → 按行数大致分段

用法：
    python tools/migrate_lyrics_sections.py              # 预览所有歌
    python tools/migrate_lyrics_sections.py --apply      # 实际写入
    python tools/migrate_lyrics_sections.py --song 勇气  # 只看一首
"""

import argparse
import sys
from pathlib import Path
from difflib import SequenceMatcher


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()


def _find_best_chorus(lines: list[str]) -> tuple[int, int] | None:
    """
    找到最长的重复连续行块（副歌候选）。

    返回: (起始行, 结束行) 或 None
    """
    n = len(lines)
    best = None
    best_score = 0

    # 从长到短搜索，优先长副歌
    for block_size in range(min(n // 3, 10), 1, -1):
        for i in range(n - block_size * 2 + 1):
            block_a = lines[i:i + block_size]

            for j in range(i + block_size, n - block_size + 1):
                block_b = lines[j:j + block_size]
                sims = [similarity(a, b) for a, b in zip(block_a, block_b)]
                avg = sum(sims) / len(sims)

                # 评分: 长度 × 相似度
                # 评分优先高相似度（平方惩罚低相似度）
                score = block_size * (avg ** 2)
                if avg >= 0.7 and score > best_score:
                    best_score = score
                    best = (i, i + block_size)

    return best


def _find_all_chorus_positions(lines: list[str], chorus: list[str]) -> list[int]:
    """找到副歌块在歌词中的所有出现位置。"""
    n = len(lines)
    c_len = len(chorus)
    positions = []

    i = 0
    while i <= n - c_len:
        block = lines[i:i + c_len]
        sims = [similarity(a, b) for a, b in zip(chorus, block)]
        avg = sum(sims) / len(sims)
        if avg >= 0.7:
            positions.append(i)
            i += c_len
        else:
            i += 1

    return positions


def detect_sections(lines: list[str]) -> dict[str, list[str]]:
    """检测歌词段落结构。返回 {段落名: [行列表]}（每个段落只保留一个实例）"""
    if len(lines) < 6:
        return {"完整": lines}

    chorus_range = _find_best_chorus(lines)

    if not chorus_range:
        return _fallback_split(lines)

    chorus_start, chorus_end = chorus_range
    chorus_block = lines[chorus_start:chorus_end]
    c_len = len(chorus_block)

    # 找到所有出现位置
    positions = _find_all_chorus_positions(lines, chorus_block)

    if len(positions) < 2:
        return _fallback_split(lines)

    sections = {}

    # 主歌1: 第一个副歌之前（至少 2 行）
    if positions[0] >= 2:
        sections["主歌1"] = lines[:positions[0]]

    # 副歌: 只保留第一个实例
    sections["副歌"] = chorus_block

    # 主歌2/3...: 副歌之间的段落
    verse_num = 2
    for idx in range(len(positions) - 1):
        start = positions[idx] + c_len
        end = positions[idx + 1]
        verse = lines[start:end]
        if len(verse) >= 2:
            sections[f"主歌{verse_num}"] = verse
            verse_num += 1

    # 尾声: 最后一个副歌之后（至少 2 行）
    last_end = positions[-1] + c_len
    if last_end < len(lines) - 1:
        outro = lines[last_end:]
        if len(outro) >= 2:
            sections["尾声"] = outro

    return sections


def _fallback_split(lines: list[str]) -> dict[str, list[str]]:
    """无检测到重复结构时的回退方案"""
    n = len(lines)
    if n <= 6:
        return {"完整": lines}

    # 3-4 段均匀分割
    pieces = 3 if n < 16 else 4
    chunk = max(2, n // pieces)
    sections = {}
    for i in range(0, n, chunk):
        part = lines[i:i + chunk]
        if len(part) >= 2:
            idx = len(sections) + 1
            sections[f"段落{idx}"] = part
    return sections


def migrate_file(filepath: Path, apply: bool = False) -> str:
    """处理单个歌词文件"""
    content = filepath.read_text(encoding="utf-8").strip()

    if not content:
        return f"\n⚠️ {filepath.name}: 空文件"

    raw_lines = content.split("\n")

    # 解析第一行（歌手名）
    artist = ""
    lyrics_start = 0
    first = raw_lines[0].strip() if raw_lines else ""
    if first and not any(ch in first for ch in "，。！？的了我在是不有会和"):
        artist = first
        lyrics_start = 1

    # 提取歌词行（去空行）
    lyrics_lines = []
    for line in raw_lines[lyrics_start:]:
        stripped = line.strip()
        if stripped:
            lyrics_lines.append(stripped)

    if len(lyrics_lines) < 2:
        # 文件太短（如新歌曲1.txt只有歌手名），跳过
        return f"\n⏭️ {filepath.name}: 歌词不足2行，跳过"

    sections = detect_sections(lyrics_lines)

    # 生成新文件
    new_lines = []
    if artist:
        new_lines.append(artist)
    new_lines.append("")

    for name, slines in sections.items():
        new_lines.append(f"[{name}]")
        new_lines.extend(slines)
        new_lines.append("")

    new_content = "\n".join(new_lines).rstrip() + "\n"

    if apply:
        # 备份（如果还没备份过）
        bak_path = filepath.with_suffix(".txt.bak")
        if not bak_path.exists():
            bak_path.write_text(content, encoding="utf-8")
        filepath.write_text(new_content, encoding="utf-8")

    # 预览
    has_chorus = any("副歌" in k for k in sections)
    preview = f"\n{'─'*50}\n📄 {filepath.name}"
    if artist:
        preview += f"  (🎤 {artist})"
    preview += f"\n   {len(sections)} 段:"
    for name, slines in sections.items():
        first_line = slines[0][:36].ljust(36)
        preview += f"\n   [{name}] {len(slines):>2}行  {first_line}"
    if not has_chorus:
        preview += "  ⚠️ 未检测到副歌"

    return preview


def main():
    parser = argparse.ArgumentParser(description="歌词段落标记迁移工具")
    parser.add_argument("--apply", action="store_true", help="实际写入文件")
    parser.add_argument("--song", type=str, help="只处理指定歌名")
    args = parser.parse_args()

    songs_dir = Path(__file__).parent.parent / "songs"
    txt_files = sorted(songs_dir.glob("*.txt"))
    # 排除备份文件
    txt_files = [f for f in txt_files if not f.name.endswith(".bak")]

    if args.song:
        target = songs_dir / f"{args.song}.txt"
        if not target.exists():
            print(f"❌ 找不到: {args.song}.txt")
            sys.exit(1)
        txt_files = [target]

    if not txt_files:
        print("❌ 没有 .txt 文件")
        sys.exit(1)

    ok = 0
    warn = 0
    for f in txt_files:
        try:
            preview = migrate_file(f, apply=args.apply)
            print(preview)
            if "⚠️" in preview:
                warn += 1
            else:
                ok += 1
        except Exception as e:
            print(f"\n❌ {f.name}: {e}")
            warn += 1

    print(f"\n{'='*50}")
    print(f"📊 {len(txt_files)} 首: ✅{ok} ⚠️{warn}")
    if not args.apply:
        print("💡 预览模式。确认后: python tools/migrate_lyrics_sections.py --apply")
        print("💡 单首预览: python tools/migrate_lyrics_sections.py --song 勇气")


if __name__ == "__main__":
    main()
