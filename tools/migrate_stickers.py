"""
表情包情绪标签迁移脚本
给 stickers/ 里所有没有情绪标签的旧图，用千问 VL 识别并打标签

用法：
    python migrate_stickers.py          # 处理所有未打标签的图
    python migrate_stickers.py --dry-run  # 预览但不执行
    python migrate_stickers.py --force    # 强制重新识别全部（覆盖已有标签）
"""

import sys
import asyncio
import base64
import json
import logging
import time
from pathlib import Path

import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("表情迁移")


# ---- 情绪分类（从 sticker.py 复制，避免导入依赖） ----
VISION_EMOTION_MAP = {
    "开心": ["笑", "开心", "快乐", "高兴", "欢乐", "哈哈", "嘻嘻", "笑容", "咧嘴", "好耶", "不错", "太好了", "笑死", "好笑"],
    "害羞": ["害羞", "脸红", "捂脸", "羞涩", "不好意思", "讨厌"],
    "撒娇": ["撒娇", "可爱", "萌", "亲亲", "抱抱", "蹭", "贴贴", "想你", "爱你", "喜欢"],
    "难过": ["哭", "难过", "伤心", "委屈", "流泪", "呜呜", "眼泪", "emo"],
    "生气": ["生气", "怒", "不爽", "瞪眼", "发脾气", "气鼓鼓", "恶心"],
    "惊讶": ["惊讶", "震惊", "吓", "目瞪口呆", "懵", "愣住"],
    "得意": ["得意", "骄傲", "炫耀", "嘚瑟", "坏笑", "奸笑", "嘿嘿", "果然", "我就说", "那当然"],
    "无奈": ["无奈", "无语", "叹气", "累", "困", "打哈欠", "疲惫", "栓Q", "随便", "没办法", "累了"],
    "搞笑": ["搞笑", "滑稽", "幽默", "逗", "恶搞", "离谱", "抽象", "整活", "难绷"],
}


def classify_emotions(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for emotion, keywords in VISION_EMOTION_MAP.items():
        if any(kw in text for kw in keywords):
            found.append(emotion)
    return found


async def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    # 加载配置
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error("找不到 config.yaml")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    vision_cfg = config["llm"].get("vision", {})
    if not vision_cfg.get("enabled"):
        logger.error("config.yaml 里 vision.enabled 是 false，先开启识图功能")
        return

    api_key = vision_cfg["api_key"]
    base_url = vision_cfg["base_url"]
    model = vision_cfg.get("model", "qwen-vl-plus")

    # 扫描 stickers 文件夹
    sticker_dir = Path(config.get("sticker_dir", "./stickers"))
    if not sticker_dir.exists():
        logger.error(f"表情包文件夹不存在: {sticker_dir}")
        return

    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    all_images = sorted(
        [p for p in sticker_dir.iterdir() if p.suffix.lower() in exts and p.is_file()]
    )
    logger.info(f"找到 {len(all_images)} 张图片")

    # 加载现有元数据
    metadata_file = sticker_dir / "metadata.json"
    metadata = {}
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
            logger.info(f"已有元数据: {len(metadata)} 条")
        except Exception:
            pass

    # 过滤出需要处理的
    todo = []
    skipped = 0
    for img in all_images:
        name = img.name
        if name == "metadata.json":
            continue
        if not force and name in metadata and metadata[name].get("emotions"):
            skipped += 1
            continue
        todo.append(img)

    if skipped:
        logger.info(f"跳过 {skipped} 张已有标签的（用 --force 强制覆盖）")
    logger.info(f"待处理: {len(todo)} 张")

    if not todo:
        logger.info("没有需要处理的图片")
        return

    if dry_run:
        logger.info("[DRY RUN] 不会实际调用API，预览前10张：")
        for img in todo[:10]:
            logger.info(f"  - {img.name} ({img.stat().st_size // 1024}KB)")
        return

    # 逐张识别
    async with httpx.AsyncClient(timeout=30.0) as client:
        success = 0
        fail = 0

        for i, img in enumerate(todo):
            name = img.name
            size_kb = img.stat().st_size // 1024
            logger.info(f"[{i+1}/{len(todo)}] {name} ({size_kb}KB)")

            try:
                # base64 编码
                img_b64 = base64.b64encode(img.read_bytes()).decode()

                # 调千问 VL
                body = {
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                            {"type": "text", "text": "用一两句简短中文描述这张图的内容和情绪。不超过30字。"}
                        ]
                    }],
                    "max_tokens": 80,
                }

                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )

                if resp.status_code != 200:
                    logger.warning(f"  ✗ API错误: {resp.status_code} {resp.text[:100]}")
                    fail += 1
                    # 速率限制时多等一会儿
                    if resp.status_code == 429:
                        wait = 10
                        logger.info(f"  限流，等待 {wait}s...")
                        await asyncio.sleep(wait)
                    continue

                data = resp.json()
                desc = data["choices"][0]["message"]["content"].strip()
                emotions = classify_emotions(desc)

                if not emotions:
                    emotions = ["未分类"]

                # 写入元数据
                metadata[name] = {
                    "emotions": emotions,
                    "desc": desc,
                    "source": "迁移脚本",
                }

                # 每处理一张就保存，防止中断丢失
                metadata_file.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )

                logger.info(f"  ✓ {desc} → {emotions}")
                success += 1

            except Exception as e:
                logger.warning(f"  ✗ 异常: {e}")
                fail += 1

            # 控制频率（千问免费版 ~60 QPM，保守一点每秒一张）
            await asyncio.sleep(1.2)

    logger.info(f"\n完成！成功: {success}, 失败: {fail}, 总计: {len(todo)}")
    logger.info(f"元数据已保存到: {metadata_file}")


if __name__ == "__main__":
    asyncio.run(main())
