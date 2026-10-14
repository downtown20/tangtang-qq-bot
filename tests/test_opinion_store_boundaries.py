"""意见征集 Store 边界回归。

意见管理器在异步消息/自治路径中不能把同步 SQLite 调用留在事件循环；
这些测试用慢 fake 固定证明线程归属和心跳仍可推进。
"""

import asyncio
import sqlite3
import threading
import time
from datetime import datetime

from agent.opinion import OpinionManager
from agent.store import Store


class SlowOpinionStore:
    def __init__(self, delay=0.08, open_campaign=False):
        self.delay = delay
        self.open_campaign = open_campaign
        self.calls = []
        self.campaign_id = 1

    def _call(self, name):
        self.calls.append((name, threading.get_ident()))
        time.sleep(self.delay)

    def get_open_opinion_campaign(self):
        self._call("get_open")
        if not self.open_campaign:
            return None
        return {"id": self.campaign_id, "topic": "probe"}

    def get_or_create_person(self, qq_id):
        self._call("person")
        return {"nickname": f"用户{qq_id}"}

    def create_opinion_campaign(self, topic):
        self._call("create")
        return self.campaign_id

    def add_opinion_participant(self, *args, **kwargs):
        self._call("participant")

    def add_opinion_message(self, *args, **kwargs):
        self._call("message")

    def get_opinion_participant(self, campaign_id, qq_id):
        self._call("get_participant")
        return {"status": "participating"}

    def update_opinion_participant(self, *args, **kwargs):
        self._call("update_participant")

    def get_opinion_participants(self, campaign_id):
        self._call("get_participants")
        return [{
            "qq_id": "u1",
            "nickname": "小蓝",
            "status": "pending",
            "last_msg_ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }]


def _bare_manager(store, llm=None):
    """跳过构造器的恢复 task，只验证目标异步方法。"""
    manager = object.__new__(OpinionManager)
    manager._store = store
    manager._llm = llm or (lambda *_args: None)
    manager._send = lambda *_args: None
    manager._base = ""
    manager._recall = None
    manager._self_state = None
    manager._bot_qq = "bot"
    manager._invite_retry_delay = 0
    manager._notify_owner = None
    manager._enrich = None
    manager._blacklist = set()
    manager._owner_qq = ""
    manager._background_tasks = set()
    manager._claimed_invites = set()
    return manager


async def _heartbeat(stop_event, interval=0.005):
    ticks = 0
    while not stop_event.is_set():
        ticks += 1
        await asyncio.sleep(interval)
    return ticks


def test_start_campaign_store_io_keeps_event_loop_alive():
    async def run():
        store = SlowOpinionStore(open_campaign=False)
        manager = _bare_manager(store)

        async def no_invites(*_args, **_kwargs):
            return None

        manager._schedule_invites = no_invites
        stop = asyncio.Event()
        ticker = asyncio.create_task(_heartbeat(stop))
        main_thread = threading.get_ident()
        started = time.perf_counter()
        result = await manager.start_campaign("话题", targets=["u1"])
        elapsed_ms = (time.perf_counter() - started) * 1000
        stop.set()
        ticks = await ticker

        assert result["queued"] == 1
        assert elapsed_ms >= 350
        assert ticks >= 20
        assert store.calls
        assert all(thread_id != main_thread for _name, thread_id in store.calls)

    asyncio.run(run())


def test_handle_user_message_store_io_keeps_event_loop_alive():
    async def llm(*_args):
        return "keep"

    async def run():
        store = SlowOpinionStore(open_campaign=True)
        manager = _bare_manager(store, llm=llm)
        stop = asyncio.Event()
        ticker = asyncio.create_task(_heartbeat(stop))
        main_thread = threading.get_ident()
        started = time.perf_counter()
        result = await manager.handle_user_message("u1", "小蓝", "意见")
        elapsed_ms = (time.perf_counter() - started) * 1000
        stop.set()
        ticks = await ticker

        assert result is None
        assert elapsed_ms >= 200
        assert ticks >= 15
        assert all(thread_id != main_thread for _name, thread_id in store.calls)

    asyncio.run(run())


def test_auto_close_stale_store_reads_keep_event_loop_alive():
    async def run():
        store = SlowOpinionStore(open_campaign=True)
        manager = _bare_manager(store)
        stop = asyncio.Event()
        ticker = asyncio.create_task(_heartbeat(stop))
        main_thread = threading.get_ident()
        started = time.perf_counter()
        await manager.auto_close_stale(pending_hours=24)
        elapsed_ms = (time.perf_counter() - started) * 1000
        stop.set()
        ticks = await ticker

        assert elapsed_ms >= 200
        assert ticks >= 15
        assert all(thread_id != main_thread for _name, thread_id in store.calls)

    asyncio.run(run())


def test_atomic_campaign_creation_rolls_back_all_rows_on_failure(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    with store._connect() as conn:
        conn.execute("""
            CREATE TRIGGER abort_opinion_participant
            BEFORE INSERT ON opinion_participants
            WHEN NEW.qq_id='boom'
            BEGIN SELECT RAISE(ABORT, 'injected participant failure'); END
        """)

    try:
        store.create_opinion_campaign_with_participants(
            "话题", [("u1", "小蓝"), ("boom", "故障")], "bot", "发起",
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("故障注入应使原子创建失败")

    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM opinion_campaigns").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM opinion_participants").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM opinion_messages").fetchone()[0] == 0


def test_atomic_campaign_creation_rejects_second_open_campaign(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    first = store.create_opinion_campaign_with_participants(
        "第一个", [("u1", "小蓝")], "bot", "发起",
    )
    second = store.create_opinion_campaign_with_participants(
        "第二个", [("u2", "小白")], "bot", "发起",
    )
    assert first == 1
    assert second is None
    assert store.get_open_opinion_campaign()["topic"] == "第一个"


def test_start_campaign_deduplicates_explicit_targets(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    manager = _bare_manager(store)

    async def no_invites(*_args, **_kwargs):
        return None

    manager._schedule_invites = no_invites
    result = asyncio.run(
        manager.start_campaign("话题", targets=["u1", "u1", "u2"], max_targets=20)
    )
    assert result["queued"] == 2
    assert [p["qq_id"] for p in store.get_opinion_participants(result["campaign_id"])] == [
        "u1", "u2"
    ]


def test_legacy_multiple_open_campaigns_are_migrated_before_unique_index(tmp_path):
    db_path = tmp_path / "legacy-opinion.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE opinion_campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT '',
                closed_at TEXT DEFAULT ''
            );
            CREATE TABLE opinion_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                qq_id TEXT NOT NULL,
                nickname TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                last_msg_ts TEXT DEFAULT '',
                UNIQUE(campaign_id, qq_id)
            );
            CREATE TABLE opinion_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                qq_id TEXT NOT NULL,
                nickname TEXT DEFAULT '',
                message TEXT NOT NULL,
                is_bot INTEGER DEFAULT 0,
                timestamp TEXT NOT NULL
            );
            INSERT INTO opinion_campaigns(topic,status,created_at)
            VALUES ('旧活动','open','2026-08-28 00:00:00');
            INSERT INTO opinion_campaigns(topic,status,created_at)
            VALUES ('新活动','open','2026-08-29 00:00:00');
        """)

    store = Store(str(db_path))
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id,status FROM opinion_campaigns ORDER BY id"
        ).fetchall()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_opinion_one_open'"
        ).fetchall()
    assert rows[0][1] == "closed"
    assert rows[1][1] == "open"
    assert indexes == [("idx_opinion_one_open",)]


def test_confirmed_invite_settlement_rolls_back_state_and_message_together(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="invite_uncertain")
    with store._connect() as conn:
        conn.execute("""
            CREATE TRIGGER abort_opinion_message
            BEFORE INSERT ON opinion_messages
            WHEN NEW.qq_id='u1'
            BEGIN SELECT RAISE(ABORT, 'injected message failure'); END
        """)

    try:
        store.settle_opinion_invite_confirmed(cid, "u1", "小蓝", "邀请")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("故障注入应使邀请收口失败")

    assert store.get_opinion_participant(cid, "u1")["status"] == "invite_uncertain"
    assert store.get_opinion_messages(cid) == []


def test_opinion_response_cas_does_not_revive_auto_closed_participant(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="pending")
    with store._connect() as conn:
        conn.execute(
            "UPDATE opinion_participants SET last_msg_ts='2020-01-01 00:00:00' "
            "WHERE campaign_id=? AND qq_id=?",
            (cid, "u1"),
        )

    judge_started = asyncio.Event()
    release_judge = asyncio.Event()

    async def blocked_llm(*_args):
        judge_started.set()
        await release_judge.wait()
        return "agree"

    async def run():
        message_manager = _bare_manager(store, llm=blocked_llm)
        close_manager = _bare_manager(store)
        close_manager._export_markdown = lambda *_args, **_kwargs: str(tmp_path / "x.md")

        message_task = asyncio.create_task(
            message_manager.handle_user_message("u1", "小蓝", "意见")
        )
        await judge_started.wait()
        await close_manager.auto_close_stale(pending_hours=0)
        release_judge.set()
        return await message_task

    assert asyncio.run(run()) is None
    assert store.get_opinion_participant(cid, "u1")["status"] == "no_reply"
    assert not any(not row["is_bot"] for row in store.get_opinion_messages(cid))


def test_invite_enrich_runs_outside_event_loop():
    async def run():
        store = SlowOpinionStore(delay=0.01)
        manager = _bare_manager(store)
        enrich_thread = []

        def enrich(reply, _group_id):
            enrich_thread.append(threading.get_ident())
            time.sleep(0.08)
            return reply

        async def llm(*_args):
            return "邀请你聊聊～"

        async def send(*_args):
            return True

        manager._llm = llm
        manager._send = send
        manager._enrich = enrich
        stop = asyncio.Event()
        ticker = asyncio.create_task(_heartbeat(stop))
        main_thread = threading.get_ident()
        await manager._invite(1, "话题", "u1", "小蓝")
        stop.set()
        ticks = await ticker

        assert enrich_thread and enrich_thread[0] != main_thread
        assert ticks >= 10

    asyncio.run(run())


def test_opinion_invite_claim_is_single_winner_and_reclaimable(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")

    async def run():
        first = _bare_manager(store)
        second = _bare_manager(store)
        winners = []

        async def record_first(*_args):
            winners.append("first")

        async def record_second(*_args):
            winners.append("second")

        first._invite_all = record_first
        second._invite_all = record_second
        tasks = await asyncio.gather(
            first._schedule_invites(cid, "话题", [("u1", "小蓝")]),
            second._schedule_invites(cid, "话题", [("u1", "小蓝")]),
        )
        background = [task for task in tasks if task is not None]
        if background:
            await asyncio.gather(*background)
        return winners

    winners = asyncio.run(run())
    assert winners in (["first"], ["second"])

    # 上一次认领仍在有效租约内，第二次认领必须失败；过期后可恢复。
    assert not store.claim_opinion_invite(cid, "u1", "fresh-token")
    with store._connect() as conn:
        conn.execute(
            "UPDATE opinion_participants SET claim_ts='2020-01-01 00:00:00' "
            "WHERE campaign_id=? AND qq_id=?",
            (cid, "u1"),
        )
    assert store.claim_opinion_invite(cid, "u1", "stale-reclaim", lease_seconds=900)


def test_invite_lease_prevents_queued_row_from_being_sent_after_expiry(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")
    token = "invite-token"
    assert store.claim_opinion_invite(cid, "u1", token)
    llm_started = asyncio.Event()
    release_llm = asyncio.Event()
    sent = []

    async def blocked_llm(*_args):
        llm_started.set()
        await release_llm.wait()
        return "邀请你聊聊～"

    async def send(*args):
        sent.append(args)
        return True

    async def run():
        manager = _bare_manager(store, llm=blocked_llm)
        manager._send = send
        invite_task = asyncio.create_task(
            manager._invite(cid, "话题", "u1", "小蓝", claim_token=token)
        )
        await llm_started.wait()
        await manager.auto_close_stale(pending_hours=0)
        release_llm.set()
        await invite_task

    asyncio.run(run())
    assert sent == []
    assert store.get_opinion_participant(cid, "u1")["status"] == "invite_expired"


def test_invite_lease_keeps_inflight_send_from_being_expired(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")
    token = "invite-token"
    assert store.claim_opinion_invite(cid, "u1", token)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def llm(*_args):
        return "邀请你聊聊～"

    async def blocked_send(*_args):
        send_started.set()
        await release_send.wait()
        return True

    async def run():
        manager = _bare_manager(store, llm=llm)
        manager._send = blocked_send
        invite_task = asyncio.create_task(
            manager._invite(cid, "话题", "u1", "小蓝", claim_token=token)
        )
        await send_started.wait()
        await manager.auto_close_stale(pending_hours=0)
        release_send.set()
        await invite_task

    asyncio.run(run())
    assert store.get_opinion_participant(cid, "u1")["status"] == "pending"
    assert any(row["is_bot"] for row in store.get_opinion_messages(cid))


def test_auto_close_idle_cas_sends_thanks_once_under_concurrency(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝")
    store.update_opinion_participant(cid, "u1", "participating")
    store.add_opinion_message(cid, "u1", "小蓝", "旧意见")
    with store._connect() as conn:
        conn.execute(
            "UPDATE opinion_messages SET timestamp='2020-01-01 00:00:00' "
            "WHERE campaign_id=? AND qq_id=? AND is_bot=0",
            (cid, "u1"),
        )
    sent = []

    async def run():
        managers = []
        for _ in range(2):
            manager = _bare_manager(store)

            async def send(qq_id, text):
                sent.append((qq_id, text))
                return True

            manager._send = send
            manager._export_markdown = lambda *_args, **_kwargs: str(tmp_path / "x.md")
            managers.append(manager)
        await asyncio.gather(*(
            manager.auto_close_stale(minutes=0) for manager in managers
        ))

    asyncio.run(run())
    assert len(sent) == 1


def test_invite_recovery_waits_for_explicit_qq_online_gate(tmp_path):
    """构造 OpinionManager 时不能在 NapCat ready 前抢跑发送。"""
    store = Store(str(tmp_path / "recovery.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")
    sent = []

    async def llm(*_args):
        return "糖糖想邀请你聊聊～"

    async def send(qq_id, text):
        sent.append((qq_id, text))
        return True

    async def run():
        manager = OpinionManager(
            store, llm, send, bot_qq="bot", invite_retry_delay=0,
        )
        # 构造阶段没有 transport ready 信号，不应产生后台发送。
        await asyncio.sleep(0)
        assert sent == []
        assert store.get_opinion_participant(cid, "u1")["status"] == "queued"

        # 只有显式的 QQ online gate 才启动恢复；后台邀请完成后再断言状态。
        await manager.recover_queued_invitations()
        tasks = list(manager._background_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    asyncio.run(run())
    assert sent and sent[0][0] == "u1"
    assert store.get_opinion_participant(cid, "u1")["status"] == "pending"


def test_closed_campaign_rejects_late_invite_transitions(tmp_path):
    """活动关闭后，迟到的 LLM/发送回执不能把邀请状态复活。"""
    store = Store(str(tmp_path / "closed-invite.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")
    token = "invite-token"
    assert store.claim_opinion_invite(cid, "u1", token)
    store.close_opinion_campaign(cid)

    assert not store.mark_opinion_invite_uncertain(cid, "u1", token)
    assert not store.settle_opinion_invite_confirmed(
        cid, "u1", "小蓝", "邀请", token,
    )
    assert not store.set_opinion_invite_delivery_state(
        cid, "u1", token, "invite_failed",
    )
    assert store.get_opinion_participant(cid, "u1")["status"] == "queued"
    assert store.get_opinion_messages(cid) == []


def test_invite_blocked_before_mark_does_not_send_after_campaign_close(tmp_path):
    """显式关窗发生在邀请文案 LLM 等待期间时，不得再执行外部发送。"""
    store = Store(str(tmp_path / "late-invite.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="queued")
    token = "invite-token"
    assert store.claim_opinion_invite(cid, "u1", token)
    llm_started = asyncio.Event()
    release_llm = asyncio.Event()
    sent = []

    async def blocked_llm(*_args):
        llm_started.set()
        await release_llm.wait()
        return "邀请你聊聊～"

    async def send(*args):
        sent.append(args)
        return True

    async def run():
        manager = _bare_manager(store, llm=blocked_llm)
        manager._send = send
        invite_task = asyncio.create_task(
            manager._invite(cid, "话题", "u1", "小蓝", claim_token=token)
        )
        await llm_started.wait()
        store.close_opinion_campaign(cid)
        release_llm.set()
        await invite_task

    asyncio.run(run())
    assert sent == []
    assert store.get_opinion_participant(cid, "u1")["status"] == "queued"


def test_opinion_response_rejects_closed_campaign(tmp_path):
    store = Store(str(tmp_path / "opinion.db"))
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "u1", "小蓝", status="participating")
    store.close_opinion_campaign(cid)
    assert store.record_opinion_response(
        cid, "u1", "小蓝", "迟到的意见", "keep"
    ) == "stale"
    assert store.get_opinion_messages(cid) == []
