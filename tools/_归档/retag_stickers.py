"""
批量为表情包重新打情绪标签
用法：python retag_stickers.py
会扫描 stickers/ 下所有图片，用千问VL识别情绪并更新 metadata.json
"""
import asyncio, json, base64, os, logging
from pathlib import Path
import httpx, yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("重标")

STICKER_DIR = Path("stickers")
META_FILE = STICKER_DIR / "metadata.json"

# 加载已有元数据
metadata = {}
if META_FILE.exists():
    metadata = json.loads(META_FILE.read_text(encoding="utf-8"))

# 加载配置
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)
vision_cfg = config["llm"]["vision"]
API_KEY = vision_cfg["api_key"]
BASE_URL = vision_cfg["base_url"]
MODEL = vision_cfg.get("model", "qwen-vl-plus")


async def get_emotion(filepath: Path) -> str:
    """调用千问VL识别单张图的情绪"""
    img_b64 = base64.b64encode(filepath.read_bytes()).decode()
    body = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": "用空格分隔的情绪词描述这张图，如：开心 撒娇。最多3个词，只输出词。"}
            ]
        }],
        "max_tokens": 30,
    }
    async with httpx.AsyncClient(timeout=15.0) as c:
        resp = await c.post(f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json=body)
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        return ""


async def main():
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    files = [f for f in STICKER_DIR.iterdir() if f.suffix.lower() in exts]

    total = len(files)
    updated = 0
    skipped = 0
    failed = 0

    for i, fp in enumerate(files):
        name = fp.name
        # 情绪词开头+下划线 = 已处理过（如 开心_撒娇_xxx.jpg）
        emotion_prefixes = (
            # === 快乐 ===
            "开心", "快乐", "高兴", "欢喜", "喜悦", "兴奋", "雀跃", "欢快", "痛快", "欣喜", "欢欣",
            # === 满足 ===
            "满足", "惬意", "舒适", "安逸", "知足", "安心", "踏实", "舒心", "放松", "轻松", "悠闲",
            # === 幸福 ===
            "甜蜜", "幸福", "喜欢", "心动", "依恋", "热恋",
            # === 温柔 ===
            "温柔", "暖心", "温馨", "治愈", "柔和", "温暖", "体贴", "关怀", "宠爱",
            # === 撒娇 ===
            "撒娇", "卖萌", "可爱", "俏皮", "呆萌", "调皮", "黏人", "淘气", "鬼马",
            # === 得意 ===
            "得意", "骄傲", "自豪", "自信", "神气", "嘚瑟", "炫耀", "显摆", "耍酷",
            # === 期待 ===
            "期待", "憧憬", "向往", "希望", "盼望",
            # === 感动 ===
            "感动", "感恩", "泪目", "感谢", "比心",
            # === 激动 ===
            "激动", "热血", "燃", "过瘾", "庆祝", "欢呼", "干杯",
            # === 鼓励 ===
            "鼓励", "加油", "奋斗", "努力", "元气", "活力",
            # === 享受 ===
            "享受", "陶醉", "沉浸", "沉迷",
            # === 难过 ===
            "难过", "伤心", "悲伤", "心碎", "失落", "沮丧", "消沉", "低落", "郁闷", "忧伤", "哀伤",
            # === 委屈 ===
            "委屈", "可怜", "求饶", "讨好", "示弱", "委屈巴巴", "可怜兮兮",
            # === 生气 ===
            "生气", "愤怒", "暴怒", "恼火", "恼怒", "不爽", "火大", "炸毛", "抓狂", "气恼",
            # === 鄙视 ===
            "鄙视", "嫌弃", "不屑", "厌恶", "反感", "恶心",
            # === 害怕 ===
            "害怕", "恐惧", "惊恐", "惊慌", "紧张", "忐忑", "不安", "畏惧", "吓到",
            # === 焦虑 ===
            "焦虑", "烦躁", "烦闷", "压抑", "心累",
            # === 失望 ===
            "失望", "遗憾", "可惜", "惋惜", "后悔", "懊悔", "自责",
            # === 孤独 ===
            "孤独", "寂寞", "孤单", "想念", "思念", "怀念", "牵挂",
            # === 无助 ===
            "无奈", "无助", "无力", "绝望", "崩溃",
            # === 疲惫 ===
            "疲惫", "困倦", "犯困", "慵懒", "劳累", "躺平", "摆烂",
            # === 害羞 ===
            "害羞", "羞涩", "脸红", "腼腆", "窘迫", "不好意思",
            # === 尴尬 ===
            "尴尬", "难堪", "不知所措",
            # === 傲娇 ===
            "傲娇", "嘴硬", "逞强", "口是心非",
            # === 惊讶 ===
            "惊讶", "震惊", "诧异", "愕然", "目瞪口呆", "懵逼", "吓一跳", "惊呆",
            # === 疑惑 ===
            "疑惑", "困惑", "不解", "迷茫", "搞不懂", "好奇",
            # === 无语 ===
            "无语", "冷漠", "高冷", "冷淡", "平静", "淡定", "无感", "面无表情",
            # === 思考 ===
            "思考", "沉思", "发呆", "走神", "专注", "认真", "恍然大悟",
            # === 搞笑 ===
            "搞笑", "好笑", "滑稽", "幽默", "逗趣", "欢乐",
            # === 坏笑 ===
            "坏笑", "邪魅", "奸笑", "腹黑", "阴险", "嘲讽",
            # === 霸气 ===
            "霸气", "强势", "威风", "帅气", "酷",
            # === 崇拜 ===
            "崇拜", "仰慕", "迷妹", "花痴",
            # === 欲望 ===
            "想要", "渴望", "贪吃", "眼馋", "垂涎",
            # === 无聊 ===
            "无聊", "没劲", "乏味", "枯燥", "无趣",
            # === 其他 ===
            "日常", "问候", "打招呼", "比耶", "OK", "好的", "收到",
            "偷看", "暗中观察", "窥视", "叹气",
        )
        if name.startswith(emotion_prefixes):
            skipped += 1
            if skipped % 50 == 0:
                logger.info(f"已跳过 {skipped} 张（已重命名）")
            continue

        if name not in metadata:
            metadata[name] = {"emotions": [], "desc": "", "source": ""}

        logger.info(f"[{i+1}/{total}] {name}")
        try:
            emotions_str = await get_emotion(fp)
        except Exception as e:
            logger.warning(f"  ✗ 失败: {e}")
            failed += 1
            continue

        if emotions_str:
            # 解析情绪词
            emotions = [e.strip() for e in emotions_str.replace("，", " ").replace(",", " ").split() if e.strip()]
            emotions = [e for e in emotions if len(e) >= 2]
            if not emotions:
                emotions = ["日常"]

            old_source = metadata.get(name, {}).get("source", "")

            # 重命名文件：情绪_原hash.ext
            import hashlib
            new_stem = "_".join(emotions[:2])
            old_hash = hashlib.md5(name.encode()).hexdigest()[:4]
            new_name = f"{new_stem}_{old_hash}{fp.suffix}"
            new_path = STICKER_DIR / new_name

            if new_path != fp and not new_path.exists():
                fp.rename(new_path)

            # 更新元数据（用新名）
            metadata.pop(name, None)
            metadata[new_name] = {"emotions": emotions, "desc": emotions_str, "source": old_source}

            updated += 1
            logger.info(f"  → {emotions} → {new_name}")

        # 每10张存一次
        if updated % 10 == 0:
            META_FILE.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已保存 ({updated}张)")

        await asyncio.sleep(0.5)  # 避免请求太快

    # 最终保存
    META_FILE.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"完成！{total}张 → 更新{updated} / 跳过{skipped} / 失败{failed}")


if __name__ == "__main__":
    asyncio.run(main())
