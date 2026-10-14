"""批 1a 验收：关系档案游标往返 / 80 边界 / 失败退避 / unknown-only 不覆盖（2026-08-16）

事故背景：get_or_create_person 不返回 relationship_updated → maybe_update
永远读 0 → 超过阈值后每轮私聊都重合成关系档案（日志 17:39-17:50 八次）。
"""
import asyncio
import threading
import time

from unittest.mock import AsyncMock

from agent.relationship import RelationshipManager


def _seed_chats(store, qq_id, n):
    for i in range(n):
        store.insert_chat(qq_id, f"测试消息第{i}条")


def _manager(store, llm=None):
    return RelationshipManager(store, llm or AsyncMock())


class TestRelationshipFields:
    def test_new_person_has_relationship_fields(self, store):
        p = store.get_or_create_person("30001", "新人")
        assert p["relationship_updated"] == 0
        assert p["last_bonus_date"] == ""

    def test_cursor_roundtrip(self, store):
        """写 relationship_updated 后，读回来不再是 0——游标成对"""
        store.get_or_create_person("30002", "游标")
        _seed_chats(store, "30002", 3)
        store._update_relationship_timestamp("30002")
        p = store.get_or_create_person("30002")
        assert p["relationship_updated"] == p["total_chats"] == 3


class TestMaybeUpdate:
    def test_under_threshold_no_synthesis(self, store):
        llm = AsyncMock(return_value="1. 认识于测试\n2. 喜欢被叫哥哥\n3. 无\n4. 一起测试")
        mgr = _manager(store, llm)
        store.get_or_create_person("30003", "边界")
        _seed_chats(store, "30003", 79)
        assert asyncio.run(mgr.maybe_update("30003")) is False
        llm.assert_not_called()

    def test_at_threshold_synthesizes(self, store):
        llm = AsyncMock(return_value="1. 认识于测试\n2. 喜欢被叫哥哥\n3. 无\n4. 一起测试")
        mgr = _manager(store, llm)
        store.get_or_create_person("30004", "边界2")
        _seed_chats(store, "30004", 80)
        assert asyncio.run(mgr.maybe_update("30004")) is True
        llm.assert_called_once()

    def test_success_advances_cursor_and_stops(self, store):
        """成功合成后游标推进——下一条消息不再触发重合成"""
        llm = AsyncMock(return_value="1. 认识于测试\n2. 喜欢被叫哥哥\n3. 无\n4. 一起测试")
        mgr = _manager(store, llm)
        store.get_or_create_person("30005", "成功")
        _seed_chats(store, "30005", 85)
        assert asyncio.run(mgr.maybe_update("30005")) is True
        p = store.get_or_create_person("30005")
        assert p["relationship_updated"] == p["total_chats"]
        # 再来一条消息：未到新阈值，不再合成
        store.insert_chat("30005", "再来一条")
        assert asyncio.run(mgr.maybe_update("30005")) is False
        assert llm.call_count == 1

    def test_failure_backoff_persists(self, store):
        """合成失败→退避 6 小时；退避期内不重试（LLM 只调一次）"""
        llm = AsyncMock(return_value="")
        mgr = _manager(store, llm)
        store.get_or_create_person("30006", "失败")
        _seed_chats(store, "30006", 81)
        assert asyncio.run(mgr.maybe_update("30006")) is False
        assert store.kv_get("rel_syn_fail_until:30006")
        # 退避期内再触发：不调用 LLM
        store.insert_chat("30006", "退避期内")
        assert asyncio.run(mgr.maybe_update("30006")) is False
        assert llm.call_count == 1

    def test_unknown_only_does_not_overwrite(self, store):
        """全「未知」结果不覆盖旧档案、不推进游标"""
        store.get_or_create_person("30007", "未知人")
        store.set_relationship_summary("30007", "旧档案：一起玩过游戏")
        store._update_relationship_timestamp("30007")
        _seed_chats(store, "30007", 80)

        llm = AsyncMock(return_value="1. 未知\n2. 未知\n3. 未知\n4. 未知")
        mgr = _manager(store, llm)
        assert asyncio.run(mgr.maybe_update("30007")) is False
        assert store.get_relationship_summary("30007") == "旧档案：一起玩过游戏"
        p = store.get_or_create_person("30007")
        assert p["relationship_updated"] == 0  # 未推进（种子消息在 timestamp 之前）

    def test_unknown_with_real_item_still_saves(self, store):
        """部分未知（有真实条目）正常覆盖"""
        llm = AsyncMock(return_value="1. 认识于群聊\n2. 未知\n3. 喜欢被叫主人\n4. 未知")
        mgr = _manager(store, llm)
        store.get_or_create_person("30008", "部分未知")
        _seed_chats(store, "30008", 82)
        assert asyncio.run(mgr.maybe_update("30008")) is True
        assert "认识于群聊" in store.get_relationship_summary("30008")

    def test_slow_store_does_not_block_event_loop(self, store):
        """关系档案异步合成的 SQLite 读写必须在线程边界执行。"""
        llm = AsyncMock(return_value="1. 认识于测试\n2. 喜欢被叫哥哥\n3. 无\n4. 一起测试")
        store.get_or_create_person("30009", "慢存储")
        _seed_chats(store, "30009", 80)
        thread_ids = []

        def slow_method(original):
            def wrapped(*args, **kwargs):
                thread_ids.append(threading.get_ident())
                time.sleep(0.05)
                return original(*args, **kwargs)
            return wrapped

        for name in (
            "kv_get", "get_or_create_person", "get_earliest_chats",
            "set_relationship_summary", "_update_relationship_timestamp",
        ):
            setattr(store, name, slow_method(getattr(store, name)))

        mgr = _manager(store, llm)
        main_thread = threading.get_ident()

        async def scenario():
            ticks = 0
            stop = False

            async def ticker():
                nonlocal ticks
                while not stop:
                    ticks += 1
                    await asyncio.sleep(0.005)

            task = asyncio.create_task(ticker())
            result = await mgr.maybe_update("30009")
            stop = True
            await task
            return ticks, result

        ticks, result = asyncio.run(scenario())

        assert result is True
        assert ticks > 0
        assert thread_ids and all(thread_id != main_thread for thread_id in thread_ids)
