"""P0-C：ActionEnvelope / 定时任务 typed 动作（2026-08-28 协作任务包）

验收点：
  1. 动作兼容 text-only / sticker-only / voice-only / 组合
  2. typed payload（text/sticker_emotion/voice_text）+ 幂等 key 持久化；旧行安全读取
  3. 到点先 claim，按 payload 执行；贴图/语音不再只发 description 文本
  4. 状态聚合：全 confirmed→done+开窗钩子；任一 uncertain/混合→冻结；全 failed→release
  5. 语音三态透传：confirmed/uncertain/failed 分别 → done/uncertain/release
  6. 幂等键 = source-event + canonical args：同源重试同 key；不同源相同提醒不同 key
  7. create_task 只吞幂等键唯一冲突，其他 IntegrityError 重抛
"""
import asyncio
import json
import types
from pathlib import Path

import pytest

from agent.handler import MessageHandler
from agent.tasks import TaskManager
from napcat.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


def _make_nap(priv=None, group=None):
    return types.SimpleNamespace(
        send_private_message=priv or (lambda *a, **k: True),
        send_group_message=group or (lambda *a, **k: True),
    )


class _Stickers:
    """贴图匹配 stub：情绪=开心 命中 1 张，其他未匹配"""

    def match_by_emotion_text(self, emotion, embed_engine=None, count=1):
        if emotion == "开心":
            return ["[CQ:image,file=s.jpg]"][:count]
        return []


def _task_status(store, tid):
    """DB 精确读任务状态（list_tasks 排除 done，不能用于断言）"""
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()
    return row[0] if row else None


# ═══════════════════════════════════════════════════════
# 1. typed payload + 幂等 key 持久化（store 层）
# ═══════════════════════════════════════════════════════

def test_create_task_persists_payload_and_idempotency(store):
    tid = store.create_task(
        "1001", "到点发个图", "2020-01-01 00:00", "",
        action_payload={"text": "图来了", "sticker_emotion": "开心"},
        idempotency_key="key-1",
    )
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action_payload, idempotency_key FROM tasks WHERE id=?",
            (tid,),
        ).fetchone()
    assert json.loads(row[0]) == {"text": "图来了", "sticker_emotion": "开心"}
    assert row[1] == "key-1"


def test_create_task_idempotent_same_key_returns_same_id(store):
    tid1 = store.create_task("1001", "喝水", "2020-01-01 00:00",
                             idempotency_key="dup-key")
    tid2 = store.create_task("1001", "喝水", "2020-01-01 00:00",
                             idempotency_key="dup-key")
    assert tid1 == tid2  # 同源重试幂等
    # 不同 key（不同源事件）即使参数相同也能新建
    tid3 = store.create_task("1001", "喝水", "2020-01-01 00:00",
                             idempotency_key="other-key")
    assert tid3 != tid1
    with store._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert n == 2


def test_create_task_raises_non_idempotency_integrity_errors(store):
    """只吞 idempotency_key 唯一冲突；其他完整性错误必须重抛"""
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(None, "x", "2020-01-01 00:00")  # owner_qq NOT NULL


def test_old_tasks_rows_safe_to_read(store):
    """旧行（无 payload 列值）读取安全：action_payload 为空串"""
    tid = store.create_task("1001", "旧任务", "2020-01-01 00:00")
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action_payload, idempotency_key FROM tasks WHERE id=?", (tid,)
        ).fetchone()
    assert row[0] == ""
    assert row[1] == ""


# ═══════════════════════════════════════════════════════
# 2. payload 解析兜底
# ═══════════════════════════════════════════════════════

def test_parse_payload_fails_closed_safely(store):
    tm = TaskManager(store, _make_nap())
    assert tm._parse_payload({}) == {}                      # 无 payload
    assert tm._parse_payload({"action_payload": ""}) == {}  # 空串
    assert tm._parse_payload(
        {"action_payload": "不是JSON"}
    ) == {"__invalid_payload__": True}  # 坏 JSON 冻结，禁止降级发送
    assert tm._parse_payload(
        {"action_payload": '{"text": "x"}'}) == {"text": "x"}
    assert tm._parse_payload(
        {"action_payload": '{"text": {"internal": "do_not_send"}}'}
    ) == {"__invalid_payload__": True}
    assert tm._parse_payload(
        {"action_payload": '{"text": "", "voice_text": "  "}'}
    ) == {"__invalid_payload__": True}


# ═══════════════════════════════════════════════════════
# 3. text-only：typed text 原样冻结（不走 LLM 改写），由 outbox 首发
# ═══════════════════════════════════════════════════════

def test_text_only_payload_queues_typed_text_for_outbox(store):
    sent = []

    async def _priv(qq, m):
        sent.append((qq, m))
        return True

    nap = types.SimpleNamespace(
        send_private_message=_priv,
        send_group_message=_priv,
    )
    tm = TaskManager(store, nap, llm_call=lambda s, u: "不应走LLM改写")
    tid = store.create_task(
        "1001", "内部备忘", "2020-01-01 00:00", "",
        action_payload={"text": "到点说这句话"})
    _run(tm._check_and_send())
    assert sent == []
    job = store.list_due_send_outbox()[0]
    assert job["message"] == "到点说这句话"  # typed text 原样冻结
    assert _task_status(store, tid) == "sending"
    assert store.claim_send_outbox(job["action_id"])
    assert store.settle_send_outbox(
        job["action_id"], "confirmed", message_ids=[101],
    ) in {"confirmed", "confirmed_duplicate"}
    assert _task_status(store, tid) == "done"


# ═══════════════════════════════════════════════════════
# 4. sticker-only：只发贴图，绝不发送 description 文本
# ═══════════════════════════════════════════════════════

def test_sticker_only_payload_sends_sticker_not_text(store):
    sent = []

    async def _priv(qq, m):
        sent.append(("priv", m))
        return True

    nap = types.SimpleNamespace(
        send_private_message=_priv,
        send_group_message=_priv,
    )
    tm = TaskManager(store, nap, stickers=_Stickers())
    tid = store.create_task(
        "1001", "到点发表情包", "2020-01-01 00:00", "",
        action_payload={"sticker_emotion": "开心"})
    _run(tm._check_and_send())
    assert sent == [("priv", "[CQ:image,file=s.jpg]")]  # 只有贴图，无 description
    assert _task_status(store, tid) == "done"


def test_sticker_unmatched_releases_for_retry(store):
    nap = _make_nap()
    tm = TaskManager(store, nap, stickers=_Stickers())
    tid = store.create_task(
        "1001", "到点发表情包", "2020-01-01 00:00", "",
        action_payload={"sticker_emotion": "不存在的情绪"})
    _run(tm._check_and_send())
    # 未匹配 = 全部确定失败 → release（可重试）
    assert _task_status(store, tid) == "pending"
    assert any(t["id"] == tid for t in store.get_due_tasks())


# ═══════════════════════════════════════════════════════
# 5. voice-only：三态透传（confirmed→done / uncertain→冻结 / failed→release）
# ═══════════════════════════════════════════════════════

def test_voice_only_confirmed_marks_done(store):
    sent = []

    async def voice_sender(scope_key, text):
        sent.append((scope_key, text))
        return SendResult(True, True, message_id=1)

    tm = TaskManager(store, _make_nap(), voice_sender=voice_sender)
    tid = store.create_task(
        "1001", "语音提醒", "2020-01-01 00:00", "",
        action_payload={"voice_text": "该睡觉啦"})
    _run(tm._check_and_send())
    assert sent == [("_private_1001", "该睡觉啦")]  # 语音发送，无文本
    assert _task_status(store, tid) == "done"


def test_voice_only_uncertain_frozen_not_replayed(store):
    """网关 accepted 但无 message_id 证据 → uncertain 冻结，不得当 failed 重放"""
    calls = {"n": 0}

    async def voice_sender(scope_key, text):
        calls["n"] += 1
        return SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")

    tm = TaskManager(store, _make_nap(), voice_sender=voice_sender)
    tid = store.create_task(
        "1001", "语音提醒", "2020-01-01 00:00", "",
        action_payload={"voice_text": "该睡觉啦"})
    _run(tm._check_and_send())
    _run(tm._check_and_send())  # 下一轮
    assert calls["n"] == 1      # 不重放
    assert _task_status(store, tid) == "uncertain"


def test_voice_only_failed_releases_for_retry(store):
    async def voice_sender(scope_key, text):
        return SendResult(False, False, error="VOICE_ENGINE_ERROR", retryable=True)

    tm = TaskManager(store, _make_nap(), voice_sender=voice_sender)
    tid = store.create_task(
        "1001", "语音提醒", "2020-01-01 00:00", "",
        action_payload={"voice_text": "该睡觉啦"})
    _run(tm._check_and_send())
    assert _task_status(store, tid) == "pending"  # release 可重试
    assert any(t["id"] == tid for t in store.get_due_tasks())


# ═══════════════════════════════════════════════════════
# 6. 组合动作：text + sticker 都执行
# ═══════════════════════════════════════════════════════

def test_combined_payload_executes_all_actions(store):
    sent = []

    async def _priv(qq, m):
        sent.append(m)
        return True

    nap = types.SimpleNamespace(
        send_private_message=_priv,
        send_group_message=_priv,
    )
    tm = TaskManager(store, nap, stickers=_Stickers())
    store.create_task(
        "1001", "组合", "2020-01-01 00:00", "",
        action_payload={"text": "看看这个", "sticker_emotion": "开心"})
    _run(tm._check_and_send())
    assert sent == ["看看这个", "[CQ:image,file=s.jpg]"]


def test_persist_task_action_plan_materializes_ordered_media_children(store):
    """scheduled media must be frozen as linked children before any send."""
    task_id = store.create_task(
        "1001", "定时媒体", "2020-01-01 00:00", "1100",
    )
    assert store.claim_task_for_send(task_id)
    result = store.persist_task_action_plan(task_id, [
        {
            "kind": "sticker",
            "payload": {
                "emotion": "开心", "count": 1,
                "asset_ref": "happy/a.jpg", "asset_sha256": "a" * 64,
                "asset_valid": True, "role_id": "default", "library_id": "stickers",
            },
            "message": "[CQ:image,file=file:///D:/stickers/happy/a.jpg]",
            "actual": {"delivery_kind": "sticker", "text": ""},
        },
        {
            "kind": "voice",
            "payload": {
                "text": "到点啦", "emotion": "温柔", "speed": 0.9,
                "pause": "舒缓", "model_profile": "v4",
                "speaker": "murasame", "voice_lang": "zh",
            },
            "message": "[CQ:record,file=file:///D:/voice/a.wav]",
            "actual": {
                "delivery_kind": "voice", "voice_generated": True,
                "text": "到点啦", "asset_ref": "voice/a.wav",
            },
        },
    ], role_id="default", library_id="stickers")

    assert result["attempt_id"] > 0
    assert len(result["children"]) == 2
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT ordinal,domain_action_id,task_attempt_id,message,receipt_template "
            "FROM send_outbox WHERE task_attempt_id=? ORDER BY ordinal",
            (result["attempt_id"],),
        ).fetchall()
        children = conn.execute(
            "SELECT ordinal,action_id,state FROM task_action_children "
            "WHERE attempt_id=? ORDER BY ordinal", (result["attempt_id"],),
        ).fetchall()
    assert [row[0] for row in rows] == [0, 1]
    assert [row[0] for row in children] == [0, 1]
    assert all(row[2] == "pending" for row in children)
    assert "identity_payload" in rows[0][4]
    assert "model_profile" in rows[1][4]


def test_unified_media_task_freezes_then_defers_all_network_send(store):
    sent = []

    async def _must_not_send(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("unified media task must enter outbox first")

    async def _prepare_voice(payload, scope):
        assert payload["voice_model_profile"] == "v4"
        assert scope == "_private_1001"
        return {
            "message": "[CQ:record,file=file:///D:/voice/frozen.wav]",
            "actual": {"asset_ref": "voice/frozen.wav", "text": "慢一点"},
        }

    nap = types.SimpleNamespace(
        send_private_message=_must_not_send,
        send_group_message=_must_not_send,
        _task_media_action_outbox_enabled=True,
    )
    tm = TaskManager(store, nap, voice_preparer=_prepare_voice)
    task_id = store.create_task(
        "1001", "媒体提醒", "2020-01-01 00:00", "",
        action_payload={
            "action_version": 2, "sticker_emotion": "开心",
            "sticker_transport": "[CQ:image,file=file:///D:/stickers/a.jpg]",
            "sticker_asset_ref": "happy/a.jpg", "sticker_asset_sha256": "a" * 64,
            "sticker_asset_valid": True, "sticker_role_id": "default",
            "sticker_library_id": "stickers", "voice_text": "慢一点",
            "voice_emotion": "温柔", "voice_speed": 0.8,
            "voice_pause": "舒缓", "voice_model_profile": "v4",
            "voice_speaker": "murasame", "voice_lang": "zh",
        },
    )
    _run(tm._check_and_send())
    assert sent == []
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT ordinal,status,task_attempt_id FROM send_outbox "
            "WHERE task_attempt_id IS NOT NULL ORDER BY ordinal",
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [(0, "pending"), (1, "pending")]
    assert _task_status(store, task_id) == "sending"


def test_unified_media_plan_restart_quarantines_only_inflight_child(store, tmp_path):
    """重启后已在发送的 child 不重放，仍允许 pending sibling 继续。"""
    task_id = store.create_task(
        "1001", "重启组合媒体", "2020-01-01 00:00", "",
    )
    assert store.claim_task_for_send(task_id)
    action = store.persist_task_action_plan(task_id, [
        {
            "kind": "text", "payload": {"text": "文字"}, "message": "文字",
            "actual": {"text": "文字"},
        },
        {
            "kind": "voice",
            "payload": {"text": "语音", "emotion": "温柔", "speed": 1.0,
                        "pause": "自然", "model_profile": "v4",
                        "speaker": "murasame", "voice_lang": "zh"},
            "message": "[CQ:record,file=file:///D:/voice/frozen.wav]",
            "actual": {"delivery_kind": "voice", "voice_generated": True,
                        "text": "语音"},
        },
    ])
    rows = store.list_due_send_outbox()
    first_id = rows[0]["action_id"]
    second_id = rows[1]["action_id"]
    assert store.claim_send_outbox(first_id)

    restarted = type(store)(str(store.db_path))
    assert restarted.recover_send_outbox_after_restart() == 1
    assert restarted.get_send_outbox(first_id)["status"] == "uncertain"
    assert restarted.get_send_outbox(second_id)["status"] == "pending"
    assert restarted.claim_send_outbox(second_id)
    assert restarted.settle_send_outbox(second_id, "confirmed", message_ids=[2002]) \
        in {"confirmed", "confirmed_duplicate"}
    # 一个 child 已被重启隔离、兄弟 child 已确认：父任务保留 partial，
    # 不能伪装成全部 uncertain，也不能标记 done。
    assert _task_status(restarted, task_id) == "partial"


def test_media_task_creation_freezes_sticker_and_voice_snapshots(store):
    tm = TaskManager(
        store, _make_nap(),
        sticker_snapshot=lambda emotion: {
            "transport": "[CQ:image,file=file:///D:/stickers/a.jpg]",
            "asset_ref": "happy/a.jpg", "asset_sha256": "b" * 64,
            "asset_valid": True, "role_id": "default", "library_id": "stickers",
        },
        voice_snapshot=lambda: {
            "emotion": "温柔", "speed": 0.85, "pause": "舒缓",
            "model_profile": "v4", "speaker": "murasame", "voice_lang": "zh",
        },
    )
    task_id = tm.add(
        minutes=1, description="冻结媒体", owner_qq="1001",
        payload={"sticker_emotion": "开心", "voice_text": "到点啦"},
    )
    row = next(task for task in store.list_tasks("1001") if task["id"] == task_id)
    payload = json.loads(row["action_payload"])
    assert payload["action_version"] == 2
    assert payload["sticker_transport"].startswith("[CQ:image")
    assert payload["sticker_asset_sha256"] == "b" * 64
    assert payload["voice_model_profile"] == "v4"
    assert payload["voice_speed"] == 0.85


def test_media_task_snapshot_preserves_empty_speaker_for_emotion_auto_selection(store):
    tm = TaskManager(
        store, _make_nap(),
        voice_snapshot=lambda: {
            "emotion": "温柔", "speed": 1.0, "pause": "自然",
            "model_profile": "michele", "speaker": "", "voice_lang": "zh",
        },
    )
    task_id = tm.add(
        minutes=1, description="米雪儿自动参考音", owner_qq="1001",
        payload={"voice_text": "你好呀"},
    )
    row = next(task for task in store.list_tasks("1001") if task["id"] == task_id)
    payload = json.loads(row["action_payload"])
    assert payload["voice_model_profile"] == "michele"
    assert payload["voice_speaker"] == ""


def test_media_snapshot_does_not_upgrade_unsupported_kind_to_v2(store):
    tm = TaskManager(
        store, _make_nap(),
        voice_snapshot=lambda: {"model_profile": "v4", "speaker": "murasame"},
    )
    task_id = tm.add(
        minutes=1, description="贴图能力缺失时保留兼容路径", owner_qq="1001",
        payload={"sticker_emotion": "开心"},
    )
    row = next(task for task in store.list_tasks("1001") if task["id"] == task_id)
    payload = json.loads(row["action_payload"])
    assert "action_version" not in payload


# ═══════════════════════════════════════════════════════
# 7. 状态聚合：全 confirmed→done+钩子；混合→uncertain；全 failed→release
# ═══════════════════════════════════════════════════════

def test_all_confirmed_triggers_on_confirmed_hook(store):
    confirmed_hook = []
    nap = _make_nap()

    async def _group(gid, m):
        return True

    nap.send_group_message = _group
    tm = TaskManager(store, nap,
                     stickers=_Stickers(),
                     on_confirmed=lambda task, scope: confirmed_hook.append(
                         (task["id"], scope)))
    tid = store.create_task(
        "1001", "群发图", "2020-01-01 00:00", "g9",
        action_payload={"text": "早", "sticker_emotion": "开心"})
    _run(tm._check_and_send())
    assert confirmed_hook == [(tid, "g9")]  # 群任务开窗 scope=group_id
    assert _task_status(store, tid) == "done"


def test_mixed_confirmed_and_failed_frozen_as_uncertain(store):
    """text confirmed + sticker failed → 冻结（重放会重复已确认的文本）"""
    sent = []

    async def _priv(qq, m):
        sent.append(m)
        return True  # confirmed

    nap = types.SimpleNamespace(
        send_private_message=_priv,
        send_group_message=_priv,
    )
    confirmed_hook = []
    tm = TaskManager(store, nap,
                     stickers=_Stickers(),  # "不存在" 未匹配 → failed
                     on_confirmed=lambda t, s: confirmed_hook.append(t["id"]))
    tid = store.create_task(
        "1001", "组合", "2020-01-01 00:00", "",
        action_payload={"text": "看看", "sticker_emotion": "不存在"})
    _run(tm._check_and_send())
    assert _task_status(store, tid) == "uncertain"  # 混合 → 冻结
    assert confirmed_hook == []                      # 未全部确认 → 不开窗
    assert not any(t["id"] == tid for t in store.get_due_tasks())  # 不重放


def test_all_failed_releases_for_retry(store):
    """全部确定失败 → release（下一轮可重试）——不是 uncertain 冻结"""
    async def _fail(*_a, **_k):
        return SendResult(False, False, error="OFFLINE", retryable=True)

    nap = types.SimpleNamespace(
        send_private_message=_fail, send_group_message=_fail)
    tm = TaskManager(store, nap, stickers=_Stickers())
    tid = store.create_task(
        "1001", "组合", "2020-01-01 00:00", "",
        action_payload={"text": "看看", "sticker_emotion": "开心"})
    _run(tm._check_and_send())
    assert _task_status(store, tid) == "pending"  # release
    assert any(t["id"] == tid for t in store.get_due_tasks())


def test_text_uncertain_not_replayed(store):
    calls = {"n": 0}

    async def _uncertain(*_a, **_k):
        calls["n"] += 1
        return SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED")

    nap = types.SimpleNamespace(
        send_private_message=_uncertain, send_group_message=_uncertain)
    tm = TaskManager(store, nap)
    tid = store.create_task("1001", "喝水", "2020-01-01 00:00",
                            action_payload={"text": "喝水啦"})
    _run(tm._check_and_send())
    job = store.list_due_send_outbox()[0]
    assert store.claim_send_outbox(job["action_id"])
    assert store.settle_send_outbox(
        job["action_id"], "uncertain",
        error_code="MESSAGE_ID_UNCONFIRMED",
    ) == "uncertain"
    _run(tm._check_and_send())  # 下一轮
    assert calls["n"] == 0      # TaskManager 从不直发，也不重放
    assert _task_status(store, tid) == "uncertain"


# ═══════════════════════════════════════════════════════
# 8. set_reminder：source-event 幂等键（同源重试同 key；不同源可新建）
# ═══════════════════════════════════════════════════════

class _TM:
    """TaskManager stub：记录 add_at/add 调用与幂等键"""

    def __init__(self):
        self.add_ats = []
        self.adds = []

    def add(self, **kw):
        self.adds.append(kw)
        return 101

    def add_at(self, time_str, description, owner_qq, date_offset=0,
               group_id="", payload=None, idempotency_key=""):
        self.add_ats.append(dict(time_str=time_str, description=description,
                                 group_id=group_id, payload=payload,
                                 idempotency_key=idempotency_key))
        return 102


def _set_reminder(fake, args, source_id, scope="", user="1001"):
    return _run(MessageHandler._execute_tool(
        fake, "set_reminder", args, scope, user, {"respond": True},
        action_source_id=source_id))


def test_set_reminder_same_source_same_args_same_key():
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    args = {"time": "09:00", "description": "喝水", "text": "该喝水啦"}
    _set_reminder(fake, args, source_id="g1:123")
    _set_reminder(fake, args, source_id="g1:123")  # 同源事件重试
    assert len(tm.add_ats) == 2
    assert tm.add_ats[0]["idempotency_key"] == tm.add_ats[1]["idempotency_key"]
    # 幂等键不是裸参数哈希：必须含 source
    assert "g1:123" not in tm.add_ats[0]["idempotency_key"]


def test_set_reminder_different_source_same_args_different_key():
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    args = {"time": "09:00", "description": "喝水", "text": "该喝水啦"}
    _set_reminder(fake, args, source_id="g1:123")   # 消息 A
    _set_reminder(fake, args, source_id="g1:456")   # 消息 B（不同源）
    assert len(tm.add_ats) == 2
    assert tm.add_ats[0]["idempotency_key"] != tm.add_ats[1]["idempotency_key"]


def test_set_reminder_typed_payload_passthrough():
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    _set_reminder(fake, {
        "time": "5分钟", "description": "到点发图",
        "text": "图来了", "sticker": "开心", "voice": "该睡啦",
    }, source_id="g1:123")
    assert tm.adds[0]["payload"] == {
        "text": "图来了", "sticker_emotion": "开心", "voice_text": "该睡啦",
    }
    assert tm.adds[0]["idempotency_key"]


def test_set_reminder_rejects_seconds_without_silent_rounding():
    """分钟级任务不能把秒级承诺静默改成另一段时间。"""
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    result = _set_reminder(fake, {
        "time": "30秒", "description": "马上提醒我",
    }, source_id="g1:130")
    assert "不支持秒级提醒" in result
    assert not tm.adds and not tm.add_ats


def test_set_reminder_voice_rhythm_parameters_are_typed_and_clamped():
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    _set_reminder(fake, {
        "time": "5分钟", "description": "慢一点叫我", "voice": "起床啦",
        "voice_emotion": "温柔", "voice_speed": 0.6, "voice_pause": "舒缓",
    }, source_id="g1:124")
    assert tm.adds[0]["payload"] == {
        "voice_text": "起床啦", "voice_emotion": "温柔",
        "voice_speed": 0.75, "voice_pause": "舒缓",
    }


def test_set_reminder_rejects_structured_content_and_empty_typed_action():
    tm = _TM()
    fake = types.SimpleNamespace(task_manager=tm, _find_last_group=lambda u: "g999")
    result = _set_reminder(fake, {
        "time": "5分钟", "description": "内部备忘",
        "text": {"internal": "do_not_send"},
    }, source_id="g1:789")
    assert "必须是文本" in result
    assert not tm.adds and not tm.add_ats

    result = _set_reminder(fake, {
        "time": "5分钟", "description": "空动作", "text": "  ",
    }, source_id="g1:790")
    assert "不能为空" in result
    assert not tm.adds and not tm.add_ats


# ═══════════════════════════════════════════════════════
# 9. 源码契约：payload/idempotency 透传链路真实存在
# ═══════════════════════════════════════════════════════

def test_source_id_and_payload_contract():
    """handler：action_source_id 贯穿回合 → 工具执行；payload/幂等键落库"""
    src = Path("agent/handler.py").read_text(encoding="utf-8")
    assert "action_source_id" in src          # 参数链
    assert "payload=" in src                  # typed payload 透传
    assert "idempotency_key=" in src          # 幂等键落库
