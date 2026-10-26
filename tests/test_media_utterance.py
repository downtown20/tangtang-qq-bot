"""T59：媒体消息的「发言身份」 + 表情包只给事实（不预判语义）。

生产证据（2026-10-01→10-16 私聊）：
  - 用户图片/表情包消息 69 条，**69/69** 都在 180s 内换到了一条回复；
  - 「文字已回复 → 120s 内一张图」**18 次，18/18 图又换来第二次回复**；
  - 图在历史里是 `（发了张图片，内容是：震惊呆滞，无言以对）`——**系统给的解释**，
    她因此分不清「表情包表达的情绪」和「照片里的内容」，把表情包当成对方说的话回应。

修复方向：系统只判**结构**（这条消息是不是上一次发言的附件）与**平台事实**
（表情包 vs 图片），语义归 LLM；她要看图有 `analyze_image`（自主调用）。
"""
import inspect
import time
from pathlib import Path

import pytest

from agent import protocols as P
from agent.handler import MessageHandler

CQ_STICKER = "[CQ:image,file=BC157265121EE4FE2ED49C0F947E84A5.jpg,url=https://x/a.jpg]"
CQ_FACE = "[CQ:image,sub_type=1,file=face.jpg,url=https://x/f.jpg]"
CQ_PHOTO = "[CQ:image,file=55B3054B.jpg,url=https://x/p.jpg]"


def _h():
    """无 __init__ 的最小实例：只挂 T59 需要的锚点表"""
    h = object.__new__(MessageHandler)
    h._last_real_utterance = {}
    return h


def _private_src() -> str:
    src = Path(inspect.getsourcefile(MessageHandler)).read_text(encoding="utf-8")
    i = src.index("    async def handle_private_message(self, msg: dict):")
    j = src.index("    async def ", i + 10)
    return src[i:j]


# ═══════════════════════════════════════════════════════
# 1. 纯媒体判定（结构判定）
# ═══════════════════════════════════════════════════════
@pytest.mark.parametrize("text,raw,expect", [
    ("[图片:[动画表情]]", CQ_STICKER, True),          # 动画表情（实测 48606 行）
    ("[图片:[图片]]", CQ_PHOTO, True),                # 纯图消息
    ("（发了张图片）", CQ_PHOTO, True),               # 中性化之后形态
    ("[表情]", CQ_FACE, True),                        # sub_type=1 内置表情
    ("今天好累", "", False),                          # 纯文本
    ("", "", False),                                  # 空
    ("看这个（发了张图片）", CQ_PHOTO, False),         # 有实质文本 → 是发言，不是附件
    ("有图有真相（发了张图片）", CQ_PHOTO, False),     # 群聊真实样本
])
def test_is_pure_media_message(text, raw, expect):
    assert P.is_pure_media_message(text, raw) is expect


# ═══════════════════════════════════════════════════════
# 2. 表情包 vs 图片（平台事实，不是语义猜测）
# ═══════════════════════════════════════════════════════
@pytest.mark.parametrize("text,raw,expect", [
    ("[图片:[动画表情]]", CQ_STICKER, True),
    ("[表情]", CQ_FACE, True),
    ("[图片]", CQ_FACE, True),                # raw 里的 sub_type=1 也算数
    ("[图片:测试表情]", CQ_PHOTO, True),       # summary 带「表情」
    ("（发了张图片）", CQ_PHOTO, False),       # 真照片：仍要正经识别
    ("[图片:猫猫]", CQ_PHOTO, False),
])
def test_looks_like_sticker(text, raw, expect):
    assert P.looks_like_sticker(text, raw) is expect


# ═══════════════════════════════════════════════════════
# 3. 发言锚点：文字 → 表情包 = 一次发言
# ═══════════════════════════════════════════════════════
def test_text_then_sticker_is_attached():
    """主人报的缺陷：文字之后再发一张表情包，不得再开一次回合"""
    h, scope = _h(), "_private_1001"
    assert h._attach_as_media_followup(scope, "今天好累", "") is False   # 实质发言：正常回
    assert h._attach_as_media_followup(scope, "[图片:[动画表情]]", CQ_STICKER) is True


def test_legacy_stub_without_anchor_table_is_safe():
    """旧替身（object.__new__、无 __init__）不得 AttributeError——
    这是全量测试实测抓到的真缺陷，不是理论顾虑。"""
    h = object.__new__(MessageHandler)      # 刻意不挂 _last_real_utterance
    assert h._attach_as_media_followup("group:g1:u1", "普通消息", "普通消息") is False
    assert h._last_real_utterance  # 惰性建好了


def test_lone_sticker_is_its_own_turn():
    """反例/负向对照：孤零零一张表情包仍然是一次发言，不能被她静默吞掉"""
    h = _h()
    assert h._attach_as_media_followup("_private_1002", "[图片:[动画表情]]", CQ_STICKER) is False


def test_window_expiry():
    h, scope = _h(), "_private_1003"
    h._attach_as_media_followup(scope, "在吗", "")
    h._last_real_utterance[scope] = time.time() - (P.MEDIA_ATTACH_WINDOW_S + 5)
    assert h._attach_as_media_followup(scope, "[表情]", CQ_FACE) is False


def test_media_does_not_move_the_anchor():
    """表情包不移动锚点——否则「每隔 100s 发一张表情包」会永久豁免回复"""
    h, scope = _h(), "_private_1004"
    h._attach_as_media_followup(scope, "喂", "")
    anchor = h._last_real_utterance[scope]
    assert h._attach_as_media_followup(scope, "[表情]", CQ_FACE) is True
    assert h._last_real_utterance[scope] == anchor
    # 锚点老化到窗口之外 → 这张表情包自己就是一次发言
    h._last_real_utterance[scope] = time.time() - (P.MEDIA_ATTACH_WINDOW_S + 1)
    assert h._attach_as_media_followup(scope, "[表情]", CQ_FACE) is False


def test_text_messages_are_never_attached():
    """契约：窗口内连续文字消息仍然逐条进 LLM（群聊窗口回复契约不可被本改动破坏）"""
    h, scope = _h(), "_private_1005"
    for _ in range(5):
        assert h._attach_as_media_followup(scope, "喂喂喂", "") is False


# ═══════════════════════════════════════════════════════
# 4. 源码级契约（路径接线，防「改了但没接上」/防旧行为复活）
# ═══════════════════════════════════════════════════════
def test_private_path_attaches_media_after_persist_before_llm():
    src = _private_src()
    assert "_attach_as_media_followup" in src, "私聊路径没有接上附件化判定"
    assert src.index("add_to_buffer") < src.index("_attach_as_media_followup"), \
        "事实必须先落库/进缓冲，再决定不开回合"


def test_private_sticker_is_not_prejudged():
    src = _private_src()
    assert "looks_like_sticker" in src
    assert "对方发了一个表情包" in src
    assert "这张图表达什么情绪或意思" not in src, "旧的情绪预判提示词复活了"
    assert "不要推测情绪" in src, "真图识图提示词必须保持中性（不得回退成预判情绪）"


def test_sticker_keeps_the_autonomous_look_path():
    """不预判 ≠ 不给能力：图仍被捕获 → analyze_image 对她可见（自主调用）"""
    src = _private_src()
    assert "_capture_recent_image" in src

def _group_src() -> str:
    src = Path(inspect.getsourcefile(MessageHandler)).read_text(encoding="utf-8")
    i = src.index("    async def handle_group_message(self, msg: dict):")
    j = src.index("    async def ", i + 10)
    return src[i:j]


def test_attach_happens_before_the_llm_call_private():
    """接线契约：附件化判定必须在**主 LLM 调用之前**返回，否则等于没修"""
    src = _private_src()
    assert src.index("_attach_as_media_followup") < src.index("_call_llm_with_skills"), \
        "私聊路径的附件化判定落在了 LLM 调用之后——那张表情包照样会被回一次"


def test_attach_happens_before_the_llm_call_group():
    src = _group_src()
    assert src.index("_attach_as_media_followup") < src.index("_call_llm_with_skills")


def test_group_sticker_is_not_prejudged_either():
    """群聊两条识图路径（按需/自然）都要给事实，不能只改一条"""
    src = _group_src()
    assert src.count("looks_like_sticker") >= 2, "群聊两处识图路径没有都接上事实判定"
    assert "发了一个表情包" in src
    assert "用一句话描述这张图的内容和情绪" not in src, "旧的情绪预判提示词复活了"
    assert "这张图表达什么情绪或意思" not in src, "旧的情绪预判提示词复活了"
    assert "不要推测情绪" in src, "群聊真图识图提示词必须保持中性"

def test_real_clock_window(monkeypatch):
    """用真实时钟跑一遍窗口语义（不伪造锚点）：0.5s 内是附件，1.2s 后不是。"""
    monkeypatch.setattr(P, "MEDIA_ATTACH_WINDOW_S", 1.0)
    h, scope = _h(), "_private_1006"
    assert h._attach_as_media_followup(scope, "先说一句", "") is False
    time.sleep(0.2)
    assert h._attach_as_media_followup(scope, "[图片:[动画表情]]", CQ_STICKER) is True
    time.sleep(1.0)
    assert h._attach_as_media_followup(scope, "[图片:[动画表情]]", CQ_STICKER) is False