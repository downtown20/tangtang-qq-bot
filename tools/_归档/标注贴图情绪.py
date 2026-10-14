"""
🎨 批量贴图情绪标注
用 MiniCPM-V (Ollama) 给每张贴图打上自然语言情绪描述。
用法：python 标注贴图情绪.py
"""

import json, re, time
from pathlib import Path
from agent.vision_local import get_vision_engine

BASE = Path(__file__).parent
STICKER_DIRS = [
    ("默认(糖糖)", BASE / "stickers"),
    ("CG(色色)", BASE / "stickers_cg"),
    ("丛雨(murasame)", BASE / "stickers_murasame"),
    ("雪莉(xueli)", BASE / "stickers_xueli"),
]

PROMPT = (
    "描述这张表情包的情绪和氛围。用中文回复，格式：\n"
    "情绪词（1-3个，逗号分隔）\n"
    "简短氛围描述（20字以内）\n\n"
    "示例：\n"
    "开心、兴奋、摇尾巴\n"
    "一只小猫高兴得尾巴翘起来"
)

engine = get_vision_engine()
if not engine.available:
    print("❌ Ollama MiniCPM-V 不可用。请先启动 Ollama。")
    exit(1)

total_all = 0
for label, sticker_dir in STICKER_DIRS:
    meta_file = sticker_dir / "metadata.json"
    if not meta_file.exists():
        print(f"  📝 {label}: metadata.json 不存在，将从头构建")

    # 加载或初始化 metadata
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    else:
        meta = {}
    # 扫描目录，把未在 metadata 中的文件加进去
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    for f in sorted(sticker_dir.iterdir()):
        if f.suffix.lower() in exts and f.name not in meta:
            meta[f.name] = {}
    total = len(meta)
    done = 0
    skipped = 0
    total_all += total

    print(f"\n🎨 {label} — {total} 张")

    for filename, info in meta.items():
        is_gif = filename.endswith('.gif')
        has_emotions = isinstance(info, dict) and info.get("emotions") and len(info["emotions"]) > 0
        # 已有 emotion_desc 且 emotions 标签也填了的跳过（GIF 例外——始终用多帧重标）
        if not is_gif and has_emotions and isinstance(info, dict) and info.get("emotion_desc"):
            skipped += 1
            continue

        filepath = sticker_dir / filename
        if not filepath.exists():
            continue

        try:
            if is_gif and hasattr(engine, 'describe_gif'):
                desc = engine.describe_gif(str(filepath), prompt=PROMPT)
            else:
                img_bytes = filepath.read_bytes()
                desc = engine.describe(img_bytes, prompt=PROMPT)
            if desc and len(desc) > 3:
                if not isinstance(info, dict):
                    info = {}
                info["emotion_desc"] = desc.strip()
                # 从 emotion_desc 提取情绪词填充 emotions 列表（旧关键词匹配的兜底）
                import re as _re
                first_line = desc.strip().split('\n')[0]
                # 提取逗号分隔的情绪词
                emotion_words = _re.findall(r'[\w一-鿿]{2,5}', first_line.replace('：',' ').replace(':',' '))
                # 过滤掉非情绪的描述词
                skip = {'这幅','表情','描述','情绪','氛围','图片','一个','角色','卡通','展示'}
                emotions_list = [w for w in emotion_words if w not in skip][:4]
                if emotions_list:
                    info["emotions"] = emotions_list
                meta[filename] = info
                done += 1
                if done % 10 == 0:
                    print(f"  {done}/{total - skipped} ...")
        except Exception as e:
            print(f"  ⚠ {filename}: {e}")

        time.sleep(0.5)

    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ 新增 {done}, 跳过 {skipped}, 总计 {total}")

print(f"\n🏁 全部完成: {total_all} 张")
