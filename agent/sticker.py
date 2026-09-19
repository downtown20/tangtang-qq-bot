"""
小糖糖的表情包系统
从本地文件夹随机挑选表情包发送
+ 情绪索引：收藏时自动打情绪标签，发图时按情绪检索
"""

import os
import json
import random
import logging
import hashlib
import threading
from pathlib import Path


def _cq_image(filepath: str) -> str:
    """生成 CQ 图片码，file:/// 前缀 + 原始路径（正斜杠，不编码）。
    SnowLuma 需要 file:/// 前缀来识别本地文件，但中文路径不需要URL编码。"""
    p = Path(filepath).resolve()
    path_str = str(p).replace('\\', '/')
    return f"[CQ:image,file=file:///{path_str}]"

logger = logging.getLogger("糖糖.Sticker")

STICKER_EXTS = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".gif", ".webp", ".bmp"})

# ---- 情绪分类：千问VL描述 / 聊天文字 → 情绪标签 ----
# 贴图情绪词前缀（单一事实源，2026-09-05 自 tools/标注贴图情绪.py 迁移）：
# 情绪词开头的文件名视为已按规范命名（如 开心_撒娇_xxx.jpg）——标注工具跳过、
# 语义索引用它做无 metadata 图的 fallback 标签。两端共用，防词表漂移。
STICKER_EMOTION_PREFIXES = (

    "开心", "快乐", "高兴", "欢喜", "喜悦", "兴奋", "雀跃", "欢快", "痛快", "欣喜", "欢欣",
    "满足", "惬意", "舒适", "安逸", "知足", "安心", "踏实", "舒心", "放松", "轻松", "悠闲",
    "甜蜜", "幸福", "喜欢", "心动", "依恋", "热恋",
    "温柔", "暖心", "温馨", "治愈", "柔和", "温暖", "体贴", "关怀", "宠爱",
    "撒娇", "卖萌", "可爱", "俏皮", "呆萌", "调皮", "黏人", "淘气", "鬼马",
    "得意", "骄傲", "自豪", "自信", "神气", "嘚瑟", "炫耀", "显摆", "耍酷",
    "期待", "憧憬", "向往", "希望", "盼望",
    "感动", "感恩", "泪目", "感谢", "比心",
    "激动", "热血", "燃", "过瘾", "庆祝", "欢呼", "干杯",
    "鼓励", "加油", "奋斗", "努力", "元气", "活力",
    "享受", "陶醉", "沉浸", "沉迷",
    "难过", "伤心", "悲伤", "心碎", "失落", "沮丧", "消沉", "低落", "郁闷", "忧伤", "哀伤",
    "委屈", "可怜", "求饶", "讨好", "示弱", "委屈巴巴", "可怜兮兮",
    "生气", "愤怒", "暴怒", "恼火", "恼怒", "不爽", "火大", "炸毛", "抓狂", "气恼",
    "鄙视", "嫌弃", "不屑", "厌恶", "反感", "恶心",
    "害怕", "恐惧", "惊恐", "惊慌", "紧张", "忐忑", "不安", "畏惧", "吓到",
    "焦虑", "烦躁", "烦闷", "压抑", "心累",
    "失望", "遗憾", "可惜", "惋惜", "后悔", "懊悔", "自责",
    "孤独", "寂寞", "孤单", "想念", "思念", "怀念", "牵挂",
    "无奈", "无助", "无力", "绝望", "崩溃",
    "疲惫", "困倦", "犯困", "慵懒", "劳累", "躺平", "摆烂",
    "害羞", "羞涩", "脸红", "腼腆", "窘迫", "不好意思",
    "尴尬", "难堪", "不知所措",
    "傲娇", "嘴硬", "逞强", "口是心非",
    "惊讶", "震惊", "诧异", "愕然", "目瞪口呆", "懵逼", "吓一跳", "惊呆",
    "疑惑", "困惑", "不解", "迷茫", "搞不懂", "好奇",
    "无语", "冷漠", "高冷", "冷淡", "平静", "淡定", "无感", "面无表情",
    "思考", "沉思", "发呆", "走神", "专注", "认真", "恍然大悟",
    "搞笑", "好笑", "滑稽", "幽默", "逗趣", "欢乐",
    "坏笑", "邪魅", "奸笑", "腹黑", "阴险", "嘲讽",
    "霸气", "强势", "威风", "帅气", "酷",
    "崇拜", "仰慕", "迷妹", "花痴",
    "想要", "渴望", "贪吃", "眼馋", "垂涎",
    "无聊", "没劲", "乏味", "枯燥", "无趣",
    "日常", "问候", "打招呼", "比耶", "OK", "好的", "收到",
    "偷看", "暗中观察", "窥视", "叹气",)


VISION_EMOTION_MAP = {
    "开心": ["笑", "开心", "快乐", "高兴", "欢乐", "哈哈", "嘻嘻", "笑容", "咧嘴", "好耶", "不错", "太好了", "笑死", "好笑",
             "兴奋", "喜悦", "愉悦", "放松", "积极"],
    "害羞": ["害羞", "脸红", "捂脸", "羞涩", "不好意思", "讨厌"],
    "撒娇": ["撒娇", "可爱", "萌", "亲亲", "抱抱", "蹭", "贴贴", "想你", "爱你", "喜欢",
             "俏皮", "温馨", "关切", "期待"],
    "难过": ["哭", "难过", "伤心", "委屈", "流泪", "呜呜", "眼泪", "emo",
             "忧郁", "孤独", "低落", "无助", "忧伤", "冷漠", "冷淡"],
    "生气": ["生气", "怒", "不爽", "瞪眼", "发脾气", "气鼓鼓", "恶心",
             "严肃", "冷峻", "不悦", "不耐烦", "威严", "锐利", "冷酷", "紧张"],
    "惊讶": ["惊讶", "震惊", "吓", "目瞪口呆", "懵", "愣住",
             "诡异", "好奇", "神秘"],
    "得意": ["得意", "骄傲", "炫耀", "嘚瑟", "坏笑", "奸笑", "嘿嘿", "果然", "我就说", "那当然",
             "自信", "傲娇", "狡黠", "自恋"],
    "无奈": ["无奈", "无语", "叹气", "累", "困", "打哈欠", "疲惫", "栓Q", "随便", "没办法", "累了",
             "焦虑", "焦急", "杂乱"],
    "搞笑": ["搞笑", "滑稽", "幽默", "逗", "恶搞", "离谱", "抽象", "整活", "难绷"],
    "色色": ["色色", "色图", "涩涩", "涩图", "好色", "好涩",
             "做爱", "呻吟", "高潮", "欲火", "肉棒", "淫水", "裸", "骚", "情趣",
             "爽死了", "想要", "插", "操", "干你", "上床", "打炮",
             "发情", "呼噜", "蹭蹭", "舔", "主人想要", "尾巴", "耳朵", "喵~"],
}


_EMOTION_NEGATORS = frozenset({
    "不", "没", "无", "未", "别", "不要", "不是", "并非", "莫", "勿",
    "没有", "从不", "从没", "不太", "未曾",
})


def _emotion_keyword_hit(text: str, keyword: str, tokens) -> bool:
    """按完整 jieba 词匹配情绪，并拒绝紧邻否定语境。"""
    if not keyword:
        return False
    # 英文/数字标签需要单词边界，避免 ``emotion`` 命中 ``emotional``。
    if all(ord(ch) < 128 for ch in keyword):
        import re
        return bool(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text))

    words = [word for word, _start, _end in tokens if word.strip()]
    keyword_len = len(keyword)
    for index, word in enumerate(words):
        if word == keyword:
            span_len = 1
        else:
            span_len = 0
            joined = ""
            for end in range(index, len(words)):
                joined += words[end]
                if len(joined) >= keyword_len:
                    if joined == keyword:
                        span_len = end - index + 1
                    break
        if not span_len:
            continue
        # 至多回看两个有效词，覆盖“不太开心/没有那么生气”等常见表达；
        # 关键词自身如“不爽”仍可正常命中，因为否定词不在其前面。
        previous = words[max(0, index - 2):index]
        if any(word in _EMOTION_NEGATORS for word in previous):
            continue
        return True
    return False


def classify_emotions(text: str) -> list[str]:
    """从一段文字（千问VL描述或任何文本）中识别情绪标签。

    这是无向量时的贴图降级，不负责替 LLM 决定回复情绪；匹配必须按完整词
    进行，并避开否定上下文，防止“不是色色”误选成人贴图。
    """
    if not text:
        return []
    try:
        import jieba
        tokens = list(jieba.tokenize(text, HMM=False))
    except Exception:
        tokens = [(text, 0, len(text))]
    found = []
    for emotion, keywords in VISION_EMOTION_MAP.items():
        if any(_emotion_keyword_hit(text, kw, tokens) for kw in keywords):
            found.append(emotion)
    return found


class StickerManager:
    """表情包管理器 + 情绪索引"""

    def __init__(self, sticker_dir: str = "./stickers"):
        self.sticker_dir = Path(sticker_dir)
        self.sticker_dir.mkdir(parents=True, exist_ok=True)
        self._cache = []
        self._hashes = set()  # 图片哈希去重
        self.collected_count = 0

        # 情绪索引
        self.metadata_file = self.sticker_dir / "metadata.json"
        self.metadata: dict[str, dict] = {}   # {filename: {emotions: [...], desc: "...", source: "..."}}
        self._emotion_index: dict[str, list[str]] = {}  # {emotion: [filenames]}

        # 语义贴图索引：向量只依赖元数据文本，不应在每次回复时逐图重算。
        # 文件与元数据同目录，按模型/文本哈希增量失效；写入采用临时文件替换，
        # 进程崩溃不会留下半个 npz。冷缓存期间搜索退回标签/关键词，不阻塞事件循环。
        self._semantic_cache_path = self.sticker_dir / "semantic_vectors.npz"
        self._semantic_vectors: dict[str, object] = {}
        self._semantic_model_id = ""
        self._semantic_lock = threading.RLock()
        self._semantic_warm_lock = threading.Lock()
        self._semantic_stats = {
            "queries": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "warm_runs": 0,
            "warm_encoded": 0,
        }

        self._load_metadata()
        self._load_semantic_cache()
        self._refresh()

    # ---- 元数据持久化 ----

    def _load_metadata(self):
        if self.metadata_file.exists():
            try:
                self.metadata = json.loads(self.metadata_file.read_text(encoding="utf-8"))
                self._rebuild_index()
                logger.info(
                    f"📋 [{self.sticker_dir}] 加载表情元数据: {len(self.metadata)} 张"
                )
            except Exception as e:
                logger.warning(f"元数据加载失败: {e}")
                self.metadata = {}

    def reload(self):
        """重新读取磁盘上的元数据和图片，供角色切换时吸收人工迁入的素材。"""
        self.metadata = {}
        self._emotion_index = {}
        self._hashes.clear()
        with self._semantic_lock:
            self._semantic_vectors = {}
            self._semantic_model_id = ""
        self._load_metadata()
        self._load_semantic_cache()
        self._refresh()

    @staticmethod
    def _semantic_model_key(embed_engine) -> str:
        """返回向量缓存的模型标识；未知引擎使用稳定的 BGE 默认标识。"""
        return str(
            getattr(embed_engine, "model_id", None)
            or getattr(embed_engine, "MODEL_ID", None)
            or "bge-small-zh-v1.5"
        )

    @staticmethod
    def _semantic_key(filename: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{filename}\x1f{digest}"

    @staticmethod
    def _filename_emotion_text(filename: str) -> str:
        """无 metadata 图的名字 fallback：按规范命名（情绪词_描述_随机）时取情绪段。

        与 tools/标注贴图情绪.py 的「情绪词开头=已处理」语义一致（2026-09-05）：
        用户按规范放图（开心_撒娇_xxx.jpg）即使不跑标注工具，也能进语义池。
        """
        head = filename.split(".")[0].split("_")
        tags = []
        for part in head:
            if part in STICKER_EMOTION_PREFIXES:
                tags.append(part)
            elif tags:  # 情绪段结束（后续是描述/随机串）
                break
        return "，".join(tags) if tags else ""

    def _semantic_entries(self) -> list[tuple[str, str, str]]:
        """按当前图库顺序返回 (cache_key, text, absolute_path)。"""
        entries = []
        for path in self._cache:
            filename = Path(path).name
            info = self.metadata.get(filename, {})
            if not isinstance(info, dict):
                continue
            text = str(info.get("emotion_desc", "") or "").strip()
            if not text and info.get("emotions"):
                text = "，".join(str(emo) for emo in info["emotions"] if str(emo).strip())
            if not text:
                text = self._filename_emotion_text(filename)   # 命名即标签 fallback
            if text:
                entries.append((self._semantic_key(filename, text), text, path))
        return entries

    def _load_semantic_cache(self) -> None:
        """加载持久向量矩阵；损坏或旧模型缓存只会被忽略并重建。"""
        path = self._semantic_cache_path
        if not path.exists():
            return
        try:
            import numpy as np
            with np.load(path, allow_pickle=False) as data:
                keys = data["keys"]
                vectors = np.asarray(data["vectors"], dtype=np.float32)
                model_id = str(np.asarray(data["model_id"]).item())
            if vectors.ndim != 2 or len(keys) != len(vectors):
                raise ValueError("向量矩阵形状无效")
            loaded = {
                str(key): vectors[i]
                for i, key in enumerate(keys)
                if vectors[i].ndim == 1 and vectors[i].size > 0
            }
            with self._semantic_lock:
                self._semantic_vectors = loaded
                self._semantic_model_id = model_id
            logger.info(
                "📋 [%s] 加载语义贴图缓存: %d 条 (model=%s)",
                self.sticker_dir, len(loaded), model_id,
            )
        except Exception as e:
            logger.warning("语义贴图缓存加载失败，将在后台重建: %s", e)
            with self._semantic_lock:
                self._semantic_vectors = {}
                self._semantic_model_id = ""

    def _save_semantic_cache(self, model_id: str) -> None:
        """原子保存当前语义向量；调用方已在 warm lock 内。"""
        import numpy as np

        with self._semantic_lock:
            items = [
                (key, vector) for key, vector in self._semantic_vectors.items()
                if getattr(vector, "ndim", 0) == 1 and getattr(vector, "size", 0) > 0
            ]
        if not items:
            try:
                self._semantic_cache_path.unlink()
            except FileNotFoundError:
                pass
            return
        keys = np.asarray([key for key, _ in items], dtype="U")
        vectors = np.vstack([np.asarray(vector, dtype=np.float32) for _, vector in items])
        temp = self._semantic_cache_path.with_name(
            f"{self._semantic_cache_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temp.open("wb") as stream:
                np.savez_compressed(
                    stream, keys=keys, vectors=vectors,
                    model_id=np.asarray(model_id, dtype="U"),
                )
            os.replace(temp, self._semantic_cache_path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def warm_semantic_cache(self, embed_engine) -> bool:
        """批量增量构建语义贴图缓存，供启动后台任务调用。

        这里不能在前台消息入口兜底逐图 encode：冷缓存只走标签降级，避免
        956 张图的 CPU 工作重新占满事件循环。批量编码失败则不写入半成品。
        """
        if not embed_engine or not getattr(embed_engine, "ready", False):
            return False
        if not self._semantic_warm_lock.acquire(blocking=False):
            return False
        try:
            model_id = self._semantic_model_key(embed_engine)
            entries = self._semantic_entries()
            with self._semantic_lock:
                if self._semantic_model_id != model_id:
                    self._semantic_vectors = {}
                    self._semantic_model_id = model_id
                missing = [entry for entry in entries if entry[0] not in self._semantic_vectors]
                # 删除已迁出/元数据已变化的条目，防止缓存无限增长。
                valid_keys = {entry[0] for entry in entries}
                self._semantic_vectors = {
                    key: vector for key, vector in self._semantic_vectors.items()
                    if key in valid_keys
                }
            encoded = []
            if missing:
                texts = [entry[1] for entry in missing]
                batch_encoder = getattr(embed_engine, "encode_batch", None)
                if callable(batch_encoder):
                    try:
                        encoded = list(batch_encoder(texts) or [])
                    except Exception:
                        encoded = []
                if len(encoded) != len(texts):
                    # fake/旧引擎可能没有严格保持批量长度；逐条重试并按原文对齐，
                    # 仍发生在后台线程，不把错位向量持久化。
                    encoded = [embed_engine.encode(text) for text in texts]
                import numpy as np
                valid = []
                for entry, vector in zip(missing, encoded):
                    if vector is None:
                        continue
                    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
                    if vector.size:
                        valid.append((entry[0], vector))
                with self._semantic_lock:
                    self._semantic_vectors.update(dict(valid))
                    self._semantic_model_id = model_id
                    self._semantic_stats["warm_encoded"] += len(valid)
            with self._semantic_lock:
                self._semantic_stats["warm_runs"] += 1
            self._save_semantic_cache(model_id)
            logger.info(
                "🎨 [%s] 语义贴图缓存完成: total=%d encoded=%d model=%s",
                self.sticker_dir, len(entries), len(encoded), model_id,
            )
            return True
        except Exception as e:
            logger.warning("🎨 [%s] 语义贴图缓存预热失败: %s", self.sticker_dir, e)
            return False
        finally:
            self._semantic_warm_lock.release()

    def semantic_cache_stats(self) -> dict[str, int]:
        """返回只读统计，供运行时观测器/健康检查使用。"""
        with self._semantic_lock:
            return dict(self._semantic_stats)

    def _save_metadata(self):
        try:
            self.metadata_file.write_text(
                json.dumps(self.metadata, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"元数据保存失败: {e}")

    def _rebuild_index(self):
        self._emotion_index = {}
        for fname, info in self.metadata.items():
            for emo in info.get("emotions", []):
                self._emotion_index.setdefault(emo, []).append(fname)

    # ---- 哈希去重 ----

    def _hash(self, data: bytes) -> str:
        import hashlib
        return hashlib.md5(data).hexdigest()

    def _is_duplicate(self, data: bytes) -> bool:
        h = self._hash(data)
        if h in self._hashes:
            return True
        self._hashes.add(h)
        return False

    # ---- 添加表情包 ----

    def add_sticker(self, file_path: str, source: str = "", custom_name: str = "",
                    emotions: list = None) -> str | None:
        """添加一张表情到库中，返回保存路径（自动去重）"""
        src = Path(file_path)
        if not src.exists():
            return None
        data = src.read_bytes()
        if self._is_duplicate(data):
            logger.debug(f"跳过重复表情: {src.name}")
            return None
        import time
        if custom_name:
            name = f"{custom_name}_{int(time.time() % 100000)}{src.suffix}"
        else:
            name = f"collected_{int(time.time() * 1000)}{src.suffix}"
        dest = self.sticker_dir / name
        dest.write_bytes(data)
        self._cache.append(str(dest))
        self.collected_count += 1

        # 存储元数据
        self._add_metadata(name, emotions or [], custom_name or "", source)

        logger.info(f"收集表情: {src.name} -> {name} (来自: {source}, 情绪: {emotions})")
        return str(dest)

    def add_sticker_from_bytes(self, data: bytes, suffix: str = ".gif", source: str = "",
                               custom_name: str = "", emotions: list = None) -> str | None:
        """从字节数据添加表情（自动去重）"""
        if self._is_duplicate(data):
            logger.debug("跳过重复表情(bytes)")
            return None
        import time
        if custom_name:
            name = f"{custom_name}_{int(time.time() % 100000)}{suffix}"
        else:
            name = f"collected_{int(time.time() * 1000)}{suffix}"
        dest = self.sticker_dir / name
        dest.write_bytes(data)
        self._cache.append(str(dest))
        self.collected_count += 1

        # 存储元数据
        self._add_metadata(name, emotions or [], custom_name or "", source)

        logger.info(f"收集表情: {name} (来自: {source}, 情绪: {emotions})")
        return str(dest)

    def tag_sticker(self, keyword: str, emotion: str = "色色") -> int:
        """给文件名包含关键词的图打上情绪标签，返回标记数量"""
        count = 0
        for fname in list(self.metadata.keys()):
            if keyword in fname and emotion not in self.metadata[fname].get("emotions", []):
                self.metadata[fname].setdefault("emotions", [])
                if emotion not in self.metadata[fname]["emotions"]:
                    self.metadata[fname]["emotions"].append(emotion)
                    self._emotion_index.setdefault(emotion, []).append(fname)
                    count += 1
        if count:
            self._save_metadata()
        return count

    def untag_sticker(self, keyword: str, emotion: str = "色色") -> int:
        """去掉文件名包含关键词的图的指定情绪标签，返回去掉数量"""
        count = 0
        for fname in list(self.metadata.keys()):
            if keyword in fname and emotion in self.metadata[fname].get("emotions", []):
                self.metadata[fname]["emotions"].remove(emotion)
                if fname in self._emotion_index.get(emotion, []):
                    self._emotion_index[emotion].remove(fname)
                count += 1
        if count:
            self._save_metadata()
        return count

    def count_by_emotion(self, emotion: str = "色色") -> int:
        """统计某情绪标签的图数量"""
        return len(self._emotion_index.get(emotion, []))

    def list_by_emotion(self, emotion: str = "色色") -> list[str]:
        """列出某情绪标签的所有文件名"""
        return self._emotion_index.get(emotion, [])

    def _add_metadata(self, filename: str, emotions: list[str], desc: str, source: str):
        """记录一张表情的元数据并更新索引"""
        self.metadata[filename] = {
            "emotions": emotions,
            "desc": desc,
            "source": source,
        }
        for emo in emotions:
            self._emotion_index.setdefault(emo, []).append(filename)
        self._save_metadata()

    # ---- 情绪检索 ----

    def get_by_emotion(self, emotion: str) -> str | None:
        """按情绪标签取一张匹配的表情包"""
        paths = self._emotion_index.get(emotion, [])
        if not paths:
            return None
        fname = random.choice(paths)
        full = self.sticker_dir / fname
        if full.exists():
            return _cq_image(str(full.resolve()))
        return None

    def get_by_emotions(self, emotions: list[str]) -> str | None:
        """按多个情绪标签取表情包，找到第一个匹配的就返回"""
        for emo in emotions:
            result = self.get_by_emotion(emo)
            if result:
                return result
        return None

    def search_by_emotion_semantic(self, emotion_text: str, embed_engine,
                                    count: int = 1) -> list[str]:
        """BGE 语义搜索贴图——用自然语言描述情绪，匹配 emotion_desc 字段。
        没有 emotion_desc 的图回退到 emotions 标签拼接。
        返回贴图文件路径列表（最多 count 个），按相似度降序。向量由启动
        后台预热任务批量构建并持久化；冷缓存不逐图编码，直接返回空让上层
        走标签/关键词降级。
        """
        if not self._cache or not embed_engine or not embed_engine.ready:
            return []

        query_vec = embed_engine.encode(emotion_text)
        if query_vec is None:
            return []
        import numpy as np

        model_id = self._semantic_model_key(embed_engine)
        entries = self._semantic_entries()
        with self._semantic_lock:
            self._semantic_stats["queries"] += 1
            if self._semantic_model_id != model_id:
                self._semantic_stats["cache_misses"] += 1
                return []
            available = [
                (vector, path) for key, _text, path in entries
                if (vector := self._semantic_vectors.get(key)) is not None
            ]
            if not available:
                self._semantic_stats["cache_misses"] += 1
                return []
            self._semantic_stats["cache_hits"] += 1

        matrix = np.vstack([vector for vector, _path in available])
        query_vec = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if matrix.ndim != 2 or matrix.shape[1] != query_vec.size:
            logger.warning(
                "🎨 [%s] 语义缓存维度不匹配: matrix=%s query=%s，回退标签",
                self.sticker_dir, matrix.shape, query_vec.shape,
            )
            return []
        scores = matrix @ query_vec
        ranked = sorted(
            zip(scores.tolist(), (path for _vector, path in available)),
            key=lambda item: item[0], reverse=True,
        )
        return [_cq_image(path) for _score, path in ranked[:count]]

    def match_by_emotion_text(self, emotion_text: str, embed_engine=None,
                              count: int = 1, excluded: set[str] | None = None) -> list[str]:
        """统一匹配自然语言情绪，供 Native Tool 和旧贴图标签共同使用。"""
        count = max(1, min(int(count), 20))
        excluded = excluded or set()
        if embed_engine and embed_engine.ready and not excluded:
            semantic = self.search_by_emotion_semantic(
                emotion_text, embed_engine, count=count
            )
            if semantic:
                return semantic

        import re
        candidates = classify_emotions(emotion_text)
        candidates.extend(re.findall(r'[\w一-鿿]{2,}', emotion_text))
        paths = []
        for emotion in candidates:
            if emotion in excluded:
                continue
            path = self.get_by_emotion(emotion)
            if path and path not in paths:
                paths.append(path)
            if len(paths) >= count:
                return paths

        if not paths and not excluded:
            path = self.get_by_keyword(emotion_text)
            if path:
                paths.append(path)
        return paths

    def sticker_keyword_summary(self, limit: int = 10) -> str:
        """生成贴图关键词摘要，供 LLM 参考"""
        if not self._cache:
            return "（暂无表情包）"
        # 取情绪索引中数量最多的几个类别
        ranked = sorted(self._emotion_index.items(), key=lambda x: len(x[1]), reverse=True)
        if not ranked:
            return "（图库中有图片，但暂无可用情绪标签）"
        top = [f"[贴图:{emo}]（{len(files)}张）" for emo, files in ranked[:limit]]
        return "可用的表情包：" + " ".join(top)

    # ---- 基础查询 ----

    def has_stickers(self) -> bool:
        return len(self._cache) > 0

    def _refresh(self):
        """扫描文件夹，更新表情列表并加载哈希，同时清理已删除文件的元数据"""
        self._cache = [
            str(p.resolve()) for p in self.sticker_dir.iterdir()
            if p.suffix.lower() in STICKER_EXTS
        ]
        self._hashes.clear()
        for path in self._cache:
            try:
                self._hashes.add(self._hash(Path(path).read_bytes()))
            except Exception:
                pass

        # 清理已删除文件的元数据
        existing_names = {Path(p).name for p in self._cache}
        stale = [name for name in self.metadata if name not in existing_names]
        if stale:
            for name in stale:
                del self.metadata[name]
            self._rebuild_index()
            self._save_metadata()
            logger.info(f"🧹 清理了 {len(stale)} 条已删除图片的元数据")

        if self._cache:
            logger.info(
                f"[{self.sticker_dir}] 加载了 {len(self._cache)} 张表情包 "
                f"({len(self._emotion_index)} 个情绪类别)"
            )

    @property
    def count(self) -> int:
        return len(self._cache)

    def random_sticker(self) -> str | None:
        """随机拿一张表情包，返回 CQ 码"""
        if not self._cache:
            return None
        path = random.choice(self._cache)
        abs_path = str(Path(path).resolve())
        return _cq_image(abs_path)

    def random_safe_sticker(self) -> str | None:
        """随机拿一张非色色的表情包"""
        if not self._cache:
            return None
        # 找出所有非色色的表情
        safe_paths = [
            p for p in self._cache
            if "色色" not in self.metadata.get(Path(p).name, {}).get("emotions", [])
        ]
        if not safe_paths:
            safe_paths = self._cache  # 全被标色色了？那就全用
        path = random.choice(safe_paths)
        abs_path = str(Path(path).resolve())
        return _cq_image(abs_path)

    def get_by_keyword(self, keyword: str) -> str | None:
        """根据关键词找表情包（搜文件名 + 搜元数据描述）"""
        keyword = keyword.lower()
        # 先搜文件名
        matches = [p for p in self._cache if keyword in Path(p).stem.lower()]
        if not matches:
            # 再搜元数据描述
            matches = [
                str(self.sticker_dir / fname)
                for fname, info in self.metadata.items()
                if keyword in info.get("desc", "").lower()
            ]
            matches = [p for p in matches if Path(p).exists()]
        if matches:
            path = random.choice(matches)
            abs_path = str(Path(path).resolve())
            return _cq_image(abs_path)
        return None


# ---- QQ 自带表情 ----
QQ_FACES = {
    "笑哭": 29, "捂脸": 100, "嘿哈": 97, "白眼": 22,
    "亲亲": 39, "色": 2, "害羞": 6, "委屈": 26,
    "抱抱": 49, "爱心": 46, "心碎": 67, "强": 74,
    "弱": 75, "胜利": 76, "勾引": 78, "坏笑": 109,
    "可怜": 108, "发呆": 3, "尴尬": 10, "调皮": 12,
    "呲牙": 13, "微笑": 14, "难过": 16, "可爱": 18,
    "抓狂": 25, "鄙视": 35, "快哭了": 37, "阴笑": 38,
    "玫瑰": 42, "蛋糕": 53, "咖啡": 60, "太阳": 72,
    "月亮": 73, "飞吻": 85, "跳跳": 128, "转圈": 131,
    "磕头": 132, "激动": 136, "献吻": 138,
}

EMOTION_TO_FACE = {
    "开心": ["笑哭", "呲牙", "嘿哈", "跳跳"],
    "害羞": ["害羞", "可爱", "捂脸"],
    "撒娇": ["亲亲", "抱抱", "飞吻", "献吻"],
    "难过": ["难过", "委屈", "快哭了", "可怜"],
    "生气": ["白眼", "鄙视", "抓狂"],
    "无奈": ["捂脸", "尴尬", "发呆"],
    "得意": ["坏笑", "调皮", "阴笑"],
    "鼓励": ["强", "胜利", "玫瑰"],
}


def random_qq_face(emotion: str = "") -> str:
    """返回一个随机 QQ 表情 CQ 码"""
    if emotion and emotion in EMOTION_TO_FACE:
        name = random.choice(EMOTION_TO_FACE[emotion])
    else:
        name = random.choice(list(QQ_FACES.keys()))
    face_id = QQ_FACES[name]
    return f"[CQ:face,id={face_id}]"


def get_face_for_text(text: str) -> str | None:
    """根据文字内容判断情绪，返回合适的表情"""
    emotion_keywords = {
        "开心": ["哈哈", "嘻嘻", "笑死", "好笑", "开心", "快乐", "高兴", "不错", "好耶", "太好了"],
        "害羞": ["害羞", "不好意思", "脸红", "讨厌"],
        "撒娇": ["贴贴", "抱抱", "想你", "亲", "爱你", "喜欢"],
        "难过": ["难过", "伤心", "哭", "呜呜", "emo", "烦"],
        "生气": ["无语", "离谱", "生气", "不爽", "恶心"],
        "得意": ["嘿嘿", "果然", "我就说", "那当然"],
        "无奈": ["累了", "栓Q", "随便", "没办法"],
    }
    for emotion, keywords in emotion_keywords.items():
        if any(kw in text for kw in keywords):
            return random_qq_face(emotion)
    return None
