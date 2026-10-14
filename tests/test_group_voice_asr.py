"""P0-B1：群语音接入同一 ASR（2026-08-28 协作任务包，先写红测再实现）

验收点：
  1. 群语音与私聊同一 _transcribe_voice ASR（不改 ASR 引擎）
  2. 规范化在批处理之前完成（源码契约），transcript 进入 msg['message']，
     raw_message/CQ:record/message_id 原样保留（P0-A 落库已断言 raw 保留）
  3. 同一语音（file_id）不重复转写（实例缓存，防重投/转发重复调用）
  4. ASR 失败保持可观测、不伪造文本、失败不缓存（重投可重试）
  5. voice_enabled=False 时不转写（与私聊入口一致）
"""
import asyncio
import ast
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agent.handler import MessageHandler


def _handler_with_transcribe(transcribe_impl):
    """无 __init__ 的最小 handler 实例：只挂载 ASR 依赖（避免整机启动）"""
    h = object.__new__(MessageHandler)
    h._group_voice_transcripts = {}
    h._transcribe_voice = transcribe_impl
    return h


# ═══════════════════════════════════════════════════════
# 1. 群语音成功：转写 + 同文件不重复转写
# ═══════════════════════════════════════════════════════

def test_group_voice_transcribes_and_caches():
    calls = {"n": 0}

    async def fake_transcribe(raw):
        calls["n"] += 1
        return "明天去爬山"

    h = _handler_with_transcribe(fake_transcribe)
    raw = "[CQ:record,file=md5.amr]"
    t1 = asyncio.run(h._normalize_group_voice(raw))
    t2 = asyncio.run(h._normalize_group_voice(raw))
    assert t1 == "明天去爬山"
    assert t2 == "明天去爬山"
    assert calls["n"] == 1  # 同一语音只转写一次（缓存命中）
    # 缓存有上限：大量不同语音不膨胀
    for i in range(210):
        asyncio.run(h._normalize_group_voice(f"[CQ:record,file=f{i}.amr]"))
    assert len(h._group_voice_transcripts) <= 200


def test_group_voice_concurrent_same_file_id_shares_transcription_task():
    """并发重投同一语音只允许一次 ASR，且两个调用都拿到结果。"""
    calls = {"n": 0}

    async def fake_transcribe(raw):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return "并发安全"

    h = _handler_with_transcribe(fake_transcribe)
    raw = "[CQ:record,file=same.amr]"

    async def run():
        return await asyncio.gather(
            h._normalize_group_voice(raw),
            h._normalize_group_voice(raw),
        )

    assert asyncio.run(run()) == ["并发安全", "并发安全"]
    assert calls["n"] == 1


def test_transcribe_voice_uses_unique_temp_files_and_cleans_up(monkeypatch):
    """同 file_id 并发下载不能共享路径；成功后临时文件必须删除。"""
    downloaded_paths = []
    recognized_paths = []

    class FakeNapcat:
        async def download_record(self, file_id, save_path):
            downloaded_paths.append(save_path)
            await asyncio.sleep(0.01)
            Path(save_path).write_bytes(b"fake audio")
            return True

    class FakeRecognizer:
        async def transcribe(self, path):
            recognized_paths.append(path)
            await asyncio.sleep(0.01)
            assert Path(path).exists()
            return "唯一临时文件"

    from agent import asr
    monkeypatch.setattr(asr, "get_recognizer", lambda: FakeRecognizer())
    h = object.__new__(MessageHandler)
    h.napcat = FakeNapcat()
    raw = "[CQ:record,file=same.amr]"

    async def run():
        return await asyncio.gather(
            h._transcribe_voice(raw),
            h._transcribe_voice(raw),
        )

    assert asyncio.run(run()) == ["唯一临时文件", "唯一临时文件"]
    assert len(downloaded_paths) == 2
    assert len(set(downloaded_paths)) == 2
    assert recognized_paths and set(recognized_paths) == set(downloaded_paths)
    assert all(not Path(path).exists() for path in downloaded_paths)


def test_transcribe_voice_cleanup_does_not_block_event_loop(tmp_path, monkeypatch):
    """语音临时文件的 finally 清理不能同步阻塞事件循环。"""
    from agent import asr

    class FakeNapcat:
        async def download_record(self, _file_id, save_path):
            Path(save_path).write_bytes(b"fake audio")
            return True

    class FakeRecognizer:
        async def transcribe(self, path):
            assert Path(path).exists()
            return "清理边界"

    monkeypatch.setattr(asr, "get_recognizer", lambda: FakeRecognizer())
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    h = object.__new__(MessageHandler)
    h.napcat = FakeNapcat()
    main_thread = threading.get_ident()
    unlinks = []
    original_unlink = Path.unlink

    def slow_unlink(path_obj, *args, **kwargs):
        unlinks.append(threading.get_ident())
        time.sleep(0.05)
        return original_unlink(path_obj, *args, **kwargs)

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
            result = await h._transcribe_voice("[CQ:record,file=clean.amr]")
        finally:
            stopped = True
            await task
        return ticks, result

    with patch.object(Path, "unlink", slow_unlink):
        ticks, result = asyncio.run(scenario())

    assert result == "清理边界"
    assert ticks > 0
    assert unlinks and all(thread_id != main_thread for thread_id in unlinks)


# ═══════════════════════════════════════════════════════
# 2. ASR 失败：可观测、不伪造、不缓存
# ═══════════════════════════════════════════════════════

def test_group_voice_failure_is_observable_not_fabricated():
    calls = {"n": 0}

    async def fake_fail(raw):
        calls["n"] += 1
        return ""

    h = _handler_with_transcribe(fake_fail)
    raw = "[CQ:record,file=bad.amr]"
    assert asyncio.run(h._normalize_group_voice(raw)) == ""
    # 失败不缓存——NapCat 重投同一语音可重试转写
    assert asyncio.run(h._normalize_group_voice(raw)) == ""
    assert calls["n"] == 2


# ═══════════════════════════════════════════════════════
# 3. 无 CQ:record：不触发 ASR
# ═══════════════════════════════════════════════════════

def test_group_voice_no_file_returns_empty():
    async def fake(raw):
        raise AssertionError("不应调用 ASR")

    h = _handler_with_transcribe(fake)
    assert asyncio.run(h._normalize_group_voice("没有语音")) == ""


# ═══════════════════════════════════════════════════════
# 4. 源码契约：规范化在批处理之前、黑名单之后、voice_enabled 门控
# ═══════════════════════════════════════════════════════

def test_group_voice_normalization_before_batching_contract():
    """handle_group_message 中语音规范化必须在批处理拦截之前完成；
    黑名单群不下载语音（隐私/资源）；ASR 关闭时不转写。
    防好心修复挪位置或删分支。

    ⚠ 门控用的是 asr_enabled 而非 voice_enabled（2026-09-18 起）——见文末
    「听/说分离」契约。改回 voice_enabled 会让「不发语音但听得懂语音消息」
    的版本重新变聋。"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "handle_group_message")
    body = ast.get_source_segment(src, node)
    # 规范化调用存在
    assert "_normalize_group_voice" in body
    # 规范化在批处理拦截（enqueue_group）之前
    assert body.index("_normalize_group_voice") < body.index("enqueue_group")
    # 黑名单检查在规范化之前——黑名单群不下载/转写语音
    assert body.index("_group_blacklist") < body.index("_normalize_group_voice")
    # ASR 门控与私聊入口一致
    assert '"[CQ:record" in raw and self.asr_enabled' in body


def test_private_voice_normalization_before_batching_contract():
    """私聊多条语音合并前必须先把每条 CQ record 转成文本。"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "handle_private_message")
    body = ast.get_source_segment(src, node)
    assert "await self._transcribe_voice(raw)" in body
    assert body.index("await self._transcribe_voice(raw)") < body.index("enqueue_private")


# ═══════════════════════════════════════════════════════
# 5. 源码契约：_normalize_group_voice 与 _transcribe_voice 同文件且不替换
# ═══════════════════════════════════════════════════════

def test_normalize_uses_same_transcribe_engine():
    """_normalize_group_voice 必须复用 _transcribe_voice（同一 ASR 引擎）"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_normalize_group_voice")
    body = ast.get_source_segment(src, node)
    # 并发去重通过共享 task 调用，但底层引擎仍统一走 _transcribe_voice。
    assert "create_task(self._transcribe_voice(raw_message))" in body


# ═══════════════════════════════════════════════════════
# 6. 听/说分离契约（2026-09-18）
# ═══════════════════════════════════════════════════════

def test_listen_and_speak_gates_are_separate():
    """听（ASR）与说（TTS）必须是两个独立开关。

    发布版「识图版」要的正是 voice.enabled=False + asr_enabled=True：
    不发语音，但仍听得懂别人发的语音消息。原先两者共用 voice_enabled，
    一关发声就顺带聋了。

    本闸门钉住三件事，防好心重构把开关重新并回去：
      1. 语音消息入口（群 + 私聊）只看 asr_enabled，与发声开关解耦
      2. 发声侧仍只看 voice_enabled，不被 asr_enabled 影响
      3. asr_enabled 缺省回退到 voice_enabled——老配置行为不变
    """
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 1. 两个语音消息入口都门控 asr_enabled
    for fn_name in ("handle_group_message", "handle_private_message"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == fn_name)
        body = ast.get_source_segment(src, node)
        assert '"[CQ:record" in raw and self.asr_enabled' in body, \
            f"{fn_name} 的语音消息入口必须门控 asr_enabled"
        assert '"[CQ:record" in raw and self.voice_enabled' not in body, \
            f"{fn_name} 不应再用 voice_enabled 门控语音输入——那会让只听不说的版本变聋"

    # 2. 发声侧的判定点必须仍用 voice_enabled（不被 asr_enabled 串味）
    for fn_name in ("handle_group_message", "handle_private_message"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == fn_name)
        body = ast.get_source_segment(src, node)
        assert "_wants_voice and self.voice_enabled" in body, \
            f"{fn_name} 的发声判定必须仍门控 voice_enabled"

    # 3. 缺省回退：老配置（没有 asr_enabled 字段）行为与改动前一致
    init = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "__init__")
    init_body = ast.get_source_segment(src, init)
    assert 'voice_cfg.get("asr_enabled", self.voice_enabled)' in init_body, \
        "asr_enabled 必须回退到 voice_enabled——否则老配置会静默改变行为"
