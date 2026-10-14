"""
测试 ReplyPipeline 回复处理管道
"""

import asyncio
import threading
import time

import pytest

from agent import protocols


class TestCleanReply:
    """测试 _clean_reply / clean() 清洗规则"""

    def test_removes_emotion_tags(self, reply_pipeline):
        """去掉开头的情绪标签：[温柔] [开心] 等"""
        assert reply_pipeline.clean("[开心] 你好呀") == "你好呀"
        assert reply_pipeline.clean("[温柔] 今天天气真好") == "今天天气真好"

    def test_keeps_bracket_actions(self, reply_pipeline):
        """括号内容保留——不再剥离，信任LLM自然表达（2026-07-25）"""
        result = reply_pipeline.clean("你好呀（耳朵抖了抖）")
        assert "你好呀" in result
        assert "耳朵抖了抖" in result
        assert "（" in result

    def test_keeps_normal_bracket_content(self, reply_pipeline):
        """所有括号内容都保留"""
        result = reply_pipeline.clean("你好呀（笑着挥手）")
        assert "你好呀" in result
        assert "（笑着挥手）" in result  # 普通动作保留
        result2 = reply_pipeline.clean("Hello (smiling)")
        assert "(smiling)" in result2  # 英文普通括号保留

    def test_removes_fake_cq_images(self, reply_pipeline):
        """去掉 LLM 瞎编的 CQ:image 码"""
        result = reply_pipeline.clean("[CQ:image,file=a1b2c3d4e5f6,url=xxx] 看这张图")
        assert "CQ:image" not in result
        assert "看这张图" in result

    def test_removes_non_at_cq_actions(self, reply_pipeline):
        """LLM 文本不能绕过工具层直接注入 OneBot 动作。"""
        for tag in (
            '[CQ:json,data={"app":"evil"}]',
            "[CQ:record,file=file:///tmp/forged.wav]",
            "[CQ:reply,id=123]",
        ):
            result = reply_pipeline.clean(f"{tag} 正常正文")
            assert "[CQ:" not in result
            assert "正常正文" in result

    def test_private_enrich_removes_unverifiable_at(self, reply_pipeline):
        result = reply_pipeline.enrich("[CQ:at,qq=123456] 私聊正文")
        assert "[CQ:at" not in result
        assert "私聊正文" in result

    def test_strips_markdown_formatting(self, reply_pipeline):
        """洗掉 Markdown 标记（需≥3字符，否则被当作空回复处理）"""
        assert "粗体字" in reply_pipeline.clean("**粗体字**")
        assert "斜体字" in reply_pipeline.clean("*斜体字*")
        assert "标题呀" in reply_pipeline.clean("### 标题呀")
        assert "删除线啦" in reply_pipeline.clean("~~删除线啦~~")
        assert "下划线呢" in reply_pipeline.clean("__下划线呢__")

    def test_preserves_normal_text(self, reply_pipeline):
        """保留正常文本不变"""
        original = "今天天气真好，一起去公园散步吧！"
        result = reply_pipeline.clean(original)
        assert result == original

    def test_handles_empty_reply(self, reply_pipeline):
        """空回复返回空字符串；短回复保留（2026-07-30 心理陪伴策略：不删短句）"""
        assert reply_pipeline.clean("") == ""
        assert reply_pipeline.clean("  ") == ""
        assert reply_pipeline.clean("a") == "a"  # 短回复是设计——心理陪伴要短句


def test_async_enrich_moves_group_at_store_lookup_off_event_loop(reply_pipeline, monkeypatch):
    """群聊 @解析的 Store 查询不能阻塞事件循环。"""
    main_thread = threading.get_ident()
    calls = []

    def slow_find(_nickname, _bot_qq):
        calls.append(threading.get_ident())
        time.sleep(0.03)
        return "123456"

    monkeypatch.setattr(reply_pipeline.store, "find_qq_for_at", slow_find)
    monkeypatch.setattr(
        reply_pipeline.store,
        "get_group_members",
        lambda _group_id: [{"qq_id": "123456"}],
    )

    async def scenario():
        ticks = 0
        stopped = False

        async def ticker():
            nonlocal ticks
            while not stopped:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            result = await reply_pipeline.enrich_async("@昵称 你好", group_id="g1")
        finally:
            stopped = True
            await task
        return result, ticks

    result, ticks = asyncio.run(scenario())
    assert "[CQ:at,qq=123456]" in result
    assert ticks > 0
    assert calls and calls[0] != main_thread


class TestMaybeSplitReply:
    """测试长回复分句"""

    def test_short_reply_not_split(self, reply_pipeline):
        """短回复不拆分"""
        parts = reply_pipeline.maybe_split_reply("你好呀")
        assert len(parts) == 1
        assert parts[0] == "你好呀"

    def test_cq_code_not_split(self, reply_pipeline):
        """CQ 码开头的回复不拆分"""
        parts = reply_pipeline.maybe_split_reply("[CQ:image,file=xxx]")
        assert len(parts) == 1

    def test_long_reply_can_split(self, reply_pipeline):
        """长回复在句号处拆分"""
        import random
        random.seed(0)  # 固定随机种子让 split 必然触发
        long_text = "这是第一句话。这是第二句话。这是第三句话也是很长的一句话用来测试分句逻辑。"
        parts = reply_pipeline.maybe_split_reply(long_text)
        # 随机种子固定后要么1段要么2段
        assert 1 <= len(parts) <= 2


class TestStickerResolution:
    def test_unmatched_tag_does_not_send_a_random_sticker(self, reply_pipeline, mock_stickers):
        result = reply_pipeline.resolve_sticker_tags("看看这个[贴图:完全不存在的情绪]")

        assert result == "看看这个"
        mock_stickers.random_safe_sticker.assert_not_called()

    def test_emotion_picker_uses_unified_matcher_without_random_fallback(
        self, reply_pipeline, mock_stickers
    ):
        mock_stickers.match_by_emotion_text.return_value = []

        assert reply_pipeline._pick_sticker_by_emotion("开心地笑了") is None
        mock_stickers.match_by_emotion_text.assert_called_once_with(
            "开心地笑了", embed_engine=reply_pipeline.embed_engine,
            count=1, excluded={"色色"}
        )
        mock_stickers.random_safe_sticker.assert_not_called()


class TestQuoteMarker:
    """引用判断归 LLM（2026-09-23）。

    这里原来测的是 `should_quote(reply, msg)`——系统启发式：`is_at_bot` → 无条件引用。
    它让群聊 **53.2%** 的回复都带引用前缀（实测 4836/9086）。真人不这样：被 @ 到时
    对方已经知道你在回谁，再引用是冗余——**引用是用来消歧和通知的，不是礼貌**。
    更要紧的是它替 LLM 做了决策（教训 #9/#24）。

    现在唯一决策源是 `[引用]` 标记；系统只剩「轮次」职责（用哪个 msg_id、挂第一段）。
    """

    def test_marker_asks_for_quote(self):
        assert protocols.has_quote_marker("[引用]好呀") is True
        assert protocols.has_quote_marker("先说一句[引用]再说") is True

    @pytest.mark.parametrize("text", [
        "你好",
        "这是一个非常长的回复" + "的" * 50,
        "嗯好",
        "正文里提到引用两个字但没方括号",
        "[分段]只有别的标记",
    ])
    def test_no_marker_means_no_quote(self, text):
        """**默认不引用**——这是这次改动最大的行为变化，也是它的目的。"""
        assert protocols.has_quote_marker(text) is False

    def test_clean_strips_the_marker(self, reply_pipeline):
        """标记绝不能进任何发送路径——clean() 是兜底防线。"""
        assert "引用" not in reply_pipeline.clean("[引用]好呀")
        assert "引用" not in reply_pipeline.clean("先说[引用]再说")

    def test_full_width_variant_is_stripped_but_not_parsed(self):
        """**解析窄、兜底宽**——同 [分段] 的非对称。
        解析认全角会在正常正文里误挂引用；兜底多剥两个方括号的代价小得多。"""
        assert protocols.has_quote_marker("前［引用］后") is False
        assert protocols.strip_quote_marker("前［引用］后") == "前后"
        assert protocols.strip_quote_marker("[ 引用 ]") == ""

    def test_clean_strips_the_silence_marker(self, reply_pipeline):
        """2026-10-12 M2（审计 P2）：[不说话] 此前没有 clean 兜底——
        非行首位置写出这四个字会原样发给用户。"""
        assert "不说话" not in reply_pipeline.clean("好呀[不说话]")
        assert "不说话" not in reply_pipeline.clean("前［不说话］后")


class TestSendReply:
    """测试发送回复"""

    @pytest.mark.asyncio
    async def test_send_group_message(self, reply_pipeline, mock_napcat):
        await reply_pipeline.send("group", "123", "你好群友")
        mock_napcat.send_group_message.assert_called_once_with("123", "你好群友")

    @pytest.mark.asyncio
    async def test_send_private_message(self, reply_pipeline, mock_napcat):
        await reply_pipeline.send("private", "456", "你好私聊")
        mock_napcat.send_private_message.assert_called_once_with("456", "你好私聊", group_id="")


class TestCQAtValidation:
    """2026-08-16：LLM 直写的 [CQ:at,qq=X] 校验——截断修复/无效移除/自己移除"""

    def _pipeline(self, store):
        from agent.reply_pipeline import ReplyPipeline
        from unittest.mock import AsyncMock, MagicMock
        return ReplyPipeline(
            napcat=AsyncMock(), stickers=MagicMock(), store=store,
            short_term={"999": [
                {"nickname": "一个包子", "qq_id": "12801301"},
                {"nickname": "管理员", "qq_id": "10004"},
            ]},
            bot_nicknames=["糖糖"], bot_qq="10000",
        )

    def test_truncated_qq_repaired_by_unique_suffix(self, store):
        """LLM 截断现场：1301 → 12801301（唯一后缀匹配才修）"""
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=1301]群主你这条消息发得比风还空", "999")
        assert "[CQ:at,qq=12801301]" in out
        assert "1301]群主" not in out.replace("12801301]", "]")  # 短号本身已修复

    def test_valid_qq_kept(self, store):
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=10004]管理员倒是积极", "999")
        assert "[CQ:at,qq=10004]" in out

    def test_invalid_qq_removed(self, store):
        """无效且无唯一后缀匹配 → 移除标签，正文保留"""
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=8888]你好呀", "999")
        assert "[CQ:at" not in out
        assert "你好呀" in out

    def test_bot_self_at_removed(self, store):
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=10000]我自己", "999")
        assert "[CQ:at" not in out

    def test_ambiguous_suffix_not_guessed(self, store):
        """两个成员 QQ 都以同一短号结尾 → 不猜，移除"""
        from agent.reply_pipeline import ReplyPipeline
        from unittest.mock import AsyncMock, MagicMock
        p = ReplyPipeline(
            napcat=AsyncMock(), stickers=MagicMock(), store=store,
            short_term={"999": [
                {"nickname": "甲", "qq_id": "11118013"},
                {"nickname": "乙", "qq_id": "22228013"},
            ]},
            bot_nicknames=["糖糖"], bot_qq="10000",
        )
        out = p.resolve_at_mentions("[CQ:at,qq=1301]谁", "999")
        assert "[CQ:at" not in out


class TestImagePlaceholder:
    """2026-08-16：LLM 逐字回显「[图片:[动画表情]]」占位符的现场修复"""

    def test_reply_clean_strips_placeholder(self, reply_pipeline):
        out = reply_pipeline.clean("收到收到喵！\n\n[图片:[动画表情]]\n\n管理员您这图可够帅的啊")
        assert "[图片" not in out
        assert "管理员您这图可够帅" in out

    def test_history_neutralizes_placeholder(self, store):
        from agent.memory import MemorySystem
        mem = MemorySystem(store=store)
        mem.short_term["888"] = [{
            "qq_id": "111", "nickname": "管理员", "seq": 1,
            "message": "[图片:[动画表情]]",
        }]
        msgs = mem.get_recent_context_messages("888", bot_qq="10000")
        assert "[图片" not in msgs[0]["content"]
        assert "发了张图片" in msgs[0]["content"]

    def test_placeholder_with_other_variants(self, reply_pipeline):
        for raw in ("[图片]", "[图片:图片]", "[图片:动画表情]"):
            assert "[图片" not in reply_pipeline.clean(f"前缀 {raw} 后缀")

    def test_normalize_helper(self):
        from agent import protocols
        assert protocols.normalize_image_placeholder("[图片:[动画表情]]") == "（发了张图片）"
        assert protocols.normalize_image_placeholder("[图片]") == "（发了张图片）"
        assert protocols.normalize_image_placeholder("你好呀") == "你好呀"

    def test_db_writeback_enriches_placeholder_row(self, store):
        """识图写回：DB 里最近一条占位符行升级为带内容描述"""
        store.get_or_create_person("222", "管理员")
        store.insert_chat("222", "（发了张图片）", group_id="888", is_bot=False)
        store.insert_chat("222", "之后又说了句话", group_id="888", is_bot=False)
        ok = store.enrich_latest_image_message("222", "888", "（发了张图片，内容是：持枪落地）")
        assert ok is True
        rows = store.get_user_recent_messages("222", limit=5)
        assert any("内容是：持枪落地" in r for r in rows)
        assert any("之后又说了句话" in r for r in rows)  # 别的消息不受影响

    def test_buffer_writeback_updates_entry(self, store):
        from agent.memory import MemorySystem
        mem = MemorySystem(store=store)
        mem.add_to_buffer("888", "222", "管理员", "（发了张图片）")
        mem.add_to_buffer("888", "333", "路人", "说句话")
        mem.enrich_image_message_in_buffer("888", "222", "（发了张图片，内容是：持枪落地）")
        entries = list(mem.short_term["888"])
        assert entries[0]["message"] == "（发了张图片，内容是：持枪落地）"
        assert entries[1]["message"] == "说句话"


class TestSeductiveMarkersNeverLeak:
    """2026-08-17：色色模式切换标记 [进入色色]/[退出色色] 是系统协议——
    clean() 兜底防线：任何发送路径都不许把标记漏给用户（状态机在 handler）。"""

    def test_markers_stripped_mid_reply(self, reply_pipeline):
        out = reply_pipeline.clean("好呀，抱抱喵~[退出色色]")
        assert "退出色色" not in out
        assert "抱抱喵" in out

    def test_enter_marker_stripped(self, reply_pipeline):
        out = reply_pipeline.clean("[进入色色]那我不客气了")
        assert "进入色色" not in out
        assert "不客气" in out


class TestCodexI3FailClosed:
    def _pipeline(self, store):
        from agent.reply_pipeline import ReplyPipeline
        from unittest.mock import AsyncMock, MagicMock
        return ReplyPipeline(
            napcat=AsyncMock(), stickers=MagicMock(), store=store,
            short_term={"999": [{"nickname": "一个包子", "qq_id": "12801301"}]},
            bot_nicknames=["糖糖"], bot_qq="10000",
        )

    def test_qq_all_variant_stripped(self, store):
        """I3 回归：qq=all 变体 fail-closed 移除"""
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=all]大家注意", "999")
        assert "[CQ:at" not in out
        assert "大家注意" in out

    def test_cross_group_qq_stripped(self, store):
        """I3 回归：不在当前群的 QQ（全局 people 存在也不行）→ 移除"""
        store.get_or_create_person("555555", "外群人士")
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=555555]你好", "999")
        assert "[CQ:at" not in out
        assert "你好" in out

    def test_group_member_kept(self, store):
        """当前群成员（group_members 表）→ 保留"""
        store.upsert_group_member("999", "777777", card="包子")
        p = self._pipeline(store)
        out = p.resolve_at_mentions("[CQ:at,qq=777777]你好", "999")
        assert "[CQ:at,qq=777777]" in out


class TestReceiptTemplateIsSingleBubbleOnly:
    """T2.4 x T32 交叉契约：回执模板只服务单气泡，多段时必须丢弃且不许伪造。

    为什么现在补：T32（2026-10-14）把空行变成段边界后，私聊多段比例从 9.2% 升到约 64%，
    「多段 -> 丢模板」这条路径的触发频率大幅上升，而它此前没有任何测试。
    而 ReplyPipeline.send() 里那条 warning 的原话是「禁止用单模板冒充一批动作」——
    正是最该被钉住、也最容易被后人「好心优化」成伪造模板的地方
    （教训 #21：每个有原则的例外都写进 docstring 并配闸门）。
    """

    def _pipeline(self, sent, store):
        from agent.reply_pipeline import ReplyPipeline
        from unittest.mock import MagicMock

        class _Napcat:
            async def send_group_message(self, target, text, receipt_template=None):
                sent.append(("group", text, receipt_template))
                return {"status": "ok"}

            async def send_private_message(self, target, text, group_id="",
                                          receipt_template=None):
                sent.append(("private", text, receipt_template))
                return {"status": "ok"}

        return ReplyPipeline(
            napcat=_Napcat(), stickers=MagicMock(), store=store,
            short_term={}, bot_nicknames=["糖糖"], bot_qq="10000",
        )

    @pytest.mark.asyncio
    async def test_multi_part_never_attaches_template(self, store):
        """多段批次：模板必须被丢弃，且一个段都不许挂。"""
        sent = []
        p = self._pipeline(sent, store)
        tpl = {"action_id": "a1", "schema_version": 2}
        await p.send("private", "456", "第一段\n第二段",
                     presplit_parts=["第一段", "第二段"],
                     receipt_template=tpl)
        assert len(sent) == 2, sent
        assert all(t is None for _, _, t in sent), (
            "多段批次挂了模板——禁止用一个模板冒充一批动作: %r" % (sent,)
        )

    @pytest.mark.asyncio
    async def test_single_part_attaches_template(self, store):
        """单气泡：模板照挂（多段丢弃不能演变成永远不挂）。"""
        sent = []
        p = self._pipeline(sent, store)
        tpl = {"action_id": "a1", "schema_version": 2}
        await p.send("private", "456", "好，设好了", receipt_template=tpl)
        assert len(sent) == 1, sent
        assert sent[0][2] == tpl, ("单气泡也没挂模板——归账链断了: %r" % (sent,))
