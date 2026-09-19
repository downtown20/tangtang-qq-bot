"""personalization 模块回归测试——生日祝福 + 偏好查询。

2026-08-16 事故：批三用 heredoc 正则重写偏好段时误删整个 BirthdayGreeter 类
（NameError → bot 启动即崩），测试配置无 owner_qq 使生日初始化路径零覆盖，
267 测试全绿没拦住。本文件钉住工厂路径与核心行为。
"""
import asyncio
import json
from datetime import datetime

from agent.personalization import (
    BirthdayGreeter,
    create_birthday_greeter,
    create_preference_tracker,
)
from onebot.ws_client import SendResult


class FakeStore:
    def __init__(self):
        self._kv = {}

    def kv_get(self, key):
        return self._kv.get(key)

    def kv_set(self, key, value):
        self._kv[key] = value

    def get_today_birthdays(self):
        bday = datetime.now().strftime("%m-%d")
        return [{"qq_id": "10001", "nickname": "小蓝", "birthday": bday}]

    def get_or_create_person(self, qq_id):
        return {"nickname": "小蓝"}

    def query_memories(self, qq_id, trusted_only=False, source_group_id=None):
        assert trusted_only is True
        return [
            {"key": "like", "value": "草莓"},
            {"key": "hate", "value": "香菜"},
        ]


async def _fake_llm(system, user):
    return "生日快乐喵～🎂"


def test_create_birthday_greeter_constructs():
    g = create_birthday_greeter(
        send_group_msg=lambda gid, msg: None,
        store=FakeStore(),
        llm_caller=None,
        get_group_ids=lambda: ["123"],
    )
    assert isinstance(g, BirthdayGreeter)


def test_birthday_greeter_sends_once_and_persists():
    store = FakeStore()
    sent = []
    async def send(gid, msg):
        sent.append((gid, msg))
        return True
    g = create_birthday_greeter(
        send_group_msg=send,
        store=store,
        llm_caller=_fake_llm,
        get_group_ids=lambda: ["123"],
    )
    asyncio.run(g._check_birthdays())
    assert len(sent) == 1
    state = json.loads(store.kv_get("birthday_sent"))
    assert "10001" in state["sent"]
    # 同一天重复检查不重复发送
    asyncio.run(g._check_birthdays())
    assert len(sent) == 1


def test_start_stop_loop_lifecycle():
    """start() 创建循环任务，stop() 干净取消（原版 start 无重入守卫，靠 _on_connected 单次调用）"""
    g = create_birthday_greeter(
        send_group_msg=lambda gid, msg: None,
        store=FakeStore(),
        llm_caller=None,
        get_group_ids=lambda: ["123"],
    )
    asyncio.run(_run_start_stop(g))


async def _run_start_stop(g):
    g.start()
    assert g._task is not None and not g._task.done()
    g.stop()
    await asyncio.sleep(0.05)
    assert g._task.done()


def test_birthday_state_survives_restart():
    """重启后恢复今天已发记录——不发重复祝福"""
    store = FakeStore()
    store.kv_set("birthday_sent", json.dumps(
        {"date": datetime.now().strftime("%Y-%m-%d"), "sent": ["10001"]}
    ))
    sent = []
    async def send(gid, msg):
        sent.append((gid, msg))
        return True
    g = create_birthday_greeter(
        send_group_msg=send,
        store=store,
        llm_caller=_fake_llm,
        get_group_ids=lambda: ["123"],
    )
    asyncio.run(g._check_birthdays())
    assert sent == []


def test_unconfirmed_birthday_is_persisted_without_claiming_sent_or_replaying():
    store = FakeStore()
    calls = []

    async def send(gid, msg):
        calls.append((gid, msg))
        return SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")

    g = create_birthday_greeter(
        send_group_msg=send, store=store,
        llm_caller=_fake_llm, get_group_ids=lambda: ["123"],
    )

    asyncio.run(g._check_birthdays())
    asyncio.run(g._check_birthdays())

    assert len(calls) == 1
    assert "10001" not in g._sent_today
    assert "10001" in g._uncertain_today
    state = json.loads(store.kv_get("birthday_sent"))
    assert "10001" in state["uncertain"]


def test_preference_tracker_reads_llm_memories():
    t = create_preference_tracker(store=FakeStore())
    prefs = t.get_preferences("10001")
    assert "草莓" in prefs and "香菜" in prefs


def test_preference_tracker_no_store_is_safe():
    t = create_preference_tracker(store=None)
    assert t.get_preferences("10001") == ""
