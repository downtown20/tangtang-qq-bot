"""持久自我状态的重启回归测试。"""

import threading
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
    """高关系数下的周期性自我状态落盘不能冻结消息事件循环。

    2026-10-16 T2.8 **重写判据**。旧版断言「30ms 内心跳 ≥5 次」，实测证明它测不出
    它想测的东西：在同一台机器上跑**不触发保存**的对照组，同样是 4~5 tick / 最大停顿
    15.8~16.0ms（`await asyncio.sleep(0.001)` 在 Windows 上受系统定时器粒度支配，
    实测约 10ms/次）⇒ 那条断言只是在赌当时的定时器粒度，全量跑里两次红灯都是假红
    （同类问题见 2026-10-15 的 test_observer_absence 修复）。

    新判据两条，都与机器负载无关：
      ① 落盘**发生在别的线程**（写线程 ident ≠ 事件循环线程 ident）——契约本体；
      ② 保存期间事件循环仍能推进：用 `await asyncio.sleep(0)` 计圈（不依赖定时器），
         并且只看**构建快照那一段**前后圈数是否增长（那是唯一还在循环上的部分）。
    阳性对照（两条，人工跑过）：把 `_write_save_data` 改回循环内同步调用 → ① 红；
    删掉 `_build_save_data_async` 里的 `await asyncio.sleep(0)` → ② 红。
    """
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
        loop_thread = threading.get_ident()
        seen: dict = {}
        ticks = 0
        spinning = True

        async def spinner():
            nonlocal ticks
            while spinning:
                ticks += 1
                await asyncio.sleep(0)   # 无定时器：只测「循环还能不能跑」

        orig_write = state._write_save_data

        def write(data):
            seen["write_thread"] = threading.get_ident()
            return orig_write(data)

        state._write_save_data = write

        orig_build = state._build_save_data_async

        async def build():
            seen["ticks_before"] = ticks
            data = await orig_build()
            seen["ticks_after"] = ticks
            return data

        state._build_save_data_async = build

        task = asyncio.create_task(spinner())
        state.accumulate_experience(
            "new", "测试", reply_sent=False, message="触发周期保存",
        )
        # 等真正的落盘任务收口——不靠 sleep 猜它有没有做完
        await asyncio.wait_for(state.flush_pending_save(), timeout=5.0)
        spinning = False
        await asyncio.gather(task, return_exceptions=True)
        return ticks, seen, loop_thread

    ticks, seen, loop_thread = asyncio.run(scenario())
    assert state_file.exists()
    # ① 落盘在别的线程（契约：不占用事件循环）
    assert seen.get("write_thread") is not None, "落盘函数从没被调用"
    assert seen["write_thread"] != loop_thread, "落盘跑在事件循环线程里了"
    # ② 建快照那一段必须让出循环（否则 1000 个关系的复制会整段堵住消息处理）
    assert seen["ticks_after"] > seen["ticks_before"] + 2, (
        f"构建快照期间事件循环几乎没推进（{seen['ticks_before']} → {seen['ticks_after']} 圈）")


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
