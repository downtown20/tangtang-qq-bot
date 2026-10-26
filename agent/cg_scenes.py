"""CG 场景标签词表——亲密场景专用标签轴的**单一事实源**。

为什么单独一条轴（2026-10-16）
────────────────────────────────
默认图库的标签轴是「情绪」（开心/害羞/撒娇…），CG 图库要回答的是另一个问题：
**这张图演的是什么**（后入/口交/骑乘/睡奸/野战…）。两者都进 `_emotion_index`
统一检索，但词表必须分开，否则：

  ① 场景词会被当成情绪词写进 `emotions`，语义上骗人（「后入」不是情绪）；
  ② 只要写一份，标注工具、抓取工具、提示词摘要三处必然漂移
     （CLAUDE.md 教训 #4 的同族：同一份事实两个副本）。

所以本模块是唯一来源：`SCENES` 定义规范名 + 别名，别处只许 import。

检索语义（与现有表情包方案完全同构）
────────────────────────────────────
  · 标签即[贴图:]关键词 —— LLM 输出 `[贴图:后入]` / 调 send_cg_sticker(tag="后入")
  · 命中路径 = StickerManager.match_by_emotion_text（有向量走 BGE 语义，无向量走标签精确）
  · 落盘格式 = `标签_标签_hash.ext` 文件名 + metadata.json 的 emotions/scene_tags/emotion_desc

别名存在的唯一理由：LLM 不会永远说「后入」——它可能说「背后位」「从后面」。
别名在**写入时**就收敛成规范名（`canonical()`），检索时只认规范名，避免索引里长出一堆近义词。
"""

from __future__ import annotations

# ---- 标签轴 1：场景（做什么）—— CG 主标签 ----
# 顺序即提示词里的展示顺序：越靠前越常用、越容易被 LLM 想起。
SCENES: dict[str, tuple[str, ...]] = {
    # 体位 / 动作
    "后入": ("背后位", "从后面", "趴着", "doggy", "doggystyle", "from_behind", "bent_over"),
    "骑乘": ("女上位", "坐在上面", "骑上去", "cowgirl", "girl_on_top", "straddling"),
    "正常位": ("正面位", "面对面", "missionary", "vaginal", "sex", "hetero"),
    "口交": ("吃棒", "含", "舔棒", "fellatio", "blowjob", "oral", "deepthroat"),
    "乳交": ("胸交", "paizuri", "titfuck", "breast_squeeze"),
    "足交": ("footjob", "脚", "用脚"),
    "手交": ("撸", "手活", "handjob", "masturbation", "fingering"),
    "素股": ("摩擦", "夹腿", "大腿间", "grinding", "thigh_sex", "intercrural"),
    "舔阴": ("口活", "舔下面", "cunnilingus", "licking_pussy", "oral_on_pussy"),
    "舔乳": ("含乳", "吸乳", "舔胸", "nipple_lick", "licking_nipples"),
    "亲吻": ("接吻", "亲嘴", "kiss", "kissing", "french_kiss"),
    "自慰": ("自己来", "摸自己", "masturbating", "self_pleasure"),
    "玩具": ("道具", "跳蛋", "按摩棒", "sex_toy", "vibrator", "dildo", "vibe"),
    "束缚": ("捆绑", "绑起来", "bondage", "restrained", "shibari", "cuffed"),
    "调教": ("master", "sub", "bdsm", "leash", "collar", "submission"),
    "多P": ("双飞", "3p", "threesome", "group_sex", "gangbang"),
    # 情境 / 场景
    "睡奸": ("睡着", "熟睡", "偷干", "不省人事", "sleeping", "sleep_sex", "unconscious"),
    "醉酒": ("喝醉", "醉倒", "宿醉", "drunk", "intoxicated"),
    "野战": ("户外", "野外", "公园", "outdoor", "outside", "public"),
    "车震": ("车里", "车厢", "car_sex", "in_car"),
    "浴室": ("淋浴", "浴缸", "shower", "bath", "bathtub", "wet_body"),
    "厨房": ("灶台", "kitchen", "counter"),
    "教室": ("课桌", "学校", "classroom", "school", "desk"),
    "办公室": ("办公桌", "加班", "office", "workplace"),
    "更衣室": ("试衣间", "locker_room", "changing_room"),
    "被窝": ("床上", "被子里", "bed", "under_covers", "bedroom", "on_bed"),
    "沙发": ("客厅", "couch", "sofa", "living_room"),
    "窗边": ("阳台", "窗前", "window", "balcony"),
    "镜子": ("对着镜", "镜前", "mirror", "reflection"),
    "夜间": ("深夜", "半夜", "晚上", "night", "midnight"),
    "清晨": ("早晨", "刚醒", "morning", "waking_up"),
    "雨天": ("下雨", "雨中", "rain", "rainy"),
    "海边": ("沙滩", "泳装", "beach", "swimsuit", "poolside"),
    "电车": ("车厢里", "train", "subway"),
    "和服": ("浴衣", "kimono", "yukata"),
    "婚纱": ("婚礼", "wedding", "bride"),
    "制服": ("校服", "护士", "女仆", "uniform", "school_uniform", "cheongsam"),
    "丝袜": ("黑丝", "白丝", "连裤袜", "pantyhose", "thighhighs", "stockings"),
    "内衣": ("蕾丝", "胸罩", "内裤", "lingerie", "bra", "panties", "underwear"),
    "半脱": ("衣服撩起", "衣衫不整", "undressing", "clothes_lift", "panties_pull"),
    "全裸": ("裸体", "脱光", "nude", "naked", "completely_nude", "topless"),
    "暴露": ("真空", "不穿内衣", "走光", "exposed", "no_bra", "flashing", "see_through"),
    # ★ 2026-10-17 补：身体部位细分。此前打标脚本自己造了 露乳/露臀/露阴/贴合 四个
    #   标签，但规范表里没有 → match_tags 返回不了它们，98 条标签在贴图检索里
    #   完全是隐形的（「胸部外露」「臀部裸露」「两人紧贴」实测都返回 []）。
    #   规范名收在这里才是根治——单一事实源，别处只许 import。
    "露乳": ("胸部外露", "乳房", "乳头", "双乳", "露胸", "胸部裸露",
             "nipples", "bare_breasts", "breasts_out"),
    "露臀": ("臀部裸露", "屁股", "高抬臀", "撅起", "露出臀部",
             "bare_ass", "presenting", "ass_up"),
    "露阴": ("阴部外露", "生殖器", "阴唇", "私处外露",
             "pussy", "exposed_pussy", "genitals_visible"),
    "贴合": ("紧贴", "身体贴合", "紧紧相贴", "摩擦",
             "grinding", "body_contact", "pressed_together"),
    "湿身": ("汗", "水", "体液", "wet", "sweat", "sweaty", "bodily_fluids"),
    "事后": ("余韵", "结束之后", "afterglow", "after_sex", "cuddling", "post_coital"),
    # 情绪 / 表情（CG 现场情绪，与默认图库情绪轴同词表）
    "害羞": ("脸红", "羞涩", "不好意思", "blush", "embarrassed", "shy"),
    "娇喘": ("呻吟", "忍不住叫", "喘", "moaning", "heavy_breathing", "gasping"),
    "高潮": ("去了", "忍不住", "orgasm", "climax", "ahegao", "rolled_eyes"),
    "沉醉": ("迷离", "舒服", "享受", "pleasure", "ecstasy", "blissful"),
    "求饶": ("不要了", "停下", "begging", "please_stop"),
    "主动": ("勾引", "挑逗", "索要", "seductive", "inviting", "provocative"),
    "支配": ("上位", "压制", "dominating", "on_top", "restraining"),
    "顺从": ("听话", "被支配", "obedient", "submissive", "serving"),
    "亲昵": ("拥抱", "抱着", "依偎", "hugging", "embracing", "cuddly"),
    "温柔": ("体贴", "珍惜", "gentle", "tender", "affectionate"),
    "紧张": ("不安", "害怕", "初次", "nervous", "scared", "first_time"),
    "得意": ("坏笑", "调戏", "smug", "teasing", "grinning"),
    "哭": ("流泪", "眼红", "crying", "tears", "teary"),
    "坦诚": ("直视", "看着你", "eye_contact", "looking_at_viewer", "smiling"),
}

# 轴 2：画面氛围（可选，第二标签位）
MOODS: tuple[str, ...] = (
    "柔光", "暖色", "冷色", "昏暗", "逆光", "烛光", "夕阳", "月夜", "雨幕",
    "特写", "全身", "俯视", "仰视", "背影", "侧脸", "朦胧", "细腻",
)

# 保留字：与默认图库共享的安全标签（random_safe_sticker 会排除含「色色」的图）
SAFE_EXCLUDED_TAG = "色色"
# 每张 CG 在 metadata 里写入的通用标签——CG 图库整体属于色色语境
CG_BASE_TAGS: tuple[str, ...] = ("色色",)

# 规范名索引（含别名），供 O(1) 归一化
_CANONICAL_INDEX: dict[str, str] = {}
for _name, _aliases in SCENES.items():
    _CANONICAL_INDEX[_name] = _name
    for _alias in _aliases:
        _CANONICAL_INDEX.setdefault(_alias.lower(), _name)

# 规范名集合（检索/校验用）
CANONICAL_SCENES: frozenset[str] = frozenset(SCENES)
# 标签总表：场景 + 氛围（标注工具、提示词摘要共用同一份）
ALL_TAGS: frozenset[str] = CANONICAL_SCENES | frozenset(MOODS)


def canonical(word: str) -> str:
    """把任意写法的标签（规范名/别名/英文/#标签）归一化成规范名。

    不认识的词原样返回（strip 后）——CG 标签是开放的，词表只负责收敛常见别名，
    不负责拒绝新词（拒绝会让主人手工加的新标签失效，违反单脑原则）。
    """
    text = str(word or "").strip().lstrip("#").replace(" ", "_")
    if not text:
        return ""
    return _CANONICAL_INDEX.get(text) or _CANONICAL_INDEX.get(text.lower()) or text


def canonical_all(words) -> list[str]:
    """批量归一化 + 去重，保持输入顺序（第一个位置最重要，写进文件名首位）。"""
    out: list[str] = []
    for word in words or ():
        name = canonical(word)
        if name and name not in out:
            out.append(name)
    return out


def match_tags(text: str) -> list[str]:
    """从一段自由文本（如源站标签串/图片描述）里捞出命中的规范标签。

    用于抓取工具：源站返回的 `tags` 列表丢进来，得到这张图该打什么标签；
    完全按别名表收敛，捞不到就返回空列表（调用方决定是否丢弃该图）。
    """
    low = str(text or "").lower()
    if not low:
        return []
    hits: list[str] = []
    for key, name in _CANONICAL_INDEX.items():
        if not key:
            continue
        # 英文标签按词边界匹配，避免 "sex" 命中 "sexy"/"bisexual" 之类误报
        if key.isascii():
            padded = f" {low.replace(',', ' ').replace(';', ' ')} "
            if f" {key} " in padded and name not in hits:
                hits.append(name)
        elif key in low and name not in hits:
            hits.append(name)
    return hits


def prompt_summary(limit: int = 18) -> str:
    """CG 图库可用的场景标签摘要——注入提示词的那一行。

    与默认图库 `StickerManager.sticker_keyword_summary()` 同构（同样是
    「可用标签 + 张数 + [贴图:关键词] 用法」），只是标签来自本词表。
    """
    names = list(SCENES)
    if not names:
        return ""
    shown = names[:limit]
    return "可用的CG场景标签：" + " ".join(f"[贴图:{name}]" for name in shown)
