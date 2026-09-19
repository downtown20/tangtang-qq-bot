"""持久自我状态的重启回归测试。"""

import asyncio
import time

from agent.self_state import TangTangSelf


def test_pending_experiences_survive_restart(tmp_path, monkeypatch):
    state_file = tmp_path / "self.json"
    monkeypatch.setattr(TangTangSelf, "STATE_FILE", str(state_file))

    state = TangTangSelf(bot_qq="bot")
    state.accumulate_experience(
        "user", "测试用户", message="这条互动还没来得及反思"
    )
    state.save()

    restored = TangTangSelf(bot_qq="bot")
    experiences = restored.get_recent_experiences(limit=200)
    assert len(experiences) == 1
    assert experiences[0]["message"] == "这条互动还没来得及反思"


def test_periodic_self_state_save_does_not_block_event_loop(tmp_path, monkeypatch):
    """高关系数下的周期性自我状态落盘不能冻结消息事件循环。"""
    state_file = tmp_path / "self.json"
    monkeypatch.setattr(TangTangSelf, "STATE_FILE", str(state_file))
    state = TangTangSelf(bot_qq="bot")
    for i in range(1000):
        rel = state.get_or_create_relationship(str(i))
        rel.learned = ["x" * 80] * 8
        rel.mentions = {str(j): 1 for j in range(5)}
        rel.unfinished = ["u" * 80] * 3
    state._experience_save_counter = 4

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(heartbeat())
        try:
            state.accumulate_experience(
                "new", "测试", reply_sent=False, message="触发周期保存",
            )
            await asyncio.sleep(0.03)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # 后台保存是有界线程任务，固定 30ms 不能证明任务已完成；
        # 显式等待同一任务，避免把正常调度抖动误报为丢失持久化。
        await asyncio.wait_for(state.flush_pending_save(), timeout=1.0)
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks >= 5
    assert state_file.exists()


def test_drive_mutations_persist_after_bounded_autonomous_batch(tmp_path, monkeypatch):
    """没有关系更新时，tick/release 也必须在固定批次内留下恢复点。"""
    state_file = tmp_path / "self.json"
    monkeypatch.setattr(TangTangSelf, "STATE_FILE", str(state_file))
    state = TangTangSelf(bot_qq="bot")
    state.drives.drives["social"].value = 0.8
    state.save()

    for _ in range(5):
        state.tick()
        state.drives.release("social", 0.01)

    expected = state.drives.get_state_json()
    restored = TangTangSelf(bot_qq="bot")

    assert restored.drives.get_state_json() == expected


def test_explicit_drive_save_restores_exact_snapshot(tmp_path, monkeypatch):
    state_file = tmp_path / "self.json"
    monkeypatch.setattr(TangTangSelf, "STATE_FILE", str(state_file))
    state = TangTangSelf(bot_qq="bot")
    state.drives.drives["social"].value = 0.456
    state.drives.drives["commitment"].value = 0.654
    expected = state.drives.get_state_json()

    state.save()
    restored = TangTangSelf(bot_qq="bot")

    assert restored.drives.get_state_json() == expected
