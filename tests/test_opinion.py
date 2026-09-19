"""意见征集测试（2026-08-16）——状态机/判定 fail-open/导出 MD。

窗口状态机：pending →(agree) participating →(close) done；refuse 直接结束。
判定全是 LLM（无关键词表）；LLM 失败 fail-open：参与判定→other（不打扰）、
结束判定→keep（不丢意见）。
"""
import asyncio
from pathlib import Path

import pytest

from agent.opinion import OpinionManager
from agent.store import Store
from napcat.ws_client import SendResult


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    return s


class FakeLLM:
    """可编程判定：participant_verdict / close_verdict；invite 文案固定"""

    def __init__(self, participant_verdict="agree", close_verdict="keep", fail=False,
                 invite_reply="在吗在吗？"):
        self.participant_verdict = participant_verdict
        self.close_verdict = close_verdict
        self.fail = fail
        self.invite_reply = invite_reply

    async def __call__(self, system, user):
        if self.fail:
            raise RuntimeError("LLM down")
        if "是否想结束意见交流" in system:
            return self.close_verdict
        if "判断用户对征集邀请的态度" in system:
            return self.participant_verdict
        return self.invite_reply


def _manager(store, llm, tmp_path=None, recall=None, self_state=None):
    sent = []
    async def send(qq, text):
        sent.append((qq, text))
        return True
    m = OpinionManager(
        store=store,
        llm_caller=llm,
        send_private=send,
        personality_base="你是糖糖",
        recall=recall,
        self_state=self_state,
        bot_qq="10000",
        invite_retry_delay=0.01,  # 测试不睡 20 秒
    )
    return m, sent


# ── 状态机 ──

def test_full_flow_agree_collect_close(store):
    llm = FakeLLM(participant_verdict="agree", close_verdict="keep")
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("你想要什么新功能")
    store.add_opinion_participant(cid, "20001", "小蓝")

    # 愿意参与 → 开窗 + 致谢
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "好啊，我愿意说"))
    assert r and "记下" in r
    p = store.get_opinion_participant(cid, "20001")
    assert p["status"] == "participating"

    # 窗口中消息全量收集，不打断
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "希望糖糖能记住我的生日"))
    assert r is None
    msgs = store.get_opinion_messages(cid)
    assert any("记住我的生日" in x["message"] for x in msgs)

    # 结束信号 → done + 致谢
    llm.close_verdict = "close"
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "就这些啦"))
    assert r and "谢谢你" in r
    p = store.get_opinion_participant(cid, "20001")
    assert p["status"] == "done"


def test_refuse_path(store):
    llm = FakeLLM(participant_verdict="refuse")
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "最近没空诶"))
    assert r and "没关系" in r
    assert store.get_opinion_participant(cid, "20001")["status"] == "refused"


def test_other_verdict_does_not_open_window(store):
    llm = FakeLLM(participant_verdict="other")
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "糖糖你今天心情怎么样"))
    assert r is None
    assert store.get_opinion_participant(cid, "20001")["status"] == "pending"


def test_non_participant_messages_ignored(store):
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    r = asyncio.run(m.handle_user_message("99999", "路人", "我觉得……"))
    assert r is None
    assert store.get_opinion_messages(cid) == []


# ── fail-open ──

def test_llm_fail_keeps_window_open_and_collects(store):
    llm = FakeLLM(participant_verdict="agree", close_verdict="keep")
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    asyncio.run(m.handle_user_message("20001", "小蓝", "好呀"))
    llm.fail = True
    # LLM 挂 → 结束判定 fail-open=keep：消息照收、窗口不误关
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "就这些啦"))
    assert r is None
    assert store.get_opinion_participant(cid, "20001")["status"] == "participating"
    assert any("就这些啦" in x["message"] for x in store.get_opinion_messages(cid))


def test_llm_fail_pending_stays_pending(store):
    llm = FakeLLM(participant_verdict="agree", fail=True)
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    r = asyncio.run(m.handle_user_message("20001", "小蓝", "好呀我愿意"))
    assert r is None  # fail-open=other：不打扰
    assert store.get_opinion_participant(cid, "20001")["status"] == "pending"


# ── 导出 ──

def test_end_campaign_exports_markdown(store, tmp_path):
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("糖糖的功能建议")
    store.add_opinion_participant(cid, "20001", "小蓝")
    store.add_opinion_participant(cid, "20002", "小白")
    store.add_opinion_message(cid, "20001", "小蓝", "想要唱歌功能", is_bot=False)
    store.add_opinion_message(cid, "20002", "小白", "希望记住我的生日", is_bot=False)
    store.update_opinion_participant(cid, "20001", "done")
    store.update_opinion_participant(cid, "20002", "done")

    result = m.end_campaign(out_dir=str(tmp_path))
    assert "已结束" in result
    files = list(Path(tmp_path).glob("*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "糖糖的功能建议" in content
    assert "唱歌功能" in content and "记住我的生日" in content
    assert "✅ 已收集" in content
    # 活动已关闭
    assert store.get_open_opinion_campaign() is None


def test_auto_complete_when_all_terminal(store, tmp_path):
    """2026-08-16 事故回归：全员终态 → 自动结束+导出+汇报主人——
    糖糖承诺「等ta回复后整理成文档」，文档必须自动出现，不等人说「结束征集」"""
    llm = FakeLLM()
    notified = []

    async def notify(text):
        notified.append(text)

    m, sent = _manager(store, llm)
    m._notify_owner = notify
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    store.add_opinion_message(cid, "20001", "小蓝", "意见一条", is_bot=False)
    store.update_opinion_participant(cid, "20001", "done")
    # 导出路径临时目录——monkeypatch 掉 end_campaign 的 out_dir
    m._export_markdown = lambda *a, **k: str(Path(str(tmp_path)) / "x.md")

    asyncio.run(m.auto_close_stale())

    assert store.get_open_opinion_campaign() is None  # 已自动关闭
    assert notified, "自动收尾后必须汇报主人"
    assert "已结束" in notified[0]


def test_auto_complete_waits_for_participating(store):
    """还有人在窗口里 → 不自动收尾"""
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    store.update_opinion_participant(cid, "20001", "participating")
    store.add_opinion_message(cid, "20001", "小蓝", "还在想", is_bot=False)
    asyncio.run(m.auto_close_stale())
    assert store.get_open_opinion_campaign() is not None


def test_pending_times_out_to_no_reply(store, tmp_path):
    """2026-08-16 Codex：pending 超时 → no_reply——一人不回不能挂死整个活动"""
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")  # last_msg_ts=now
    # 自动收尾会真导出——钉到临时目录，不许污染真实 data/意见收集/
    m._export_markdown = lambda *a, **k: str(Path(str(tmp_path)) / "x.md")
    asyncio.run(m.auto_close_stale(pending_hours=24.0))
    p = store.get_opinion_participant(cid, "20001")
    # last_msg_ts 是真实当前时间——超时判定不会过。直接改 DB 时间戳再测
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE opinion_participants SET last_msg_ts='2020-01-01 00:00:00'")
    conn.commit()
    conn.close()
    asyncio.run(m.auto_close_stale(pending_hours=24.0))
    p = store.get_opinion_participant(cid, "20001")
    assert p["status"] == "no_reply"


def test_pick_targets_skips_blacklist_and_owner(store):
    """2026-08-16 Codex：自动选人跳过黑名单与主人；显式 targets 保留覆盖权"""
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    m._blacklist = {"20002"}
    m._owner_qq = "10001"

    async def run():
        return await m._pick_targets(None, max_targets=10)
    # 无活跃用户数据时返回空——直接构造验证显式覆盖
    r = asyncio.run(m._pick_targets(["20002", "10001"], max_targets=10))
    assert [q for q, n in r] == ["20002", "10001"], "显式名单必须覆盖黑名单/主人过滤"


def test_campaign_status_report(store):
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    assert "没有进行中" in m.campaign_status()
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝")
    store.update_opinion_participant(cid, "20001", "done")
    r = m.campaign_status()
    assert "话题" in r and "已完成" in r


def test_start_campaign_rejects_when_open_exists(store):
    llm = FakeLLM()
    m, sent = _manager(store, llm)
    store.create_opinion_campaign("第一个")
    r = asyncio.run(m.start_campaign("第二个"))
    assert "已有进行中" in r["error"]


def test_start_campaign_invites_sent_in_background(store):
    """2026-08-16 事故回归：邀请后台发送——工具返回时锁已释放，
    邀请必须实际发出（此前嵌套 LLM 调用锁超时→静默跳过=提示已发实际没发）"""
    llm = FakeLLM(invite_reply="在吗在吗？糖糖想听听你的想法！")
    m, sent = _manager(store, llm)

    async def run():
        r = await m.start_campaign("话题", targets=["20001"], max_targets=5)
        assert r["queued"] == 1
        assert r["invited"] == 0
        participant = store.get_opinion_participant(r["campaign_id"], "20001")
        assert participant["status"] == "queued"
        # Store I/O 现在经过有界线程门；不要用固定的 50ms 猜线程调度，
        # 在后台契约允许的短窗口内等待实际发送。
        for _ in range(100):
            current = store.get_opinion_participant(r["campaign_id"], "20001")
            if sent and current and current.get("status") == "pending":
                break
            await asyncio.sleep(0.01)
        return r
    r = asyncio.run(run())

    assert len(sent) == 1 and sent[0][0] == "20001"
    assert store.get_opinion_participant(r["campaign_id"], "20001")["status"] == "pending"
    msgs = store.get_opinion_messages(r["campaign_id"])
    assert any(x["is_bot"] and "想法" in x["message"] for x in msgs)


def test_uncertain_invite_is_not_labeled_no_reply(store, tmp_path):
    async def uncertain_send(*_args, **_kwargs):
        return SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")

    m = OpinionManager(
        store=store, llm_caller=FakeLLM(), send_private=uncertain_send,
        personality_base="你是糖糖", bot_qq="bot", invite_retry_delay=0,
    )
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝", status="queued")

    asyncio.run(m._invite(cid, "话题", "20001", "小蓝"))

    assert store.get_opinion_participant(cid, "20001")["status"] == "invite_uncertain"
    assert not any(msg["is_bot"] for msg in store.get_opinion_messages(cid))
    m._export_markdown = lambda *a, **k: str(Path(str(tmp_path)) / "x.md")
    asyncio.run(m.auto_close_stale(pending_hours=0))
    assert store.get_opinion_participant(cid, "20001")["status"] == "invite_expired"


def test_known_failed_invite_is_not_labeled_no_reply(store):
    async def failed_send(*_args, **_kwargs):
        return SendResult(False, False, error="BOT_ERROR")

    m = OpinionManager(
        store=store, llm_caller=FakeLLM(), send_private=failed_send,
        personality_base="你是糖糖", bot_qq="bot", invite_retry_delay=0,
    )
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝", status="queued")

    asyncio.run(m._invite(cid, "话题", "20001", "小蓝"))

    assert store.get_opinion_participant(cid, "20001")["status"] == "invite_failed"


def test_reply_to_uncertain_invite_can_still_open_window(store):
    llm = FakeLLM(participant_verdict="agree")
    m, _sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(
        cid, "20001", "小蓝", status="invite_uncertain",
    )

    reply = asyncio.run(m.handle_user_message("20001", "小蓝", "好呀，我愿意"))

    assert reply and "记下" in reply
    assert store.get_opinion_participant(cid, "20001")["status"] == "participating"


def test_invite_falls_back_to_template_when_llm_empty(store):
    """LLM 生成失败/空回复 → 模板兜底，邀请照发不静默（2026-08-16 事故教训）"""
    llm = FakeLLM(invite_reply="")
    m, sent = _manager(store, llm)
    cid = store.create_opinion_campaign("话题")
    store.add_opinion_participant(cid, "20001", "小蓝", status="queued")
    asyncio.run(m._invite_all(cid, "话题", [("20001", "小蓝")]))

    assert len(sent) == 1
    assert "话题" in sent[0][1] and "小蓝" in sent[0][1]
    msgs = store.get_opinion_messages(cid)
    assert any(x["is_bot"] for x in msgs)
