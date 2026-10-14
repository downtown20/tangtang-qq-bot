"""P0-D1：统一 send_message 与传话回执（2026-08-28 协作任务包）

验收点：
  1. verbatim：actual 与 requested 完全相同；relay：归因 owner + 原话片段未丢
  2. natural：LLM 可改写，回执 actual 与 NapCat 实参一致；失败原样兜底
  3. 统一结构化 receipt：requested/actual/target/channel/mode/attribution/
     message_id/status（confirmed/uncertain/failed/draft）
  4. 只有 confirmed 可说已发送；uncertain 明确禁止重发；异常一律 uncertain
  5. 旧 relay_message 执行别名与 /传话 走同一发送 helper（不再隐藏主人逻辑）
  6. 工具目录仅 send_message 暴露（旧三工具移除，group_say_later 保留）
"""
import asyncio
import json
import types

from agent.handler import MessageHandler
from agent.send_actions import build_receipt, execute_send_action
from onebot.ws_client import SendResult


def _run(coro):
    return asyncio.run(coro)


class _Nap:
    """记录实参的 napcat stub"""

    def __init__(self, priv_result=True, group_result=True):
        self.priv_calls = []
        self.group_calls = []
        self._priv_result = priv_result
        self._group_result = group_result

    async def send_private_message(self, qq, msg):
        self.priv_calls.append((qq, msg))
        return self._priv_result

    async def send_group_message(self, gid, msg):
        self.group_calls.append((gid, msg))
        return self._group_result


def _receipt(r):
    return json.loads(r)


# ═══════════════════════════════════════════════════════
# 1. verbatim：actual == requested 完全相同
# ═══════════════════════════════════════════════════════

def test_verbatim_actual_identical_to_requested():
    nap = _Nap(priv_result=SendResult(True, True, message_id=77))
    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1001", message="再验收一会吧",
        mode="verbatim")))
    assert r["requested"] == "再验收一会吧"
    assert r["actual"] == "再验收一会吧"          # 完全相同
    assert r["status"] == "confirmed"
    assert r["message_id"] == 77
    assert r["mode"] == "verbatim"
    assert nap.priv_calls == [("1001", "再验收一会吧")]  # NapCat 实参 == requested


# ═══════════════════════════════════════════════════════
# 2. relay：归因强制 owner，原话原样不丢
# ═══════════════════════════════════════════════════════

def test_relay_owner_attribution_and_requested_intact():
    nap = _Nap()
    msg = "再验收一会吧，还有几个问题没处理"
    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1002", message=msg, mode="relay",
        attribution="self")))  # 传 self 也会被强制 owner
    assert r["mode"] == "relay"
    assert r["attribution"] == "owner"              # 转达归因强制主人
    assert r["requested"] == msg
    assert msg in r["actual"]                       # 请求原文完整保留
    assert "主人" in r["actual"]                    # 归因必须让接收者看得见
    assert nap.priv_calls == [("1002", r["actual"])]


# ═══════════════════════════════════════════════════════
# 3. natural：LLM 改写但回执记录实际正文，NapCat 实参一致
# ═══════════════════════════════════════════════════════

def test_natural_receipt_actual_matches_napcat_arg():
    nap = _Nap()

    async def fake_llm(system, user):
        assert "再验收一会" in user
        return "好的～我们继续看看刚才那几个问题吧"

    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1003", message="再验收一会",
        mode="natural", llm_call=fake_llm)))
    assert r["actual"] == "好的～我们继续看看刚才那几个问题吧"
    assert nap.priv_calls == [("1003", "好的～我们继续看看刚才那几个问题吧")]
    assert r["requested"] == "再验收一会"           # 请求保留在回执


def test_natural_failure_falls_back_to_requested():
    nap = _Nap()

    async def broken_llm(system, user):
        raise RuntimeError("LLM 挂了")

    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1003", message="原话",
        mode="natural", llm_call=broken_llm)))
    assert r["actual"] == "原话"                    # 失败原样兜底


# ═══════════════════════════════════════════════════════
# 4. 送达状态：confirmed / uncertain / failed / 异常 / draft
# ═══════════════════════════════════════════════════════

def test_uncertain_never_claims_success():
    nap = _Nap(priv_result=SendResult(True, False, error="MESSAGE_ID_UNCONFIRMED"))
    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1001", message="hi")))
    assert r["status"] == "uncertain"               # 不声称已发送


def test_failed_reported_as_failed():
    nap = _Nap(priv_result=SendResult(False, False, error="OFFLINE"))
    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1001", message="hi")))
    assert r["status"] == "failed"


def test_exception_is_uncertain_not_failed():
    class Boom:
        async def send_private_message(self, qq, msg):
            raise RuntimeError("response lost after POST")

    nap = Boom()
    r = _receipt(_run(execute_send_action(
        nap, channel="private", target="1001", message="hi")))
    assert r["status"] == "uncertain"               # 异常一律 uncertain


def test_review_returns_draft_without_sending():
    nap = _Nap()
    r = _receipt(_run(execute_send_action(
        nap, channel="group", target="g1", message="草稿内容",
        mode="natural", review=True)))
    assert r["status"] == "draft"
    assert nap.group_calls == []                    # 未发送


def test_invalid_args_failed_without_side_effect():
    nap = _Nap()
    r = _receipt(_run(execute_send_action(
        nap, channel="email", target="x", message="hi")))   # 非法 channel
    assert r["status"] == "failed"
    r2 = _receipt(_run(execute_send_action(
        nap, channel="private", target="", message="")))    # 空参数
    assert r2["status"] == "failed"
    assert nap.priv_calls == [] and nap.group_calls == []


# ═══════════════════════════════════════════════════════
# 5. 工具目录：仅 send_message 暴露
# ═══════════════════════════════════════════════════════

def test_control_catalog_exposes_only_send_message():
    handler = object.__new__(MessageHandler)
    handler._allowed_groups = {"g1"}
    tools = handler._build_control_tools()
    names = {t["function"]["name"] for t in tools}
    assert "send_message" in names                  # 新唯一发送工具
    assert "group_say" not in names                 # 旧工具从目录移除
    assert "send_private_message" not in names
    assert "relay_message" not in names
    assert "group_say_later" in names               # 定时发言不动


# ═══════════════════════════════════════════════════════
# 6. _execute_tool 薄分支：send_message 返回 receipt；旧别名走统一 helper
# ═══════════════════════════════════════════════════════

def test_execute_tool_send_message_returns_receipt():
    nap = _Nap(priv_result=SendResult(True, True, message_id=9))
    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1"}, _get_admin_groups=lambda u: [],
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "private", "target": "1001",
                               "message": "说这句话", "mode": "verbatim"},
        "", "owner-1", {"respond": True}))
    r = json.loads(result)
    assert r["status"] == "confirmed"
    assert r["actual"] == "说这句话"
    assert r["message_id"] == 9
    assert nap.priv_calls == [("1001", "说这句话")]


def test_execute_tool_send_message_unprivileged_rejected():
    """执行侧权限 fail-closed：非 owner/group-owner 直接伪造调用必须被拒"""
    nap = _Nap()
    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1"}, _get_admin_groups=lambda u: [],
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "private", "target": "1001",
                               "message": "伪造发送"},
        "", "evil-user", {"respond": True}))
    assert "没有权限" in result
    assert nap.priv_calls == []  # 未发起任何发送


def test_execute_tool_send_message_group_whitelist_enforced():
    """channel=group：非白名单群拒绝（failed receipt，不发）"""
    nap = _Nap()
    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1"}, _get_admin_groups=lambda u: [],
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "group", "target": "g9",
                               "message": "hi"},
        "", "owner-1", {"respond": True}))
    r = json.loads(result)
    assert r["status"] == "failed"
    assert nap.group_calls == []


def test_execute_tool_send_message_default_group_resolution():
    """target 空=默认群：优先当前用户管理群，其次 allowed_groups 第一个"""
    nap = _Nap()
    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1", "g2"}, _get_admin_groups=lambda u: ["g2"],
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "group", "target": "",
                               "message": "群公告"},
        "", "owner-1", {"respond": True}))
    r = json.loads(result)
    assert r["status"] == "confirmed"
    assert nap.group_calls == [("g2", "群公告")]  # 管理群优先

    # 无管理群 → allowed_groups 第一个
    fake2 = types.SimpleNamespace(
        napcat=_Nap(), owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1", "g2"}, _get_admin_groups=lambda u: [],
    )
    _run(MessageHandler._execute_tool(
        fake2, "send_message", {"channel": "group", "target": "",
                                "message": "群公告"},
        "", "owner-1", {"respond": True}))
    assert fake2.napcat.group_calls == [("g1", "群公告")]


def test_execute_tool_send_message_nickname_resolution():
    """私聊 target 昵称 → 精确解析 QQ；解析失败 = failed receipt"""
    nap = _Nap()
    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups=set(), _get_admin_groups=lambda u: [],
        _resolve_target_qq=lambda name: "1002" if name == "小红" else "",
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "private", "target": "小红",
                               "message": "想你了"},
        "", "owner-1", {"respond": True}))
    assert json.loads(result)["status"] == "confirmed"
    assert nap.priv_calls == [("1002", "想你了")]  # 解析后发送

    # 解析失败 → failed receipt，绝不猜测发送
    result2 = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "private", "target": "查无此人",
                               "message": "hi"},
        "", "owner-1", {"respond": True}))
    assert json.loads(result2)["status"] == "failed"


def test_execute_tool_send_message_review_writes_pending_pm():
    """review=true：draft receipt + 草稿写 _pending_pm/state:pending_pm（说「发吧」可发送）"""
    nap = _Nap()
    saved = {}

    fake = types.SimpleNamespace(
        napcat=nap, owner_qq="owner-1", _is_group_owner=lambda u: False,
        _allowed_groups={"g1"}, _get_admin_groups=lambda u: [],
        _save_state_kv=lambda k, v: saved.update({k: v}),
    )
    result = _run(MessageHandler._execute_tool(
        fake, "send_message", {"channel": "group", "target": "g1",
                               "message": "草稿内容", "review": True},
        "", "owner-1", {"respond": True}))
    r = json.loads(result)
    assert r["status"] == "draft"
    assert nap.group_calls == []  # 未发送
    assert fake._pending_pm["message"] == "草稿内容"   # 草稿落盘
    assert fake._pending_pm["group_id"] == "g1"
    assert "state:pending_pm" in saved              # 持久化（重启后仍可「发吧」）


def test_execute_tool_legacy_relay_uses_unified_helper():
    nap = _Nap()
    fake = types.SimpleNamespace(napcat=nap, owner_qq="owner-1")
    result = _run(MessageHandler._execute_tool(
        fake, "relay_message", {"target": "1002", "message": "验收先到这里"},
        "", "owner-1", {"respond": True}))
    r = json.loads(result)
    assert r["mode"] == "relay"
    assert r["attribution"] == "owner"              # 走统一 helper 的 relay 语义
    assert r["requested"] == "验收先到这里"
    assert "验收先到这里" in r["actual"]            # 原话未丢
    assert "主人" in r["actual"]                    # 接收者可见归因
    assert nap.priv_calls == [("1002", r["actual"])]


# ═══════════════════════════════════════════════════════
# 7. /传话（_do_relay）走统一 helper：原话转达 + 人类化回报
# ═══════════════════════════════════════════════════════

def test_do_relay_uses_unified_helper_verbatim():
    from agent.handler_commands import CommandRouter

    nap = _Nap(priv_result=SendResult(True, True, message_id=5))
    handler = types.SimpleNamespace(
        napcat=nap,
        _resolve_target_qq=lambda name: "1002" if name == "小红" else "",
        memory=types.SimpleNamespace(store=types.SimpleNamespace(
            get_or_create_person=lambda qq, nick: {"nickname": "小红"})),
    )
    router = CommandRouter(handler)
    result = _run(router._do_relay("小红", "再验收一会吧"))
    assert "已送达" in result                        # 人类化确认
    assert "再验收一会吧" in nap.priv_calls[0][1]    # 原话完整保留，无 LLM 隐藏改写
    assert "主人" in nap.priv_calls[0][1]            # 对接收者显式归因
    assert "再验收一会吧" in result                  # 回报含实际内容


def test_build_receipt_roundtrip():
    r = _receipt(build_receipt(
        requested="a", actual="b", channel="group", target="g1",
        mode="verbatim", attribution="none", status="confirmed", message_id=3))
    assert r == {"requested": "a", "actual": "b", "target": "g1",
                 "channel": "group", "mode": "verbatim", "attribution": "none",
                 "message_id": 3, "status": "confirmed"}
