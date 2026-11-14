"""2026-10-25 第 4 轮自查：重启后她从 chat_log 重建对话窗口

## 缺陷

`memory.short_term` 是纯内存 `deque` —— bot 一重启**所有对话窗口归零**，
她下一句话就「不记得刚才聊到哪」。

取证：`turn_traces#80` 的 `prompt_digest.history_count=0`（重启后第一轮她
只收到当前那一句话、没有任何上下文），`trace#81` 回升到 2 条 —— 证实是
内存态被清，不是「会客厅不拼历史」。这个字段是第 3 轮刚补的留档，**上线第一
回合就抓到了它**。

## 修法

从 `chat_log` **重建**窗口，而不是把 buffer 持久化到磁盘：

- `chat_log` 本来就是唯一事实源，buffer 只是它的内存投影 —— 投影可以重算；
- 持久化会造出第二份真相，两份不一致时没人知道该信谁。

已落盘（`状态/` 之类）的运行时状态该持久化（教训 #15）；**能重算的东西不该存**。
"""
import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT = "10002"


def _ts(minutes_ago: int) -> str:
    """相对「现在」生成一个 chat_log 时间戳。

    ⚠ 这里曾写死 `2026-10-25 06:00:00` 这样的日历时间，而回灌是按
    `active_hours` 从**当下**往前推的窗口——写死的时间每过一天就更旧一点，
    超过窗口后整个文件一起变红（2026-11-14 实测：时间戳距今 480 小时，
    窗口 240 小时，8 条断言全炸，看着像「短时记忆结构没被填充」，
    实际是测试自己过期了）。凡是被「距今多久」判定的数据，一律现算。
    """
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def memory(store):
    from agent.memory import MemorySystem
    return MemorySystem(store=store)


def _seed_group(store):
    store.insert_chat("10001", "大家好呀", "888", False, timestamp=_ts(11))
    store.insert_chat("10001", "早上好", "888", True, timestamp=_ts(10))
    store.insert_chat("10002", "那我呢", "888", False, timestamp=_ts(9))
    store.get_or_create_person("10001", "小明")
    store.get_or_create_person("10002", "小红")


def _seed_private(store):
    store.insert_chat("20001", "私聊一句", "", False, timestamp=_ts(8))
    store.insert_chat("20001", "私聊回一句", "", True, timestamp=_ts(7))
    store.get_or_create_person("20001", "小刚")


class TestWarmupBuildsWindow:
    def test_group_window_rebuilt(self, store, memory):
        _seed_group(store)
        stats = memory.warm_short_term_from_chat_log(
            bot_qq=BOT, bot_name="小糖糖", active_hours=240)
        assert stats["scopes"] >= 1
        buf = list(memory.short_term.get("888") or [])
        assert [e["message"] for e in buf] == ["大家好呀", "早上好", "那我呢"]

    def test_private_window_uses_private_key(self, store, memory):
        """私聊在 chat_log 里 group_id 为空，回灌必须落到 `_private_<qq>` 这个键"""
        _seed_private(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        assert "_private_20001" in memory.short_term
        assert [e["message"] for e in memory.short_term["_private_20001"]] == [
            "私聊一句", "私聊回一句"]

    def test_bot_reply_gets_bot_qq_identity(self, store, memory):
        """她自己那条必须记成 bot_qq——否则 `get_recent_context_messages` 会把
        她自己的话渲染成对方说的（role 判断就是拿 qq_id 比的）"""
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        bot_entries = [e for e in memory.short_term["888"] if e["qq_id"] == BOT]
        assert bot_entries and bot_entries[0]["message"] == "早上好"

    def test_nicknames_resolved(self, store, memory):
        """历史里得有人名，否则她又会看到一串数字（第 1 轮那个坑）"""
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        nicks = {e["nickname"] for e in memory.short_term["888"]}
        assert "小明" in nicks and "小红" in nicks

    def test_time_field_is_hhmmss(self, store, memory):
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        assert all(len(e["time"]) == 8 and e["time"].count(":") == 2
                   for e in memory.short_term["888"])

    def test_seq_monotonic_and_continues_after_warmup(self, store, memory):
        """回灌出的 seq 必须接上后续 add_to_buffer，不能重复也不能倒回 0
        —— 第 1 轮那个「当前消息被排除」的修复就是靠 seq 锚定的"""
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        seqs = [e["seq"] for e in memory.short_term["888"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        new_seq = memory.add_to_buffer("888", "10001", "小明", "回灌之后的新消息")
        assert new_seq > max(seqs)

    def test_rebuilt_window_feeds_llm_history_correctly(self, store, memory):
        """端到端：回灌出来的窗口要能直接产出 role 正确的历史消息"""
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        msgs = memory.get_recent_context_messages("888", BOT, limit=10)
        assert msgs[0] == {"role": "user", "content": "小明: 大家好呀"}
        assert msgs[1] == {"role": "assistant", "content": "早上好"}


class TestWarmupBoundaries:
    def test_does_not_overwrite_live_buffer(self, store, memory):
        """已有内存数据不能被回灌覆盖——回灌只补空窗，不动活数据"""
        memory.add_to_buffer("888", "10001", "小明", "活数据")
        _seed_group(store)
        stats = memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        assert stats["scopes"] == 0
        assert [e["message"] for e in memory.short_term["888"]] == ["活数据"]

    def test_excludes_synthetic_rows(self, store, memory):
        """框架合成行（「【同时有 N 个人找你…】」）是提示词不是聊天，
        回灌进来她会把系统提示当成自己或对方说过的话"""
        store.insert_chat("10001", "【同时有 3 个人找你，请回复主人】", "888", False,
                          timestamp=_ts(6))
        with store._connect() as conn:
            conn.execute("UPDATE chat_log SET is_synthetic=1 WHERE qq_id='10001'")
            conn.commit()
        _seed_group(store)
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        texts = [e["message"] for e in memory.short_term.get("888") or []]
        assert not any("同时有" in t for t in texts)

    def test_excludes_old_messages(self, store, memory):
        """超出 active_hours 的旧会话不回灌——全量回灌会拖慢启动，且旧窗口没价值"""
        store.insert_chat("10001", "很久以前", "999", False, timestamp=_ts(60 * 24 * 40))
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=24)
        assert "999" not in memory.short_term

    def test_empty_db_is_noop(self, store, memory):
        stats = memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=24)
        assert stats == {"scopes": 0, "entries": 0}

    def test_no_bot_qq_is_noop(self, store, memory):
        _seed_group(store)
        assert memory.warm_short_term_from_chat_log(bot_qq="", active_hours=240) == {
            "scopes": 0, "entries": 0}
        assert not memory.short_term

    def test_missing_person_falls_back_to_qq(self, store, memory):
        """people 表里没这个人时用 QQ 号兜底，不抛异常"""
        store.insert_chat("29999", "没有档案的人", "888", False,
                          timestamp=_ts(5))
        memory.warm_short_term_from_chat_log(bot_qq=BOT, active_hours=240)
        entries = [e for e in memory.short_term.get("888") or []
                   if e["qq_id"] == "29999"]
        assert entries and entries[0]["nickname"] == "29999"


class TestStartupWiring:
    def test_start_services_calls_warmup(self):
        """接线闸门：`start_services` 必须真的调回灌——否则代码写了没人调
        （教训 #12：写了 infra ≠ 接入业务）"""
        src = (ROOT / "agent" / "handler.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "start_services":
                fn = node
                break
        assert fn is not None, "找不到 start_services"
        called = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "warm_short_term_from_chat_log"
            for n in ast.walk(fn)
        )
        assert called, (
            "start_services 没有再调用 warm_short_term_from_chat_log——"
            "重启后对话窗口又会归零（turn_traces#80 那种 history_count=0 会复发）"
        )

    def test_warmup_failure_does_not_block_startup(self):
        """回灌是体验优化不是启动前提：它抛异常时启动必须继续"""
        src = (ROOT / "agent" / "handler.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "start_services":
                fn = node
                break
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(fn))
        assert has_try, "start_services 里的回灌没有 try——一次回灌失败会连 TTS 都起不来"
