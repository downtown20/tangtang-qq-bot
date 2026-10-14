"""场景热重载的一致性回归。"""

import threading
import time

from agent.scenario import Scenario, ScenarioManager


def test_scenario_reload_and_get_are_consistent(tmp_path):
    """重载期间 get 必须等待完整场景快照，不能看到 clear/load 空窗。"""
    manager = ScenarioManager(str(tmp_path))
    manager._scenarios["daily"] = Scenario(
        name="daily", display="日常", role="陪伴",
    )
    original_load = manager._load_all
    load_started = threading.Event()
    release_load = threading.Event()

    def slow_load():
        manager._scenarios.clear()
        load_started.set()
        release_load.wait(timeout=2)
        original_load()

    manager._load_all = slow_load
    reload_thread = threading.Thread(target=manager.reload)
    reload_thread.start()
    assert load_started.wait(timeout=1)

    result: list[Scenario | None] = []
    get_done = threading.Event()

    def get_scenario():
        result.append(manager.get("daily"))
        get_done.set()

    get_thread = threading.Thread(target=get_scenario)
    get_thread.start()
    time.sleep(0.1)
    assert get_done.is_set() is False

    release_load.set()
    reload_thread.join(timeout=2)
    get_thread.join(timeout=2)
    assert reload_thread.is_alive() is False
    assert get_thread.is_alive() is False
    assert result == [None]
