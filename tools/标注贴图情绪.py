"""
🎨 批量贴图情绪标注（统一版）

用法：python tools/标注贴图情绪.py [目录名...]

后端选择（控制台「识图方式」配置，见 config.yaml → llm.vision.provider）：
    local      → 本地 MiniCPM-V (Ollama) 优先，不可用自动切云端千问 VL
    api / qwen → 云端千问 VL 优先，失败自动切本地

功能（合并原 retag_stickers.py + 标注贴图情绪.py）：
    - 扫描 4 个贴图目录（stickers / stickers_cg / stickers_murasame / stickers_michele）
    - 未打标签的图用视觉模型识别情绪 → 写 metadata.json
    - 情绪词开头的文件名视为已处理，自动跳过（如 开心_撒娇_abc.png）
    - GIF 用本地多帧感知（可感知情绪变化）
    - 可指定目录：python tools/标注贴图情绪.py stickers_cg

CG 图库（stickers_cg）走**场景轴**（2026-10-16）：
    提问改成「这张图演的是什么」，答案用 agent/cg_scenes.py 的规范名（后入/骑乘/
    口交/睡奸/野战…）过一遍归一化，写进 metadata 的 `scene_tags`，并落在文件名
    首位（`场景_情绪_hash.ext`）。这样 `[贴图:后入]` 与 send_cg_sticker(tag="后入")
    都能精确命中——标签体系、展示、获取方式与默认图库完全一致。
"""
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # agent 包可导入
from agent.cg_scenes import SCENES, canonical, match_tags
from agent.sticker import STICKER_EMOTION_PREFIXES as EMOTION_PREFIXES
from agent.sticker import STICKER_EXTS
from agent.vision_router import get_vision_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("标注")

BASE = Path(__file__).resolve().parent.parent
STICKER_DIRS = [
    ("默认(糖糖)", BASE / "stickers"),
    # 分组名只描述「哪个图库」，不写内容标签（CLAUDE.md 教训 #52：
    # 内容名不进信息架构；判断走下面的 CG_DIR_NAMES，与显示名无关）
    ("CG 图库", BASE / "stickers_cg"),
    ("丛雨(murasame)", BASE / "stickers_murasame"),
    ("米雪儿(michele)", BASE / "stickers_michele"),
]
EXTS = STICKER_EXTS
PROMPT = ("用空格分隔的情绪词描述这张图，如：开心 撒娇。最多3个词，只输出词。")
# CG 图库用场景轴提问——「这张图演的是什么」，不是「她什么心情」（2026-10-16）
CG_DIR_NAMES = frozenset({"stickers_cg"})
CG_PROMPT = (
    "用空格分隔的场景词描述这张成人向CG，可自由用词，但优先从这些词里选："
    + " ".join(list(SCENES)[:40])
    + "。最多4个词，只输出词。"
)

# 情绪词开头 = 已重命名处理过（跳过）——与原 retag_stickers.py 一致


def load_metadata(meta_file: Path) -> dict:
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(f"  ⚠ metadata.json 损坏，重新构建: {meta_file}")
    return {}


def _dedup(words) -> list[str]:
    """保持顺序去重（标签首位最重要，不能被 set 打乱）。"""
    out: list[str] = []
    for word in words:
        if word and word not in out:
            out.append(word)
    return out


def save_metadata(meta_file: Path, meta: dict):
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def is_animated_image(filepath: Path) -> bool:
    if filepath.suffix.lower() == ".gif":
        return True
    if filepath.suffix.lower() != ".webp":
        return False
    try:
        from PIL import Image
        with Image.open(filepath) as image:
            return getattr(image, "n_frames", 1) > 1
    except Exception:
        return False


async def label_dir(label: str, sticker_dir: Path) -> tuple[int, int, int]:
    """处理单个贴图目录。返回 (更新, 跳过, 失败)"""
    if not sticker_dir.exists():
        logger.info(f"[{label}] 目录不存在，跳过: {sticker_dir}")
        return 0, 0, 0

    is_cg = sticker_dir.name in CG_DIR_NAMES
    prompt = CG_PROMPT if is_cg else PROMPT
    meta_file = sticker_dir / "metadata.json"
    meta = load_metadata(meta_file)
    files = []
    updated = skipped = failed = 0

    # 只处理缺失/空标签，已有有效元数据绝不重复调用视觉模型。
    for f in sorted(sticker_dir.iterdir()):
        if f.suffix.lower() not in EXTS:
            continue
        old = meta.get(f.name, {})
        if isinstance(old, dict) and old.get("needs_label"):
            # 抓取器收进来还没读图的图——即使带着来源标签也要走识图（2026-10-16）
            old = {k: v for k, v in old.items() if k != "needs_label"}
            meta[f.name] = old
            files.append(f)
            continue
        # CG 图库的完整标签 = 场景轴；只有情绪、没有场景的条目同样要补（老条目兼容）
        if is_cg and not old.get("scene_tags"):
            meta.setdefault(f.name, {})
            files.append(f)
            continue
        if old.get("emotions"):
            skipped += 1
            continue

        # 标注工具自身生成的「情绪_情绪_hash.ext」可直接从文件名恢复，
        # 避免 metadata 丢失后再次识图、再次改名。
        if f.name.startswith(EMOTION_PREFIXES):
            inferred = [part for part in f.stem.split("_")
                        if part in EMOTION_PREFIXES][:2]
            if not inferred:
                first = next((emo for emo in EMOTION_PREFIXES if f.name.startswith(emo)), "日常")
                inferred = [first]
            # 保留 grabber 写入的 scene_tags / md5 / source_site（只补缺失字段）
            rec = dict(old) if isinstance(old, dict) else {}
            rec.update({
                "emotions": inferred,
                "desc": rec.get("desc") or " ".join(inferred),
                "source": rec.get("source", ""),
            })
            meta[f.name] = rec
            updated += 1
            continue

        # 抓取器落库的 CG（文件名已含场景标签）同样可从名字恢复，不必识图
        if is_cg:
            from_name = match_tags(f.stem.replace("_", " "))
            if from_name:
                rec = dict(old) if isinstance(old, dict) else {}
                scene = [t for t in from_name if t in SCENES]
                rec.update({
                    "emotions": [t for t in from_name if t not in SCENES] or ["色色"],
                    "scene_tags": scene,
                    "desc": rec.get("desc") or "、".join(from_name),
                    "source": rec.get("source", ""),
                })
                meta[f.name] = rec
                updated += 1
                continue

        meta.setdefault(f.name, {"emotions": [], "desc": "", "source": ""})
        files.append(f)

    total = len(files)

    if total == 0:
        if updated:
            save_metadata(meta_file, meta)
        logger.info(f"[{label}] 无需处理")
        return updated, skipped, failed

    router = get_vision_router()
    logger.info(f"[{label}] 待标注 {total} 张（后端: {router.provider}，轴: "
                f"{'场景' if is_cg else '情绪'}）")
    for i, fp in enumerate(files):
        name = fp.name
        logger.info(f"  [{i+1}/{total}] {name}")
        try:
            if is_animated_image(fp):
                emotions_str = router.describe_gif(fp, prompt)
            else:
                emotions_str = router.describe(fp.read_bytes(), prompt)
        except Exception as e:
            logger.warning(f"  ✗ 识别失败: {e}")
            failed += 1
            continue

        if not emotions_str:
            logger.warning(f"  ✗ 空结果（本地+云端都不可用？）")
            failed += 1
            continue

        raw_emotions = emotions_str.replace("，", " ").replace(",", " ").split()
        emotions = []
        for item in raw_emotions:
            emotion = item.strip(" \t\r\n。.!！?？、；;：:\"'“”‘’()（）[]【】")
            if len(emotion) >= 2 and emotion not in emotions:
                emotions.append(emotion)
            if len(emotions) >= 3:
                break
        if not emotions:
            emotions = ["日常"]

        old_source = meta.get(name, {}).get("source", "")
        rec = dict(meta.get(name) or {})
        rec.pop("needs_label", None)          # 读图完成，待标注标记就地消费
        scene_tags: list[str] = []

        if is_cg:
            # 场景轴归一化：模型给什么词都过一遍别名表，收敛成规范名（保持顺序去重）
            scene_tags = _dedup([canonical(word) for word in emotions])
            if not scene_tags:
                scene_tags = [canonical(emotions[0])]
            emotion_tag = next((t for t in scene_tags if t not in SCENES), "色色")
            # 词表外的新词也保留（标签轴开放），但文件名首位只放规范名，保证可检索
            rec.update({"scene_tags": scene_tags,
                        "emotions": [t for t in scene_tags if t not in SCENES] or ["色色"],
                        "desc": emotions_str, "source": old_source})
            new_stem = "_".join([scene_tags[0], emotion_tag])
        else:
            rec.update({"emotions": emotions, "desc": emotions_str, "source": old_source})
            new_stem = "_".join(emotions[:2])

        # 重命名：情绪_原hash.ext（已处理过的跳过逻辑依赖这个命名）
        old_hash = hashlib.md5(name.encode()).hexdigest()[:4]
        new_name = f"{new_stem}_{old_hash}{fp.suffix}"
        new_path = sticker_dir / new_name
        if new_path != fp and not new_path.exists():
            fp.rename(new_path)

        meta.pop(name, None)
        meta[new_name] = rec
        updated += 1
        logger.info(f"  → {scene_tags or emotions} → {new_name}")
        if updated % 10 == 0:
            save_metadata(meta_file, meta)

    save_metadata(meta_file, meta)
    logger.info(f"[{label}] 完成！{total}张 → 更新{updated} / 跳过{skipped} / 失败{failed}")
    return updated, skipped, failed


async def main() -> int:
    targets = [a for a in sys.argv[1:] if a]
    dirs = STICKER_DIRS
    if targets:
        dirs = [(name, d) for name, d in STICKER_DIRS
                if any(t in name or t in str(d) for t in targets)]
        if not dirs:
            print(f"❌ 找不到匹配的目录: {targets}")
            return 1

    router = get_vision_router()
    print(f"🎨 贴图情绪标注（后端优先: {router.provider}，失败自动切换）")
    print(f"   provider 配置来自控制台「识图方式」: config.yaml → llm.vision.provider")

    total_u = total_s = total_f = 0
    for label, d in dirs:
        u, s, f = await label_dir(label, d)
        total_u += u
        total_s += s
        total_f += f
    print(f"\n✅ 全部完成：更新{total_u} / 跳过{total_s} / 失败{total_f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))