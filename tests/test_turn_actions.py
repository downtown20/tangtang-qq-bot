"""
H1 回合隔离契约测试（2026-08-10）

工具循环的动作意图（语音/唱歌/贴图/CG）必须写入 turn_actions 局部容器，
不得再写 Handler 实例字段——否则群聊/私聊并发时 A 回合的后处理
会读到 B 回合的工具意图（语音/歌曲/贴图发错对象）。
"""

import asyncio
import hashlib
import inspect
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.handler import MessageHandler

TURN_ACTIONS = {"respond": True, "voice": False, "sing": None, "stickers": [], "cg": False}


def _make_fake(**overrides):
    fake = types.SimpleNamespace(voice_enabled=True, **overrides)
    return fake


def _run(coro):
    return asyncio.run(coro)


class TestTurnActionsContract:
    def test_skip_response_writes_structured_decision(self):
        """LLM 用结构化动作决定沉默，不靠特殊文本标记。"""
        fake = _make_fake()
        actions = dict(TURN_ACTIONS, respond=True)
        result = _run(MessageHandler._execute_tool(
            fake, "skip_response", {"reason": "这句是在群友之间接话"}, "", "", actions))
        assert actions["respond"] is False
        assert actions["response_reason"] == "这句是在群友之间接话"
        assert "不回复" in result

    def test_send_voice_writes_turn_actions(self):
        """send_voice 写入 turn_actions['voice']，不写实例字段"""
        fake = _make_fake()
        actions = dict(TURN_ACTIONS)
        _run(MessageHandler._execute_tool(
            fake, "send_voice", {"text": "哥～哥～"}, "", "", actions
        ))
        assert actions["voice"] is True
        assert actions["voice_text"] == "哥～哥～"
        assert not hasattr(fake, "_pending_voice")

    def test_send_voice_without_turn_actions_no_crash(self):
        """turn_actions=None 时行为不变（安全回退）——不写任何实例字段"""
        fake = _make_fake()
        result = _run(MessageHandler._execute_tool(fake, "send_voice", {}, "", ""))
        assert "语音" in result
        assert not hasattr(fake, "_pending_voice")

    def test_send_cg_writes_turn_actions(self):
        """send_cg_sticker 写入 turn_actions['cg']"""
        cg = types.SimpleNamespace(has_stickers=lambda: True, random_sticker=lambda: None)
        fake = _make_fake(cg_stickers=cg)
        actions = dict(TURN_ACTIONS)
        _run(MessageHandler._execute_tool(fake, "send_cg_sticker", {}, "", "", actions))
        assert actions["cg"] is True

    def test_sing_writes_turn_actions(self):
        """sing 写入 turn_actions['sing']（歌曲对象）"""
        songs = types.SimpleNamespace(search=lambda name: {"title": name, "id": 1},
                                      list_songs=lambda: [])
        fake = _make_fake(songs=songs)
        actions = dict(TURN_ACTIONS)
        result = _run(MessageHandler._execute_tool(fake, "sing", {"song": "小星星"}, "", "", actions))
        assert actions["sing"] is not None
        assert actions["sing"]["title"] == "小星星"
        assert not hasattr(fake, "_pending_sing")

    def test_media_tool_actions_append_shared_ordinals_and_keep_compatibility_views(self):
        """多次 tool call 必须保留全局顺序，旧 stickers/sing 视图仍可读取。"""
        stickers = types.SimpleNamespace(
            match_by_emotion_text=lambda emotion, embed_engine=None, count=1: [
                f"{emotion}-{index}" for index in range(count)
            ],
            sticker_dir="stickers-v1",
        )
        songs = types.SimpleNamespace(
            search=lambda name: {"title": name, "id": 1},
            list_songs=lambda: [],
        )
        fake = _make_fake(
            stickers=stickers, songs=songs, embed_engine=None,
            _current_sticker_role="default",
        )
        actions = dict(TURN_ACTIONS, stickers=[])

        _run(MessageHandler._execute_tool(
            fake, "send_stickers", {"emotion": "开心", "count": 1},
            "g1", "u1", actions,
        ))
        _run(MessageHandler._execute_tool(
            fake, "sing", {"song": "小星星"}, "g1", "u1", actions,
        ))
        _run(MessageHandler._execute_tool(
            fake, "send_stickers", {"emotion": "温柔", "count": 2},
            "g1", "u1", actions,
        ))

        assert [(item["kind"], item["ordinal"]) for item in actions["action_intents"]] == [
            ("sticker", 0), ("sing", 1), ("sticker", 2), ("sticker", 3),
        ]
        assert actions["stickers"] == ["开心-0", "温柔-0", "温柔-1"]
        assert actions["sing"]["title"] == "小星星"

    def test_image_skill_freezes_local_asset_into_turn_actions(self, monkeypatch, tmp_path):
        """图片工具的真实 CQ 必须在 ReplyPipeline 清洗前转为冻结 image intent。"""
        image = tmp_path / "draw.png"
        image.write_bytes(b"draw-result")
        cq = f"[CQ:image,file=file:///{image.as_posix()}]"

        async def _skill(_name, _args):
            return f"图片好了：{cq}"

        monkeypatch.setattr("agent.skills.execute_skill", _skill)
        actions = dict(TURN_ACTIONS)
        result = _run(MessageHandler._execute_tool(
            _make_fake(), "generate_image", {"prompt": "猫"}, "g1", "u1", actions,
            action_source_id="g1:502",
        ))

        assert result.startswith("图片好了")
        assert actions["images"] == [{
            "asset_ref": "draw.png",
            "asset_sha256": hashlib.sha256(b"draw-result").hexdigest(),
            "asset_valid": True,
            "library_id": str(tmp_path.resolve()),
            "source_skill": "generate_image",
            "ordinal": 0,
        }]
        assert actions["action_intents"] == [{"kind": "image", "ordinal": 0}]

    def test_turn_actions_isolated_between_calls(self):
        """两个回合的 turn_actions 互不影响（模拟并发 A/B 回合）"""
        fake = _make_fake()
        actions_a = dict(TURN_ACTIONS)
        actions_b = dict(TURN_ACTIONS)
        _run(MessageHandler._execute_tool(fake, "send_voice", {}, "", "", actions_a))
        # B 回合没有任何语音意图
        assert actions_b["voice"] is False
        assert actions_a["voice"] is True

    def test_send_stickers_appends_multiple_tool_calls_without_overwrite(self):
        """同一回合多次 send_stickers 必须按调用顺序保留全部批次。"""
        stickers = types.SimpleNamespace(
            match_by_emotion_text=lambda emotion, embed_engine=None, count=1: [
                f"{emotion}-{i}" for i in range(count)
            ],
            sticker_dir="stickers-v1",
        )
        fake = _make_fake(stickers=stickers, embed_engine=None,
                          _current_sticker_role="default")
        actions = dict(TURN_ACTIONS, stickers=[])
        _run(MessageHandler._execute_tool(
            fake, "send_stickers", {"emotion": "开心", "count": 1},
            "g1", "u1", actions,
        ))
        _run(MessageHandler._execute_tool(
            fake, "send_stickers", {"emotion": "温柔", "count": 2},
            "g1", "u1", actions,
        ))
        assert actions["stickers"] == ["开心-0", "温柔-0", "温柔-1"]
        assert [item["emotion"] for item in actions["sticker_intents"]] == ["开心", "温柔"]

    def test_send_stickers_rejects_invalid_count_without_exception(self):
        fake = _make_fake(stickers=types.SimpleNamespace(
            match_by_emotion_text=lambda *args, **kwargs: []
        ))
        result = _run(MessageHandler._execute_tool(
            fake, "send_stickers", {"emotion": "开心", "count": "oops"},
            "g1", "u1", dict(TURN_ACTIONS),
        ))
        assert "count" in result and "整数" in result

    def test_send_stickers_matching_does_not_block_event_loop(self):
        """贴图语义匹配是同步 CPU/模型工作，不能冻结工具循环。"""
        def slow_match(*_args, **_kwargs):
            time.sleep(0.12)
            return ["slow.jpg"]

        fake = _make_fake(
            stickers=types.SimpleNamespace(
                match_by_emotion_text=slow_match,
                sticker_dir="stickers-v1",
            ),
            embed_engine=None,
            _current_sticker_role="default",
        )
        actions = dict(TURN_ACTIONS, stickers=[])

        async def scenario():
            worker = asyncio.create_task(MessageHandler._execute_tool(
                fake, "send_stickers", {"emotion": "开心"},
                "g1", "u1", actions,
            ))
            ticks = 0
            while not worker.done():
                await asyncio.sleep(0.02)
                ticks += 1
            return await worker, ticks

        result, ticks = _run(scenario())

        assert "准备发1张" in result
        assert ticks >= 3

    def test_analyze_image_uses_only_current_turn_image(self):
        """A 回合分析 A 的图片，不能读 Handler 上残留的 B 图片。"""
        seen = []

        async def vision(url, file_id, prompt=""):
            seen.append((url, file_id, prompt))
            return f"看到了 {url}"

        fake = _make_fake(
            _pending_image={"url": "https://legacy/B.jpg", "file_id": "B"},
            _call_vision=vision,
        )
        actions_a = dict(
            TURN_ACTIONS,
            image_ref={
                "url": "https://turn/A.jpg", "file_id": "A",
                "scope_id": "group-1", "user_id": "user-A",
            },
        )

        result = _run(MessageHandler._execute_tool(
            fake, "analyze_image", {"query": "图里是谁"},
            "group-1", "user-A", actions_a,
        ))

        assert result == "看到了 https://turn/A.jpg"
        assert seen == [("https://turn/A.jpg", "A", "图里是谁")]

    def test_analyze_image_rejects_legacy_global_without_turn_image(self):
        """当前回合没图时，即使实例残留旧图也必须拒绝分析。"""
        called = False

        async def vision(*_args, **_kwargs):
            nonlocal called
            called = True
            return "不应看到"

        fake = _make_fake(
            _pending_image={"url": "https://other-user/secret.jpg", "file_id": "secret"},
            _call_vision=vision,
        )

        result = _run(MessageHandler._execute_tool(
            fake, "analyze_image", {}, "group-2", "user-B", dict(TURN_ACTIONS),
        ))

        assert "没有图片" in result
        assert called is False

    def test_analyze_image_rejects_turn_image_from_other_scope(self):
        """即使拿到 image_ref，scope/user 不一致也不能跨会话读取。"""
        called = False

        async def vision(*_args, **_kwargs):
            nonlocal called
            called = True
            return "不应看到"

        fake = _make_fake(_call_vision=vision)
        actions_a = dict(
            TURN_ACTIONS,
            image_ref={
                "url": "https://private/A.jpg", "file_id": "A",
                "scope_id": "_private_user-A", "user_id": "user-A",
            },
        )

        result = _run(MessageHandler._execute_tool(
            fake, "analyze_image", {}, "_private_user-B", "user-B", actions_a,
        ))

        assert "没有图片" in result
        assert called is False

    def test_llm_turn_contract_accepts_image_ref(self):
        """图片引用必须沿 LLM 回合参数传递，不能退回 Handler 实例字段。"""
        params = inspect.signature(MessageHandler._call_llm_with_skills).parameters
        assert "image_ref" in params
        source = Path("agent/handler.py").read_text(encoding="utf-8")
        assert "_pending_image" not in source


class TestLlmReturnContract:
    """回归（2026-08-10）：LLM 调用链的返回类型契约——
    曾把 turn_actions 误写到 _call_deepseek 的返回，导致 _call_llm_light
    （记忆提取/反思等后台任务）收到 tuple，raw.find() 崩溃。"""

    def test_call_llm_light_returns_str(self):
        """_call_llm_light 必须返回 str——后台任务（记忆提取）依赖"""
        from unittest.mock import AsyncMock

        fake = types.SimpleNamespace(
            _llm_lock=asyncio.Lock(),
            _llm_busy=False,
            metrics=types.SimpleNamespace(incr=lambda *a, **k: None),
            llm_config={"provider": "deepseek"},
        )
        # 实例属性 mock（SimpleNamespace 的 MRO 找不到类属性，必须挂在实例上）
        fake._call_deepseek = AsyncMock(return_value="提取结果文本")
        result = _run(MessageHandler._call_llm_light(fake, "sys", "user"))
        assert isinstance(result, str)

    def test_call_deepseek_no_tools_returns_str(self):
        """_call_deepseek 无工具路径必须返回 str（而不是 tuple）"""
        from unittest.mock import AsyncMock

        fake = types.SimpleNamespace(llm_config={
            "provider": "deepseek", "api_key": "test-key",
            "model": "test-model", "base_url": "https://test", "max_tokens": 100, "temperature": 0.7,
        })
        fake._stream_deepseek = AsyncMock(return_value=("回复文本", None, 0.5))
        result = _run(MessageHandler._call_deepseek(fake, "sys", "user"))
        assert isinstance(result, str)
        assert result == "回复文本"
