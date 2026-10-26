"""CG 图库的标签体系——与默认表情包方案同构（2026-10-16）。

任务要求（主人原话）：「让这些色图与别的表情包一样支持显示标签，例如「后入」这类
表情包可以直接拿到；如无特别说明，标签体系与展示、获取方式沿用现有表情包方案」。

所以这里钉的是**同构性**，不是新发明的行为：
  1. 场景标签进同一个索引（`_emotion_index`）与同一套检索 API
  2. `[贴图:后入]` 走得通（与默认图库同一条解析路径）
  3. `send_cg_sticker(tag=...)` 按标签选图，且**冻结不漂移**（图库增删不变指向）
  4. 提示词里能看到 CG 图库的标签清单（与默认图库摘要同格式）
"""

import asyncio
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent.cg_scenes import SCENES, canonical, canonical_all, match_tags, prompt_summary
from agent.handler import MessageHandler
from agent.reply_pipeline import ReplyPipeline
from agent.sticker import StickerManager


# ═══════════════════════════════════════════════
# 词表
# ═══════════════════════════════════════════════

class TestSceneVocabulary:
    def test_backdoor_is_a_canonical_scene(self):
        """主人在任务里点名的「后入」必须是规范场景名。"""
        assert "后入" in SCENES
        assert canonical("后入") == "后入"

    def test_aliases_collapse_to_canonical(self):
        assert canonical("背后位") == "后入"
        assert canonical("doggystyle") == "后入"
        assert canonical("from_behind") == "后入"
        assert canonical("女上位") == "骑乘"
        assert canonical("sleeping") == "睡奸"
        assert canonical_all(["背后位", "后入", "女上位"]) == ["后入", "骑乘"]

    def test_unknown_words_survive(self):
        """标签轴是开放的——主人手写的新词不能被词表吃掉。"""
        assert canonical("主人昨天教的新姿势") == "主人昨天教的新姿势"

    def test_match_tags_reads_booru_tag_strings(self):
        """源站（booru）标签串要能直接转成我们的规范标签。"""
        hits = match_tags("1girl doggystyle from_behind blush bed")
        assert "后入" in hits
        assert "害羞" in hits
        assert "被窝" in hits

    def test_match_tags_uses_word_boundaries_for_ascii(self):
        """`sex` 不能命中 `sexy`/`bisexual`——否则整库标签都会串味。"""
        assert "正常位" not in match_tags("sexy bisexual")
        assert "正常位" in match_tags("1girl sex missionary")

    def test_prompt_summary_is_non_empty_and_uses_placeholder_format(self):
        text = prompt_summary(limit=6)
        assert "[贴图:" in text
        assert "后入" in text


# ═══════════════════════════════════════════════
# 与默认图库同构的检索面
# ═══════════════════════════════════════════════

def _make_cg_dir(tmp_path: Path, entries: dict) -> Path:
    d = tmp_path / "stickers_cg"
    d.mkdir(parents=True, exist_ok=True)
    for name in entries:
        (d / name).write_bytes(b"\xff\xd8\xff\xe0" + name.encode())  # 假 JPEG 头
    (d / "metadata.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return d


class TestCgTagRetrievalIsSameAsStickers:
    def test_scene_tags_enter_the_shared_index(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {
                "emotions": ["害羞", "色色"], "scene_tags": ["后入"],
            },
            "骑乘_娇喘_色色_bbbbbbbb.jpg": {
                "emotions": ["娇喘", "色色"], "scene_tags": ["骑乘"],
            },
        })
        m = StickerManager(str(d))

        assert m.count_by_emotion("后入") == 1
        assert m.list_by_emotion("后入") == ["后入_害羞_色色_aaaaaaaa.jpg"]
        assert "后入" in [p for p in m._emotion_index]

    def test_get_by_emotion_returns_cq_for_scene_tag(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        m = StickerManager(str(d))

        cq = m.get_by_emotion("后入")
        assert cq and "后入_害羞_色色_aaaaaaaa.jpg" in cq.replace("\\", "/")
        assert cq.startswith("[CQ:image,file=file:///")

    def test_match_by_emotion_text_hits_scene_tag_without_vectors(self, tmp_path):
        """无向量（冷缓存）时靠标签精确命中——和默认图库同一条降级路径。"""
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        m = StickerManager(str(d))

        hits = m.match_by_emotion_text("后入", embed_engine=None, count=1)
        assert len(hits) == 1
        assert "aaaaaaaa" in hits[0]

    def test_scene_axis_in_metadata_is_searchable(self, tmp_path):
        """带 scene_tags 的 metadata 条目必须能被标签检索命中（标签体系的主路径）。

        构造说明（变异对照实测后的诚实标注）：这条走的是 `_emotion_index` 索引路径
        ——**把 `_semantic_entries` 里的 scene_text 置空，本测试仍然绿**，因为索引
        已经命中，根本不会走到语义分支。语义文本只在 `embed_engine.ready` 时生效
        （自然语言「从后面来」那类查询），而单测里加载 BGE 不划算，所以那条支线
        由 `--allow-green` 显式接受，并在此写明原因，避免后人误以为它被覆盖了。
        索引路径的变异（场景轴不进 `_rebuild_index`）另有一条真红的判据。
        """
        d = _make_cg_dir(tmp_path, {
            "cg_aaaaaaaa.jpg": {"emotions": ["害羞", "色色"], "scene_tags": ["后入"]},
        })
        m = StickerManager(str(d))

        assert "后入" not in "cg_aaaaaaaa.jpg"          # 前提：名字里没有标签
        hits = m.match_by_emotion_text("后入", embed_engine=None, count=1)
        assert len(hits) == 1 and "cg_aaaaaaaa" in hits[0]

    def test_alias_reaches_scene_tag_without_vectors(self, tmp_path):
        """近义词也要取到图：LLM 说「背后位」时不该落空（别名表必须真参与匹配）。

        对照：同一份图库里查一个既非标签也非别名的词必须**取不到**——
        否则这条断言可能被别处的兜底蒙对（变异对照逼出来的写法）。
        """
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        m = StickerManager(str(d))

        hits = m.match_by_emotion_text("背后位", embed_engine=None, count=1)
        assert len(hits) == 1 and "aaaaaaaa" in hits[0]
        assert m.match_by_emotion_text("完全不存在的词", embed_engine=None, count=1) == []

    def test_filename_fallback_works_without_metadata(self, tmp_path):
        """命名即标签：metadata.json 丢了也能按「后入」取到图。"""
        d = tmp_path / "stickers_cg"
        d.mkdir()
        (d / "后入_娇喘_色色_cccccccc.jpg").write_bytes(b"\xff\xd8\xff\xe0x")
        m = StickerManager(str(d))

        assert m.metadata == {}
        hits = m.match_by_emotion_text("后入", embed_engine=None, count=1)
        assert len(hits) == 1 and "cccccccc" in hits[0]

    def test_sticker_tag_summary_lists_cg_vocabulary(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
            "骑乘_娇喘_色色_bbbbbbbb.jpg": {"emotions": ["娇喘"], "scene_tags": ["骑乘"]},
        })
        m = StickerManager(str(d))

        text = m.sticker_tag_summary(list(SCENES), limit=10)
        assert "[贴图:后入]" in text and "[贴图:骑乘]" in text
        assert "（1张）" in text
        # 默认图库的词表不该混进 CG 摘要
        assert "[贴图:撒娇]" not in text

    def test_sticker_tag_summary_empty_for_empty_library(self, tmp_path):
        d = tmp_path / "stickers_cg"
        d.mkdir()
        m = StickerManager(str(d))
        assert m.sticker_tag_summary(list(SCENES)) == ""

    def test_default_library_summary_unaffected(self, tmp_path):
        """反例闸门：默认图库的摘要格式不能因为 CG 的改动而变。"""
        d = tmp_path / "stickers"
        d.mkdir()
        (d / "开心_晚安_aaaa.jpg").write_bytes(b"\xff\xd8\xff\xe0x")
        (d / "metadata.json").write_text(json.dumps({
            "开心_晚安_aaaa.jpg": {"emotions": ["开心"]},
        }, ensure_ascii=False), encoding="utf-8")
        m = StickerManager(str(d))

        assert m.sticker_keyword_summary() == "可用的表情包：[贴图:开心]（1张）"


# ═══════════════════════════════════════════════
# 取图链路：工具 → turn_actions → 发送
# ═══════════════════════════════════════════════

TURN_ACTIONS = {"respond": True, "voice": False, "sing": None, "stickers": [], "cg": False}


def _run(coro):
    return asyncio.run(coro)


class TestSendCgStickerTool:
    def _handler(self, cg):
        fake = types.SimpleNamespace(
            cg_stickers=cg, voice_enabled=True, memory=None, embed_engine=None,
            _sed_active={},
        )
        return fake

    def test_tag_is_recorded_and_matched(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        actions = dict(TURN_ACTIONS)
        result = _run(MessageHandler._execute_tool(
            self._handler(mgr), "send_cg_sticker", {"tag": "后入"}, "", "", actions))

        assert actions["cg"] is True
        assert actions["cg_tag"] == "后入"
        assert "后入" in result

    def test_alias_in_tool_arg_matches_canonical_entry(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        actions = dict(TURN_ACTIONS)
        # LLM 可能说「背后位」——走同一条 match_by_emotion_text（别名表兜住）
        _run(MessageHandler._execute_tool(
            self._handler(mgr), "send_cg_sticker", {"tag": "背后位"}, "", "", actions))
        assert actions["cg_tag"] == "背后位"

    def test_missing_tag_falls_back_and_says_so(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        actions = dict(TURN_ACTIONS)
        result = _run(MessageHandler._execute_tool(
            self._handler(mgr), "send_cg_sticker", {"tag": "没有这个标签"},
            "", "", actions))

        assert actions["cg"] is True          # 仍然发图（回落随机），不是静默失败
        assert "随机" in result

    def test_empty_args_keeps_legacy_behaviour(self):
        """旧调用（无参数）必须一字不变——回归闸门。"""
        cg = types.SimpleNamespace(has_stickers=lambda: True, random_sticker=lambda: None)
        actions = dict(TURN_ACTIONS)
        result = _run(MessageHandler._execute_tool(
            self._handler(cg), "send_cg_sticker", {}, "", "", actions))

        assert actions["cg"] is True
        assert "cg_tag" not in actions
        assert result == "好的"


class TestCgSelectionByTag:
    # 4 张库里只有 1 张带「后入」：source="src-1" 的全库确定性下标落在**口交**那张上，
    # 所以「按标签选」与「全库选」结果必然不同——`tagged = []` 之类的退化变异
    # 会立刻让下面两条断言变红（这是变异对照逼出来的构造，不是随手写的库）。
    _LIB = {
        "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞", "色色"], "scene_tags": ["后入"]},
        "骑乘_娇喘_色色_bbbbbbbb.jpg": {"emotions": ["娇喘", "色色"], "scene_tags": ["骑乘"]},
        "口交_沉醉_色色_cccccccc.jpg": {"emotions": ["沉醉", "色色"], "scene_tags": ["口交"]},
        "睡奸_紧张_色色_dddddddd.jpg": {"emotions": ["紧张", "色色"], "scene_tags": ["睡奸"]},
    }

    def _handler(self, mgr, store=None):
        handler = object.__new__(MessageHandler)
        handler.cg_stickers = mgr
        handler.memory = types.SimpleNamespace(store=store) if store else None
        handler._send_sticker_actions = AsyncMock(return_value={"attempted": 1})
        return handler

    def test_pick_stays_inside_the_tagged_subset(self, tmp_path):
        """按标签选图必须只在命中该标签的子集里选——退化成全库就是没接上。"""
        mgr = StickerManager(str(_make_cg_dir(tmp_path, self._LIB)))
        handler = self._handler(mgr)

        _run(handler._send_cg_actions("u1", True, action_source_id="src-1", tag="后入"))

        asset = handler._send_sticker_actions.await_args.args[1][0]
        assert "aaaaaaaa" in asset, f"没按标签选：{asset}"
        assert "bbbbbbbb" not in asset, "落到全库选图了（标签过滤没生效）"

    def test_untagged_pick_would_have_chosen_another_image(self, tmp_path):
        """反例闸门：同一 source 不带标签时确实选到别的图——证明上面那条有效力。"""
        mgr = StickerManager(str(_make_cg_dir(tmp_path, self._LIB)))
        handler = self._handler(mgr)

        _run(handler._send_cg_actions("u1", True, action_source_id="src-1"))

        asset = handler._send_sticker_actions.await_args.args[1][0]
        assert "aaaaaaaa" not in asset, "全库选图竟也落到同一张，上一条测试就无法证伪变异了"

    def test_same_source_and_tag_never_drift(self, tmp_path):
        """冻结语义：同一 (source, tag) 重复调用必须落到同一张图。"""
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
            "后入_娇喘_色色_bbbbbbbb.jpg": {"emotions": ["娇喘"], "scene_tags": ["后入"]},
            "后入_沉醉_色色_cccccccc.jpg": {"emotions": ["沉醉"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))

        picked = set()
        for _ in range(6):
            handler = self._handler(mgr)
            _run(handler._send_cg_actions("u1", True, action_source_id="src-1", tag="后入"))
            picked.add(handler._send_sticker_actions.await_args.args[1][0])
        assert len(picked) == 1, f"同一 source+tag 漂移了：{picked}"

    def test_different_tags_are_independent(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
            "骑乘_娇喘_色色_bbbbbbbb.jpg": {"emotions": ["娇喘"], "scene_tags": ["骑乘"]},
        })
        mgr = StickerManager(str(d))

        h1 = self._handler(mgr)
        _run(h1._send_cg_actions("u1", True, action_source_id="src-1", tag="后入"))
        h2 = self._handler(mgr)
        _run(h2._send_cg_actions("u1", True, action_source_id="src-1", tag="骑乘"))

        assert "aaaaaaaa" in h1._send_sticker_actions.await_args.args[1][0]
        assert "bbbbbbbb" in h2._send_sticker_actions.await_args.args[1][0]

    def test_no_tag_keeps_full_library_pick(self, tmp_path):
        """不带标签 = 全库选（旧行为），不是「选不出来」。"""
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        handler = self._handler(mgr)

        _run(handler._send_cg_actions("u1", True, action_source_id="src-x"))

        asset = handler._send_sticker_actions.await_args.args[1][0]
        assert "aaaaaaaa" in asset
        intents = handler._send_sticker_actions.await_args.kwargs["sticker_intents"]
        assert intents[0]["library_id"] == "stickers_cg"
        assert intents[0]["emotion"] == "CG"

    def test_tag_is_reported_in_intent(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        handler = self._handler(mgr)

        _run(handler._send_cg_actions("u1", True, tag="后入"))
        intents = handler._send_sticker_actions.await_args.kwargs["sticker_intents"]
        assert intents[0]["emotion"] == "后入"

    def test_unknown_tag_falls_back_to_full_library(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        handler = self._handler(mgr)

        stats = _run(handler._send_cg_actions("u1", True, tag="不存在的标签"))

        assert handler._send_sticker_actions.await_count == 1
        assert stats == {"attempted": 1}

    def test_frozen_mapping_is_replayed_by_tag_key(self, tmp_path):
        """带标签的选中项同样冻结进 kv；第二次调用读映射，不再重新抽。"""
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        kv: dict = {}
        store = types.SimpleNamespace(kv_get=kv.get,
                                      kv_set=lambda k, v: kv.__setitem__(k, v))

        h1 = self._handler(mgr, store=store)
        _run(h1._send_cg_actions("u1", True, action_source_id="src-1", tag="后入"))
        assert len(kv) == 1

        # 图库里再加一张同标签图：旧 mapping 必须继续指向原来那张（不漂移）
        (d / "后入_沉醉_色色_dddddddd.jpg").write_bytes(b"\xff\xd8\xff\xe0dd")
        mgr.reload()
        h2 = self._handler(mgr, store=store)
        _run(h2._send_cg_actions("u1", True, action_source_id="src-1", tag="后入"))

        asset = h2._send_sticker_actions.await_args.args[1][0]
        assert "aaaaaaaa" in asset


# ═══════════════════════════════════════════════
# [贴图:标签] 同一条解析路径
# ═══════════════════════════════════════════════

class TestStickerTagProtocol:
    def test_scene_tag_resolves_through_the_shared_pipeline(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        mgr = StickerManager(str(d))
        pipe = ReplyPipeline.__new__(ReplyPipeline)
        pipe.stickers = mgr
        pipe.embed_engine = None

        out = pipe.resolve_sticker_tags("那就这样吧[贴图:后入]")

        assert "[CQ:image" in out
        assert "aaaaaaaa" in out.replace("\\", "/")
        assert "[贴图:" not in out


# ═══════════════════════════════════════════════
# 提示词投影
# ═══════════════════════════════════════════════

class TestSendSitesCarryTheTag:
    """标签必须穿过**两个**发送调用点（群/私聊），不只是工具写入端。

    源码级闸门，理由：调用点深埋在 `handler.py` 的巨型方法里，单测打进去的成本
    远高于它要防的风险（漏一个调用点 = 那个场景下标签静默失效，而测试全绿）。
    变异对照实测：只改调用点、不改工具端时，行为级测试全部 GREEN——
    所以这条断言必须存在（教训 #37 的「假绿比假红危险」）。
    """

    @staticmethod
    def _source() -> str:
        return (Path(__file__).resolve().parent.parent / "agent" / "handler.py"
                ).read_text(encoding="utf-8")

    def test_both_send_sites_pass_cg_tag(self):
        source = self._source()
        assert source.count('tag=turn_actions.get("cg_tag", "")') == 2, \
            "两个 _send_cg_actions 调用点都必须把 cg_tag 传下去（群聊一处、私聊一处）"

    def test_tool_writes_the_tag_it_was_given(self):
        assert 'turn_actions["cg_tag"] = tag' in self._source()


class TestCgSummaryInjection:
    def _handler(self, mgr):
        handler = object.__new__(MessageHandler)
        handler._sed_active = {"user-a": __import__("time").time() + 3600}
        handler.cg_stickers = mgr
        handler.voice_enabled = False
        handler.memory = None
        handler._call_llm = AsyncMock(return_value="好的")
        return handler

    def _prompt(self, handler):
        with patch("agent.skills.list_skills", return_value=[]):
            asyncio.run(handler._call_llm_with_skills(
                "system", "user", current_user="user-a"))
        return handler._call_llm.await_args.args[0]

    def test_summary_lists_tags_for_active_user(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        prompt = self._prompt(self._handler(StickerManager(str(d))))

        assert "[贴图:后入]" in prompt
        assert "send_cg_sticker" in prompt

    def test_empty_library_advertises_nothing(self, tmp_path):
        """图库为空时不许报标签——工具此时根本没注册，说「可以发CG」是骗她。

        这条是反例闸门：CG 标签清单只在**真能取到图**时才投影。
        """
        d = tmp_path / "stickers_cg"
        d.mkdir()
        prompt = self._prompt(self._handler(StickerManager(str(d))))

        assert "[贴图:后入]" not in prompt
        assert "可用的CG标签" not in prompt

    def test_inactive_user_gets_no_cg_tags(self, tmp_path):
        d = _make_cg_dir(tmp_path, {
            "后入_害羞_色色_aaaaaaaa.jpg": {"emotions": ["害羞"], "scene_tags": ["后入"]},
        })
        handler = self._handler(StickerManager(str(d)))

        with patch("agent.skills.list_skills", return_value=[]):
            asyncio.run(handler._call_llm_with_skills(
                "system", "user", current_user="user-b"))
        prompt = handler._call_llm.await_args.args[0]

        assert "[贴图:后入]" not in prompt


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
